.PHONY: lint typecheck test check setup-python-dev compose-ci compose-ci-down

# Use virtual environment executables
VENV_BIN = .venv/bin

lint:
	$(VENV_BIN)/ruff check .

typecheck:
	$(VENV_BIN)/mypy engram/

test:
	$(VENV_BIN)/pytest -q

check: lint typecheck test
	@echo "All checks passed!"

setup-python-dev:
	bash scripts/setup-python-dev.sh

# IDE and agent sessions can inherit a stale supplementary-group list even
# when the current account is configured as a member of the docker group.
# Prefer direct Docker access; fall back to activating that configured group
# for this command only.
compose-ci:
	@if docker info >/dev/null 2>&1; then \
		docker compose -f docker-compose.ci.yml up --build \
			--abort-on-container-exit --exit-code-from engram-test; \
	elif getent group docker | cut -d: -f4 | tr ',' '\n' | grep -Fxq "$$(id -un)"; then \
		sg docker -c 'docker compose -f docker-compose.ci.yml up --build \
			--abort-on-container-exit --exit-code-from engram-test'; \
	else \
		echo "Docker is not accessible and $$(id -un) is not configured in the docker group." >&2; \
		exit 1; \
	fi

compose-ci-down:
	@if docker info >/dev/null 2>&1; then \
		docker compose -f docker-compose.ci.yml down -v; \
	elif getent group docker | cut -d: -f4 | tr ',' '\n' | grep -Fxq "$$(id -un)"; then \
		sg docker -c 'docker compose -f docker-compose.ci.yml down -v'; \
	else \
		echo "Docker is not accessible and $$(id -un) is not configured in the docker group." >&2; \
		exit 1; \
	fi

# ── Gate B: canonical trust-proof selector ────────────────────────────
# Runs the consolidated trust, scope, RLS, attribution, review, feedback,
# promotion, conflict, classification, and worker-concurrency proof suite
# against the real PostgreSQL database with ENGRAM_FAIL_ON_DB_SKIP=1.
# The file list is maintained in scripts/trust_proof_files.py.
.PHONY: trust-proof compose-trust-proof

trust-proof:
	ENGRAM_FAIL_ON_DB_SKIP=1 $(VENV_BIN)/python scripts/run_trust_proof.py

# Compose-backed variant: runs the trust proofs inside the CI Docker stack
# against a fresh PostgreSQL 16 + pgvector instance with the non-owner
# application role.
compose-trust-proof:
	@if docker info >/dev/null 2>&1; then \
		docker compose -f docker-compose.ci.yml run --build --rm \
			-e ENGRAM_FAIL_ON_DB_SKIP=1 \
			engram-test python scripts/run_trust_proof.py; \
	elif getent group docker | cut -d: -f4 | tr ',' '\n' | grep -Fxq "$$(id -un)"; then \
		sg docker -c 'docker compose -f docker-compose.ci.yml run --build --rm \
			-e ENGRAM_FAIL_ON_DB_SKIP=1 \
			engram-test python scripts/run_trust_proof.py'; \
	else \
		echo "Docker is not accessible and $$(id -un) is not configured in the docker group." >&2; \
		exit 1; \
	fi
