FROM python:3.12-slim

WORKDIR /app

# Install build deps for asyncpg/bcrypt, then clean up
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Install dependencies first (better layer caching)
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy application code
COPY engram/ ./engram/
COPY migrations/ ./migrations/

EXPOSE 8000

CMD ["uvicorn", "engram.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
