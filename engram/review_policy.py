"""Pure authorization policy for memory review-state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID

from engram.auth import Scope

ReviewStatus = Literal["proposed", "active", "disputed", "rejected", "archived"]


class TrustedReviewOperation(StrEnum):
    """Server-selected review authorities that callers cannot request."""

    PROMOTION = "promotion_service"
    CONFLICT_RESOLUTION = "conflict_resolution_service"


class TransitionOutcome(StrEnum):
    ALLOWED = "allowed"
    SELF_WITHDRAWAL = "self_withdrawal"
    TRUSTED = "trusted_internal"
    NOOP = "noop"
    INVALID = "invalid"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class ReviewTransitionDecision:
    outcome: TransitionOutcome

    @property
    def allowed(self) -> bool:
        return self.outcome in {
            TransitionOutcome.ALLOWED,
            TransitionOutcome.SELF_WITHDRAWAL,
            TransitionOutcome.TRUSTED,
            TransitionOutcome.NOOP,
        }


def can_human_verify(principal_type: str) -> bool:
    """Whether this authenticated principal can personally verify a memory."""
    return principal_type in {"user", "admin"}


def can_resolve_conflict(principal_type: str) -> bool:
    """Whether this authenticated principal may adjudicate a conflict.

    API scope only admits an attempted operation.  Conflict decisions remain
    human-governed, so credentialed agents and ordinary system principals are
    not authorized even when their key carries ``review`` or ``admin`` scope.
    """
    return principal_type in {"user", "admin"}


_STATUSES: frozenset[str] = frozenset(
    {"proposed", "active", "disputed", "rejected", "archived"}
)
_STRUCTURAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
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


def evaluate_transition(
    *,
    principal_id: UUID,
    principal_type: str,
    item_author_principal_id: UUID,
    current_status: str,
    requested_status: str,
    trusted_operation: TrustedReviewOperation | None = None,
) -> ReviewTransitionDecision:
    """Evaluate one transition without database or HTTP concerns."""
    if current_status not in _STATUSES or requested_status not in _STATUSES:
        return ReviewTransitionDecision(TransitionOutcome.INVALID)
    if current_status == requested_status:
        return ReviewTransitionDecision(TransitionOutcome.NOOP)
    if (current_status, requested_status) not in _STRUCTURAL_TRANSITIONS:
        return ReviewTransitionDecision(TransitionOutcome.INVALID)

    if trusted_operation is not None:
        allowed = (
            trusted_operation is TrustedReviewOperation.PROMOTION
            and current_status == "proposed"
            and requested_status == "active"
        ) or trusted_operation is TrustedReviewOperation.CONFLICT_RESOLUTION
        return ReviewTransitionDecision(
            TransitionOutcome.TRUSTED if allowed else TransitionOutcome.INVALID
        )

    if principal_type in {"user", "admin"}:
        if (
            current_status == "archived"
            and requested_status == "active"
            and principal_type != "admin"
        ):
            return ReviewTransitionDecision(TransitionOutcome.FORBIDDEN)
        return ReviewTransitionDecision(TransitionOutcome.ALLOWED)

    if requested_status == "disputed" and current_status in {"proposed", "active"}:
        return ReviewTransitionDecision(TransitionOutcome.ALLOWED)
    if (
        current_status == "proposed"
        and requested_status == "archived"
        and principal_id == item_author_principal_id
    ):
        return ReviewTransitionDecision(TransitionOutcome.SELF_WITHDRAWAL)
    return ReviewTransitionDecision(TransitionOutcome.FORBIDDEN)


# --- Transition scope classification (V2-BL-004) -----------------------------
#
# `POST /v1/items/{item_id}/review` is a mixed-purpose endpoint: collaborative
# actions (dispute, self-withdrawal) are safe for `write`-scoped agents, but
# activating/reactivating/rejecting an item is a privileged review decision
# that must additionally require the `review` scope, even for a human user
# whose principal-type would otherwise be allowed to perform it (scope and
# principal-type are orthogonal — see the module docstring / V2-BL-004).
#
# This classification is independent of `principal_type`/`evaluate_transition`:
# it answers "may this credential *attempt* this transition at all," not
# "may this specific principal perform it." Both checks must pass.

_WRITE_PERMITTED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("proposed", "disputed"),
        ("active", "disputed"),
    }
)


def required_scope_for_review_transition(
    *,
    current_status: str,
    requested_status: str,
    is_author: bool,
) -> Scope | None:
    """The scope a caller must hold to attempt this review-status transition.

    Returns ``None`` for a same-state (no-op) request — the endpoint's base
    admission (``write`` or ``review``) already suffices for that case.
    Returns ``"write"`` for collaborative actions (dispute, and self-withdrawal
    via ``proposed -> archived`` by the item's own author). Returns
    ``"review"`` for every other transition, including archival of another
    principal's proposal and any structurally invalid or unrecognized pair —
    the conservative default requires the higher scope rather than silently
    permitting an unclassified transition under `write` alone.
    """
    if current_status == requested_status:
        return None
    if (current_status, requested_status) in _WRITE_PERMITTED_TRANSITIONS:
        return "write"
    if current_status == "proposed" and requested_status == "archived" and is_author:
        return "write"
    return "review"
