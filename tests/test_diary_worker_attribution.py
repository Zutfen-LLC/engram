"""Focused regression proofs for diary attribution integrity.

The broader authenticated PostgreSQL matrices live in the existing diary,
worker, trusted-actor, auth, and scope suites. These small resolver tests keep
the legacy/modern boundary executable even when PostgreSQL is unavailable.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from engram.api.routes.diary import (
    DiaryAttributionIntegrityError,
    resolve_diary_attribution,
)
from engram.authority import authority_label
from engram.models import ItemEvent, MemoryItem


def _item() -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        tenant_id=uuid4(),
        principal_id=uuid4(),
        content="historical diary line",
        content_hash="hash",
        kind="diary_entry",
        subject_name="topic",
        visibility="private",
        review_status="active",
        memory_confidence=0.9,
        source_trust=0.9,
        importance=0.4,
        source_type="manual",
        authority=50,
        sensitivity="normal",
    )


def _session_with(events: list[ItemEvent]) -> AsyncMock:
    scalars = SimpleNamespace(all=lambda: events)
    result = SimpleNamespace(scalars=lambda: scalars)
    session = AsyncMock()
    session.execute.return_value = result
    return session


def _event(item: MemoryItem, actor: UUID, **overrides: object) -> ItemEvent:
    details: dict[str, object] = {
        "owner_principal_id": str(item.principal_id),
        "actor_principal_id": str(actor),
        "represented": False,
        "on_behalf_of_principal_id": None,
        "source_type": item.source_type,
        "source_trust": item.source_trust,
        "memory_confidence": item.memory_confidence,
        "authority": item.authority,
        "authority_label": authority_label(item.authority),
        "review_status": item.review_status,
        "topic": item.subject_name,
    }
    details.update(overrides)
    return ItemEvent(
        id=uuid4(),
        item_id=item.id,
        event_type="diary_create",
        actor_principal_id=actor,
        new_value=json.dumps(details),
    )


@pytest.mark.asyncio
async def test_zero_creation_events_is_truthful_legacy_unknown() -> None:
    item = _item()
    session = _session_with([])

    attribution = await resolve_diary_attribution(session, item)

    assert attribution.attribution_status == "legacy_unknown"
    assert attribution.actor_principal_id is None
    assert attribution.represented is None
    session.add.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_one_valid_creation_event_is_recorded() -> None:
    item = _item()
    actor = uuid4()
    session = _session_with([_event(item, actor)])
    attribution = await resolve_diary_attribution(session, item)

    assert attribution.attribution_status == "recorded"
    assert attribution.actor_principal_id == actor
    assert attribution.represented is False
    session.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"owner_principal_id": str(uuid4())}, "owner"),
        ({"actor_principal_id": str(uuid4())}, "actor"),
        ({"represented": "false"}, "represented"),
        ({"represented": True, "on_behalf_of_principal_id": None}, "target"),
        ({"authority": 10}, "authority"),
        ({"authority_label": "inferred"}, "authority label"),
    ],
)
async def test_malformed_modern_attribution_fails_closed(
    mutation: dict[str, object], expected: str
) -> None:
    item = _item()
    actor = uuid4()
    session = _session_with([_event(item, actor, **mutation)])

    with pytest.raises(DiaryAttributionIntegrityError, match=expected):
        await resolve_diary_attribution(session, item)
    session.add.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_json_and_duplicate_events_are_not_legacy() -> None:
    item = _item()
    actor = uuid4()
    event = _event(item, actor)
    event.new_value = "not-json"
    with pytest.raises(DiaryAttributionIntegrityError, match="invalid JSON"):
        await resolve_diary_attribution(_session_with([event]), item)

    valid = _event(item, actor)
    with pytest.raises(DiaryAttributionIntegrityError, match="multiple"):
        await resolve_diary_attribution(_session_with([valid, valid]), item)


@pytest.mark.asyncio
async def test_null_event_actor_is_not_legacy() -> None:
    item = _item()
    event = _event(item, uuid4())
    event.actor_principal_id = None
    with pytest.raises(DiaryAttributionIntegrityError, match="missing its actor"):
        await resolve_diary_attribution(_session_with([event]), item)
