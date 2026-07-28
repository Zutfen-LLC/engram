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


def test_hosted_workflow_runs_supplemental_merge_ref_real_db_gate() -> None:
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
    assert re.search(r"^  compose-real-db-merge-ref:\s*$", workflow, re.MULTILINE)
    assert re.search(r"^  compose-validate:\s*$", workflow, re.MULTILINE)


def test_hosted_workflow_runs_conformance_and_lock_drift_gates() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()

    # The parallel cross-language conformance job (ENG-CONTEXT-001): runs both
    # verifiers and the shared negative-fixture set against the same checked-in
    # artifacts. Kept off the Compose real-DB critical path.
    assert re.search(r"^  conformance-vectors:\s*$", workflow, re.MULTILINE)
    assert "python scripts/verify_context_manifest_vectors.py" in workflow
    assert "node conformance/context-manifest-v1/verify.mjs" in workflow
    assert "python scripts/verify_context_manifest_negatives.py" in workflow
    assert "node conformance/context-manifest-v1/verify_negatives.mjs" in workflow
    # Node comes only from actions/setup-node here, never from the CI image.
    assert "actions/setup-node@v5" in workflow

    # The parallel lock-drift gate: uv.lock must match pyproject.toml. Uses the
    # pinned uv version and a non-rewriting check, so a drifted lock fails CI.
    assert re.search(r"^  lock-drift:\s*$", workflow, re.MULTILINE)
    assert "astral-sh/setup-uv@v6" in workflow
    assert "version: \"0.11.29\"" in workflow
    assert "uv lock --check" in workflow
    # The lock gate never rewrites the lockfile.
    assert "uv lock\n" not in workflow

    # The lock must span the whole workspace, not just the root project, or the
    # gate silently ignores the SDK and adapters — and the CI image cannot be
    # pinned from it. See the Dockerfile contract test for why that matters.
    project = (REPOSITORY_ROOT / "pyproject.toml").read_text()
    assert "[tool.uv.workspace]" in project
    for member in ("sdk/engram-client", "adapters/mcp-server", "adapters/engram-hooks"):
        assert f'"{member}"' in project
    assert "engram-client = { workspace = true }" in project

    assert (
        "group: ${{ github.workflow }}-"
        "${{ github.event.pull_request.number || github.ref }}"
    ) in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow


def test_hosted_workflow_uses_read_only_github_hosted_runners() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()

    assert "runs-on: self-hosted" not in workflow
    # Five read-only hosted jobs: compose-real-db-merge-ref, runtime-image-smoke
    # (builds and boots the production `runtime` target), compose-validate, the
    # parallel conformance-vectors cross-language job (ENG-CONTEXT-001), and
    # the parallel lock-drift gate (uv.lock ↔ pyproject.toml consistency).
    assert workflow.count("runs-on: ubuntu-24.04") == 5
    assert re.search(r"^permissions:\s*\n  contents: read\s*$", workflow, re.MULTILINE)
    assert not re.search(r"^\s+[a-z-]+: write\s*$", workflow, re.MULTILINE)
    assert "pull_request_target" not in workflow
    assert workflow.count("uses: actions/checkout@v6") == 5
    assert workflow.count("persist-credentials: false") == 5


def test_hosted_workflow_builds_once_with_event_isolated_cache() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()

    assert "uses: docker/setup-buildx-action@v4" in workflow
    # Three builds: the push-event and pull_request-event CI-image builds (only
    # one of which ever runs, selected by the event guards below), plus the
    # runtime-image-smoke build of the production target.
    assert workflow.count("uses: docker/build-push-action@v7") == 3
    assert "if: github.event_name == 'push'" in workflow
    assert "if: github.event_name == 'pull_request'" in workflow
    assert workflow.count("load: true") == 3
    assert workflow.count("push: false") == 3
    assert "docker/login-action" not in workflow
    assert "pull_request_target" not in workflow

    # The CI image is still built exactly once per event: two mutually
    # exclusive `target: ci` builds sharing one tag and one cache scope.
    assert workflow.count("target: ci") == 2
    assert workflow.count("tags: ${{ env.ENGRAM_CI_IMAGE }}") == 2
    assert workflow.count("cache-from: type=gha,scope=engram-ci") == 2
    assert "cache-to: type=gha,mode=max,scope=engram-ci" in workflow
    assert "cache-to: type=gha,mode=min,scope=engram-ci" in workflow
    assert "ENGRAM_CI_IMAGE: engram-ci:${{ github.sha }}" in workflow

    # The runtime smoke build is a distinct image on its own cache scope, so it
    # can neither be mistaken for the CI image nor evict the CI cache entries.
    assert workflow.count("target: runtime") == 1
    assert "ENGRAM_RUNTIME_IMAGE: engram-runtime:${{ github.sha }}" in workflow
    assert "tags: ${{ env.ENGRAM_RUNTIME_IMAGE }}" in workflow
    assert "cache-from: type=gha,scope=engram-runtime" in workflow
    assert "cache-to: type=gha,mode=min,scope=engram-runtime" in workflow


