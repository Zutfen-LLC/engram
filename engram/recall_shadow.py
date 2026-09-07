"""Authorized, read-only shadow comparison of candidate recall profiles.

The #160 rollout boundary (see ``docs/adr-160-recall-profiles.md``): until a
profile is certified (``recall_profiles.CERTIFIED_SERVING_PROFILES``),
``POST /v1/recall`` serves the legacy packet only, and governed/exploratory
packets are computed *exclusively* here. This surface exists so dogfood and
certification evaluation can compare "what legacy serves today" with "what a
candidate profile would serve" — without either changing what any caller is
actually served.

Non-authoritative by construction:

* the comparison writes nothing — no ``recall_logs`` row, no
  ``recall_count``/``last_recalled_at`` exposure-counter updates, no receipts,
  no jobs, no promotion/evidence inputs. An uncertified profile can therefore
  never affect recall telemetry as though its packet had been served;
* every packet is evaluated by the same read-only core
  (:func:`engram.recall.evaluate_semantic_profile`) authoritative serving
  uses, under the caller's real tenant/visibility/workspace boundary;
* the response always names the certified authoritative profile it compared
  against, so a result can never be mistaken for a served packet.

Authorization is enforced by the route (``REVIEW_SCOPE`` capability plus the
tenant's ``tenant_config.recall_profile_shadow_enabled`` policy) — capability
alone is not sufficient, and tenant denial wins even for a capable caller.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engram import recall as recall_module
from engram import recall_signals
from engram.memory_access import resolve_workspace_scope
from engram.memory_context import ResolvedMemoryContext
from engram.memory_kinds import get_disputed_stay_kind_names
from engram.models import TenantConfig
from engram.recall_profiles import (
    CERTIFIED_SERVING_PROFILES,
    LEGACY_PROFILE,
    RECALL_PROFILE_CONTRACT_VERSION,
    SEMANTIC_PROFILES,
    RecallProfileSpec,
)

# Version identity of the comparison surface itself (distinct from the signal
# model / admission policy versions carried per packet). v2 (issue #186):
# candidate packets are bound to the exact #158 V2 per-surface admission
# decisions and carry the bounded admission diagnostics + resolution summary.
SHADOW_COMPARISON_VERSION: Final = "recall-shadow-compare-v2"

# Candidate profiles that may be *evaluated* here. This is deliberately not
# derived from SEMANTIC_PROFILES: it must never contain a certified profile
# (comparing legacy against itself is noise) and adding one requires the same
# ADR-level decision as certification itself.
EVALUABLE_PROFILES: Final[tuple[str, ...]] = ("governed", "exploratory")


class RecallShadowProfileError(ValueError):
    """An invalid candidate-profile selection for shadow comparison."""


async def tenant_allows_candidate_profile_inspection(
    session: AsyncSession,
    tenant_id: str,
) -> bool:
    """The tenant's explicit allow decision for candidate-profile inspection.

    Reviewer capability (``REVIEW_SCOPE``) is necessary but never sufficient:
    the tenant must also opt in via ``tenant_config``. Absent config fails
    closed — no tenant policy, no inspection.
    """
    config = await session.scalar(
        select(TenantConfig).where(
            TenantConfig.tenant_id == tenant_id,
            TenantConfig.active.is_(True),
        )
    )
    return config is not None and bool(config.recall_profile_shadow_enabled)


def _packet_payload(evaluation: recall_module.SemanticPacketEvaluation) -> dict[str, Any]:
    """One evaluated packet, in the same item shape /v1/recall serves.

    V2-bound candidate packets additionally carry the bounded admission
    diagnostics (one content-free entry per withheld candidate) and the V2
    resolution summary — the operator-facing evidence of exactly which #158
    decisions admitted or withheld each candidate (issue #186). Since issue
    #190 each packet also carries the bounded relationship-expansion summary
    (contract version, seed/neighbor/admission counts), and since issue #192
    the bounded ``recall-packing-v1`` summary (selected count, preserved
    conflict pairs, omission counts by reason — counts only, no rejected
    content or counterpart identities). Legacy evaluates to an empty
    diagnostics list / ``None`` summaries.
    """
    profile = evaluation.profile
    return {
        "profile": profile.key,
        "scoring_version": profile.ranking_version,
        "signals_version": (
            recall_signals.SIGNALS_VERSION if profile.signals_enabled else None
        ),
        "item_count": evaluation.item_count,
        "byte_count": evaluation.byte_count,
        "candidate_count": evaluation.candidate_count,
        "omitted_by_admission": dict(sorted(evaluation.omitted_by_admission.items())),
        "admission_diagnostics": evaluation.admission_diagnostics,
        "v2_resolution": evaluation.v2_resolution,
        "expansion": evaluation.expansion,
        "packing": evaluation.packing,
        "effective_byte_budget": evaluation.byte_budget,
        "effective_token_budget": evaluation.token_budget,
        "effective_item_budget": evaluation.item_budget,
        "items": evaluation.items,
    }


async def evaluate_recall_shadow_comparison(
    session: AsyncSession,
    *,
    memory_context: ResolvedMemoryContext,
    workspace: str | None,
    query: str,
    candidate_profiles: list[str],
    byte_budget: int | None,
    token_budget: int | None,
    item_budget: int | None,
) -> dict[str, Any]:
    """Evaluate legacy versus each requested candidate profile, writing nothing.

    Raises :class:`RecallShadowProfileError` for an empty or non-candidate
    selection (the route maps it to HTTP 422). One query embedding is
    generated and shared by every evaluation, so packets are comparable.
    """
    if not candidate_profiles:
        raise RecallShadowProfileError("at least one candidate profile is required")
    unknown = [key for key in candidate_profiles if key not in EVALUABLE_PROFILES]
    if unknown:
        raise RecallShadowProfileError(
            f"not evaluatable on the shadow surface (valid: "
            f"{', '.join(EVALUABLE_PROFILES)}): {', '.join(unknown)}"
        )
    # Deduplicate, preserving request order.
    ordered: list[str] = []
    for key in candidate_profiles:
        if key not in ordered:
            ordered.append(key)

    now = datetime.now(UTC)
    tenant_id = str(memory_context.tenant_id)
    principal_id = str(memory_context.principal_id)

    byte_budget, token_budget, item_budget = recall_module._resolve_recall_budgets(
        byte_budget=byte_budget,
        token_budget=token_budget,
        item_budget=item_budget,
    )

    # An explicit workspace request that doesn't resolve, or where the caller
    # isn't a member, must not fall back to a broader corpus — the comparison
    # evaluates zero candidates instead, exactly like serving.
    workspace_id, workspace_accessible = await resolve_workspace_scope(
        session, memory_context=memory_context, workspace=workspace
    )

    from engram.embedding_profiles import get_active_profile

    embedding_profile = await get_active_profile(session)
    stay_kinds: set[str] = set()
    profiles: list[RecallProfileSpec] = [LEGACY_PROFILE] + [
        SEMANTIC_PROFILES[key] for key in ordered
    ]
    if any("disputed" in p.review_statuses for p in profiles):
        stay_kinds = await get_disputed_stay_kind_names(session, tenant_id)

    # Embedding preflight: run the comparison iff ANY requested packet — the
    # legacy baseline or a candidate — has an eligible corpus. The legacy
    # count alone must never gate the run: a tenant whose only eligible item
    # is a disputed governed stay kind has an empty legacy corpus (its window
    # is active + proposed) but a non-empty governed corpus, and legacy may
    # legitimately evaluate to an empty packet while a candidate is
    # non-empty. Each packet's own count travels in its payload.
    corpus_denied = not memory_context.may_read_anything or (
        workspace is not None and not workspace_accessible
    )
    eligible_total = 0
    if not corpus_denied:
        for eval_profile in profiles:
            eligible_total += await recall_module._profile_candidate_count(
                session,
                memory_context=memory_context,
                workspace_id=workspace_id,
                profile=eval_profile,
                stay_kinds=stay_kinds,
                embedding_profile=embedding_profile,
            )

    query_embedding = None
    embedding_outcome = "not_attempted"
    if eligible_total > 0:
        query_embedding = await recall_module.generate_query_embedding(
            query,
            embedding_profile=embedding_profile,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        embedding_outcome = "succeeded" if query_embedding is not None else "disabled"

    payload: dict[str, Any] = {
        "shadow_comparison_version": SHADOW_COMPARISON_VERSION,
        "recall_profile_contract_version": RECALL_PROFILE_CONTRACT_VERSION,
        "query": query,
        # The authoritative behavior this comparison evaluates against, and
        # the boundary statement: shadow results are never served packets.
        "authoritative_profile": LEGACY_PROFILE.key,
        "certified_serving_profiles": sorted(CERTIFIED_SERVING_PROFILES),
        "message": None,
        "workspace_id": str(workspace_id) if workspace_id else None,
        # Total eligible corpus across the compared profiles — nonzero
        # exactly when the comparison had anything to evaluate.
        "candidate_count": eligible_total,
        "embedding_outcome": embedding_outcome,
        "legacy": None,
        "candidates": [],
    }

    if query_embedding is None or eligible_total == 0:
        payload["message"] = recall_module._NO_EMBEDDINGS_MESSAGE
        return payload

    legacy_evaluation = await recall_module.evaluate_semantic_profile(
        session,
        memory_context=memory_context,
        workspace_id=workspace_id,
        profile=LEGACY_PROFILE,
        query_embedding=query_embedding,
        embedding_profile=embedding_profile,
        stay_kinds=stay_kinds,
        byte_budget=byte_budget,
        token_budget=token_budget,
        item_budget=item_budget,
        now=now,
    )
    payload["legacy"] = _packet_payload(legacy_evaluation)

    legacy_ids = {item["id"] for item in legacy_evaluation.items}
    for key in ordered:
        profile: RecallProfileSpec = SEMANTIC_PROFILES[key]
        evaluation = await recall_module.evaluate_semantic_profile(
            session,
            memory_context=memory_context,
            workspace_id=workspace_id,
            profile=profile,
            query_embedding=query_embedding,
            embedding_profile=embedding_profile,
            stay_kinds=stay_kinds,
            byte_budget=byte_budget,
            token_budget=token_budget,
            item_budget=item_budget,
            now=now,
        )
        candidate_ids = {item["id"] for item in evaluation.items}
        payload["candidates"].append(
            {
                **_packet_payload(evaluation),
                # Id-level overlap with the authoritative packet — the
                # comparison itself. Content of items withheld by the
                # candidate's admission gate is never retained.
                "comparison": {
                    "in_both": sorted(legacy_ids & candidate_ids),
                    "only_in_legacy": sorted(legacy_ids - candidate_ids),
                    "only_in_candidate": sorted(candidate_ids - legacy_ids),
                },
            }
        )

    return payload


__all__ = [
    "EVALUABLE_PROFILES",
    "SHADOW_COMPARISON_VERSION",
    "RecallShadowProfileError",
    "evaluate_recall_shadow_comparison",
    "tenant_allows_candidate_profile_inspection",
]
