"""Pure authorization policy for memory review-state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID

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
