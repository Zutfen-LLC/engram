"""Contract tests for candidate demonstrated usefulness (issue #196)."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engram.demonstrated_usefulness import (
    DEMONSTRATED_USEFULNESS_VERSION,
    FeedbackVerdict,
    QualifyingFeedback,
    load_demonstrated_usefulness,
    summarize_demonstrated_usefulness,
)
from engram.models import MemoryItem


@pytest.mark.parametrize(
    ("verdicts", "state", "adjustment"),
    [
        ([], "none", 0.00),
        (["useful"], "positive", 0.10),
        (["noise"], "negative", -0.10),
        (["useful", "noise"], "mixed", 0.00),
        (["useful"] * 10, "positive", 0.10),
    ],
)
def test_demonstrated_usefulness_v1_is_non_amplifying(
    verdicts: list[str], state: str, adjustment: float
) -> None:
    """Current qualifying verdicts map to the fixed v1 state table."""
    summary = summarize_demonstrated_usefulness(
        [QualifyingFeedback(verdict=cast(FeedbackVerdict, verdict)) for verdict in verdicts]
    )

    assert summary.version == DEMONSTRATED_USEFULNESS_VERSION
    assert summary.state == state
    assert summary.adjustment == adjustment
    assert summary.qualifying_useful_actor_count == verdicts.count("useful")
    assert summary.qualifying_noise_actor_count == verdicts.count("noise")


@pytest.mark.asyncio
async def test_bulk_loader_qualifies_current_bound_external_feedback_once() -> None:
    """The admitted-set loader fails closed on self and invalid exposure bindings."""
    tenant_id, item_id, author_id, external_id = uuid4(), uuid4(), uuid4(), uuid4()
    noise_actor_id = uuid4()
    item = MemoryItem(id=item_id, tenant_id=tenant_id, principal_id=author_id)
    rows = [
        _feedback_row(item_id, "useful", external_id, tenant_id, external_id, [item_id]),
        _feedback_row(item_id, "noise", noise_actor_id, tenant_id, noise_actor_id, [item_id]),
        _feedback_row(item_id, "useful", author_id, tenant_id, author_id, [item_id]),
        _feedback_row(item_id, "useful", uuid4(), None, None, None),
        _feedback_row(item_id, "noise", uuid4(), tenant_id, uuid4(), [uuid4()]),
    ]
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    summaries = await load_demonstrated_usefulness(
        cast(AsyncSession, session), tenant_id=tenant_id, items=[item]
    )

    summary = summaries[item_id]
    assert session.execute.await_count == 1
    statement = str(session.execute.await_args.args[0])
    assert "feedback_events.tenant_id" in statement
    assert "feedback_events.superseded_at IS NULL" in statement
    assert summary.state == "mixed"
    assert summary.adjustment == 0.00
    assert summary.qualifying_useful_actor_count == 1
    assert summary.qualifying_noise_actor_count == 1
    assert summary.excluded_self_or_author_count == 1
    assert summary.excluded_unbound_exposure_count == 2


def _feedback_row(
    item_id: UUID,
    verdict: str,
    actor_id: UUID,
    log_tenant_id: UUID | None,
    log_principal_id: UUID | None,
    log_item_ids: list[UUID] | None,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "verdict": verdict,
        "actor_id": actor_id,
        "recall_log_tenant_id": log_tenant_id,
        "recall_log_principal_id": log_principal_id,
        "recall_log_item_ids": log_item_ids,
    }
