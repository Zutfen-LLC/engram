"""Recall admission profiles (issue #160 / ENG-RECALL-003).

A recall profile is the explicit, named policy selector for the recall read
path. Before this module, "semantic recall" silently meant "active plus
proposed items ranked by ``similarity * trust_score``" — a single blended
number that mixed relevance, popularity, source priors, and review state (see
``engram.semantic.compute_semantic_trust_score``). Profiles replace that
implicit behavior with an explicit admission boundary:

* ``legacy`` — the named compatibility profile. Byte-for-byte the pre-#160
  semantic behavior (active + proposed, blended trust ranking, relationship
  expansion). It exists so rollout and rollback are a parameter change, not a
  code change, and can be removed once SDK/MCP consumers migrate.
* ``governed`` — ordinary operational recall. Only items the admission gate
  admits for this serving mode: reviewed/active items (plus governed-disputed
  stay kinds, mirroring startup eligibility), each bound to the durable
  admission assessment that authorized serving when one exists
  (``engram.admission_assessment``). Proposed items are excluded no matter how
  similar or important they are.
* ``exploratory`` — explicit caller opt-in. May include proposals and marked
  (not hidden) uncertainty, under tighter budgets, with every uncertain item
  machine-readable (``epistemic_state`` + ``warning_codes``).

``startup`` is not a selectable semantic profile — startup recall *is* its own
profile, with its own deterministic pipeline (``engram.recall`` startup path).

Serving authority (the rollout boundary): until a profile is certified
(:data:`CERTIFIED_SERVING_PROFILES`), it may only be *evaluated* — never
served. ``POST /v1/recall`` always serves the certified packet (``legacy``
today); governed/exploratory packets are computed exclusively by the
authorized shadow-comparison surface (``engram.recall_shadow``), which is
read-only and never changes what any caller is served. A caller may name any
registered profile for evaluation, but Engram stays authoritative for
eligibility, admission, reasons, budgets, and whether a profile may serve at
all. ``apply_profile_budget_caps`` bounds exploratory packets below the
tenant/default budgets. Review/audit surfaces (``review``,
``historical/audit`` in the issue's candidate list) are separate follow-up
work with their own capability requirements; they are deliberately not
selectable here yet.

See ``docs/adr-160-recall-profiles.md`` for the decision record and the
profile matrix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

# Contract identity for everything in this module. Bump/branch this only with
# an ADR: recall logs and receipts reference it.
RECALL_PROFILE_CONTRACT_VERSION: Final[Literal["recall-profiles-v1"]] = "recall-profiles-v1"

# Ranking produced by the separated signal model (relevance x utility, after
# admission — see engram.recall_signals). Distinct from the legacy blend's
# "semantic-v3" so recall_logs.scoring_version identifies which ranking
# produced a given packet.
SIGNALS_RANKING_VERSION: Final[Literal["semantic-signals-v1"]] = "semantic-signals-v1"

STARTUP_PROFILE_KEY: Final[Literal["startup"]] = "startup"


class RecallProfileError(ValueError):
    """An unknown or mode-incompatible profile was requested."""


class RecallProfileNotServableError(RecallProfileError):
    """A known profile was requested for serving but is not certified.

    Governed/exploratory are evaluation candidates until accepted #162
    certification records them in :data:`CERTIFIED_SERVING_PROFILES`. The
    serving routes map this to HTTP 422: silently substituting the legacy
    packet for an explicitly requested profile would mislead the caller about
    what produced their working memory.
    """


# The #162 certification boundary for authoritative serving on POST /v1/recall.
#
# A profile key appears here ONLY after the accepted #162 certification gates
# pass and the ADR records the decision. It is a code-level constant precisely
# so that no request parameter, tenant setting, or deployment configuration
# can grant serving authority: #162D/#176 terminated NOT_CERTIFIED and there is
# currently no certification for governed/exploratory serving behavior. Until
# one exists, ordinary recall serves the legacy packet only, and candidate
# profiles are computable solely through the read-only shadow comparison
# surface (engram.recall_shadow + POST /v1/recall/shadow-compare).
CERTIFIED_SERVING_PROFILES: Final[frozenset[str]] = frozenset({"legacy"})


@dataclass(frozen=True)
class RecallProfileSpec:
    """One recall profile's admission and budget policy.

    ``review_statuses`` is the pre-admission corpus window (SQL-level
    ``review_status IN (...)``); the profile's admission gate
    (``engram.recall_signals.decide_recall_admission``) makes the per-item
    admit/withhold decision after relevance retrieval, never before. The
    admission flags below are the gate's policy knobs — the gate dispatches on
    them, never on the profile key.
    """

    key: str
    description: str
    ranking_version: str
    review_statuses: tuple[str, ...]
    # Whether the separated signal model + admission gate apply (governed /
    # exploratory) or the legacy blended ranking is used unchanged (legacy).
    signals_enabled: bool
    # Admission policy: may proposed items be admitted (exploratory only)?
    admits_proposals: bool = False
    # Admission policy: does a stale durable assessment withhold (governed)
    # or just get marked (exploratory)? An explicit "blocked" outcome
    # withholds in every profile regardless of this flag.
    strict_stale: bool = True
    item_budget_cap: int | None = None
    byte_budget_cap: int | None = None
    token_budget_cap: int | None = None


LEGACY_PROFILE: Final = RecallProfileSpec(
    key="legacy",
    description=(
        "Compatibility profile: pre-#160 semantic behavior (active + proposed, "
        "blended trust ranking, relationship expansion). Opt-in only; the default "
        "until SDK/MCP consumers migrate."
    ),
    ranking_version="semantic-v3",
    review_statuses=("active", "proposed"),
    signals_enabled=False,
)

GOVERNED_PROFILE: Final = RecallProfileSpec(
    key="governed",
    description=(
        "Ordinary operational recall: admission-gated reviewed corpus; disputed "
        "items only for governed stay kinds; each served item bound to its "
        "admission decision."
    ),
    ranking_version=SIGNALS_RANKING_VERSION,
    # Disputed items enter the window so governed stay kinds survive; the
    # admission gate withholds disputed items whose kind is not a stay kind.
    review_statuses=("active", "disputed"),
    signals_enabled=True,
    admits_proposals=False,
    strict_stale=True,
)

EXPLORATORY_PROFILE: Final = RecallProfileSpec(
    key="exploratory",
    description=(
        "Explicit opt-in discovery recall: proposals and marked uncertainty are "
        "admitted with machine-readable epistemic state under tighter budgets."
    ),
    ranking_version=SIGNALS_RANKING_VERSION,
    review_statuses=("active", "proposed"),
    signals_enabled=True,
    admits_proposals=True,
    strict_stale=False,
    # Tighter than the default recall budgets (settings.recall_item_budget=50,
    # recall_byte_budget=4096): an exploratory packet must never crowd a
    # governed packet's budget for the same request.
    item_budget_cap=20,
    byte_budget_cap=2048,
)

SEMANTIC_PROFILES: Final[dict[str, RecallProfileSpec]] = {
    LEGACY_PROFILE.key: LEGACY_PROFILE,
    GOVERNED_PROFILE.key: GOVERNED_PROFILE,
    EXPLORATORY_PROFILE.key: EXPLORATORY_PROFILE,
}


def resolve_recall_profile(
    requested: str | None,
    *,
    mode: str,
    default: str = "legacy",
) -> RecallProfileSpec:
    """Resolve the effective semantic recall profile for a request.

    ``mode='semantic'``: ``requested=None`` falls back to ``default`` (the safe
    tenant default — ``legacy`` until governed is certified); otherwise the
    named profile must be a registered semantic profile.

    ``mode='startup'``: startup recall is its own profile. Only ``None`` and
    ``"startup"`` are accepted so a semantic profile can never be smuggled
    into the startup pipeline.

    Raises :class:`RecallProfileError` (a ``ValueError``) for unknown or
    mode-incompatible profiles; the API route maps that to HTTP 422.
    """
    if mode == "startup":
        if requested is None or requested == STARTUP_PROFILE_KEY:
            # Startup has no spec in SEMANTIC_PROFILES; the caller uses the
            # dedicated startup pipeline. Resolve to the sentinel key via a
            # minimal spec so the return type stays uniform.
            return _STARTUP_SPEC
        raise RecallProfileError(
            f"recall_profile={requested!r} requires mode='semantic' "
            f"(startup recall has no selectable profiles)"
        )

    if mode != "semantic":
        raise RecallProfileError(f"mode={mode!r} not supported (use 'startup' or 'semantic')")

    key = requested if requested is not None else default
    spec = SEMANTIC_PROFILES.get(key)
    if spec is None:
        valid = ", ".join(sorted(SEMANTIC_PROFILES))
        raise RecallProfileError(
            f"recall_profile={key!r} not supported for mode='semantic' (valid: {valid})"
        )
    return spec


# Startup recall keeps its own deterministic pipeline (pinned items, coarse
# score pools, anti-feedback) and is not profile-switchable; this spec exists
# only so callers can log/record the effective profile uniformly.
_STARTUP_SPEC: Final[RecallProfileSpec] = RecallProfileSpec(
    key=STARTUP_PROFILE_KEY,
    description="Deterministic startup working set (its own pipeline, not selectable).",
    ranking_version="v1",
    review_statuses=("active",),
    signals_enabled=False,
)


@dataclass(frozen=True)
class ServingProfileResolution:
    """The profile that may authoritatively serve, and how it was chosen.

    ``refused_default`` names an uncertified profile that
    ``settings.recall_default_profile`` asked to make authoritative: the
    request still serves the certified ``legacy`` packet, and callers surface
    the refusal (structured warning log) so a misconfiguration is visible
    rather than silently rewriting every packet's provenance.
    """

    spec: RecallProfileSpec
    refused_default: str | None = None


def resolve_serving_profile(
    requested: str | None,
    *,
    mode: str,
    default: str = "legacy",
) -> ServingProfileResolution:
    """Resolve the profile that may authoritatively serve a recall packet.

    The #162 certification boundary, enforced at the single choke point every
    serving path goes through:

    * an explicitly requested uncertified profile raises
      :class:`RecallProfileNotServableError` (fail closed — the caller asked
      for a profile that may not change their working memory, and a silent
      legacy substitute would misattribute the packet);
    * an uncertified ``default`` (e.g. a deployment setting
      ``recall_default_profile=governed``) is refused, not honored: serving
      falls back to the certified ``legacy`` profile and the refusal is
      reported via ``refused_default``. Configuration alone can never promote
      a candidate profile into production authority.
    * startup mode is unaffected (its pipeline has its own eligibility
      boundary and is not part of the semantic rollout).
    """
    spec = resolve_recall_profile(requested, mode=mode, default=default)
    if mode == "startup" or spec.key in CERTIFIED_SERVING_PROFILES:
        return ServingProfileResolution(spec)
    if requested is not None:
        raise RecallProfileNotServableError(
            f"recall_profile={spec.key!r} is not certified for authoritative serving "
            f"(accepted #162 certification required; see docs/adr-160-recall-profiles.md). "
            f"Valid serving profiles: {', '.join(sorted(CERTIFIED_SERVING_PROFILES))}. "
            f"Uncertified candidate profiles can be evaluated only via the "
            f"shadow comparison surface."
        )
    return ServingProfileResolution(LEGACY_PROFILE, refused_default=spec.key)


def apply_profile_budget_caps(
    spec: RecallProfileSpec,
    byte_budget: int | None,
    token_budget: int | None,
    item_budget: int | None,
) -> tuple[int | None, int | None, int | None]:
    """Clamp already-resolved budgets down to the profile's caps.

    Caps only ever lower a budget — a caller's tighter explicit budget always
    wins — and unset budgets stay unset so "no cap" remains expressible.
    """
    return (
        min(byte_budget, spec.byte_budget_cap)
        if byte_budget is not None and spec.byte_budget_cap is not None
        else byte_budget,
        min(token_budget, spec.token_budget_cap)
        if token_budget is not None and spec.token_budget_cap is not None
        else token_budget,
        min(item_budget, spec.item_budget_cap)
        if item_budget is not None and spec.item_budget_cap is not None
        else item_budget,
    )


__all__ = [
    "CERTIFIED_SERVING_PROFILES",
    "EXPLORATORY_PROFILE",
    "GOVERNED_PROFILE",
    "LEGACY_PROFILE",
    "RECALL_PROFILE_CONTRACT_VERSION",
    "SEMANTIC_PROFILES",
    "SIGNALS_RANKING_VERSION",
    "STARTUP_PROFILE_KEY",
    "RecallProfileError",
    "RecallProfileNotServableError",
    "RecallProfileSpec",
    "ServingProfileResolution",
    "apply_profile_budget_caps",
    "resolve_recall_profile",
    "resolve_serving_profile",
]