def test_hosted_workflow_smokes_the_production_runtime_image() -> None:
    """CI must build and boot the `runtime` target, not only the `ci` target.

    Every other job builds `target: ci`, and compose-validate only runs
    `docker compose config` (which never builds), so without this job a broken
    non-editable `pip install .`, a dependency only the dev extras were
    providing, or a broken uvicorn entrypoint reaches main undetected.
    """
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
    compose = (REPOSITORY_ROOT / "docker-compose.ci.yml").read_text()

    assert re.search(r"^  runtime-image-smoke:\s*$", workflow, re.MULTILINE)
    # The image must actually serve, not merely build: liveness and readiness
    # are both probed. /ready is load-bearing — it proves the production image
    # reached Postgres as the non-owner app role under FORCE RLS, resolved a
    # tenant context, and found a new enough pgvector.
    assert "probe /health" in workflow
    assert "probe /ready" in workflow
    assert "import engram.api.app, engram.cli" in workflow

    # The smoke service is profile-gated so it never joins the default `up`
    # that runs the test suite.
    assert re.search(r"^  engram-runtime-smoke:\s*$", compose, re.MULTILINE)
    assert 'profiles: ["smoke"]' in compose
    assert "image: ${ENGRAM_RUNTIME_IMAGE:-engram-runtime:local}" in compose
    assert re.search(r"build:\s*\n\s+context: \.\s*\n\s+target: runtime", compose)
    # It connects as the non-owner application role, exactly as the deployment
    # stack does — this is the only place CI exercises that configuration.
    assert "engram_app:engram_app@postgres:5432/engram" in compose


def test_ci_compose_disables_durability_and_reports_results() -> None:
    """The CI Postgres trades durability for throughput, and both real-DB
    workflows export JUnit XML.

    The cluster is rebuilt from the bundled migrations on every run and
    destroyed with `down -v`, so the write barriers only cost time — losing
    these flags silently regresses the suite. The JUnit export must happen
    before teardown, or a failing run (the case that matters) reports nothing.
    """
    compose = (REPOSITORY_ROOT / "docker-compose.ci.yml").read_text()

    for flag in ("-c fsync=off", "-c synchronous_commit=off", "-c full_page_writes=off"):
        assert flag in compose
    assert "-c max_connections=200" in compose
    assert "/var/lib/postgresql/data:size=" in compose

    for name in ("ci.yml", "exact-head-ci.yml"):
        workflow = (REPOSITORY_ROOT / ".github/workflows" / name).read_text()
        normalized = " ".join(workflow.split())
        export = workflow.index("Export JUnit Test Results")
        upload = workflow.index("Upload JUnit Test Results")
        teardown = workflow.index("down -v --remove-orphans")
        assert export < upload < teardown
        assert "engram-test:/app/test-results/." in normalized
        assert "uses: actions/upload-artifact@v7" in workflow


def test_exact_head_workflow_checks_out_and_tests_the_pr_head() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/exact-head-ci.yml").read_text()

    assert re.search(r"^  compose-real-db-exact-head:\s*$", workflow, re.MULTILINE)
    assert "ref: ${{ github.event.pull_request.head.sha }}" in workflow
    assert "ENGRAM_CI_IMAGE: engram-ci:${{ github.event.pull_request.head.sha }}" in workflow
    assert 'ACTUAL_HEAD="$(git rev-parse HEAD)"' in workflow
    assert 'test "$ACTUAL_HEAD" = "$EXPECTED_HEAD"' in workflow
    assert workflow.count("Run Compose Real-DB CI Stack") == 1
    assert "--exit-code-from engram-test" in workflow
    assert "if: always()" in workflow


