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

# --- Data directories ------------------------------------------------------
# Original downloads land in uploads/; generated summaries in processed/.
DATA_DIR = _ROOT / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
PROCESSED_DIR = DATA_DIR / "processed"

# --- Model (any OpenAI-compatible endpoint) -------------------------------
# NEXO_MODEL: provider-prefixed name, e.g. openai:gpt-4o-mini / openai:glm-4.6
# OPENAI_API_KEY / OPENAI_BASE_URL: consumed directly by pydantic-ai.
MODEL_NAME = os.getenv("NEXO_MODEL", "openai:gpt-4o-mini")

# --- WeCom (企业微信) AI bot ------------------------------------------------
# Credentials from the WeCom admin backend. The bot SDK connects out to
# wss://openws.work.weixin.qq.com using these.
WECHAT_BOT_ID = os.getenv("WECHAT_BOT_ID", "")
WECHAT_BOT_SECRET = os.getenv("WECHAT_BOT_SECRET", "")
