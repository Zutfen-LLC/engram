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


def test_hosted_workflow_runs_isolated_real_db_shards() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
    workflow_dir = REPOSITORY_ROOT / ".github/workflows"

    assert "scripts/run_trust_proof.py" not in workflow
    assert "Run Canonical Trust Proof" not in workflow
    assert "Reset Trust-Proof Stack" not in workflow
    assert workflow.count("Run Compose Real-DB CI Shard") == 1
    assert not (workflow_dir / "exact-head-ci.yml").exists()
    assert sum(
        path.read_text().count("Run Compose Real-DB CI Shard")
        for path in workflow_dir.glob("*.yml")
    ) == 1

    normalized_workflow = " ".join(workflow.split())
    assert (
        "docker compose -f docker-compose.ci.yml up --no-build "
        "--abort-on-container-exit --exit-code-from engram-test"
    ) in normalized_workflow
    assert "docker compose -f docker-compose.ci.yml up --build" not in normalized_workflow
    assert "if: always()" in workflow
    assert "down -v --remove-orphans" in normalized_workflow
    assert re.search(r"^  compose-real-db-shard:\s*$", workflow, re.MULTILINE)
    assert re.search(r"^  compose-real-db:\s*$", workflow, re.MULTILINE)
    assert "name: compose-real-db / shard ${{ matrix.shard_label }}" in workflow
    assert "fail-fast: false" in workflow
    assert "shard_index: 0" in workflow
    assert "shard_index: 3" in workflow
    assert 'ENGRAM_CI_MODE: "root-shard"' in workflow
    assert "ENGRAM_CI_SHARD_INDEX: ${{ matrix.shard_index }}" in workflow
    assert 'ENGRAM_CI_SHARD_COUNT: "4"' in workflow
    assert "needs.compose-real-db-shard.result" in workflow
    assert "needs.conformance-vectors.result" in workflow
    assert re.search(r"^  repository-safety:\s*$", workflow, re.MULTILINE)
    assert "Validate Compose Config" not in workflow

    # Documentation-only changes still scan for leaked credentials but skip the
    # image/database/toolchain jobs. This avoids both ten-minute gates without
    # creating a security blind spot through top-level paths-ignore filters.
    assert "paths-ignore:" not in workflow
    assert "python scripts/scan_credential_leaks.py" in workflow
    assert "runtime_changed: ${{ steps.changes.outputs.runtime_changed }}" in workflow
    assert "':(exclude,glob)**/*.md' ':(exclude,glob)docs/**'" in workflow
    assert workflow.count("needs: repository-safety") == 3
    assert workflow.count(
        "if: needs.repository-safety.outputs.runtime_changed == 'true'"
    ) == 3


def test_hosted_workflow_runs_one_pinned_conformance_and_lock_gate() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()

    # The parallel cross-language conformance job (ENG-CONTEXT-001): runs both
    # verifiers and the shared negative-fixture set against the same checked-in
    # artifacts. Kept off the Compose real-DB critical path.
    assert re.search(r"^  conformance-vectors:\s*$", workflow, re.MULTILINE)
    assert ".venv/bin/python scripts/verify_context_manifest_vectors.py" in workflow
    assert "node conformance/context-manifest-v1/verify.mjs" in workflow
    assert ".venv/bin/python conformance/context-manifest-v1/run_cross_language.py" in workflow
    # The driver owns both negative verifier executions; calling either directly
    # here would run the same fixture set twice.
    assert "scripts/verify_context_manifest_negatives.py" not in workflow
    assert "node conformance/context-manifest-v1/verify_negatives.mjs" not in workflow
    # Node comes only from actions/setup-node here, never from the CI image.
    assert "actions/setup-node@v5" in workflow

    # Lock drift and conformance share one setup instead of paying for a second
    # runner and checkout. Dependencies come from the checked-in lock.
    assert not re.search(r"^  lock-drift:\s*$", workflow, re.MULTILINE)
    assert "astral-sh/setup-uv@v6" in workflow
    assert "version: \"0.11.29\"" in workflow
    assert "uv lock --check" in workflow
    assert "uv sync --frozen --all-packages --all-extras" in workflow
    conformance_job = workflow.split("  conformance-vectors:", 1)[1]
    assert "pip install" not in conformance_job
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
    # Five read-only hosted job definitions. The real-DB matrix expands its
    # single definition into four isolated runners.
    assert workflow.count("runs-on: ubuntu-24.04") == 5
    assert re.search(r"^permissions:\s*\n  contents: read\s*$", workflow, re.MULTILINE)
    assert not re.search(r"^\s+[a-z-]+: write\s*$", workflow, re.MULTILINE)
    assert "pull_request_target" not in workflow
    assert workflow.count("uses: actions/checkout@v6") == 4
    assert workflow.count("persist-credentials: false") == 4