def test_compose_supports_prebuilt_hosted_and_local_build_modes() -> None:
    compose = (REPOSITORY_ROOT / "docker-compose.ci.yml").read_text()

    assert "image: ${ENGRAM_CI_IMAGE:-engram-ci:local}" in compose
    assert "pull_policy: never" in compose
    assert re.search(r"build:\s*\n\s+context: \.\s*\n\s+target: ci", compose)
    assert "image: pgvector/pgvector:pg16" in compose
    assert re.search(
        r"depends_on:\s*\n\s+postgres:\s*\n\s+condition: service_healthy", compose
    )


def test_compose_propagates_context_receipt_dark_write_settings() -> None:
    """The API-only context-receipt dark-write settings must be explicitly
    passed to the API service in docker-compose.yml so future settings cannot
    silently disappear from the deployable configuration (ENG-CONTEXT-002B).

    These are API-only: they must NOT be in the shared ``x-env`` anchor (the
    worker never sees them).
    """
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text()
    assert "ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED" in compose
    assert "ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_TIMEOUT_SECONDS" in compose
    # API service carries the settings.
    api_section = compose.split("engram-service:", 1)[1].split("engram-worker:", 1)[0]
    assert "ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED" in api_section
    assert "ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_TIMEOUT_SECONDS" in api_section
    # Worker section must NOT carry them (API-only).
    worker_section = compose.split("engram-worker:", 1)[1]
    assert "ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED" not in worker_section
    # The shared anchor must NOT carry them (API-only).
    anchor = compose.split("x-env:", 1)[1].split("services:", 1)[0]
    assert "ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED" not in anchor


def test_env_example_documents_context_receipt_dark_write_settings() -> None:
    """The example env file must document the new settings so operators can
    discover and configure them (ENG-CONTEXT-002B)."""
    env = (REPOSITORY_ROOT / ".env.example").read_text()
    assert "ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED=false" in env
    assert "ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_TIMEOUT_SECONDS=1.0" in env


def test_ci_dockerfile_separates_dependencies_from_source_binding() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text()

    dependencies_stage = dockerfile.index("FROM base AS ci-dependencies")
    metadata_copy = dockerfile.index("COPY pyproject.toml README.md LICENSE.md uv.lock ./")
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

    # The CI image installs third-party versions pinned by uv.lock rather than
    # re-resolving the floating floors in pyproject.toml, so the image CI tests
    # in matches `uv sync`. The export MUST cover the whole workspace: a
    # root-only export cannot describe this image (mcp alone adds httpx2,
    # mcp-types, opentelemetry-api, pyjwt and truststore), and constraining an
    # environment the lock does not describe makes pip silently backtrack to an
    # ancient version that fits — which is how mcp 0.1.0 once got installed.
    dependency_block = dockerfile[dependency_install:ci_stage]
    assert "uv export" in dependency_block
    assert "--frozen" in dependency_block
    assert "--all-packages" in dependency_block
    assert "--all-extras" in dependency_block
    # Local members are installed editable, never from the constraints file.
    assert "--no-emit-workspace" in dependency_block
    assert "pip install -c /tmp/ci-constraints.txt" in dependency_block
    # `--frozen` never rewrites the lockfile, repeating the lock-drift gate.
    assert "uv lock" not in dependency_block
    # Pinned to the same uv the lock-drift job uses.
    assert 'pip install "uv==0.11.29"' in dependency_block


def test_ci_runner_selects_complete_root_suite_with_skip_and_timing_guards() -> None:
    runner = (REPOSITORY_ROOT / "scripts/run_ci.py").read_text()

    assert 'env["ENGRAM_FAIL_ON_DB_SKIP"] = "1"' in runner
    assert (
        '_run("pytest", "-q", "--durations=25", *_pytest_flags("root"), "tests", env=env)'
    ) in runner
    assert '_run("python", "scripts/scan_credential_leaks.py")' in runner
    assert "run_trust_proof.py" not in runner

    # Every suite carries the same timeout and JUnit reporting guards. The
    # timeout turns a deadlock (the concurrency suites are the realistic
    # source) into a named failure instead of a silent 30-minute job timeout.
    assert 'f"--timeout={TEST_TIMEOUT_SECONDS}"' in runner
    # `signal`, never `thread`: thread hard-exits via os._exit() and would
    # abort pytest before it writes the JUnit XML below.
    assert '"--timeout-method=signal"' in runner
    assert '"--timeout-method=thread"' not in runner
    assert 'f"--junitxml={RESULTS_DIR / f\'{suite}.xml\'}"' in runner
    for suite in ("root", "sdk", "mcp-adapter", "engram-hooks"):
        assert f'_pytest_flags("{suite}")' in runner
