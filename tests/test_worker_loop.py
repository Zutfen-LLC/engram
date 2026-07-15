"""DB-free tests for background worker loop scheduling."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from engram import worker


async def _run_worker(*, max_jobs: int | None = None, once: bool = False) -> int:
    return await worker.run_worker(
        worker_id="test-worker",
        session_factory=AsyncMock(),
        app_session_factory=AsyncMock(),
        once=once,
        poll_interval=2.0,
        max_jobs=max_jobs,
    )


async def test_backlog_including_handled_failures_drains_without_inter_job_sleep(
    monkeypatch: pytest.MonkeyPatch,
):
    # True means a job was handled even when its handler internally failed and
    # the job was retried or dead-lettered. Every handled job remains work-conserving.
    process_one_job = AsyncMock(side_effect=[True, True, True])
    sleep = AsyncMock()
    monkeypatch.setattr(worker, "process_one_job", process_one_job)
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)

    result = await _run_worker(max_jobs=3)

    assert result == 0
    assert process_one_job.await_count == 3
    sleep.assert_not_awaited()


async def test_idle_queue_sleeps_once_before_available_job(
    monkeypatch: pytest.MonkeyPatch,
):
    process_one_job = AsyncMock(side_effect=[False, True])
    sleep = AsyncMock()
    monkeypatch.setattr(worker, "process_one_job", process_one_job)
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)

    result = await _run_worker(max_jobs=1)

    assert result == 0
    assert process_one_job.await_count == 2
    sleep.assert_awaited_once_with(2.0)


async def test_unexpected_loop_failure_logs_and_backs_off(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    process_one_job = AsyncMock(side_effect=[RuntimeError("unexpected"), True])
    sleep = AsyncMock()
    monkeypatch.setattr(worker, "process_one_job", process_one_job)
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)

    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        result = await _run_worker(max_jobs=1)

    assert result == 0
    assert process_one_job.await_count == 2
    sleep.assert_awaited_once_with(2.0)
    assert "error in process_one_job; continuing" in caplog.text


@pytest.mark.parametrize("did_process", [True, False])
async def test_once_makes_one_attempt_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
    did_process: bool,
):
    process_one_job = AsyncMock(return_value=did_process)
    sleep = AsyncMock()
    monkeypatch.setattr(worker, "process_one_job", process_one_job)
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)

    result = await _run_worker(once=True)

    assert result == 0
    process_one_job.assert_awaited_once()
    sleep.assert_not_awaited()
