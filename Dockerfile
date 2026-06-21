# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Search Typeahead — multi-stage, slim, multi-arch (amd64 + arm64), non-root.
# Single deployable service (FastAPI serving the API + the static frontend).
# ---------------------------------------------------------------------------

# ---- builder: compile/collect dependencies, never shipped to runtime ----
FROM python:3.12-slim AS builder
WORKDIR /app
# Copy only the lockfile first so the dep layer caches across code changes.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install -r requirements.txt

# ---- runtime: only the installed deps + app code ----
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TYPEAHEAD_DB=/app/data/typeahead.db
WORKDIR /app

# Installed Python packages from the builder (no compilers in this image).
COPY --from=builder /install /usr/local

# App code (data/ and docs/ are excluded via .dockerignore).
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

# Bake the dataset into the image so the first request is fast and the
# container is self-contained (no network at runtime). Prefer the REAL
# Wikipedia-pageviews dataset (pinned, reproducible); if the build host
# has no network, fall back to the deterministic synthetic generator.
RUN mkdir -p /app/data && cd /app/backend && \
    ( python fetch_dataset.py \
      || python -c "from data_loader import _synthesize_dataset; _synthesize_dataset('/app/data/queries.csv')" )

# Run as a non-root user; /app must be writable for the SQLite DB.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

WORKDIR /app/backend
EXPOSE 8765

# Liveness probe (no curl in slim — use the stdlib).
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health')" || exit 1

# uvicorn handles SIGTERM -> our lifespan drains the batch buffer on shutdown.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
