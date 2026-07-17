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
COPY pyproject.toml README.md LICENSE.md ./
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

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
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
