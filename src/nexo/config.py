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
# User uploads are NOT written here — they land in the nexo-vfs mount
# (see the upload section below). Kept because the Docker healthcheck reads it.
DATA_DIR = _ROOT / "data"

# --- Debug frame capture ---------------------------------------------------
# When enabled, every inbound WeCom frame's body is appended (one JSON line)
# to DEBUG_FRAMES_PATH. Used to discover the payload shape of message types
# the aibot SDK doesn't model (video, location, WeDrive file, imagetext, ...):
# the SDK emits a generic `message` event for ALL frames, so a catch-all
# listener sees even the ones it otherwise drops. Off by default.
DEBUG_FRAMES = os.getenv("NEXO_DEBUG_FRAMES", "") == "1"
DEBUG_FRAMES_PATH = DATA_DIR / "debug_frames.jsonl"

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

# 用户所属组织 ID。所有用户的上传文件夹都归属到该组织 ID 下，便于按组织
# 隔离/归档。与机器人 AK/SK 写在一起便于维护。
NEXO_ORG_ID = os.getenv("NEXO_ORG_ID", "")

# WeCom file/image download HTTP timeout (milliseconds). The aibot SDK defaults
# to 10000 (10s), which is too tight for slow links or larger uploads — the
# download is a direct aiohttp GET that ignores HTTP(S)_PROXY. Raise to 60s.
WECOM_REQUEST_TIMEOUT_MS = int(os.getenv("WECOM_REQUEST_TIMEOUT_MS", "60000"))

# --- 用户上传落盘（写入 nexo-vfs 分布式文件系统）----------------------------
# 收到的文件/图片/视频直接流式写入 nexo-vfs 挂载点，不区分类型，统一落到
# <NEXO_VFS_DIR>/<NEXO_ORG_ID>/<user_id>/ 下。NEXO_ORG_ID 见上方 WeCom 块。
# NEXO_VFS_DIR 默认 ~/nexo-vfs；Docker 内对应 docker-compose 挂载的
# /home/app/nexo-vfs。缺失时首次上传会抛清晰报错。
NEXO_VFS_DIR = os.getenv("NEXO_VFS_DIR", str(Path.home() / "nexo-vfs"))
