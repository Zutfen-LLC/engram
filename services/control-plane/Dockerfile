# syntax=docker/dockerfile:1
#
# Control Plane image (ENG-PORTAL-001A).
#
# Built with REPOSITORY-ROOT context (docker compose build context: ..), so the
# uv workspace + root uv.lock resolve correctly. The Control Plane is an
# independent package and MUST NOT import `engram` Core — it is installed via a
# frozen package-specific workspace command, NOT an independent pip resolution.
#
# This Dockerfile is intentionally separate from the Core Dockerfile; Core's
# build path is unchanged.

# hadolint ignore=DL3006
FROM python:3.12-slim AS base

WORKDIR /app

# Install uv (pinned) and build deps for asyncpg.
# hadolint ignore=DL3008
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /bin/

# Resolve the control-plane workspace member from the frozen root lockfile.
# Copy only the project metadata needed for resolution first so dependency
# layers cache independently of source changes.
COPY pyproject.toml uv.lock ./
COPY services/control-plane/pyproject.toml services/control-plane/README.md services/control-plane/

RUN uv sync --frozen --package engram-control-plane --no-dev --no-install-package engram-control-plane

# Now copy the control-plane source and install just the package.
COPY services/control-plane/ services/control-plane/

RUN uv sync --frozen --package engram-control-plane --no-dev

ENV ENGRAM_CONTROL_HOST=0.0.0.0 \
    ENGRAM_CONTROL_PORT=8100 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

EXPOSE 8100

CMD ["engram-control-plane", "serve"]
