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
        "docker compose -f docker-compose.ci.yml up --no-build "
        "--abort-on-container-exit --exit-code-from engram-test"
    ) in normalized_workflow
    assert "docker compose -f docker-compose.ci.yml up --build" not in normalized_workflow
    assert "if: always()" in workflow
    assert "down -v --remove-orphans" in normalized_workflow
    assert re.search(r"^  compose-real-db-ci:\s*$", workflow, re.MULTILINE)
    assert re.search(r"^  compose-validate:\s*$", workflow, re.MULTILINE)

    assert (
        "group: ${{ github.workflow }}-"
        "${{ github.event.pull_request.number || github.ref }}"
    ) in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow


def test_hosted_workflow_uses_read_only_github_hosted_runners() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()

    assert "runs-on: self-hosted" not in workflow
    assert workflow.count("runs-on: ubuntu-24.04") == 2
    assert re.search(r"^permissions:\s*\n  contents: read\s*$", workflow, re.MULTILINE)
    assert not re.search(r"^\s+[a-z-]+: write\s*$", workflow, re.MULTILINE)
    assert "pull_request_target" not in workflow
    assert workflow.count("uses: actions/checkout@v6") == 2
    assert workflow.count("persist-credentials: false") == 2


def test_hosted_workflow_builds_once_with_event_isolated_cache() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()

    assert "uses: docker/setup-buildx-action@v4" in workflow
    assert workflow.count("uses: docker/build-push-action@v7") == 2
    assert "if: github.event_name == 'push'" in workflow
    assert "if: github.event_name == 'pull_request'" in workflow
    assert workflow.count("target: ci") == 2
    assert workflow.count("load: true") == 2
    assert workflow.count("push: false") == 2
    assert workflow.count("tags: ${{ env.ENGRAM_CI_IMAGE }}") == 2
    assert workflow.count("cache-from: type=gha,scope=engram-ci") == 2
    assert "cache-to: type=gha,mode=max,scope=engram-ci" in workflow
    assert "cache-to: type=gha,mode=min,scope=engram-ci" in workflow
    assert "ENGRAM_CI_IMAGE: engram-ci:${{ github.sha }}" in workflow
    assert "docker/login-action" not in workflow
    assert "pull_request_target" not in workflow


def test_compose_supports_prebuilt_hosted_and_local_build_modes() -> None:
    compose = (REPOSITORY_ROOT / "docker-compose.ci.yml").read_text()

    assert "image: ${ENGRAM_CI_IMAGE:-engram-ci:local}" in compose
    assert "pull_policy: never" in compose
    assert re.search(r"build:\s*\n\s+context: \.\s*\n\s+target: ci", compose)
    assert "image: pgvector/pgvector:pg16" in compose
    assert re.search(
        r"depends_on:\s*\n\s+postgres:\s*\n\s+condition: service_healthy", compose
    )


def test_ci_dockerfile_separates_dependencies_from_source_binding() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text()

    dependencies_stage = dockerfile.index("FROM base AS ci-dependencies")
    metadata_copy = dockerfile.index("COPY pyproject.toml README.md LICENSE.md ./")
    dependency_install = dockerfile.index("RUN --mount=type=cache,target=/root/.cache/pip")
    ci_stage = dockerfile.index("FROM ci-dependencies AS ci")
    source_copy = dockerfile.index("COPY . .", ci_stage)
    source_binding = dockerfile.index(
        "RUN pip install --no-build-isolation --no-deps", source_copy
    )

    assert dependencies_stage < metadata_copy < dependency_install < ci_stage
    assert ci_stage < source_copy < source_binding
    assert "sdk/engram-client/pyproject.toml" in dockerfile[metadata_copy:dependency_install]
    assert "adapters/mcp-server/pyproject.toml" in dockerfile[metadata_copy:dependency_install]
    assert "adapters/engram-hooks/pyproject.toml" in dockerfile[metadata_copy:dependency_install]
    assert '-e "./sdk/engram-client[dev]"' in dockerfile[source_binding:]
    assert '-e "./adapters/mcp-server[dev]"' in dockerfile[source_binding:]
    assert '-e "./adapters/engram-hooks[dev]"' in dockerfile[source_binding:]
    assert 'CMD ["python", "scripts/run_ci.py"]' in dockerfile


def test_ci_runner_selects_complete_root_suite_with_skip_and_timing_guards() -> None:
    runner = (REPOSITORY_ROOT / "scripts/run_ci.py").read_text()

    assert 'env["ENGRAM_FAIL_ON_DB_SKIP"] = "1"' in runner
    assert '_run("pytest", "-q", "--durations=25", "tests", env=env)' in runner
    assert "run_trust_proof.py" not in runner
