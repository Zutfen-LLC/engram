"""Fail-closed contract for required service-provisioning PostgreSQL proofs."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest_plugins = ("pytester",)


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


@pytest.mark.parametrize("when", ("setup", "call"))
def test_required_service_provisioning_skip_is_recorded_once_per_node(
    monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    conftest = _load_conftest()
    monkeypatch.setenv("ENGRAM_FAIL_ON_DB_SKIP", "1")
    conftest._db_skipped_tests.clear()
    conftest.pytest_collection_modifyitems([_MarkedItem()])  # type: ignore[arg-type]
    report = SimpleNamespace(
        when=when,
        skipped=True,
        nodeid=_MarkedItem.nodeid,
        longrepr="unrelated skip text",
    )
    conftest.pytest_runtest_logreport(report)  # type: ignore[arg-type]
    conftest.pytest_runtest_logreport(report)  # type: ignore[arg-type]
    assert conftest._db_skipped_tests == {_MarkedItem.nodeid}


def test_marked_fixture_skip_fails_closed_in_a_real_nested_pytest(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal pytest reporting path records setup-phase fixture skips."""
    root_conftest = Path(__file__).resolve().parent / "conftest.py"
    pytester.makeconftest(
        "\n".join(
            (
                "import importlib.util",
                f"_path = {str(root_conftest)!r}",
                "_spec = importlib.util.spec_from_file_location('root_conftest', _path)",
                "assert _spec is not None and _spec.loader is not None",
                "_module = importlib.util.module_from_spec(_spec)",
                "_spec.loader.exec_module(_module)",
                "pytest_collection_modifyitems = _module.pytest_collection_modifyitems",
                "pytest_runtest_logreport = _module.pytest_runtest_logreport",
                "pytest_sessionfinish = _module.pytest_sessionfinish",
            )
        )
    )
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def unavailable_database():
            pytest.skip("fixture cannot reach database")

        @pytest.mark.service_provisioning_postgres
        def test_required_proof(unavailable_database):
            pass

        def test_unmarked_skip_is_unrelated():
            pytest.skip("not a certification proof")
        """
    )
    monkeypatch.setenv("ENGRAM_FAIL_ON_DB_SKIP", "1")
    result = pytester.runpytest("-q")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        ["*Database-required tests skipped unexpectedly: *test_required_proof*"]
    )
    assert result.stdout.str().count("test_required_proof") == 1


def test_marked_fixture_skip_is_allowed_without_certification_enforcement(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def unavailable_database():
            pytest.skip("fixture cannot reach database")

        @pytest.mark.service_provisioning_postgres
        def test_required_proof(unavailable_database):
            pass
        """
    )
    monkeypatch.delenv("ENGRAM_FAIL_ON_DB_SKIP", raising=False)
    result = pytester.runpytest("-q")
    assert result.ret == pytest.ExitCode.OK


def test_ci_compose_supplies_all_required_provisioning_certification_inputs() -> None:
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.ci.yml").read_text()
    assert 'ENGRAM_FAIL_ON_DB_SKIP: "1"' in compose
    assert "ENGRAM_OWNER_DATABASE_URL" in compose
    assert "ENGRAM_DATABASE_URL" in compose
    assert "ENGRAM_PROVISIONER_DATABASE_URL" in compose
