"""Exhaustive, table-driven coverage of the pure review-transition policy.

``engram.review_policy.evaluate_transition`` is the single source of truth
for who may move a memory item between review states. This module certifies
the *complete* state machine rather than scattered examples:

* every structurally allowed transition, for every principal type, including
  the agent-author-vs-non-author split on ``proposed -> archived``;
* administrator-only ``archived -> active`` restoration;
* every same-state no-op, including under a trusted operation;
* every structurally invalid transition pair, for every principal type;
* invalid status values on either side;
* trusted promotion allowed for ``proposed -> active`` only, denied (as
  ``INVALID``) for every other pair;
* the ``conflict_resolution_service`` trusted authority, which (unlike
  promotion) accepts any structurally valid pair;
* human-verification authority per principal type.

A leading drift guard (``test_module_transition_set_matches_expected_matrix``)
asserts the module's private ``_STATUSES``/``_STRUCTURAL_TRANSITIONS`` still
equal the sets transcribed here from the V2-BL-003 policy table. If a future
change adds/removes a status or a structural transition without updating this
file, that guard fails first and loudly — every other test in this module
derives its expected pairs from the transcribed sets, not the module's, so
they would silently stop exercising the new state without it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from engram.review_policy import (
    _STATUSES,
    _STRUCTURAL_TRANSITIONS,
    ReviewTransitionDecision,
    TransitionOutcome,
    TrustedReviewOperation,
    can_human_verify,
    evaluate_transition,
)

# ---------------------------------------------------------------------------
# Transcribed authoritative matrix (V2-BL-003 review-policy table)
# ---------------------------------------------------------------------------

EXPECTED_STATUSES: frozenset[str] = frozenset(
    {"proposed", "active", "disputed", "rejected", "archived"}
)

EXPECTED_STRUCTURAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("proposed", "active"),
        ("proposed", "disputed"),
        ("proposed", "rejected"),
        ("proposed", "archived"),
        ("active", "disputed"),
        ("active", "rejected"),
        ("active", "archived"),
        ("disputed", "active"),
        ("disputed", "rejected"),
        ("disputed", "archived"),
        ("rejected", "active"),
        ("archived", "active"),
    }
)

ALL_ORDERED_PAIRS: frozenset[tuple[str, str]] = frozenset(
    (old, new) for old in EXPECTED_STATUSES for new in EXPECTED_STATUSES
)
SAME_STATE_PAIRS: frozenset[tuple[str, str]] = frozenset(
    (s, s) for s in EXPECTED_STATUSES
)
STRUCTURALLY_INVALID_PAIRS: frozenset[tuple[str, str]] = (
    ALL_ORDERED_PAIRS - SAME_STATE_PAIRS - EXPECTED_STRUCTURAL_TRANSITIONS
)

HUMAN_TYPES = ("user", "admin")
NON_HUMAN_TYPES = ("agent", "system")
ALL_PRINCIPAL_TYPES = (*HUMAN_TYPES, *NON_HUMAN_TYPES)


def test_module_transition_set_matches_expected_matrix() -> None:
    """Drift guard: fail loudly if the policy's state space changes."""
    assert _STATUSES == EXPECTED_STATUSES
    assert _STRUCTURAL_TRANSITIONS == EXPECTED_STRUCTURAL_TRANSITIONS
    # Sanity on the derived invalid-pair count so the matrix below stays honest.
    assert len(STRUCTURALLY_INVALID_PAIRS) == 8


def _decision(
    principal_type: str,
    old: str,
    new: str,
    *,
    author: bool = False,
    trusted_operation: TrustedReviewOperation | None = None,
) -> ReviewTransitionDecision:
    principal = uuid4()
    return evaluate_transition(
        principal_id=principal,
        principal_type=principal_type,
        item_author_principal_id=principal if author else uuid4(),
        current_status=old,
        requested_status=new,
        trusted_operation=trusted_operation,
    )


