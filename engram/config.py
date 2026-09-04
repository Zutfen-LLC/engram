"""Configuration management for Engram service."""

from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENGRAM_",
        env_file=".env",
        extra="ignore",
    )

    # Database — runtime application role (non-owner; RLS-enforced).
    database_url: str = "postgresql+asyncpg://engram:engram@localhost:5432/engram"

    # Owner/migration role — used for DDL (``engram init-db``), the first-key
    # bootstrap, cross-tenant admin/CLI scans, and principal/key resolution
    # (which must see across tenants and therefore bypass RLS). When unset it
    # falls back to ``database_url`` (single-role dev/test, where the same role
    # is both owner and app). In the default Compose deployment this points at
    # the table-owning superuser so migrations and admin commands work.
    owner_database_url: str | None = None

    # Optional, dedicated least-privilege connection for service-to-service
    # tenant provisioning.  It intentionally has no fallback: pointing this at
    # the application or owner role would collapse the provisioning boundary.
    service_provisioning_enabled: bool = False
    provisioner_database_url: str | None = None
    provisioner_database_role: str = "engram_provisioner"
    delegation_enabled: bool = False
    delegation_default_ttl_seconds: int = 60
    delegation_max_ttl_seconds: int = 300
    review_delegation_enabled: bool = False
    review_delegation_default_ttl_seconds: int = 30
    review_delegation_max_ttl_seconds: int = 60
    # Fixed Portal installation enrollment. The credential itself is never an
    # environment variable. It is read from this mode-0600 file when enabled.
    portal_enrollment_enabled: bool = False
    portal_enrollment_secret_file: str | None = None
    portal_enrollment_require_https: bool = True
    portal_development_setup: bool = False

    # Read-oriented database URL (ENG-AUD-011 / F18). Optional: when unset,
    # read-heavy paths (currently: startup recall candidate selection) use
    # ``database_url`` like every other request. When set, it should point at
    # a read replica (or any read-only-safe connection) reachable with the
    # same app-role credentials/RLS posture as ``database_url`` — RLS context
    # is applied identically. Write actions (promotion, telemetry, item
    # events, job enqueue) never use this connection, regardless of whether
    # it is set.
    read_database_url: str | None = None

    # Service
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # Embeddings
    embedding_dim: int = 1536
    # Provider: "openai", "local", or "none" (defer embedding generation)
    embedding_provider: str = "none"
    openai_api_key: str | None = None
    # Base URL for OpenAI-compatible embedding providers (e.g. OpenRouter).
    # When set, passed explicitly to the AsyncOpenAI client so it does not
    # depend on the OPENAI_BASE_URL env var being present in the container.
    openai_base_url: str | None = None
    # Default embedding model name. Used by the setup-embeddings CLI command
    # and as a fallback when no embedding profile is configured in the DB.
    embedding_model: str = "text-embedding-3-small"
    embedding_activation_coverage_threshold: float = 95.0

    # Classification
    # Provider: "openai", "local", or "none" (rule-based fallback only)
    classification_provider: str = "none"
    classification_model: str = "gpt-4o-mini"
    classification_confidence_threshold: float = 0.5
    # Dedicated API key/base URL for classification. When set, used instead of
    # the shared openai_* settings — allows classification on a different
    # provider (e.g. DeepInfra) while embeddings use OpenRouter.
    classification_api_key: str | None = None
    classification_base_url: str | None = None

    # Auth
    # When false, auth is skipped (dev mode). Production must set True.
    auth_enabled: bool = False

    # Short TTL (seconds) for the in-process principal cache used by new-format
    # (eng_<key_id>_<secret>) API keys. A successful verification is cached so
    # repeated requests with the same key skip the DB lookup. Set to 0 to
    # disable caching. Revocation takes effect after at most this many seconds
    # (a revoked key may still authenticate until its cache entry expires).
    api_key_cache_ttl_seconds: int = 60
    # Safety cap on the number of cached principals (per process). Evicts the
    # soonest-expiring entries when exceeded.
    api_key_cache_max_size: int = 4096

    # Recall defaults
    recall_byte_budget: int = 4096
    recall_item_budget: int = 50
    max_pinned_tokens: int = 2048  # hard ceiling for pinned items in startup recall
    stale_after_days: int = 90  # items not verified in N days are "stale"
    # penalize after N startup recalls without feedback
    startup_recall_penalty_threshold: int = 5
    startup_recall_penalty_factor: float = 0.5  # reduce recency bonus per excess recall
    startup_recall_penalty_floor: float = 0.1  # recency component minimum (never zero)
    # N distinct non-author agents for partial penalty reset
    quorum_reset_agent_count: int = 2

    # Bounded SQL candidate selection for startup recall (ENG-AUD-011 / F18).
    # Postgres performs a coarse first-stage selection of at most this many
    # candidate rows (across the diversified sub-pools — see
    # engram.recall._fetch_startup_candidates); Python's detailed scorer then
    # runs only over that bounded set instead of the whole eligible corpus.
    # Must be >= recall_item_budget (enforced at settings load) since a
    # candidate pool smaller than the item budget could under-fill recall.
    # Callers cannot raise this through the public recall API — it is a
    # deployment-level setting only.
    startup_recall_candidate_limit: int = 500
    # Hard safety cap: startup_recall_candidate_limit is clamped to this even
    # if misconfigured, so a bad env value cannot reintroduce an unbounded scan.
    startup_recall_candidate_limit_max: int = 5000

    # Write-path cost control
    # if False, low-trust writes defer conflict check to promotion
    conflict_check_on_write: bool = True

    # Usage/metering telemetry (ENG-METER-001). Observability only — never
    # gates or throttles a request. Defaults to False for the open-source/
    # self-hosted distribution; dogfood/operator deployments opt in
    # explicitly. When False, engram.usage helpers are cheap no-ops that never
    # open a database session.
    usage_telemetry_enabled: bool = False

    # Context-receipt startup dark writes (ENG-CONTEXT-002B). API-only,
    # default-off, fail-open. When disabled, the recall route performs no
    # receipt work at all (no manifest, no receipt DB session, no telemetry).
    # When enabled, a successful startup recall additionally builds a
    # ContextManifestV1 from the finalized RecallResponse, persists one
    # immutable context_receipts row on a dedicated app-role session, reloads
    # and verifies the stored JSONB, and commits only after verification
    # succeeds. Any receipt failure is swallowed and the recall response is
    # returned unchanged. The timeout bounds the whole enabled attempt. Not
    # propagated to the worker (API-only).
    context_receipt_dark_write_enabled: bool = False
    # Strictly positive; invalid (<=0) configuration fails settings
    # validation with no silent negative-to-positive coercion. Covers manifest
    # construction, persistence, reload, verification, and commit.
    context_receipt_dark_write_timeout_seconds: float = 1.0

    # Background worker / job queue (ENG-AUD-008). The service still works
    # without a worker running; pending jobs simply queue and semantic recall /
    # LLM refinement lag until processed.
    # Idle/error backoff (seconds) before the worker tries another claim.
    # Available jobs are processed serially without this delay; ``--once`` ignores it.
    job_poll_interval_seconds: float = 2.0
    # Default per-job retry ceiling before a job is marked dead (overridable per
    # enqueue). Matches the jobs.max_attempts column default.
    job_max_attempts: int = 5
    # A running job whose locked_at is older than this (seconds) is considered
    # abandoned (worker crash) and returned to pending on the next reclaim.
    job_lease_stale_after_seconds: int = 300

    # Classification vocabulary cache (ENG-AUD-008 / F20). In-process TTL cache
    # so the six DISTINCT vocab scans run at most once per window per tenant.
    # Set to 0 to disable caching (every classify() rescans — useful in tests).
    vocab_cache_ttl_seconds: int = 120
    # Safety cap on the number of cached tenant vocab entries (LRU eviction).
    vocab_cache_max_tenants: int = 256

    # LLM classification refinement (async, classification.refine jobs).
    # Deprecated compatibility setting. Taxonomy refinement no longer mutates
    # memory_confidence; retained so existing deployment configuration still loads.
    # Historical blend policy: candidate = min(source_default,
    # classifier_confidence); new_confidence blends toward candidate by this.
    classification_confidence_blend: float = 0.5
    # Minimum confidence delta before a refine job records a change (idempotency
    # guard against oscillation). 0.0 applies any improvement.
    classification_refine_min_delta: float = 0.0

    # Relationship-aware recall — graph + tunnel expansion (ENG-AUD-012 / F19).
    # Semantic recall expands its top candidates via depth-1 graph edges and
    # tunnel membership before rescoring/budget packing (see
    # engram.relationship_recall). All limits below are deployment-level
    # safety caps, not part of the public recall API.
    relationship_expansion_enabled: bool = True
    # How many of the top semantic candidates are used as expansion seeds.
    # Protects the pipeline from expanding a huge unbounded semantic result.
    recall_semantic_expansion_seed_limit: int = 50
    # Per-seed neighbor caps — a single highly-connected node cannot dominate
    # recall (requirement 11: "avoid relationship explosions").
    max_graph_neighbors_per_item: int = 5
    max_tunnel_neighbors_per_item: int = 5
    # Overall additions from each expansion stage, after eligibility filtering.
    max_graph_expanded_items: int = 20
    max_tunnel_additions: int = 20
    # Ceiling on the merged (semantic + graph + tunnel) candidate set before
    # relationship-aware rescoring and budget packing.
    recall_candidate_ceiling: int = 100
    # Relationship-aware scoring weights (requirement 6). Semantic relevance
    # must dominate — relationship/tunnel bonuses stay intentionally modest.
    relationship_score_weight_semantic: float = 0.70
    relationship_score_weight_relationship: float = 0.15
    relationship_score_weight_tunnel: float = 0.10
    relationship_score_weight_importance: float = 0.05

    # Promotion (Path A auto-promotion, ENG-AUD-007)
    # Hard cap on proposed items scanned per lazy startup-recall promotion pass
    # (POST /v1/recall mode=startup) — keeps startup recall bounded regardless
    # of how large a tenant's proposed backlog grows. The explicit CLI/admin
    # promotion paths pass their own limit (or None for unbounded).
    startup_promotion_limit: int = 20
    # Top-k plausible active-item candidates considered by the promotion-time
    # conflict recheck (engram.conflicts.find_promotion_conflict_candidates).
    promotion_conflict_candidate_k: int = 5
    # Rollout flag for the canonical promotion.evaluate job contract (issue
    # #155, ENG-PROMOTION-003B2). Default false preserves the existing
    # promotion.path_a scheduling behavior at the one producer wired in this
    # slice (classification.refine's delayed evidence-promotion schedule).
    # Legacy promotion.path_a jobs remain supported and executable regardless
    # of this flag's value.
    promotion_evaluate_jobs_enabled: bool = False
    # Rollout flag for the bounded promotion reconciliation backstop (issue
    # #155, ENG-PROMOTION-003B4). Default false: no promotion.reconcile work
    # is created, the worker chain bootstrap is inert, and startup recall's
    # lazy promotion pass is untouched. Turning the flag off again stops new
    # reconciliation work without disabling legacy startup promotion or
    # already-queued promotion.evaluate / promotion.path_a jobs. Independent
    # of promotion_evaluate_jobs_enabled: when this flag is on but the
    # evaluate flag is off, reconciliation runs its bounded passes but
    # suppresses (and records) promotion.evaluate repairs rather than
    # substituting any broader/legacy mutation mechanism.
    promotion_reconciliation_enabled: bool = False
    # Period between bounded backstop reconciliation passes per tenant (the
    # self-rescheduling promotion.reconcile chain's interval). Each pass is
    # bounded by promotion_reconciliation_pass_limit regardless.
    promotion_reconciliation_interval_seconds: int = 3600
    # Hard per-pass bound: rows inspected (and at most that many repair jobs
    # emitted) by one promotion.reconcile pass. Deployment-level cap, not part
    # of any public API.
    promotion_reconciliation_pass_limit: int = 20

    @model_validator(mode="after")
    def _clamp_startup_recall_candidate_limit(self) -> Settings:
        """Enforce the documented invariants for the candidate-pool setting.

        Positive, bounded by ``startup_recall_candidate_limit_max`` (a
        misconfigured env value cannot reintroduce an unbounded scan), and at
        least ``recall_item_budget`` (a pool smaller than the item budget
        could under-fill recall before Python packing even runs).
        """
        limit = max(1, self.startup_recall_candidate_limit)
        limit = min(limit, self.startup_recall_candidate_limit_max)
        limit = max(limit, self.recall_item_budget)
        self.startup_recall_candidate_limit = limit
        return self

    @model_validator(mode="after")
    def _validate_service_provisioner(self) -> Settings:
        if self.service_provisioning_enabled and not self.provisioner_database_url:
            raise ValueError(
                "provisioner_database_url is required when service_provisioning_enabled=true"
            )
        return self

    @model_validator(mode="after")
    def _validate_delegation(self) -> Settings:
        if not 30 <= self.delegation_max_ttl_seconds <= 300:
            raise ValueError("delegation_max_ttl_seconds must be between 30 and 300")
        if not 30 <= self.delegation_default_ttl_seconds <= self.delegation_max_ttl_seconds:
            raise ValueError(
                "delegation_default_ttl_seconds must be between 30 and "
                "delegation_max_ttl_seconds"
            )
        if self.delegation_enabled and not self.service_provisioning_enabled:
            raise ValueError(
                "service_provisioning_enabled must be true when delegation_enabled=true"
            )
        if not 30 <= self.review_delegation_max_ttl_seconds <= 60:
            raise ValueError(
                "review_delegation_max_ttl_seconds must be between 30 and 60"
            )
        if not (
            30
            <= self.review_delegation_default_ttl_seconds
            <= self.review_delegation_max_ttl_seconds
        ):
            raise ValueError(
                "review_delegation_default_ttl_seconds must be between 30 and "
                "review_delegation_max_ttl_seconds"
            )
        if self.review_delegation_enabled and not self.service_provisioning_enabled:
            raise ValueError(
                "service_provisioning_enabled must be true when "
                "review_delegation_enabled=true"
            )
        if self.portal_enrollment_enabled:
            if not self.portal_enrollment_secret_file:
                raise ValueError(
                    "portal_enrollment_secret_file is required when "
                    "portal_enrollment_enabled=true"
                )
            if not (
                self.service_provisioning_enabled
                and self.delegation_enabled
                and self.review_delegation_enabled
            ):
                raise ValueError(
                    "service provisioning, read delegation, and review delegation "
                    "must be enabled when portal enrollment is enabled"
                )
            if self.delegation_max_ttl_seconds < 60:
                raise ValueError(
                    "delegation_max_ttl_seconds must be at least 60 when portal enrollment "
                    "is enabled"
                )
            if not self.portal_enrollment_require_https and not self.portal_development_setup:
                raise ValueError(
                    "portal enrollment can use HTTP only when portal_development_setup=true"
                )
        return self

    @model_validator(mode="after")
    def _validate_promotion_reconciliation(self) -> Settings:
        """Keep the backstop strictly bounded and honestly periodic.

        A non-positive pass limit or interval is invalid configuration, not a
        request to disable the feature (the dedicated
        ``promotion_reconciliation_enabled`` flag owns that). Surfaces at
        settings load rather than silently producing an unbounded or hot-loop
        reconciliation pass.
        """
        if self.promotion_reconciliation_pass_limit < 1:
            raise ValueError("promotion_reconciliation_pass_limit must be >= 1")
        if self.promotion_reconciliation_interval_seconds < 1:
            raise ValueError("promotion_reconciliation_interval_seconds must be >= 1")
        return self

    @model_validator(mode="after")
    def _validate_context_receipt_dark_write_timeout(self) -> Settings:
        """Require a finite, strictly positive context-receipt dark-write timeout.

        A non-positive timeout is invalid configuration, not a request to
        disable the feature (the dedicated ``context_receipt_dark_write_
        enabled`` flag owns that). There is no silent negative-to-positive
        coercion: a misconfigured value surfaces at settings load rather than
        silently bounding every enabled attempt to zero.

        NaN and ±Infinity are also rejected: NaN compares unequal to every
        value (so ``NaN <= 0`` is ``False`` and would slip through a naive
        ``<= 0`` check), and an unbounded timeout defeats the fail-open
        guarantee. ``math.isfinite`` rejects both in one check.
        """
        import math

        timeout = self.context_receipt_dark_write_timeout_seconds
        if not math.isfinite(timeout) or timeout <= 0.0:
            raise ValueError(
                "context_receipt_dark_write_timeout_seconds must be a finite, "
                "strictly positive number (got "
                f"{timeout!r})"
            )
        return self


settings = Settings()
