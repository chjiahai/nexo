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

# --- 用户上传落盘（华为云 OBS 对象存储）--------------------------------------
# 收到的文件/图片/视频由 `nexo drain` 进程上传到华为云 OBS 桶。对象 key 为确定性
# 派生（<org>/<user>/<msg_id>-<name>），保证 drain 崩溃重放时幂等（同 key 同对象）。
# OBS_ENDPOINT 为区域端点（如 obs.cn-south-1.myhuaweicloud.com），不含 bucket 名。
# 缺失时 drain 首次上传会抛清晰报错。
OBS_ACCESS_KEY_ID = os.getenv("OBS_ACCESS_KEY_ID", "")
OBS_SECRET_ACCESS_KEY = os.getenv("OBS_SECRET_ACCESS_KEY", "")
OBS_ENDPOINT = os.getenv("OBS_ENDPOINT", "")
OBS_BUCKET = os.getenv("OBS_BUCKET", "")

# --- 暂存目录 + outbox（bot→drain 的本地持久化交接）--------------------------
# bot 收到媒体后立即下载（WeCom 签名 URL 新鲜）写入暂存目录，并把"意图"写进
# SQLite outbox；`nexo drain` 从 outbox 读出暂存路径、上传 OBS、publish 富事件、
# 删暂存。加固模式：URL 过期前已落盘字节，drain 宕机也不丢媒体。
# NOTE: `os.getenv(...) or <default>` — an empty value (e.g. `NEXO_OUTBOX_PATH=`
# in .env) falls back to the default, rather than becoming "" (which sqlite3
# treats as an in-memory DB, losing all rows on connection close).
NEXO_STAGING_DIR = os.getenv("NEXO_STAGING_DIR") or str(DATA_DIR / "staging")
NEXO_OUTBOX_PATH = os.getenv("NEXO_OUTBOX_PATH") or str(DATA_DIR / "outbox.db")

# --- media 简短回执文案（运维可配）------------------------------------------
# upload 离开 bot 后无进度气泡；bot 收到媒体后回这一句。text 仍走内联 LLM 流式。
MEDIA_ACK_TEXT = os.getenv("NEXO_MEDIA_ACK_TEXT", "已收到，归档中")

# --- NATS JetStream (消息可靠落盘 + 跨机分发) --------------------------------
# `nexo drain` 把"富事件"（帧 + 回复/obs_key/org/bot_id）发布到 JetStream 流；
# `nexo archive` 消费者拉取并写入 MySQL。drain 用 js.publish（不走 $JS.API）可连
# 本地 leaf（127.0.0.1:4222，见 scripts/nats/leaf.conf）；archive 用 pull_subscribe/
# fetch（走 $JS.API）须直连核心节点——无 JS 的 leaf 不透传 $JS.API。默认值适用
# bot/drain，archive 主机请改填核心节点（如 nats://10.13.11.7:4222）。
# NATS_STREAM / NATS_SUBJECT_PREFIX 需与 `nats stream create` 时一致。
# NATS_ARCHIVE_DURABLE 为 archive 消费者 durable 名。
NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
NATS_STREAM = os.getenv("NATS_STREAM", "WECOM_MSG")
NATS_SUBJECT_PREFIX = os.getenv("NATS_SUBJECT_PREFIX", "nexo.wecom.msg")
NATS_ARCHIVE_DURABLE = os.getenv("NATS_ARCHIVE_DURABLE", "wxcom_msg_archive")

# --- MySQL (archive 消费者落库) ----------------------------------------------
# 仅 `nexo archive` 使用（运行在能内网连通云数据库的那台云主机上）。填云厂商
# MySQL 的内网地址。MYSQL_PASSWORD 留空则不传密码。
MYSQL_HOST = os.getenv("MYSQL_HOST", "")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "")
