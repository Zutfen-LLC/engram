from __future__ import annotations

import os

from _pytest.config import ExitCode
from _pytest.reports import TestReport
from _pytest.terminal import TerminalReporter
from pytest import Session

_DB_SKIP_REASON = "requires a live PostgreSQL with the v2 schema"
_db_skipped_tests: list[str] = []


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
