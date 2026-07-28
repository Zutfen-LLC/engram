# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

WORKDIR /app

# Install build deps for asyncpg/bcrypt, then clean up.
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

EXPOSE 8000

FROM base AS runtime

COPY . .

RUN pip install --no-cache-dir .

CMD ["uvicorn", "engram.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS ci-dependencies

# Resolve third-party dependencies from package metadata before copying source.
# Minimal package directories let setuptools evaluate each local project without
# allowing application or test changes to invalidate this expensive layer.
# uv.lock joins them so this layer also invalidates when dependencies change.
COPY pyproject.toml README.md LICENSE.md uv.lock ./
COPY sdk/engram-client/pyproject.toml sdk/engram-client/pyproject.toml
COPY adapters/mcp-server/pyproject.toml adapters/mcp-server/README.md adapters/mcp-server/
COPY adapters/engram-hooks/pyproject.toml adapters/engram-hooks/README.md adapters/engram-hooks/

RUN mkdir -p \
        engram \
        sdk/engram-client/engram_client \
        adapters/mcp-server/engram_mcp \
        adapters/engram-hooks/engram_hooks && \
    touch \
        engram/__init__.py \
        sdk/engram-client/engram_client/__init__.py \
        adapters/mcp-server/engram_mcp/__init__.py \
        adapters/engram-hooks/engram_hooks/__init__.py

# Pin every third-party version that uv.lock resolves, so the image CI tests in
# matches what `uv sync --extra dev` gives developers locally. Without this the
# build re-resolves the floating floors in pyproject.toml (fastapi>=0.115,
# openai>=1.0, ...) on every cache miss, so an upstream release could break CI
# with no change to this repository.
#
# The export must cover the whole workspace (`--all-packages`), not just the
# root project. A root-only lock cannot describe this image: `mcp` alone pulls
# in httpx2, mcp-types, opentelemetry-api, pyjwt and truststore, which a
# root-only lock has no entry for while still bounding the packages `mcp`
# shares with the service. Constraining an environment the lock does not
# describe makes pip backtrack to whatever ancient version fits — silently,
# because a constraints file bounds versions but never forces one. With every
# package pinned to a set uv proved consistent, pip must install that set or
# fail loudly.
#
# `--no-emit-workspace` drops the four local members; they are installed
# editable below. `uv export --frozen` never rewrites uv.lock and fails if it
# has drifted from pyproject.toml, so this repeats the lock-drift gate inside
# the build. uv is pinned to the same version the lock-drift CI job uses.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install "uv==0.11.29" && \
    uv export \
        --frozen \
        --all-packages \
        --all-extras \
        --no-emit-workspace \
        --no-hashes \
        --format requirements-txt \
        -o /tmp/ci-constraints.txt && \
    pip install -c /tmp/ci-constraints.txt \
        "setuptools>=68" \
        wheel \
        -e ".[dev]" \
        -e "./sdk/engram-client[dev]" \
        -e "./adapters/mcp-server[dev]" \
        -e "./adapters/engram-hooks[dev]"

FROM ci-dependencies AS ci

COPY . .

RUN pip install --no-build-isolation --no-deps \
    -e ".[dev]" \
    -e "./sdk/engram-client[dev]" \
    -e "./adapters/mcp-server[dev]" \
    -e "./adapters/engram-hooks[dev]"

CMD ["python", "scripts/run_ci.py"]
