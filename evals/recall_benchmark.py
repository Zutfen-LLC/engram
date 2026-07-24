"""Recall-quality benchmark and ranking decomposition (ENG-AUDIT-002B).

A deterministic, fail-closed benchmark that decomposes the semantic recall
ranking pipeline to answer: where does an item move from its raw semantic rank
to its final served rank?

This is NOT a production endpoint. It operates at the service/test-harness
layer where exact pipeline stages are available, and uses an isolated audit
tenant under real PostgreSQL with RLS enabled.

Key properties (correcting defects F1-F11 from the certification packets):

- **Exact candidate count** (F1): eligible_candidate_count comes from
  ``semantic.candidate_count()``, NOT from the public ``RecallResponse`` which
  strips ``candidate_count``.

- **True raw similarity rank** (F2/F10): raw_similarity_rank is computed by
  cosine distance over the production candidate window BEFORE trust
  re-ranking, using the exact production ordering (distance ascending, then
  ``created_at`` descending). A separate diagnostic lane can optionally
  examine the complete eligible corpus for ``corpus_raw_rank``.

- **Fail-closed** (F3): non-2xx HTTP, malformed JSON, missing fields, and
  embedding failures are explicit BenchmarkErrors, never quality degradation.

- **Controlled corpus** (F4): ``CorpusProfile`` is executable — it inserts a
  controlled set of items with known embeddings into an isolated audit tenant,
  making small, distractor-heavy, and existing-corpus scenarios reproducible.

- **Per-budget metrics** (F5): MRR, top-k, and recall@k are computed
  separately for each budget. No aggregate ranking score across mixed budgets.

- **Real pipeline** (F6): ``ServiceBenchmarkSuite`` invokes the actual
  production pipeline functions (``semantic.search``,
  ``expand_recall_candidates``, ``_enforce_semantic_budget``) under real
  PostgreSQL, not constructed dataclasses.

- **Dual-lane separation** (F7): the production lane uses the exact production
  fetch limit ``min(item_limit * _SEMANTIC_OVERFETCH, _SEMANTIC_OVERFETCH_CAP)``
  and feeds only that window through enrichment, relationship expansion, and
  budget enforcement. The diagnostic raw-rank lane optionally inspects the
  complete eligible corpus solely to calculate ``corpus_raw_rank``. The
  diagnostic set never feeds into the production lane.

- **Actual measurements** (F8): ``returned_count = len(selected)`` and
  ``returned_bytes`` is the exact sum of UTF-8 content bytes in the selected
  set — never placeholder values.

- **Existing-corpus executable** (F9): ``existing_corpus_mode`` is runnable via
  either an explicit query vector or an injected query-embedding function that
  uses the active embedding profile. Fails closed when neither is available.

- **Complete fingerprint** (F11): the corpus fingerprint hashes every vector
  component, not just the first four.

Requires a live PostgreSQL with the v2 schema and pgvector.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from engram.memory_context import ResolvedMemoryContext

__all__ = [
    "BenchmarkError",
    "EmptyFixtureIdError",
    "EmbeddingFailure",
    "PipelineError",
    "TransportError",
    "CorpusItem",
    "CorpusProfile",
    "StageRanks",
    "BenchmarkResult",
    "BenchmarkReport",
    "QueryFixture",
    "ExclusionDisposition",
    "ControlledCorpus",
    "ServiceBenchmarkSuite",
    "HttpBenchmarkClient",
    "small_controlled_corpus",
    "distractor_heavy_corpus",
    "existing_corpus_mode",
]

# Type alias for the injected query-embedding function (F9).
# signature: async (query: str) -> list[float] | None
QueryEmbeddingFn = Callable[[str], Awaitable[list[float] | None]]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BenchmarkError(Exception):
    """Base for all benchmark failures. Benchmark callers should fail closed."""


class EmptyFixtureIdError(BenchmarkError):
    """An empty expected_item_id was provided."""

    def __init__(self) -> None:
        super().__init__(
            "expected_item_id must not be empty — reject before any "
            "benchmark request or database operation"
        )


class EmbeddingFailure(BenchmarkError):
    """Query embedding generation failed or returned None."""


class PipelineError(BenchmarkError):
    """A production pipeline stage raised or produced an unproven result."""


class TransportError(BenchmarkError):
    """An HTTP transport failure (non-2xx, malformed JSON, missing fields)."""


# ---------------------------------------------------------------------------
# Stable exclusion dispositions
# ---------------------------------------------------------------------------


class ExclusionDisposition:
    """Why an expected item is absent from (or present in) the final served list."""

    SELECTED = "selected"
    NOT_ELIGIBLE = "not_eligible"
    NO_EMBEDDING = "no_embedding"
    OUTSIDE_CANDIDATE_WINDOW = "outside_candidate_window"
    TRUST_DEMOTED = "trust_demoted"
    RELATIONSHIP_DEMOTED = "relationship_demoted"
    ITEM_BUDGET_EXCLUDED = "item_budget_excluded"
    BYTE_BUDGET_EXCLUDED = "byte_budget_excluded"
    TOKEN_BUDGET_EXCLUDED = "token_budget_excluded"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Corpus definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusItem:
    """A controlled item inserted into the audit tenant's corpus.

    ``embedding_vector`` controls the item's position in semantic distance
    ordering. ``None`` means the item gets no embedding (it is eligible but
    will never appear in semantic candidates).
    """

    content: str
    source_trust: float = 0.5
    memory_confidence: float = 0.5
    importance: float = 0.5
    human_verified: bool = False
    review_status: str = "active"
    embedding_vector: list[float] | None = None
    kind: str = "fact"
    visibility: str = "tenant"
    label: str = ""


@dataclass(frozen=True)
class CorpusProfile:
    """An executable corpus definition for benchmarking.

    ``mode='controlled'`` profiles carry ``items`` and ``query_vector`` and are
    reproducible across runs. ``mode='existing'`` profiles use whatever corpus
    is already in the tenant — ranks are window-relative. Existing mode is
    runnable via an explicit ``query_vector`` or an injected
    ``query_embedding_fn`` that uses the active embedding profile.
    """

    name: str
    description: str
    mode: str  # "controlled" or "existing"
    items: tuple[CorpusItem, ...] = ()
    query_vector: list[float] | None = None
    # The query text used for the benchmark (for report provenance).
    query_text: str = ""
    # F9: for existing mode, an injected query-embedding function.
    query_embedding_fn: QueryEmbeddingFn | None = None

    @property
    def is_controlled(self) -> bool:
        return self.mode == "controlled"

    def corpus_fingerprint(self) -> str:
        """Stable SHA-256 fingerprint of the complete corpus definition.

        F11 fix: hashes ALL vector components, not just the first four.
        """
        h = hashlib.sha256()
        h.update(self.name.encode())
        h.update(self.mode.encode())
        for item in self.items:
            h.update(item.content.encode())
            h.update(str(item.source_trust).encode())
            h.update(str(item.memory_confidence).encode())
            h.update(str(item.importance).encode())
            h.update(str(item.human_verified).encode())
            h.update(item.review_status.encode())
            h.update(item.kind.encode())
            h.update(item.visibility.encode())
            if item.embedding_vector is not None:
                # F11: hash the complete vector, not just the first 4 components.
                h.update(str(len(item.embedding_vector)).encode())
                for v in item.embedding_vector:
                    h.update(str(round(v, 6)).encode())
            else:
                h.update(b"no-embedding")
        if self.query_vector is not None:
            # F11: hash all query vector components too.
            h.update(str(len(self.query_vector)).encode())
            for v in self.query_vector:
                h.update(str(round(v, 6)).encode())
        return h.hexdigest()[:16]


def _unit_vec(dimensions: int, position: int) -> list[float]:
    """Create a unit vector with 1.0 at *position*, 0.0 elsewhere."""
    vec = [0.0] * dimensions
    if 0 <= position < dimensions:
        vec[position] = 1.0
    return vec


def small_controlled_corpus(dimensions: int) -> CorpusProfile:
    """5 controlled items: 2 near-query, 3 distractors.

    The target item (index 0) shares the query vector so its cosine distance
    is 0.0 (similarity 1.0). The second item is close but not identical. The
    three distractors are orthogonal. This is small enough that the production
    over-fetch window covers the complete eligible corpus, yielding exact raw
    ranks.
    """
    query_vec = _unit_vec(dimensions, 0)
    target = CorpusItem(
        content="benchmark target: the exact answer to the benchmark query",
        source_trust=0.50,
        memory_confidence=0.50,
        importance=0.50,
        human_verified=False,
        review_status="active",
        embedding_vector=list(query_vec),
        label="target",
    )
    near = CorpusItem(
        content="benchmark near-target: semantically adjacent to the query",
        source_trust=0.80,
        memory_confidence=0.80,
        importance=0.80,
        human_verified=True,
        review_status="active",
        # Slightly off-axis: mostly component 0 with a small component 1.
        embedding_vector=_near_unit_vec(dimensions, 0, 1),
        label="near-target-high-trust",
    )
    distractors = [
        CorpusItem(
            content=f"benchmark distractor {i}: semantically unrelated content",
            source_trust=0.50,
            memory_confidence=0.50,
            importance=0.50,
            review_status="active",
            embedding_vector=_unit_vec(dimensions, i + 1),
            label=f"distractor-{i}",
        )
        for i in range(3)
    ]
    return CorpusProfile(
        name="small_controlled",
        description="5 controlled items: target, near-target, 3 distractors",
        mode="controlled",
        items=(target, near, *distractors),
        query_vector=list(query_vec),
        query_text="benchmark target query",
    )


def _near_unit_vec(dimensions: int, primary: int, secondary: int) -> list[float]:
    """A vector mostly along *primary* with a small *secondary* component.

    Cosine distance from the pure *primary* unit vector is small (~0.29),
    so this item is semantically near the query but not identical.
    """
    vec = [0.0] * dimensions
    if 0 <= primary < dimensions:
        vec[primary] = 0.95
    if 0 <= secondary < dimensions:
        vec[secondary] = 0.312  # makes norm ≈ 1.0
    return vec


def distractor_heavy_corpus(dimensions: int) -> CorpusProfile:
    """1 target + 30 distractors. Tests ranking under heavy noise.

    The target shares the query vector (distance 0.0). All 30 distractors are
    orthogonal unit vectors. With 31 eligible items and production over-fetch
    of item_budget*3, budgets 5/10/20 yield fetch limits of 15/30/60 — so
    budget 5 may not examine the complete corpus.
    """
    query_vec = _unit_vec(dimensions, 0)
    target = CorpusItem(
        content="distractor-heavy target: the exact answer",
        source_trust=0.50,
        memory_confidence=0.50,
        importance=0.50,
        review_status="active",
        embedding_vector=list(query_vec),
        label="target",
    )
    distractors = tuple(
        CorpusItem(
            content=f"distractor-heavy distractor {i}: noise item {i}",
            source_trust=0.50,
            memory_confidence=0.50,
            importance=0.50,
            review_status="active",
            embedding_vector=_unit_vec(dimensions, (i % (dimensions - 1)) + 1),
            label=f"distractor-{i}",
        )
        for i in range(30)
    )
    return CorpusProfile(
        name="distractor_heavy",
        description="31 controlled items: 1 target + 30 distractors",
        mode="controlled",
        items=(target, *distractors),
        query_vector=list(query_vec),
        query_text="distractor-heavy target query",
    )


def existing_corpus_mode(
    *,
    query_vector: list[float] | None = None,
    query_text: str = "",
    query_embedding_fn: QueryEmbeddingFn | None = None,
) -> CorpusProfile:
    """Use whatever corpus is already in the tenant.

    No items are inserted. Ranks are window-relative (raw_rank_exact=False)
    because the production over-fetch window may not cover the full corpus.

    F9 fix: existing mode is now executable. Provide either:

    - ``query_vector``: an explicit query vector (dims must match the active
      embedding profile), OR
    - ``query_embedding_fn``: an async function that takes the query text and
      returns a vector (or None on failure) using the active embedding profile.

    Fails closed when neither an explicit vector nor a successful embedding
    result is available.
    """
    return CorpusProfile(
        name="existing",
        description="Existing tenant corpus — no controlled items inserted",
        mode="existing",
        items=(),
        query_vector=query_vector,
        query_text=query_text,
        query_embedding_fn=query_embedding_fn,
    )


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageRanks:
    """Exact pipeline-stage decomposition of one expected item in one query.

    All ranks are 0-based. ``position_1based`` is provided for each rank in
    the ``*_rank_1based`` fields for operator-readable reports.

    F7 dual-lane: ``raw_similarity_rank`` is computed over the production
    candidate window. ``corpus_raw_rank`` is optionally computed over the
    complete eligible corpus (diagnostic lane) and is always separate from the
    production pipeline.
    """

    eligible_candidate_count: int
    candidate_window_size: int

    # Stage 1a: raw semantic rank in the PRODUCTION candidate window
    # (by cosine distance, before trust re-ranking).
    # F10: ordered by distance ascending, then created_at descending
    # (matching production semantic.search ordering exactly).
    raw_similarity_rank: int | None
    raw_similarity_rank_1based: int | None
    raw_similarity_score: float | None  # 1 - distance
    raw_rank_exact: bool  # True only when production window covers the full corpus

    # Stage 1b (diagnostic lane, F7): raw rank over the COMPLETE eligible corpus.
    # Only populated when the diagnostic lane was run. This rank CANNOT
    # influence trust_rank, post_relationship_rank, final_served_rank, etc.
    corpus_raw_rank: int | None
    corpus_raw_rank_1based: int | None
    corpus_raw_rank_exact: bool  # True only when full corpus was examined

    # Stage 2: trust re-ranking (production lane only)
    trust_rank: int | None  # position after trust-weighted re-ranking
    trust_rank_1based: int | None
    trust_score: float | None

    # Stage 3: relationship expansion (production lane only)
    post_relationship_rank: int | None
    post_relationship_rank_1based: int | None

    # Stage 4: final served rank (after budget enforcement, production lane only)
    final_served_rank: int | None
    final_served_rank_1based: int | None
    final_score: float | None

    # Origin and disposition
    candidate_origin: str  # "semantic", "graph", "tunnel", "semantic+graph", etc.
    exclusion_disposition: str

    # Budgets
    item_budget: int | None
    byte_budget: int | None
    token_budget: int | None


@dataclass(frozen=True)
class BenchmarkResult:
    """One query-item-budget measurement.

    F8 fix: ``returned_count`` and ``returned_bytes`` are actual measurements
    of the selected set, not placeholders.
    """

    query: str
    query_digest: str  # SHA-256 prefix for safe provenance
    expected_item_id: str
    item_budget: int | None
    byte_budget: int | None
    token_budget: int | None
    top_k_hit: dict[str, bool]
    latency_ms: float
    latency_type: str  # "service" or "end_to_end"
    returned_bytes: int  # F8: exact sum of UTF-8 content bytes in selected
    returned_count: int  # F8: len(selected)
    stages: StageRanks | None
    error: str | None = None  # None when successful


@dataclass(frozen=True)
class BenchmarkReport:
    """Machine-readable benchmark report with full provenance."""

    repository_sha: str
    scoring_version: str
    config_version: str
    embedding_profile: str
    embedding_model: str
    embedding_dimensions: int
    corpus_profile_name: str
    corpus_fingerprint: str
    results: list[BenchmarkResult]
    summary: dict[str, Any]
    generated_at: str


@dataclass(frozen=True)
class QueryFixture:
    """A controlled query-to-item pair for benchmarking."""

    query: str
    expected_item_id: str
    label: str


# ---------------------------------------------------------------------------
# Controlled corpus setup and teardown
# ---------------------------------------------------------------------------


class ControlledCorpus:
    """Manages setup and teardown of a controlled corpus in an isolated tenant.

    Inserts items with explicit embeddings so that raw similarity ranks are
    deterministic. All items are cleaned up on ``teardown()``.
    """

    def __init__(
        self,
        tenant_id: str,
        principal_id: str,
        profile: CorpusProfile,
    ) -> None:
        self.tenant_id = tenant_id
        self.principal_id = principal_id
        self.profile = profile
        self._item_ids: list[str] = []
        self._embedding_profile_row: tuple[str, str, int] | None = None

    async def setup(self, session_factory: Any) -> dict[str, str]:
        """Insert controlled items with embeddings. Returns label→item_id map.

        For ``mode='existing'`` profiles, returns an empty dict without
        inserting anything.
        """
        import uuid

        from sqlalchemy import text

        label_map: dict[str, str] = {}
        if not self.profile.is_controlled:
            return label_map

        # Resolve the active embedding profile.
        async with session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id::text, model, dimensions FROM embedding_profiles "
                        "WHERE state='active' LIMIT 1"
                    )
                )
            ).one()
            self._embedding_profile_row = (str(row[0]), str(row[1]), int(row[2]))

        profile_id, model, dimensions = self._embedding_profile_row

        for item in self.profile.items:
            item_id = str(uuid.uuid4())
            self._item_ids.append(item_id)
            label = item.label or item.content[:40]
            label_map[label] = item_id

            async with session_factory() as session:
                await session.execute(
                    text(
                        "INSERT INTO memory_items ("
                        "id, tenant_id, principal_id, content, content_hash, kind, "
                        "visibility, review_status, memory_confidence, source_trust, "
                        "importance, human_verified, source_type, authority, "
                        "created_at, valid_from"
                        ") VALUES ("
                        ":id, :tenant_id, :principal_id, :content, :content_hash, :kind, "
                        ":visibility, :review_status, :memory_confidence, :source_trust, "
                        ":importance, :human_verified, :source_type, 10, "
                        ":now, :now"
                        ")"
                    ),
                    {
                        "id": item_id,
                        "tenant_id": self.tenant_id,
                        "principal_id": self.principal_id,
                        "content": item.content,
                        "content_hash": f"sha256:{uuid.uuid4().hex}",
                        "kind": item.kind,
                        "visibility": item.visibility,
                        "review_status": item.review_status,
                        "memory_confidence": item.memory_confidence,
                        "source_trust": item.source_trust,
                        "importance": item.importance,
                        "human_verified": item.human_verified,
                        "source_type": "manual",
                        "now": datetime.now(UTC),
                    },
                )
                await session.commit()

            # Insert embedding if the item has a vector.
            if item.embedding_vector is not None:
                rendered = "[" + ",".join(str(v) for v in item.embedding_vector) + "]"
                async with session_factory() as session:
                    await session.execute(
                        text(
                            "INSERT INTO memory_embeddings "
                            "(id, memory_item_id, tenant_id, profile_id, embedding_model, "
                            "embedding_dim, embedding, embedding_status) "
                            "VALUES (:id, :item_id, :tenant_id, :profile_id, :model, "
                            ":dimensions, CAST(:embedding AS vector), 'ready')"
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "item_id": item_id,
                            "tenant_id": self.tenant_id,
                            "profile_id": profile_id,
                            "model": model,
                            "dimensions": dimensions,
                            "embedding": rendered,
                        },
                    )
                    await session.commit()

        return label_map

    async def teardown(self, session_factory: Any) -> None:
        """Remove all controlled items and their embeddings."""
        from sqlalchemy import text

        if not self._item_ids:
            return
        async with session_factory() as session:
            await session.execute(
                text("DELETE FROM memory_embeddings WHERE memory_item_id = ANY(:ids)"),
                {"ids": self._item_ids},
            )
            await session.execute(
                text("DELETE FROM memory_items WHERE id = ANY(:ids)"),
                {"ids": self._item_ids},
            )
            await session.commit()


# ---------------------------------------------------------------------------
# Production ordering helpers (F10)
# ---------------------------------------------------------------------------


def _sort_key_production_raw(item: dict[str, Any]) -> tuple[float, float]:
    """Production raw-distance sort key: distance asc, then created_at desc.

    Matches ``semantic.search()`` SQL ordering: ``ORDER BY distance ASC,
    created_at DESC``. Uses the same negation trick as
    ``semantic._sort_created_desc``.
    """
    distance = float(item.get("distance", 1.0))
    created_at = item.get("created_at")
    if created_at is None:
        ts_key = float("inf")
    else:
        try:
            ts_key = float(-created_at.timestamp())
        except (AttributeError, ValueError, OSError):
            ts_key = float("inf")
    return (distance, ts_key)


# ---------------------------------------------------------------------------
# Service-layer benchmark suite
# ---------------------------------------------------------------------------


class ServiceBenchmarkSuite:
    """Runs recall-quality benchmarks at the service layer.

    Uses the real production pipeline functions under real PostgreSQL with RLS
    enabled. Each query's pipeline stages are captured exactly, producing
    deterministic, reproducible stage ranks.

    F7 dual-lane architecture: the production lane uses the exact production
    fetch limit and feeds only that window through enrichment, relationship
    expansion, and budget enforcement. The diagnostic raw-rank lane
    optionally examines the complete eligible corpus solely to compute
    ``corpus_raw_rank`` — it never feeds into the production lane.

    Usage::

        suite = ServiceBenchmarkSuite(session_factory)
        results = await suite.run_single_query(
            tenant_id=..., principal_id=...,
            query="...", query_vector=[...],
            expected_item_id="...", item_budget=10,
        )
    """

    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory

    async def run_single_query(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        query: str,
        query_vector: list[float] | None,
        expected_item_id: str,
        item_budget: int | None,
        byte_budget: int | None = None,
        token_budget: int | None = None,
        memory_context: ResolvedMemoryContext | None = None,
        query_embedding_fn: QueryEmbeddingFn | None = None,
    ) -> BenchmarkResult:
        """Run one query at one budget and capture full stage decomposition.

        Raises ``EmptyFixtureIdError`` if expected_item_id is empty.
        Raises ``EmbeddingFailure`` if query_vector is None AND no
        query_embedding_fn is provided or it returns None (F9).
        """
        # F3: reject empty expected_item_id before any DB operation.
        if not expected_item_id or not expected_item_id.strip():
            raise EmptyFixtureIdError()

        # F9: resolve query vector — explicit, or via injected embedding fn.
        if query_vector is None:
            if query_embedding_fn is not None:
                query_vector = await query_embedding_fn(query)
            if query_vector is None:
                raise EmbeddingFailure(
                    "query_vector is None and no query_embedding_fn produced a "
                    "vector — embedding generation failed"
                )

        query_digest = hashlib.sha256(query.encode()).hexdigest()[:16]
        start = time.monotonic()

        try:
            stages, selected = await self._run_pipeline_stages(
                tenant_id=tenant_id,
                principal_id=principal_id,
                query_vector=query_vector,
                expected_item_id=expected_item_id,
                item_budget=item_budget,
                byte_budget=byte_budget,
                token_budget=token_budget,
                memory_context_input=memory_context,
            )
        except BenchmarkError:
            raise
        except Exception as exc:
            raise PipelineError(f"pipeline stage failed: {exc}") from exc

        latency_ms = (time.monotonic() - start) * 1000

        # Compute top-k from the final served rank (0-based).
        final_rank = stages.final_served_rank
        top_k = {
            "top_1": final_rank is not None and final_rank < 1,
            "top_5": final_rank is not None and final_rank < 5,
            "top_10": final_rank is not None and final_rank < 10,
        }

        # F8: returned_count and returned_bytes are exact measurements.
        returned_count = len(selected)
        returned_bytes = sum(len(c["content"].encode("utf-8")) for c in selected)

        return BenchmarkResult(
            query=query,
            query_digest=query_digest,
            expected_item_id=expected_item_id,
            item_budget=item_budget,
            byte_budget=byte_budget,
            token_budget=token_budget,
            top_k_hit=top_k,
            latency_ms=round(latency_ms, 1),
            latency_type="service",
            returned_bytes=returned_bytes,
            returned_count=returned_count,
            stages=stages,
            error=None,
        )

    async def _build_memory_context(
        self, tenant_id: str, principal_id: str
    ) -> Any:
        """Build an unrestricted ResolvedMemoryContext for the audit tenant."""
        from engram.auth import Principal
        from engram.memory_context import unrestricted_memory_context

        principal = Principal(
            tenant_id=tenant_id,
            principal_id=principal_id,
            api_key_id="",
            scopes=("read", "write", "admin"),
            memory_profile_id=None,
            memory_profile_revision_id=None,
            memory_profile_slug=None,
            memory_profile_version=None,
        )
        return unrestricted_memory_context(principal)

    async def _run_pipeline_stages(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        query_vector: list[float],
        expected_item_id: str,
        item_budget: int | None,
        byte_budget: int | None,
        token_budget: int | None,
        memory_context_input: ResolvedMemoryContext | None,
    ) -> tuple[StageRanks, list[dict[str, Any]]]:
        """Execute the production pipeline stage-by-stage, capturing exact ranks.

        F7 dual-lane: production lane uses exact production fetch limit.
        Diagnostic lane optionally examines full corpus for corpus_raw_rank.
        Returns (StageRanks, selected_items) — selected for F8 byte/count.
        """
        from uuid import UUID as UUID_Type

        from sqlalchemy import select

        from engram import recall as recall_mod
        from engram import semantic
        from engram.config import settings
        from engram.db import apply_rls_context
        from engram.embedding_profiles import get_active_profile
        from engram.memory_access import read_eligibility_expression
        from engram.models import MemoryItem
        from engram.relationship_recall import expand_recall_candidates

        _SEMANTIC_REVIEW_STATUSES = ("active", "proposed")

        if memory_context_input is None:
            memory_context = await self._build_memory_context(tenant_id, principal_id)
        else:
            memory_context = memory_context_input

        async with self.session_factory() as session:
            await apply_rls_context(
                session, tenant_id=tenant_id, principal_id=principal_id
            )

            # --- Resolve budgets using the production resolver ---
            resolved_byte, resolved_token, resolved_item = (
                recall_mod._resolve_recall_budgets(
                    byte_budget=byte_budget,
                    token_budget=token_budget,
                    item_budget=item_budget,
                )
            )

            # --- Stage 0: active embedding profile ---
            embedding_profile = await get_active_profile(session)

            # --- Stage 1: count eligible candidates (F1 fix) ---
            eligible_count = await semantic.candidate_count(
                session,
                memory_context=memory_context,
                review_statuses=_SEMANTIC_REVIEW_STATUSES,
                embedding_profile=embedding_profile,
            )

            if eligible_count == 0:
                # No candidates — the expected item cannot be in any stage.
                return self._make_absent_stages(
                    eligible_count=0,
                    window_size=0,
                    expected_item_id=expected_item_id,
                    disposition=ExclusionDisposition.NOT_ELIGIBLE,
                    item_budget=resolved_item,
                    byte_budget=resolved_byte,
                    token_budget=resolved_token,
                )

            # ===================================================================
            # F7 PRODUCTION LANE: exact production fetch limit
            # ===================================================================
            # Use the EXACT production formula from execute_semantic_recall:
            #   fetch_limit = min(item_limit * _SEMANTIC_OVERFETCH, _SEMANTIC_OVERFETCH_CAP)
            # Do NOT inflate this to eligible_count — that would feed an
            # enlarged set through downstream production stages.
            item_limit = (
                resolved_item if resolved_item is not None else settings.recall_item_budget
            )
            production_fetch_limit = min(
                item_limit * recall_mod._SEMANTIC_OVERFETCH,
                recall_mod._SEMANTIC_OVERFETCH_CAP,
            )

            # --- Production lane: semantic search with exact production limit ---
            production_candidates = await semantic.search(
                session,
                query_vector,
                production_fetch_limit,
                memory_context=memory_context,
                review_statuses=_SEMANTIC_REVIEW_STATUSES,
                embedding_profile=embedding_profile,
            )

            candidate_window_size = len(production_candidates)

            # raw_rank_exact: True only when the production window covers
            # the full eligible corpus (small controlled corpus case).
            production_window_exact = candidate_window_size >= eligible_count

            production_candidate_ids = {c["id"] for c in production_candidates}

            # --- F10: Compute raw similarity rank in production window ---
            # Match production ordering exactly: distance asc, then
            # created_at desc (same as semantic.search SQL ORDER BY).
            by_distance_prod = sorted(
                production_candidates, key=_sort_key_production_raw
            )
            raw_rank = self._find_rank(by_distance_prod, expected_item_id)
            raw_score = self._find_field(
                production_candidates, expected_item_id, "similarity_score"
            )

            # --- Compute trust rank (candidates are already trust-ranked) ---
            trust_rank = self._find_rank(production_candidates, expected_item_id)
            trust_score = self._find_field(
                production_candidates, expected_item_id, "trust_score"
            )

            # ===================================================================
            # F7 DIAGNOSTIC LANE (optional): full corpus raw rank
            # ===================================================================
            # Only examine the complete eligible corpus if it exceeds the
            # production window — otherwise the production lane already
            # provides exact ranks and the diagnostic lane is redundant.
            corpus_raw_rank: int | None = None
            corpus_raw_rank_exact: bool = False

            if not production_window_exact and eligible_count > candidate_window_size:
                # Fetch the complete eligible corpus for diagnostic ranking.
                diagnostic_fetch_limit = max(eligible_count, production_fetch_limit)
                diagnostic_candidates = await semantic.search(
                    session,
                    query_vector,
                    diagnostic_fetch_limit,
                    memory_context=memory_context,
                    review_statuses=_SEMANTIC_REVIEW_STATUSES,
                    embedding_profile=embedding_profile,
                )
                diagnostic_exact = len(diagnostic_candidates) >= eligible_count
                by_distance_diag = sorted(
                    diagnostic_candidates, key=_sort_key_production_raw
                )
                corpus_raw_rank = self._find_rank(by_distance_diag, expected_item_id)
                corpus_raw_rank_exact = diagnostic_exact
            elif production_window_exact:
                # Production window already covered the full corpus.
                corpus_raw_rank = raw_rank
                corpus_raw_rank_exact = True

            # ===================================================================
            # PRODUCTION LANE: check candidate window membership
            # ===================================================================
            if expected_item_id not in production_candidate_ids:
                # The expected item is NOT in the production candidate window.
                # This is outside_candidate_window — even if the diagnostic
                # lane found it in the full corpus.
                disposition = await self._classify_absence(
                    session,
                    expected_item_id,
                    tenant_id,
                )
                # If the diagnostic lane found the item, it is specifically
                # outside the PRODUCTION candidate window, not missing entirely.
                if (
                    corpus_raw_rank is not None
                    and disposition == ExclusionDisposition.OUTSIDE_CANDIDATE_WINDOW
                ):
                    disposition = ExclusionDisposition.OUTSIDE_CANDIDATE_WINDOW
                return self._make_absent_stages(
                    eligible_count=eligible_count,
                    window_size=candidate_window_size,
                    expected_item_id=expected_item_id,
                    disposition=disposition,
                    item_budget=resolved_item,
                    byte_budget=resolved_byte,
                    token_budget=resolved_token,
                    raw_rank=raw_rank,
                    raw_score=raw_score,
                    raw_rank_exact=production_window_exact,
                    corpus_raw_rank=corpus_raw_rank,
                    corpus_raw_rank_exact=corpus_raw_rank_exact,
                    trust_rank=trust_rank,
                    trust_score=trust_score,
                )

            # ===================================================================
            # PRODUCTION LANE: enrich, expand, enforce budget
            # ===================================================================
            # Only the production candidate window flows downstream.
            cand_uuid_ids = [UUID_Type(c["id"]) for c in production_candidates]
            item_by_id: dict[UUID_Type, MemoryItem] = {}
            if cand_uuid_ids:
                rows = await session.execute(
                    select(MemoryItem).where(
                        MemoryItem.id.in_(cand_uuid_ids),
                        read_eligibility_expression(memory_context),
                    )
                )
                item_by_id = {item.id: item for item in rows.scalars().all()}

            enriched: list[dict[str, Any]] = []
            for cand in production_candidates:
                item = item_by_id.get(UUID_Type(cand["id"]))
                if item is None:
                    continue
                warnings: list[str] = []
                if item.review_status == "proposed":
                    warnings.append("unreviewed")
                enriched.append(
                    {
                        **cand,
                        "pinned": item.pinned,
                        "importance": item.importance,
                        "source_trust": item.source_trust,
                        "memory_confidence": item.memory_confidence,
                        "human_verified": item.human_verified,
                        "authority": item.authority,
                        "visibility": item.visibility,
                        "workspace_id": str(item.workspace_id) if item.workspace_id else None,
                        "conflict_type": item.conflict_type,
                        "conflict_resolution_status": item.conflict_resolution_status,
                        "warnings": warnings,
                    }
                )

            # --- Relationship expansion (production lane only) ---
            expanded = await expand_recall_candidates(
                session,
                memory_context=memory_context,
                workspace_id=None,
                semantic_items=enriched,
                item_by_id=item_by_id,
                now=datetime.now(UTC),
            )

            post_rel_rank = self._find_rank(expanded, expected_item_id)
            candidate_origin = (
                self._find_field(expanded, expected_item_id, "origin") or "semantic"
            )

            # Check if item was dropped by expansion ceiling
            expanded_ids = {c["id"] for c in expanded}
            if expected_item_id not in expanded_ids:
                return self._make_absent_stages(
                    eligible_count=eligible_count,
                    window_size=candidate_window_size,
                    expected_item_id=expected_item_id,
                    disposition=ExclusionDisposition.RELATIONSHIP_DEMOTED,
                    item_budget=resolved_item,
                    byte_budget=resolved_byte,
                    token_budget=resolved_token,
                    raw_rank=raw_rank,
                    raw_score=raw_score,
                    raw_rank_exact=production_window_exact,
                    corpus_raw_rank=corpus_raw_rank,
                    corpus_raw_rank_exact=corpus_raw_rank_exact,
                    trust_rank=trust_rank,
                    trust_score=trust_score,
                )

            # --- Budget enforcement (production lane only) ---
            selected = recall_mod._enforce_semantic_budget(
                expanded,
                byte_budget=resolved_byte,
                token_budget=resolved_token,
                item_budget=resolved_item,
            )

            final_rank = self._find_rank(selected, expected_item_id)
            final_score = self._find_field(selected, expected_item_id, "score")

            # --- Determine exclusion disposition ---
            selected_ids = {c["id"] for c in selected}
            if expected_item_id in selected_ids:
                disposition = ExclusionDisposition.SELECTED
            else:
                disposition = self._determine_budget_exclusion(
                    expanded=expanded,
                    expected_item_id=expected_item_id,
                    raw_rank=raw_rank,
                    trust_rank=trust_rank,
                    post_rel_rank=post_rel_rank,
                    item_budget=resolved_item,
                    byte_budget=resolved_byte,
                    token_budget=resolved_token,
                    recall_mod=recall_mod,
                )

            return StageRanks(
                eligible_candidate_count=eligible_count,
                candidate_window_size=candidate_window_size,
                raw_similarity_rank=raw_rank,
                raw_similarity_rank_1based=(raw_rank + 1) if raw_rank is not None else None,
                raw_similarity_score=round(raw_score, 4) if raw_score is not None else None,
                raw_rank_exact=production_window_exact,
                corpus_raw_rank=corpus_raw_rank,
                corpus_raw_rank_1based=(
                    (corpus_raw_rank + 1) if corpus_raw_rank is not None else None
                ),
                corpus_raw_rank_exact=corpus_raw_rank_exact,
                trust_rank=trust_rank,
                trust_rank_1based=(trust_rank + 1) if trust_rank is not None else None,
                trust_score=round(trust_score, 4) if trust_score is not None else None,
                post_relationship_rank=post_rel_rank,
                post_relationship_rank_1based=(post_rel_rank + 1)
                if post_rel_rank is not None else None,
                final_served_rank=final_rank,
                final_served_rank_1based=(final_rank + 1) if final_rank is not None else None,
                final_score=round(final_score, 4) if final_score is not None else None,
                candidate_origin=str(candidate_origin),
                exclusion_disposition=disposition,
                item_budget=resolved_item,
                byte_budget=resolved_byte,
                token_budget=resolved_token,
            ), selected

    @staticmethod
    def _find_rank(items: list[dict[str, Any]], expected_id: str) -> int | None:
        """Find the 0-based rank of expected_id in items (by their current order)."""
        for i, item in enumerate(items):
            if str(item.get("id")) == str(expected_id):
                return i
        return None

    @staticmethod
    def _find_field(
        items: list[dict[str, Any]], expected_id: str, field_name: str
    ) -> Any:
        """Find a field value for expected_id in items."""
        for item in items:
            if str(item.get("id")) == str(expected_id):
                return item.get(field_name)
        return None

    async def _classify_absence(
        self,
        session: Any,
        expected_item_id: str,
        tenant_id: str,
    ) -> str:
        """Classify why an item is absent from the candidate window."""
        from sqlalchemy import text

        row = (
            (
                await session.execute(
                    text(
                        "SELECT review_status, valid_to, tenant_id::text AS tid "
                        "FROM memory_items WHERE id = :id"
                    ),
                    {"id": expected_item_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return ExclusionDisposition.NOT_ELIGIBLE
        if str(row["tid"]) != str(tenant_id):
            return ExclusionDisposition.NOT_ELIGIBLE
        if row["valid_to"] is not None:
            return ExclusionDisposition.NOT_ELIGIBLE
        if row["review_status"] not in ("active", "proposed"):
            return ExclusionDisposition.NOT_ELIGIBLE

        # Eligible but not in candidates — check for embedding.
        emb = (
            (
                await session.execute(
                    text(
                        "SELECT embedding_status FROM memory_embeddings "
                        "WHERE memory_item_id = :id AND embedding_status = 'ready' "
                        "LIMIT 1"
                    ),
                    {"id": expected_item_id},
                )
            )
            .mappings()
            .first()
        )
        if emb is None:
            return ExclusionDisposition.NO_EMBEDDING
        return ExclusionDisposition.OUTSIDE_CANDIDATE_WINDOW

    @staticmethod
    def _determine_budget_exclusion(
        *,
        expanded: list[dict[str, Any]],
        expected_item_id: str,
        raw_rank: int | None,
        trust_rank: int | None,
        post_rel_rank: int | None,
        item_budget: int | None,
        byte_budget: int | None,
        token_budget: int | None,
        recall_mod: Any,
    ) -> str:
        """Determine which budget excluded the item from the final served list.

        Does NOT infer item_budget_excluded merely from absence — re-runs the
        budget enforcement with individual budgets to prove the cause.
        """
        # Re-run with only item_budget to see if item_budget alone excluded it.
        if item_budget is not None:
            item_only = recall_mod._enforce_semantic_budget(
                expanded, byte_budget=None, token_budget=None, item_budget=item_budget
            )
            item_only_ids = {c["id"] for c in item_only}
            if expected_item_id not in item_only_ids:
                # item_budget excluded it. Was it trust-demoted?
                if (
                    raw_rank is not None
                    and trust_rank is not None
                    and raw_rank < item_budget
                    and trust_rank >= item_budget
                ):
                    return ExclusionDisposition.TRUST_DEMOTED
                # Was it relationship-demoted?
                if (
                    trust_rank is not None
                    and post_rel_rank is not None
                    and trust_rank < item_budget
                    and post_rel_rank >= item_budget
                ):
                    return ExclusionDisposition.RELATIONSHIP_DEMOTED
                return ExclusionDisposition.ITEM_BUDGET_EXCLUDED

        # item_budget didn't exclude it — check byte_budget.
        if byte_budget is not None:
            byte_only = recall_mod._enforce_semantic_budget(
                expanded, byte_budget=byte_budget, token_budget=None, item_budget=None
            )
            if expected_item_id not in {c["id"] for c in byte_only}:
                return ExclusionDisposition.BYTE_BUDGET_EXCLUDED

        # Check token_budget.
        if token_budget is not None:
            token_only = recall_mod._enforce_semantic_budget(
                expanded, byte_budget=None, token_budget=token_budget, item_budget=None
            )
            if expected_item_id not in {c["id"] for c in token_only}:
                return ExclusionDisposition.TOKEN_BUDGET_EXCLUDED

        return ExclusionDisposition.UNKNOWN

    @staticmethod
    def _make_absent_stages(
        *,
        eligible_count: int,
        window_size: int,
        expected_item_id: str,
        disposition: str,
        item_budget: int | None,
        byte_budget: int | None,
        token_budget: int | None,
        raw_rank: int | None = None,
        raw_score: float | None = None,
        raw_rank_exact: bool = False,
        corpus_raw_rank: int | None = None,
        corpus_raw_rank_exact: bool = False,
        trust_rank: int | None = None,
        trust_score: float | None = None,
    ) -> tuple[StageRanks, list[dict[str, Any]]]:
        """Build StageRanks for an item absent from a given stage."""
        return StageRanks(
            eligible_candidate_count=eligible_count,
            candidate_window_size=window_size,
            raw_similarity_rank=raw_rank,
            raw_similarity_rank_1based=(raw_rank + 1) if raw_rank is not None else None,
            raw_similarity_score=round(raw_score, 4) if raw_score is not None else None,
            raw_rank_exact=raw_rank_exact,
            corpus_raw_rank=corpus_raw_rank,
            corpus_raw_rank_1based=(corpus_raw_rank + 1) if corpus_raw_rank is not None else None,
            corpus_raw_rank_exact=corpus_raw_rank_exact,
            trust_rank=trust_rank,
            trust_rank_1based=(trust_rank + 1) if trust_rank is not None else None,
            trust_score=round(trust_score, 4) if trust_score is not None else None,
            post_relationship_rank=None,
            post_relationship_rank_1based=None,
            final_served_rank=None,
            final_served_rank_1based=None,
            final_score=None,
            candidate_origin="semantic",
            exclusion_disposition=disposition,
            item_budget=item_budget,
            byte_budget=byte_budget,
            token_budget=token_budget,
        ), []

    async def run_benchmark(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        corpus_profile: CorpusProfile,
        queries: list[QueryFixture],
        budgets: list[int] | None = None,
        byte_budgets: list[int | None] | None = None,
        token_budgets: list[int | None] | None = None,
    ) -> list[BenchmarkResult]:
        """Run all query-budget combinations.

        Each budget is measured independently — results are NOT aggregated
        across budgets in the ranking metrics (F5 fix).
        """
        if budgets is None:
            budgets = [5, 10, 20]
        resolved_byte_budgets = (
            byte_budgets if byte_budgets is not None else [None] * len(budgets)
        )
        resolved_token_budgets = (
            token_budgets if token_budgets is not None else [None] * len(budgets)
        )

        query_vector = corpus_profile.query_vector
        query_embedding_fn = corpus_profile.query_embedding_fn

        results: list[BenchmarkResult] = []
        for qf in queries:
            for i, budget in enumerate(budgets):
                result = await self.run_single_query(
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    query=qf.query,
                    query_vector=query_vector,
                    expected_item_id=qf.expected_item_id,
                    item_budget=budget,
                    byte_budget=resolved_byte_budgets[i]
                    if i < len(resolved_byte_budgets) else None,
                    token_budget=resolved_token_budgets[i]
                    if i < len(resolved_token_budgets) else None,
                    query_embedding_fn=query_embedding_fn,
                )
                results.append(result)
        return results

    @staticmethod
    def summarize(results: list[BenchmarkResult]) -> dict[str, Any]:
        """Compute per-budget metrics from benchmark results.

        MRR, top-k, and recall@k are calculated separately for each budget.
        No aggregate ranking score across mixed budgets is published without
        an explicit label (F5 fix).
        """
        if not results:
            return {"error": "no results"}

        # Group by item_budget (F5: never combine across budgets).
        per_budget: dict[int | str, dict[str, Any]] = {}
        budgets_seen = sorted(
            {r.item_budget or "none" for r in results},
            key=lambda x: (x is None, x),
        )
        for budget in budgets_seen:
            budget_results = [
                r for r in results
                if (r.item_budget or "none") == budget
            ]
            total = len(budget_results)
            hits_top1 = sum(1 for r in budget_results if r.top_k_hit.get("top_1", False))
            hits_top5 = sum(1 for r in budget_results if r.top_k_hit.get("top_5", False))
            hits_top10 = sum(1 for r in budget_results if r.top_k_hit.get("top_10", False))

            reciprocal_ranks = []
            for r in budget_results:
                if r.stages and r.stages.final_served_rank is not None:
                    reciprocal_ranks.append(1.0 / (r.stages.final_served_rank + 1))
                else:
                    reciprocal_ranks.append(0.0)
            mrr = sum(reciprocal_ranks) / total if total > 0 else 0.0

            latencies = [r.latency_ms for r in budget_results]
            per_budget[budget] = {
                "total_queries": total,
                "top_1_hit_rate": round(hits_top1 / total, 4) if total else 0.0,
                "top_5_hit_rate": round(hits_top5 / total, 4) if total else 0.0,
                "top_10_hit_rate": round(hits_top10 / total, 4) if total else 0.0,
                "recall_at_5": round(hits_top5 / total, 4) if total else 0.0,
                "recall_at_10": round(hits_top10 / total, 4) if total else 0.0,
                "mrr": round(mrr, 4),
                "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
            }

        return {
            "total_measurements": len(results),
            "budgets_measured": sorted(
                {r.item_budget for r in results if r.item_budget is not None}
            ),
            "per_budget": per_budget,
            "note": (
                "Metrics are computed per-budget; "
                "no cross-budget ranking aggregate is published."
            ),
        }

    @staticmethod
    def generate_report(
        *,
        repository_sha: str,
        results: list[BenchmarkResult],
        corpus_profile: CorpusProfile,
        embedding_profile_key: str,
        embedding_model: str,
        embedding_dimensions: int,
        scoring_version: str,
        config_version: str,
    ) -> BenchmarkReport:
        """Produce a machine-readable benchmark report with full provenance."""
        summary = ServiceBenchmarkSuite.summarize(results)
        return BenchmarkReport(
            repository_sha=repository_sha,
            scoring_version=scoring_version,
            config_version=config_version,
            embedding_profile=embedding_profile_key,
            embedding_model=embedding_model,
            embedding_dimensions=embedding_dimensions,
            corpus_profile_name=corpus_profile.name,
            corpus_fingerprint=corpus_profile.corpus_fingerprint(),
            results=results,
            summary=summary,
            generated_at=datetime.now(UTC).isoformat(),
        )


# ---------------------------------------------------------------------------
# HTTP benchmark client (optional, latency-only mode)
# ---------------------------------------------------------------------------


class HttpBenchmarkClient:
    """Optional HTTP-mode benchmark client for end-to-end latency measurement.

    This mode does NOT provide stage decomposition — the public RecallResponse
    contract strips internal pipeline fields (candidate_count, distance,
    trust_score, etc.). Stage decomposition is only available via
    ``ServiceBenchmarkSuite``.

    Fail-closed: non-2xx, malformed JSON, and missing required fields raise
    ``TransportError`` rather than degrading quality metrics (F3 fix).
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self._client: Any = None

    async def __aenter__(self) -> HttpBenchmarkClient:
        import httpx

        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def run_single_query(
        self,
        query: str,
        expected_item_id: str,
        item_budget: int,
    ) -> BenchmarkResult:
        """Run one query via HTTP. Fails closed on transport/parsing errors."""
        if not expected_item_id or not expected_item_id.strip():
            raise EmptyFixtureIdError()

        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)

        query_digest = hashlib.sha256(query.encode()).hexdigest()[:16]
        headers = {"Authorization": f"Bearer {self.api_key}"}
        start = time.monotonic()

        try:
            resp = await self._client.post(
                f"{self.base_url}/v1/recall",
                json={"query": query, "mode": "semantic", "item_budget": item_budget},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"HTTP transport failure: {exc}") from exc

        latency_ms = (time.monotonic() - start) * 1000

        # F3: non-2xx must fail closed.
        if resp.status_code < 200 or resp.status_code >= 300:
            raise TransportError(
                f"HTTP {resp.status_code}: benchmark cannot proceed with "
                f"a non-2xx response"
            )

        # F3: malformed JSON must fail closed.
        try:
            data = resp.json()
        except Exception as exc:
            raise TransportError(f"malformed JSON response: {exc}") from exc

        # F3: missing required fields must fail closed.
        for required_field in ("items", "item_count", "byte_count"):
            if required_field not in data:
                raise TransportError(
                    f"missing required field '{required_field}' in response"
                )

        items = data["items"]
        # F8: use actual byte_count from the response.
        returned_bytes = data["byte_count"]

        # Find the expected item's position (0-based).
        exact_rank = None
        for i, item in enumerate(items):
            if str(item.get("id")) == str(expected_item_id):
                exact_rank = i
                break

        top_k = {
            "top_1": exact_rank is not None and exact_rank < 1,
            "top_5": exact_rank is not None and exact_rank < 5,
            "top_10": exact_rank is not None and exact_rank < 10,
        }

        return BenchmarkResult(
            query=query,
            query_digest=query_digest,
            expected_item_id=expected_item_id,
            item_budget=item_budget,
            byte_budget=None,
            token_budget=None,
            top_k_hit=top_k,
            latency_ms=round(latency_ms, 1),
            latency_type="end_to_end",
            returned_bytes=returned_bytes,
            returned_count=len(items),  # F8: actual count
            stages=None,  # Stage decomposition unavailable via HTTP
            error=None,
        )