# ---------------------------------------------------------------------------
# A. Human reviewers (user, admin): every structurally allowed transition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("principal_type", HUMAN_TYPES)
@pytest.mark.parametrize(("old", "new"), sorted(EXPECTED_STRUCTURAL_TRANSITIONS))
def test_human_reviewers_allowed_transitions(
    principal_type: str, old: str, new: str
) -> None:
    """Every structural transition is allowed for user/admin, except
    archived -> active, which is administrator-only."""
    decision = _decision(principal_type, old, new)
    if (old, new) == ("archived", "active") and principal_type != "admin":
        assert decision.outcome is TransitionOutcome.FORBIDDEN
        assert not decision.allowed
    else:
        assert decision.allowed
        assert decision.outcome is TransitionOutcome.ALLOWED


def test_only_admin_can_restore_archived_item() -> None:
    assert _decision("user", "archived", "active").outcome is TransitionOutcome.FORBIDDEN
    assert _decision("admin", "archived", "active").allowed
    assert _decision("admin", "archived", "active").outcome is TransitionOutcome.ALLOWED


# ---------------------------------------------------------------------------
# B. Agents / system principals (non-trusted): dispute + self-withdrawal only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("principal_type", NON_HUMAN_TYPES)
@pytest.mark.parametrize(("old", "new"), sorted(EXPECTED_STRUCTURAL_TRANSITIONS))
@pytest.mark.parametrize("author", [True, False])
def test_agent_and_system_structural_transitions(
    principal_type: str, old: str, new: str, author: bool
) -> None:
    decision = _decision(principal_type, old, new, author=author)
    if new == "disputed" and old in {"proposed", "active"}:
        assert decision.allowed
        assert decision.outcome is TransitionOutcome.ALLOWED
    elif old == "proposed" and new == "archived" and author:
        assert decision.allowed
        assert decision.outcome is TransitionOutcome.SELF_WITHDRAWAL
    else:
        assert not decision.allowed
        assert decision.outcome is TransitionOutcome.FORBIDDEN


def test_agent_cannot_archive_non_author_proposal() -> None:
    """proposed -> archived is self-withdrawal only; a non-author agent is denied."""
    decision = _decision("agent", "proposed", "archived", author=False)
    assert decision.outcome is TransitionOutcome.FORBIDDEN
    assert not decision.allowed


def test_agent_author_may_withdraw_own_proposal() -> None:
    decision = _decision("agent", "proposed", "archived", author=True)
    assert decision.outcome is TransitionOutcome.SELF_WITHDRAWAL
    assert decision.allowed


@pytest.mark.parametrize("principal_type", NON_HUMAN_TYPES)
@pytest.mark.parametrize(("old", "new"), [("proposed", "active"), ("disputed", "active")])
def test_agents_and_system_cannot_make_privileged_decisions(
    principal_type: str, old: str, new: str
) -> None:
    assert _decision(principal_type, old, new, author=True).outcome is (
        TransitionOutcome.FORBIDDEN
    )


# ---------------------------------------------------------------------------
# C. Same-state requests are idempotent no-ops, for every status/principal,
#    even under a trusted operation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(EXPECTED_STATUSES))
@pytest.mark.parametrize("principal_type", ALL_PRINCIPAL_TYPES)
@pytest.mark.parametrize(
    "trusted_operation",
    [None, TrustedReviewOperation.PROMOTION, TrustedReviewOperation.CONFLICT_RESOLUTION],
)
def test_same_state_is_always_noop(
    status: str,
    principal_type: str,
    trusted_operation: TrustedReviewOperation | None,
) -> None:
    decision = _decision(
        principal_type, status, status, trusted_operation=trusted_operation
    )
    assert decision.outcome is TransitionOutcome.NOOP
    assert decision.allowed


# ---------------------------------------------------------------------------
# D. Invalid status values (either side) are always INVALID, regardless of
#    principal type or trusted operation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("bogus", "active"),
        ("proposed", "bogus"),
        ("", "active"),
        ("proposed", ""),
        ("ACTIVE", "proposed"),  # case-sensitive: not a recognized status
    ],
)
@pytest.mark.parametrize("principal_type", ALL_PRINCIPAL_TYPES)
def test_invalid_status_values_are_invalid(
    old: str, new: str, principal_type: str
) -> None:
    decision = _decision(principal_type, old, new)
    assert decision.outcome is TransitionOutcome.INVALID
    assert not decision.allowed


