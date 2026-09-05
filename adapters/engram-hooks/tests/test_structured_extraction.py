"""Local guard and result terminology for opt-in structured capture."""

from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from engram_hooks.config import HooksConfig
from engram_hooks.hooks import HookResult, LifecycleHooks


def test_result_serializes_proposed_writes():
    result = HookResult(event="sync_turn", written_proposed=2)
    assert asdict(result)["written_proposed"] == 2
    assert "promoted" not in asdict(result)
    assert result.promoted == result.remembered == 2
    result.promoted += 1
    assert result.written_proposed == 3


@pytest.mark.asyncio
async def test_guard_blocks_the_complete_structured_batch(tmp_path):
    hooks = LifecycleHooks(
        HooksConfig(
            structured_extraction=True,
            volatile_path=str(tmp_path / "volatile.jsonl"),
        )
    )
    client = AsyncMock()
    hooks._client = client
    result = await hooks.sync_turn(
        {
            "messages": [
                {"role": "user", "content": "I prefer concise answers."},
                {"role": "assistant", "content": "password=" + "synthetic-local-secret"},
            ]
        }
    )
    assert result.rejected == 1 and result.written_proposed == 0
    client.extract.assert_not_called()
    assert hooks.volatile.all() == []


@pytest.mark.asyncio
async def test_contract_overflow_returns_a_controlled_rejection(tmp_path):
    hooks = LifecycleHooks(
        HooksConfig(
            structured_extraction=True,
            volatile_path=str(tmp_path / "volatile.jsonl"),
        )
    )
    hooks._client = AsyncMock()
    result = await hooks.sync_turn([{"role": "user", "content": "I prefer short answers."}] * 65)
    assert result.rejected == 1
    assert result.details[0]["reason"] == "invalid_structured_payload"
    hooks._client.extract.assert_not_called()


@pytest.mark.asyncio
async def test_server_rejection_does_not_park_input(tmp_path):
    from engram_client import EngramValidationError

    hooks = LifecycleHooks(
        HooksConfig(
            structured_extraction=True,
            volatile_path=str(tmp_path / "volatile.jsonl"),
        )
    )
    hooks._client = AsyncMock()
    hooks._client.extract.side_effect = EngramValidationError(422, "rejected", {})
    result = await hooks.sync_turn({"role": "user", "content": "I prefer concise answers."})
    assert result.rejected == 1 and result.parked == 0
    assert hooks.volatile.all() == []
