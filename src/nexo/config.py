"""Runtime configuration.

All project configuration lives in `.env` at the repo root. We load it once
at import time with python-dotenv so the values reach both this module (via
`os.getenv`) and pydantic-ai's OpenAI provider (which reads `OPENAI_API_KEY`
/ `OPENAI_BASE_URL` straight from the environment).

Copy `.env.example` to `.env` and fill in real values. `.env` is gitignored.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root, regardless of the current working dir.
# config.py is at <root>/src/nexo/config.py -> parents[2] is the root.
_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

# --- Data directory --------------------------------------------------------
# Used ONLY for the liveness heartbeat (`data/.heartbeat`, see observability).
# User uploads are NOT written here anymore — they live in Volcengine TOS
# (see the TOS section below). Kept because the Docker healthcheck reads it.
DATA_DIR = _ROOT / "data"

# --- Model (any OpenAI-compatible endpoint) -------------------------------
# NEXO_MODEL: provider-prefixed name, e.g. openai:gpt-4o-mini / openai:glm-4.6
# OPENAI_API_KEY / OPENAI_BASE_URL: consumed directly by pydantic-ai.
MODEL_NAME = os.getenv("NEXO_MODEL", "openai:gpt-4o-mini")

# --- Observability (Langfuse tracing) -------------------------------------
# Langfuse SDK reads these directly from the environment. Both keys must be
# set to enable tracing; with neither set, tracing is disabled (local dev).
# LANGFUSE_BASE_URL: cloud region (https://cloud.langfuse.com, us/jp/hipaa) or
# a self-hosted Langfuse URL. import of `langfuse` must happen AFTER load_dotenv
# (this module runs load_dotenv at import), so observability.py imports langfuse
# lazily inside configure() — not at module top.
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_BASE_URL = os.getenv("LANGFUSE_BASE_URL", "")

# --- WeCom (企业微信) AI bot ------------------------------------------------
# Credentials from the WeCom admin backend. The bot SDK connects out to
# wss://openws.work.weixin.qq.com using these.
WECHAT_BOT_ID = os.getenv("WECHAT_BOT_ID", "")
WECHAT_BOT_SECRET = os.getenv("WECHAT_BOT_SECRET", "")

# WeCom file/image download HTTP timeout (milliseconds). The aibot SDK defaults
# to 10000 (10s), which is too tight for slow links or larger uploads — the
# download is a direct aiohttp GET that ignores HTTP(S)_PROXY. Raise to 60s.
WECOM_REQUEST_TIMEOUT_MS = int(os.getenv("WECOM_REQUEST_TIMEOUT_MS", "60000"))

# --- Volcengine TOS (火山引擎对象存储) -------------------------------------
# Primary durable store for everything users upload (files + images). The bot
# no longer persists upload content to local disk. Set ALL FIVE — the first
# TOS call fails fast with a clear message if any is missing.
TOS_ACCESS_KEY_ID = os.getenv("TOS_ACCESS_KEY_ID", "")
TOS_SECRET_ACCESS_KEY = os.getenv("TOS_SECRET_ACCESS_KEY", "")
TOS_ENDPOINT = os.getenv("TOS_ENDPOINT", "")  # e.g. tos-cn-beijing.volces.com (no bucket, no https://)
TOS_REGION = os.getenv("TOS_REGION", "")  # e.g. cn-beijing
TOS_BUCKET = os.getenv("TOS_BUCKET", "")
