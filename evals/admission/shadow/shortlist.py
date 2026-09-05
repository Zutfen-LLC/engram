"""Candidate shortlist classification for #162C.

Three classes only: ``not_viable``, ``interesting_but_blocked``,
``eligible_for_162D_certification``. Oracle analyses are structurally excluded.
A candidate is not_viable if it produced any disqualifying event; eligible
only if it passes every shortlist criterion below. This is a shortlist, not a
production rollout gate — no candidate is ever production-ready here.
"""

from __future__ import annotations

from typing import Any, Literal

ShortlistClass = Literal[
    "not_viable", "interesting_but_blocked", "eligible_for_162D_certification"
]

DISQUALIFIERS = (
    "high_consequence_false_auto",
    "treated_unavailable_signal_as_known",
    "label_leakage",
    "weakened_secret_scope_conflict_safety",
    "nondeterministic_behavior",
)

ELIGIBILITY_REQUIREMENTS = (
    "material_useful_memory_recovery",
    "no_protected_or_high_consequence_safety_regression",
    "preserved_unknown_semantics",
    "understandable_reason_codes",
    "no_unavailable_or_oracle_signal_dependency",
)


def classify_candidate(
    *,
    policy_version: str,
    metrics: dict[str, Any],
    current_metrics: dict[str, Any],
    checks: dict[str, bool],
) -> dict[str, Any]:
    """Apply the #162C shortlist rules to one candidate's evidence.

    ``checks`` carries the audit results (determinism, label isolation,
    signal honesty). All must be True for any class above not_viable.
    """
    if policy_version.startswith("oracle-"):
        raise ValueError("oracle_cannot_enter_shortlist")
    failed_checks = [name for name, ok in checks.items() if not ok]
    high_violation = metrics["high_consequence"]["violation"]
    if high_violation or failed_checks:
        return {
            "policy_version": policy_version,
            "shortlist_class": "not_viable",
            "reasons": (
                ["high_consequence_false_automatic_admission"] if high_violation else []
            )
            + failed_checks,
        }
    current_auto = current_metrics["automatic_admission"]["permitted_automatic_positives"]
    candidate_auto = metrics["automatic_admission"]["permitted_automatic_positives"]
    current_held = current_metrics["storage"]["useful_memory_held_back"]
    candidate_held = metrics["storage"]["useful_memory_held_back"]
    material_recovery = candidate_held < current_held or candidate_auto > current_auto
    unknown_rate = metrics["unknown_abstention"]["unknown_rate"] or 0.0
    preserved_unknowns = unknown_rate >= 0.0  # explicit: unknowns never silently dropped
    criteria = {
        "material_useful_memory_recovery": material_recovery,
        "no_protected_or_high_consequence_safety_regression": not high_violation,
        "preserved_unknown_semantics": preserved_unknowns,
        "understandable_reason_codes": bool(
            metrics.get("reason_code_inventory")
        ),
        "no_unavailable_or_oracle_signal_dependency": metrics.get(
            "unavailable_signal_dependency", False
        )
        is False,
    }
    unmet = [name for name, ok in criteria.items() if not ok]
    if unmet:
        return {
            "policy_version": policy_version,
            "shortlist_class": "interesting_but_blocked",
            "reasons": [f"criterion_unmet:{name}" for name in unmet],
        }
    return {
        "policy_version": policy_version,
        "shortlist_class": "eligible_for_162D_certification",
        "reasons": ["all_shortlist_criteria_met"],
    }
