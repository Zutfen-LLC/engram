"""DB-free regression tests for the hosted CI coverage contract."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from scripts.trust_proof_files import TRUST_PROOF_FILES

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = (REPOSITORY_ROOT / "tests").resolve()


def test_trust_proof_files_are_unique_existing_root_suite_tests() -> None:
    assert TRUST_PROOF_FILES
    assert len(TRUST_PROOF_FILES) == len(set(TRUST_PROOF_FILES))

    for entry in TRUST_PROOF_FILES:
        relative_path = PurePosixPath(entry)
        assert not relative_path.is_absolute()
        assert relative_path.parts[0] == "tests"

        resolved_path = (REPOSITORY_ROOT / Path(*relative_path.parts)).resolve()
        assert resolved_path.is_relative_to(TESTS_ROOT)
        assert resolved_path.is_file()


def test_hosted_workflow_runs_one_complete_real_db_gate() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()

    assert "scripts/run_trust_proof.py" not in workflow
    assert "Run Canonical Trust Proof" not in workflow
    assert "Reset Trust-Proof Stack" not in workflow
    assert workflow.count("Run Compose Real-DB CI Stack") == 1

    normalized_workflow = " ".join(workflow.split())
    assert (
        "docker compose -f docker-compose.ci.yml up --build "
        "--abort-on-container-exit --exit-code-from engram-test"
    ) in normalized_workflow
    assert "if: always()" in workflow
    assert "down -v --remove-orphans" in normalized_workflow
    assert re.search(r"^  compose-real-db-ci:\s*$", workflow, re.MULTILINE)
    assert re.search(r"^  compose-validate:\s*$", workflow, re.MULTILINE)

    assert (
        "group: ${{ github.workflow }}-"
        "${{ github.event.pull_request.number || github.ref }}"
    ) in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow


def test_ci_runner_selects_complete_root_suite_with_skip_and_timing_guards() -> None:
    runner = (REPOSITORY_ROOT / "scripts/run_ci.py").read_text()

    assert 'env["ENGRAM_FAIL_ON_DB_SKIP"] = "1"' in runner
    assert '_run("pytest", "-q", "--durations=25", "tests", env=env)' in runner
    assert "run_trust_proof.py" not in runner
