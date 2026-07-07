FROM python:3.12-slim AS base

WORKDIR /app

# Install build deps for asyncpg/bcrypt, then clean up.
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 8000

FROM base AS runtime

RUN pip install --no-cache-dir .

CMD ["uvicorn", "engram.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS ci

RUN pip install --no-cache-dir \
    -e ".[dev]" \
    -e "./sdk/engram-client[dev]" && \
    cd /app/adapters/mcp-server && \
    pip install --no-cache-dir -e ".[dev]"

CMD ["python", "scripts/run_ci.py"]
