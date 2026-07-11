from __future__ import annotations

import os

import pytest
from _pytest.config import ExitCode
from _pytest.reports import TestReport
from _pytest.terminal import TerminalReporter
from pytest import Session

_DB_SKIP_REASON = "requires a live PostgreSQL with the v2 schema"
_db_skipped_tests: list[str] = []


@pytest.fixture(autouse=True)
def _restore_auth_enabled_default():
    """Guard against global `settings.auth_enabled` leakage across tests.

    `engram.config.settings` is a process-wide singleton; several test files
    mutate `settings.auth_enabled` directly (without their own teardown) to
    exercise auth-enabled vs. auth-disabled behavior. Since V2-BL-004 made
    `get_current_principal` load-bearing for nearly every route (via
    ScopeGuard), a leaked `auth_enabled=True` from one test file now makes
    unrelated tests in files that assume dev-mode (no bearer token required)
    fail with 401 instead of running. Restore the pre-test value after every
    test so mutations never leak across test boundaries.
    """
    from engram.config import settings

    original = settings.auth_enabled
    yield
    settings.auth_enabled = original


@pytest.fixture(autouse=True)
async def _dispose_db_engines_after_test():
    """Dispose the real module-level SQLAlchemy engines after every test.

    `engram.db.engine`/`owner_engine`/`read_engine` are created once at
    import time and pool real asyncpg connections. pytest-asyncio gives each
    test function its own event loop by default, but a pooled connection
    checked out in one test's loop is not valid in the next test's loop —
    reusing it raises `RuntimeError: ... attached to a different loop` or
    `asyncpg.exceptions._base.InterfaceError: cannot perform operation:
    another operation is in progress`. Before V2-BL-004 these engines were
    only touched by a handful of admin-route tests; ScopeGuard's
    `get_current_principal` dependency now touches them from nearly every
    guarded route, so the cross-loop hazard is exercised far more broadly.
    Disposing after each test forces the next test to open fresh connections
    in its own loop. This is test-only — the real server has one long-lived
    event loop for its whole process, so this hazard never occurs in
    production.
    """
    yield
    import engram.db as db_module

    for eng in (db_module.engine, db_module.owner_engine, db_module.read_engine):
        await eng.dispose()


def pytest_runtest_logreport(report: TestReport) -> None:
    if os.environ.get("ENGRAM_FAIL_ON_DB_SKIP") != "1":
        return
    if report.when != "call" or not report.skipped:
        return
    if _DB_SKIP_REASON in str(report.longrepr):
        _db_skipped_tests.append(report.nodeid)


def pytest_sessionfinish(session: Session, exitstatus: int) -> None:
    if os.environ.get("ENGRAM_FAIL_ON_DB_SKIP") != "1":
        return
    if not _db_skipped_tests:
        return
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    if isinstance(terminal, TerminalReporter):
        joined = ", ".join(_db_skipped_tests)
        terminal.write_line(
            f"Database-required tests skipped unexpectedly: {joined}",
            red=True,
        )
    session.exitstatus = ExitCode.TESTS_FAILED
