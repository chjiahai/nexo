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
# User uploads are NOT written here — they are scp'd to a remote folder
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

# WeCom file/image download HTTP timeout (milliseconds). The aibot SDK defaults
# to 10000 (10s), which is too tight for slow links or larger uploads — the
# download is a direct aiohttp GET that ignores HTTP(S)_PROXY. Raise to 60s.
WECOM_REQUEST_TIMEOUT_MS = int(os.getenv("WECOM_REQUEST_TIMEOUT_MS", "60000"))

# --- 远程上传目标（scp 投递用户上传的文件/图片/视频）------------------------
# nexo 通过 scripts/ship_media.sh 用 scp 把收到的媒体发到这台远程机器的
# 指定文件夹。三个必填 —— 首次上传缺失会抛清晰报错。
# 私钥需挂载进容器（见 docker-compose.yml）且对 appuser 可读（chmod 600），
# 远程需 `ssh-copy-id` 配好免密登录。
NEXO_UPLOAD_HOST = os.getenv("NEXO_UPLOAD_HOST", "")
NEXO_UPLOAD_USER = os.getenv("NEXO_UPLOAD_USER", "")
NEXO_UPLOAD_DIR = os.getenv("NEXO_UPLOAD_DIR", "")  # 远程绝对路径，如 /data/nexo-uploads
NEXO_UPLOAD_KEY = os.getenv("NEXO_UPLOAD_SSH_KEY", "")  # 容器内私钥路径，可空
NEXO_UPLOAD_PORT = os.getenv("NEXO_UPLOAD_SSH_PORT", "")  # 可空，默认 22