# ---------------------------------------------------------------------------
# E. Structurally invalid (but individually valid-status) pairs are always
#    INVALID — before principal-type authorization is even considered.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("old", "new"), sorted(STRUCTURALLY_INVALID_PAIRS))
@pytest.mark.parametrize("principal_type", ALL_PRINCIPAL_TYPES)
def test_structurally_invalid_pairs_are_invalid(
    old: str, new: str, principal_type: str
) -> None:
    decision = _decision(principal_type, old, new)
    assert decision.outcome is TransitionOutcome.INVALID
    assert not decision.allowed


@pytest.mark.parametrize(("old", "new"), sorted(STRUCTURALLY_INVALID_PAIRS))
def test_structurally_invalid_pairs_invalid_even_for_trusted_promotion(
    old: str, new: str
) -> None:
    decision = _decision(
        "system", old, new, trusted_operation=TrustedReviewOperation.PROMOTION
    )
    assert decision.outcome is TransitionOutcome.INVALID


# ---------------------------------------------------------------------------
# F. Trusted promotion: allowed for proposed -> active only.
# ---------------------------------------------------------------------------


def test_promotion_requires_explicit_trusted_operation() -> None:
    """Without trusted_operation, 'system' gets the same non-trusted denial
    as any other non-human principal — promotion authority is never ambient."""
    assert not _decision("system", "proposed", "active", author=True).allowed
    assert _decision("system", "proposed", "active", author=True).outcome is (
        TransitionOutcome.FORBIDDEN
    )


def test_trusted_promotion_allowed_for_proposed_to_active() -> None:
    decision = _decision(
        "system", "proposed", "active", trusted_operation=TrustedReviewOperation.PROMOTION
    )
    assert decision.outcome is TransitionOutcome.TRUSTED
    assert decision.allowed


_NON_PROMOTION_PAIRS = sorted(
    (EXPECTED_STRUCTURAL_TRANSITIONS | STRUCTURALLY_INVALID_PAIRS) - {("proposed", "active")}
)


@pytest.mark.parametrize(("old", "new"), _NON_PROMOTION_PAIRS)
def test_trusted_promotion_rejected_for_every_other_pair(old: str, new: str) -> None:
    decision = _decision(
        "system", old, new, trusted_operation=TrustedReviewOperation.PROMOTION
    )
    assert not decision.allowed
    assert decision.outcome is TransitionOutcome.INVALID


def test_trusted_promotion_ignores_principal_type_and_authorship() -> None:
    """Trusted promotion is a server-selected authority, not principal-derived
    — the outcome for (proposed, active) is TRUSTED regardless of which
    principal_type/author combination happens to be passed in."""
    for principal_type in ALL_PRINCIPAL_TYPES:
        for author in (True, False):
            decision = _decision(
                principal_type,
                "proposed",
                "active",
                author=author,
                trusted_operation=TrustedReviewOperation.PROMOTION,
            )
            assert decision.outcome is TransitionOutcome.TRUSTED


# ---------------------------------------------------------------------------
# G. Trusted conflict-resolution authority: any structurally valid pair.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("old", "new"), sorted(EXPECTED_STRUCTURAL_TRANSITIONS))
def test_trusted_conflict_resolution_allows_any_structural_pair(
    old: str, new: str
) -> None:
    decision = _decision(
        "system", old, new, trusted_operation=TrustedReviewOperation.CONFLICT_RESOLUTION
    )
    assert decision.outcome is TransitionOutcome.TRUSTED
    assert decision.allowed


@pytest.mark.parametrize(("old", "new"), sorted(STRUCTURALLY_INVALID_PAIRS))
def test_trusted_conflict_resolution_still_invalid_for_bad_pairs(
    old: str, new: str
) -> None:
    decision = _decision(
        "system", old, new, trusted_operation=TrustedReviewOperation.CONFLICT_RESOLUTION
    )
    assert decision.outcome is TransitionOutcome.INVALID


# ---------------------------------------------------------------------------
# H. Human verification authority per principal type.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("principal_type", "allowed"),
    [("agent", False), ("system", False), ("user", True), ("admin", True)],
)
def test_human_verification_authority(principal_type: str, allowed: bool) -> None:
    assert can_human_verify(principal_type) is allowed
