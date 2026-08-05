# syntax=docker/dockerfile:1.7

# ---------- Stage 1: builder ----------
# python:3.13-slim-bookworm matches the runtime base (glibc-compatible). uv is
# installed via pip: the official uv image lives on ghcr.io, which is slow or
# unreachable from some deploy hosts; pip lets us use a PyPI mirror instead.
FROM python:3.13-slim-bookworm AS builder

# PyPI index for both `pip install uv` and `uv sync`. Defaults to PyPI;
# override via --build-arg on hosts behind a faster mirror (e.g. Tencent CVMs
# use http://mirrors.tencentyun.com/pypi/simple + UV_TRUSTED_HOST=mirrors.tencentyun.com,
# passed through docker-compose.override.yml on that host).
ARG UV_INDEX_URL=https://pypi.org/simple
ARG UV_TRUSTED_HOST=

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_FROZEN=1 \
    UV_INDEX_URL=${UV_INDEX_URL} \
    UV_INSECURE_HOST=${UV_TRUSTED_HOST}

WORKDIR /app

# Install uv from the configured index.
RUN pip install --no-cache-dir --index-url "${UV_INDEX_URL}" \
        ${UV_INSECURE_HOST:+--trusted-host "${UV_INSECURE_HOST}"} uv

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

# Optional Debian apt mirror (e.g. mirrors.huaweicloud.com on Huawei Cloud,
# where deb.debian.org is slow). Empty = use the official Debian repos.
ARG APT_MIRROR=
# ca-certificates: required for wss:// TLS to the WeCom endpoint and the
# OpenAI-compatible HTTPS calls. tini: proper PID-1 signal handling so the
# asyncio loop receives SIGINT for a clean shutdown.
RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" \
        /etc/apt/sources.list.d/debian.sources; \
    fi && \
    apt-get update && \
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

# Pre-create the data dir owned by appuser (the bind-mount overlays this).
# The outbox (data/outbox.db) + media staging (data/staging/) live here, shared
# between `nexo bot` and `nexo drain`. Media bytes are uploaded to Huawei OBS by
# drain, not written to local disk.
RUN mkdir -p /app/data && \
    chown -R appuser:appgroup /app

USER appuser

# Copy source so config.py's Path(__file__).parents[2] == /app holds, making
# `docker run -v ./.env:/app/.env ...` work without compose.
# prompts.toml is read at import time by nexo.prompts — must be at /app/prompts.toml.
COPY --chown=appuser:appgroup src/ ./src/
COPY --chown=appuser:appgroup pyproject.toml README.md prompts.toml ./

ENTRYPOINT ["tini", "--"]
CMD ["nexo", "bot"]
