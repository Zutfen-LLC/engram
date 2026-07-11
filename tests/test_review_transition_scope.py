"""Unit tests for `required_scope_for_review_transition` (V2-BL-004).

Pure function, no database — covers every row of the mixed
`POST /v1/items/{item_id}/review` endpoint's scope-classification matrix from
the ticket. `evaluate_transition` (principal-type policy) is untouched by
this slice and has its own coverage in test_review_policy-style tests; these
tests only check the *scope* dimension.
"""

from __future__ import annotations

import pytest

from engram.review_policy import required_scope_for_review_transition


def test_noop_requires_no_additional_scope():
    for status in ("proposed", "active", "disputed", "rejected", "archived"):
        assert (
            required_scope_for_review_transition(
                current_status=status, requested_status=status, is_author=False
            )
            is None
        )
        assert (
            required_scope_for_review_transition(
                current_status=status, requested_status=status, is_author=True
            )
            is None
        )


@pytest.mark.parametrize(
    ("current_status", "requested_status"),
    [
        ("proposed", "disputed"),
        ("active", "disputed"),
    ],
)
def test_write_permitted_dispute_transitions(current_status, requested_status):
    assert (
        required_scope_for_review_transition(
            current_status=current_status,
            requested_status=requested_status,
            is_author=False,
        )
        == "write"
    )


def test_self_withdrawal_by_author_requires_write():
    assert (
        required_scope_for_review_transition(
            current_status="proposed", requested_status="archived", is_author=True
        )
        == "write"
    )


def test_archival_of_non_authored_proposal_requires_review():
    assert (
        required_scope_for_review_transition(
            current_status="proposed", requested_status="archived", is_author=False
        )
        == "review"
    )


@pytest.mark.parametrize(
    ("current_status", "requested_status"),
    [
        ("proposed", "active"),
        ("disputed", "active"),
        ("rejected", "active"),
        ("archived", "active"),
        ("proposed", "rejected"),
        ("active", "rejected"),
        ("disputed", "rejected"),
        ("active", "archived"),
        ("disputed", "archived"),
    ],
)
def test_privileged_transitions_require_review(current_status, requested_status):
    assert (
        required_scope_for_review_transition(
            current_status=current_status,
            requested_status=requested_status,
            is_author=False,
        )
        == "review"
    )
    # Authorship never downgrades a privileged transition below `review` —
    # only the specific proposed->archived self-withdrawal case is write-eligible.
    assert (
        required_scope_for_review_transition(
            current_status=current_status,
            requested_status=requested_status,
            is_author=True,
        )
        == "review"
    )


def test_unrecognized_transition_defaults_to_review():
    assert (
        required_scope_for_review_transition(
            current_status="bogus", requested_status="whatever", is_author=False
        )
        == "review"
    )