def test_hosted_workflow_builds_each_isolated_shard_from_one_cache_scope() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()

    assert "uses: docker/setup-buildx-action@v4" in workflow
    # Two build definitions: the matrix expands the CI definition once per
    # shard, and the runtime target remains a distinct build.
    assert workflow.count("uses: docker/build-push-action@v7") == 2
    assert workflow.count("load: true") == 2
    assert workflow.count("push: false") == 2
    assert "docker/login-action" not in workflow
    assert "pull_request_target" not in workflow

    # One action invocation handles both event types; only the cache export mode
    # differs, avoiding two almost-identical guarded YAML blocks.
    assert workflow.count("target: ci") == 1
    assert workflow.count("tags: ${{ env.ENGRAM_CI_IMAGE }}") == 1
    assert workflow.count("cache-from: type=gha,scope=engram-ci") == 1
    assert "matrix.export_cache" in workflow
    assert workflow.count("github.event_name == 'push' && 'max' || 'min'") == 2
    assert "ENGRAM_CI_IMAGE: engram-ci:${{ github.sha }}" in workflow

    # The runtime smoke build is a distinct image on its own cache scope, so it
    # can neither be mistaken for the CI image nor evict the CI cache entries.
    assert workflow.count("target: runtime") == 1
    assert "ENGRAM_RUNTIME_IMAGE: engram-runtime:${{ github.sha }}" in workflow
    assert "tags: ${{ env.ENGRAM_RUNTIME_IMAGE }}" in workflow
    assert "cache-from: type=gha,scope=engram-runtime" in workflow
    assert "scope=engram-runtime" in workflow


