"""WeCom (企业微信) AI bot transport adapter.

The wecom-aibot-python-sdk is a WebSocket *client* that connects out to
wss://openws.work.weixin.qq.com. This module owns that connection and
bridges it to the application layer, dispatching by WeCom message type
(deterministic routing — no router agent):

    message.text ──> app.handle_text  ──> chat_agent (streamed)
    message.file ──> app.handle_file  ──> ingest pipeline (placeholder)

Each handler streams the reply back via `reply_stream` (finish=False per
chunk, finish=True at end). This is a transport adapter in the API layer —
it knows about the SDK wire protocol and delegates all agent/session logic
to `nexo.app`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from aibot import WSClient, WSClientOptions, generate_req_id

from nexo.app import handle_file, handle_text
from nexo.config import WECHAT_BOT_ID, WECHAT_BOT_SECRET

logger = logging.getLogger("nexo.wecom")

# Flush buffered tokens either when they reach this size, or after this delay
# (seconds) — whichever comes first. The SDK acks every reply_stream call
# serially per req_id, so sending one frame per token would be slow.
_FLUSH_BYTES = 64
_FLUSH_INTERVAL = 0.4

_WELCOME_TEXT = "您好！我是 Nexo 智能助手，有什么可以帮您的吗？"


def _user_text(frame: dict[str, Any]) -> str:
    """Extract the user's text content from a `message.text` frame."""
    return frame.get("body", {}).get("text", {}).get("content", "")


def _filename_from_frame(frame: dict[str, Any]) -> str:
    """Best-effort filename from a `message.file` frame.

    Exact body.file field is confirmed at runtime (like the session id); we
    try the obvious keys and fall back to the msgid.
    """
    body = frame.get("body", {}) or {}
    file_obj = body.get("file") or {}
    for key in ("filename", "name", "file_name"):
        val = file_obj.get(key)
        if val:
            return str(val)
    return body.get("msgid", "unknown-file")


def _session_id_from_frame(frame: dict[str, Any]) -> str:
    """Derive a stable session id from an inbound frame.

    Confirmed frame shape (single chat):
        body = {chattype: "single", from: {userid: "..."}, ...}
    Group chat uses a top-level body.chatid. We branch on `chattype` first,
    then fall back to a defensive key scan, then to req_id so multi-turn
    still works even if the shape is unexpected.
    """
    body = frame.get("body", {}) or {}
    chattype = body.get("chattype", "")

    # Single chat -> the user (body.from.userid).
    if chattype == "single":
        userid = (body.get("from") or {}).get("userid")
        if userid:
            return f"wecom:{userid}"

    # Group chat -> the conversation (body.chatid).
    if chattype == "group":
        chatid = body.get("chatid")
        if chatid:
            return f"wecom:{chatid}"

    # Defensive scan for unexpected shapes.
    from_obj = body.get("from") or {}
    for key in ("chatid", "userid"):
        val = body.get(key) or from_obj.get(key)
        if val:
            return f"wecom:{val}"

    headers = frame.get("headers", {}) or {}
    fallback = headers.get("req_id", "unknown")
    logger.warning(
        "No conversation id found in frame (chattype=%s, body keys=%s); "
        "falling back to req_id %s",
        chattype, list(body.keys()), fallback,
    )
    return f"wecom:{fallback}"


async def _reply_streamed(
    ws_client: WSClient, frame: dict[str, Any], chunks: AsyncIterator[str]
) -> None:
    """Stream an async generator of reply chunks back to WeCom.

    WeCom's stream model REPLACES the bubble content on each refresh (it does
    not append) — see the long-connection doc's 流式消息回复机制. So every
    frame carries the FULL accumulated text so far, and the finish frame
    carries the full final text (an empty finish frame would clear the
    bubble). On error, send a finish frame preserving any partial text.
    """
    stream_id = generate_req_id("stream")

    async def _send(content: str, finish: bool) -> None:
        await ws_client.reply_stream(frame, stream_id, content, finish=finish)

    full: list[str] = []

    try:
        await _send("正在思考…", finish=False)
        last_sent = "正在思考…"
        pending = 0  # bytes accumulated since the last flush

        async for chunk in chunks:
            full.append(chunk)
            pending += len(chunk)
            if pending >= _FLUSH_BYTES:
                text = "".join(full)
                if text != last_sent:
                    await _send(text, finish=False)
                    last_sent = text
                pending = 0

        # Finish frame: repeat the full text (WeCom replaces on refresh, so an
        # empty content here would wipe the bubble).
        final_text = "".join(full) or "（无回复）"
        await _send(final_text, finish=True)

    except Exception as exc:  # noqa: BLE001 — must not leave the bubble hanging
        logger.exception("Error while handling WeCom message")
        try:
            partial = "".join(full)
            msg = f"{partial}\n\n[出错] {exc}".strip() if partial else f"[出错] {exc}"
            await _send(msg, finish=True)
        except Exception:
            logger.exception("Failed to send error frame to WeCom")


def register_handlers(ws_client: WSClient) -> None:
    """Wire WeCom SDK events to nexo's application layer."""

    @ws_client.on("connected")
    def _on_connected() -> None:
        logger.info("WeCom WebSocket connected")

    @ws_client.on("authenticated")
    def _on_authenticated() -> None:
        logger.info("WeCom authenticated — bot is live")

    @ws_client.on("disconnected")
    def _on_disconnected(reason: str) -> None:
        logger.warning("WeCom disconnected: %s", reason)

    @ws_client.on("reconnecting")
    def _on_reconnecting(attempt: int) -> None:
        logger.info("WeCom reconnecting (attempt %s)", attempt)

    @ws_client.on("error")
    def _on_error(error: Exception) -> None:
        logger.error("WeCom error: %s", error)

    @ws_client.on("message.text")
    async def _on_text(frame: dict[str, Any]) -> None:
        text = _user_text(frame)
        session_id = _session_id_from_frame(frame)
        logger.info("WeCom text from %s: %s", session_id, text)
        await _reply_streamed(ws_client, frame, handle_text(session_id, text))

    @ws_client.on("message.file")
    async def _on_file(frame: dict[str, Any]) -> None:
        session_id = _session_id_from_frame(frame)
        filename = _filename_from_frame(frame)
        logger.info("WeCom file from %s: %s", session_id, filename)
        # File download is deferred until the documents/ + memory/ pipeline
        # exists; handle_file currently just acknowledges receipt.
        await _reply_streamed(ws_client, frame, handle_file(session_id, filename))

    @ws_client.on("event.enter_chat")
    async def _on_enter_chat(frame: dict[str, Any]) -> None:
        await ws_client.reply_welcome(
            frame, {"msgtype": "text", "text": {"content": _WELCOME_TEXT}}
        )


def build_client() -> WSClient:
    """Build a configured, handler-wired WSClient. Raises if creds missing."""
    if not WECHAT_BOT_ID or not WECHAT_BOT_SECRET:
        raise RuntimeError(
            "WeCom credentials missing: set WECHAT_BOT_ID and "
            "WECHAT_BOT_SECRET in your .env"
        )

    ws_client = WSClient(
        WSClientOptions(bot_id=WECHAT_BOT_ID, secret=WECHAT_BOT_SECRET)
    )
    register_handlers(ws_client)
    return ws_client


async def run() -> None:
    """Connect to WeCom and serve until interrupted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    ws_client = build_client()
    try:
        await ws_client.connect()
        # The SDK runs its receive loop as a background task on the running
        # loop; block here until stopped (Ctrl+C / signal).
        await asyncio.Event().wait()
    finally:
        ws_client.disconnect()
