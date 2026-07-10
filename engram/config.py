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
    embedding_activation_coverage_threshold: float = 95.0

    # Classification
    # Provider: "openai", "local", or "none" (rule-based fallback only)
    classification_provider: str = "none"
    classification_model: str = "gpt-4o-mini"
    classification_confidence_threshold: float = 0.5

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

    # Background worker / job queue (ENG-AUD-008). The service still works
    # without a worker running; pending jobs simply queue and semantic recall /
    # LLM refinement lag until processed.
    # How often (seconds) a polling worker claims jobs. ``--once`` ignores this.
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
    # Blend weight for memory_confidence: candidate = min(source_default,
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


settings = Settings()