def test_hosted_workflow_smokes_the_production_runtime_image() -> None:
    """CI must build and boot the `runtime` target, not only the `ci` target.

    Every other build uses `target: ci`, and repository-safety only resolves
    the Compose contract, so without this job a broken
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
    """The disposable CI database uses non-durable settings and reports results.

    The cluster is rebuilt from the bundled migrations on every run and
    destroyed with `down -v`; the settings are safe even though measurements
    show no material speedup. JUnit export must happen before teardown so both
    failures and successful timing baselines remain inspectable.
    """
    compose = (REPOSITORY_ROOT / "docker-compose.ci.yml").read_text()

    for flag in ("-c fsync=off", "-c synchronous_commit=off", "-c full_page_writes=off"):
        assert flag in compose
    assert "-c max_connections=200" in compose
    assert "/var/lib/postgresql/data:size=" in compose

    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
    normalized = " ".join(workflow.split())
    export = workflow.index("Export JUnit Test Results")
    upload = workflow.index("Upload JUnit Test Results")
    teardown = workflow.index("down -v --remove-orphans")
    assert export < upload < teardown
    assert "engram-test:/app/test-results/." in normalized
    assert "uses: actions/upload-artifact@v7" in workflow
    assert workflow.count("if: always()") >= 4


def test_compose_validation_has_one_reusable_entrypoint() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
    validator = (REPOSITORY_ROOT / "scripts/validate_compose_contract.sh").read_text()
    makefile = (REPOSITORY_ROOT / "Makefile").read_text()

    assert "bash scripts/validate_compose_contract.sh" in workflow
    assert "bash scripts/validate_compose_contract.sh" in makefile
    assert "docker compose config -q" not in workflow
    assert "docker compose config --services" in validator
    assert "docker compose config engram-service" in validator
    assert "docker compose config engram-worker" in validator
    assert "ENGRAM_JOB_MAX_ATTEMPTS" in validator


def test_compose_supports_prebuilt_hosted_and_local_build_modes() -> None:
    compose = (REPOSITORY_ROOT / "docker-compose.ci.yml").read_text()

    assert "image: ${ENGRAM_CI_IMAGE:-engram-ci:local}" in compose
    assert "pull_policy: never" in compose
    assert re.search(r"build:\s*\n\s+context: \.\s*\n\s+target: ci", compose)
    assert "image: pgvector/pgvector:pg16" in compose
    assert re.search(
        r"depends_on:\s*\n\s+postgres:\s*\n\s+condition: service_healthy", compose
    )
    assert "ENGRAM_CI_MODE: ${ENGRAM_CI_MODE:-full}" in compose
    assert "ENGRAM_CI_SHARD_INDEX: ${ENGRAM_CI_SHARD_INDEX:-0}" in compose
    assert "ENGRAM_CI_SHARD_COUNT: ${ENGRAM_CI_SHARD_COUNT:-1}" in compose


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


def test_compose_and_env_document_review_delegation_settings() -> None:
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text()
    ci_compose = (REPOSITORY_ROOT / "docker-compose.ci.yml").read_text()
    env = (REPOSITORY_ROOT / ".env.example").read_text()

    api_section = compose.split("engram-service:", 1)[1].split("engram-worker:", 1)[0]
    worker_section = compose.split("engram-worker:", 1)[1]
    for setting in (
        "ENGRAM_REVIEW_DELEGATION_ENABLED",
        "ENGRAM_REVIEW_DELEGATION_DEFAULT_TTL_SECONDS",
        "ENGRAM_REVIEW_DELEGATION_MAX_TTL_SECONDS",
    ):
        assert setting in api_section
        assert setting not in worker_section
        assert setting in env
    assert 'ENGRAM_REVIEW_DELEGATION_ENABLED: "true"' in ci_compose


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
    # `--frozen` never rewrites the lockfile, repeating the contract-job check.
    assert "uv lock" not in dependency_block
    # Pinned to the same uv the contract job uses.
    assert 'pip install "uv==0.11.29"' in dependency_block


def test_ci_runner_supports_full_preflight_and_root_shard_modes() -> None:
    runner = (REPOSITORY_ROOT / "scripts/run_ci.py").read_text()

    assert 'env["ENGRAM_FAIL_ON_DB_SKIP"] = "1"' in runner
    assert 'choices=("full", "preflight", "root-shard")' in runner
    assert 'default=os.environ.get("ENGRAM_CI_MODE", "full")' in runner
    assert 'os.environ["ENGRAM_CI_SHARD_INDEX"]' in runner
    assert 'os.environ["ENGRAM_CI_SHARD_COUNT"]' in runner
    assert "select_root_test_shard(" in runner
    assert 'f"root-shard-{shard_index + 1}-of-{shard_count}"' in runner
    assert '"tests"' in runner
    assert '_run("python", "scripts/scan_credential_leaks.py")' in runner
    assert "run_trust_proof.py" not in runner

    # All four shipped Python packages declare strict mypy settings and are
    # checked. MYPYPATH exposes the typed sibling SDK source to both adapters.
    for config, package in (
        ("sdk/engram-client/pyproject.toml", "sdk/engram-client/engram_client"),
        ("adapters/mcp-server/pyproject.toml", "adapters/mcp-server/engram_mcp"),
        ("adapters/engram-hooks/pyproject.toml", "adapters/engram-hooks/engram_hooks"),
    ):
        assert config in runner
        assert package in runner
    assert 'workspace_env["MYPYPATH"] = "sdk/engram-client"' in runner

    # Cheap failures surface before the expensive database suite begins.
    assert runner.index("Credential Leak Scan") < runner.index("Root Service Tests")
    assert runner.index("Type Check: engram-hooks Adapter") < runner.index("Root Service Tests")
    assert runner.index("SDK Tests") < runner.index("Root Service Tests")
    assert runner.index("MCP Adapter Tests") < runner.index("Root Service Tests")
    assert runner.index("engram-hooks Tests") < runner.index("Root Service Tests")

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

    # Static checks and DB-free package suites run once in the existing
    # conformance job. The real-DB matrix runs only the DB verification, the
    # MCP integration suite on shard zero, and one root-test shard.
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
    assert "Run Python preflight" in workflow
    assert "scripts/run_ci.py --mode preflight" in workflow
    assert "ENGRAM_CI_RESULTS_DIR: test-results" in workflow
