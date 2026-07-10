from uuid import uuid4

import pytest

from engram.review_policy import (
    TransitionOutcome,
    TrustedReviewOperation,
    can_human_verify,
    evaluate_transition,
)


def _decision(principal_type: str, old: str, new: str, *, author: bool = False):
    principal = uuid4()
    return evaluate_transition(
        principal_id=principal,
        principal_type=principal_type,
        item_author_principal_id=principal if author else uuid4(),
        current_status=old,
        requested_status=new,
    )


@pytest.mark.parametrize("principal_type", ["agent", "system"])
@pytest.mark.parametrize(
    ("old", "new"),
    [("proposed", "active"), ("disputed", "active"), ("active", "rejected")],
)
def test_agents_cannot_make_privileged_decisions(
    principal_type: str, old: str, new: str
) -> None:
    assert _decision(principal_type, old, new, author=True).outcome is TransitionOutcome.FORBIDDEN


def test_agents_may_dispute_and_only_author_may_withdraw_proposal() -> None:
    assert _decision("agent", "active", "disputed").allowed
    assert _decision("agent", "proposed", "archived", author=True).outcome is (
        TransitionOutcome.SELF_WITHDRAWAL
    )
    assert not _decision("agent", "proposed", "archived").allowed


@pytest.mark.parametrize("principal_type", ["user", "admin"])
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("proposed", "active"),
        ("disputed", "active"),
        ("proposed", "rejected"),
        ("active", "disputed"),
        ("active", "archived"),
        ("rejected", "active"),
    ],
)
def test_human_reviewers_may_make_governed_transitions(
    principal_type: str, old: str, new: str
) -> None:
    assert _decision(principal_type, old, new).allowed


def test_only_admin_can_restore_archived_item() -> None:
    assert _decision("user", "archived", "active").outcome is TransitionOutcome.FORBIDDEN
    assert _decision("admin", "archived", "active").allowed


def test_promotion_requires_explicit_trusted_operation() -> None:
    principal = uuid4()
    assert not _decision("system", "proposed", "active", author=True).allowed
    assert evaluate_transition(
        principal_id=principal,
        principal_type="system",
        item_author_principal_id=principal,
        current_status="proposed",
        requested_status="active",
        trusted_operation=TrustedReviewOperation.PROMOTION,
    ).outcome is TransitionOutcome.TRUSTED


@pytest.mark.parametrize(
    ("principal_type", "allowed"),
    [("agent", False), ("system", False), ("user", True), ("admin", True)],
)
def test_human_verification_authority(principal_type: str, allowed: bool) -> None:
    assert can_human_verify(principal_type) is allowed
