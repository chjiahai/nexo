# syntax=docker/dockerfile:1.7

# ---------- Stage 1: builder ----------
# Official uv image bundles uv + python 3.13. bookworm-slim matches the
# runtime base so glibc is compatible between stages.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_FROZEN=1

WORKDIR /app

# Layer-cache: copy lock + manifest first so the deps layer is reused
# across source-only changes.
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY README.md ./

# Two-step sync: install deps (cached) then the project itself, excluding dev.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project && \
    uv sync --frozen --no-dev

# ---------- Stage 2: runtime ----------
FROM python:3.13-slim-bookworm AS runtime

# ca-certificates: required for wss:// TLS to the WeCom endpoint and the
# OpenAI-compatible HTTPS calls. tini: proper PID-1 signal handling so the
# asyncio loop receives SIGINT for a clean shutdown.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates tini && \
    rm -rf /var/lib/apt/lists/*

# Non-root user (uid 10001 to avoid colliding with host uid 1000).
RUN groupadd --gid 10001 --system appgroup && \
    useradd  --uid 10001 --gid appgroup --system --create-home --home-dir /home/app appuser

WORKDIR /app

# Copy the self-contained venv from the builder.
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Pre-create data dirs owned by appuser (the bind-mount overlays these).
RUN mkdir -p /app/data/uploads /app/data/processed /app/data/index && \
    chown -R appuser:appgroup /app

USER appuser

# Copy source so config.py's Path(__file__).parents[2] == /app holds, making
# `docker run -v ./.env:/app/.env ...` work without compose.
# prompts.toml is read at import time by nexo.prompts — must be at /app/prompts.toml.
COPY --chown=appuser:appgroup src/ ./src/
COPY --chown=appuser:appgroup pyproject.toml README.md prompts.toml ./

ENTRYPOINT ["tini", "--"]
CMD ["nexo", "bot"]
