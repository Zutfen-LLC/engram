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
    """The admitted-set loader maps SQL aggregate diagnostics without identities."""
    tenant_id, item_id, author_id = uuid4(), uuid4(), uuid4()
    item = MemoryItem(id=item_id, tenant_id=tenant_id, principal_id=author_id)
    rows = [
        _aggregate_row(
            item_id,
            qualifying_useful_count=1,
            qualifying_noise_count=1,
            excluded_self_or_author_count=1,
            excluded_unbound_exposure_count=2,
        )
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
    assert "GROUP BY memory_items.id" in statement
    assert "FILTER" in statement
    assert summary.state == "mixed"
    assert summary.adjustment == 0.00
    assert summary.qualifying_useful_actor_count == 1
    assert summary.qualifying_noise_actor_count == 1
    assert summary.excluded_self_or_author_count == 1
    assert summary.excluded_unbound_exposure_count == 2


@pytest.mark.asyncio
async def test_bulk_loader_result_cardinality_is_bounded_by_admitted_items() -> None:
    """Many actors produce one SQL aggregate row for each admitted item."""
    tenant_id = uuid4()
    items = [
        MemoryItem(id=uuid4(), tenant_id=tenant_id, principal_id=uuid4()),
        MemoryItem(id=uuid4(), tenant_id=tenant_id, principal_id=uuid4()),
    ]
    rows = [
        _aggregate_row(items[0].id, qualifying_useful_count=10_000),
        _aggregate_row(items[1].id, qualifying_noise_count=7_000),
    ]
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    summaries = await load_demonstrated_usefulness(
        cast(AsyncSession, session), tenant_id=tenant_id, items=items
    )

    assert len(rows) <= len(items)
    assert len(summaries) == len(items)
    assert summaries[items[0].id].adjustment == 0.10
    assert summaries[items[1].id].adjustment == -0.10
    statement = str(session.execute.await_args.args[0])
    assert "GROUP BY memory_items.id" in statement


def _aggregate_row(
    item_id: UUID,
    *,
    qualifying_useful_count: int = 0,
    qualifying_noise_count: int = 0,
    excluded_self_or_author_count: int = 0,
    excluded_unbound_exposure_count: int = 0,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "qualifying_useful_count": qualifying_useful_count,
        "qualifying_noise_count": qualifying_noise_count,
        "excluded_self_or_author_count": excluded_self_or_author_count,
        "excluded_unbound_exposure_count": excluded_unbound_exposure_count,
    }
