"""Fail-closed contract for required service-provisioning PostgreSQL proofs."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_conftest():  # type: ignore[no-untyped-def]
    path = Path(__file__).resolve().parent / "conftest.py"
    spec = importlib.util.spec_from_file_location("service_provisioning_conftest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _MarkedItem:
    nodeid = "tests/test_required.py::test_database_proof"

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name == "service_provisioning_postgres" else None


def test_required_service_provisioning_skip_fails_session(monkeypatch: pytest.MonkeyPatch) -> None:
    conftest = _load_conftest()
    monkeypatch.setenv("ENGRAM_FAIL_ON_DB_SKIP", "1")
    conftest._db_skipped_tests.clear()
    conftest.pytest_collection_modifyitems([_MarkedItem()])  # type: ignore[arg-type]
    report = SimpleNamespace(
        when="call",
        skipped=True,
        nodeid=_MarkedItem.nodeid,
        longrepr="unrelated skip text",
    )
    conftest.pytest_runtest_logreport(report)  # type: ignore[arg-type]
    session = SimpleNamespace(
        config=SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda _name: None)),
        exitstatus=pytest.ExitCode.OK,
    )
    conftest.pytest_sessionfinish(session, pytest.ExitCode.OK)  # type: ignore[arg-type]
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_ci_compose_supplies_all_required_provisioning_certification_inputs() -> None:
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.ci.yml").read_text()
    assert "ENGRAM_FAIL_ON_DB_SKIP: \"1\"" in compose
    assert "ENGRAM_OWNER_DATABASE_URL" in compose
    assert "ENGRAM_DATABASE_URL" in compose
    assert "ENGRAM_PROVISIONER_DATABASE_URL" in compose
