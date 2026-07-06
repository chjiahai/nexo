"""WeCom (企业微信) AI bot transport adapter.

The wecom-aibot-python-sdk is a WebSocket *client* that connects out to
wss://openws.work.weixin.qq.com. This module owns that connection and
bridges it to the application layer, dispatching by WeCom message type
(deterministic routing — no router agent):

    message.text ──> app.handle_text  ──> chat_agent (streamed)
    message.file ──> app.handle_file  ──> documents pipeline (download+parse+summarize)

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

import logfire
from aibot import WSClient, WSClientOptions, generate_req_id

from nexo.app import handle_file, handle_text
from nexo.config import WECHAT_BOT_ID, WECHAT_BOT_SECRET
from nexo.prompts import msg

logger = logging.getLogger("nexo.wecom")

# Flush buffered tokens either when they reach this size, or after this delay
# (seconds) — whichever comes first. The SDK acks every reply_stream call
# serially per req_id, so sending one frame per token would be slow.
_FLUSH_BYTES = 64
_FLUSH_INTERVAL = 0.4

_WELCOME_TEXT = msg("welcome")


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


def _file_url_from_frame(frame: dict[str, Any]) -> str:
    """Extract the download URL from a `message.file` frame.

    The WeCom file payload carries the link under `body.file.url` (confirmed
    shape in tests/test_wecom_bridge.py). Returns "" if absent — the caller
    then surfaces a clear error to the user.
    """
    body = frame.get("body", {}) or {}
    file_obj = body.get("file") or {}
    url = file_obj.get("url")
    return str(url) if url else ""


def _file_aeskey_from_frame(frame: dict[str, Any]) -> str:
    """Extract the AES key for decrypting a `message.file` payload.

    WeCom file URLs serve AES-256-CBC-encrypted ciphertext; the Base64 key
    rides in `body.file.aeskey`. Without it the downloaded bytes are gibberish.
    Returns "" if absent (download will then return raw, unusable bytes).
    """
    body = frame.get("body", {}) or {}
    file_obj = body.get("file") or {}
    aeskey = file_obj.get("aeskey") or file_obj.get("aes_key")
    return str(aeskey) if aeskey else ""


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
    ws_client: WSClient,
    frame: dict[str, Any],
    chunks: AsyncIterator[str],
    *,
    accumulate: bool = True,
) -> None:
    """Stream an async generator of reply chunks back to WeCom.

    WeCom's stream model REPLACES the bubble content on each refresh (it does
    not append) — every frame carries the FULL text the bubble should show.

    Two modes:
    - accumulate=True (chat): each frame carries the GROWING accumulated text,
      so the streamed reply reads like a growing prefix. The finish frame carries
      the full reply.
    - accumulate=False (file pipeline): each chunk REPLACES the previous one, so
      the staged progress messages ("正在下载…" -> "正在解析…" -> "正在生成摘要…")
      show transiently and are wiped when the next stage appears. The final
      summary chunk replaces all progress, exactly like a chat reply.

    On error, send a finish frame preserving whatever was last shown.
    """
    stream_id = generate_req_id("stream")

    async def _send(content: str, finish: bool) -> None:
        await ws_client.reply_stream(frame, stream_id, content, finish=finish)

    # `last_content` is whatever the bubble currently shows — used both to skip
    # redundant frames and to preserve partial text on error.
    last_content = ""
    full: list[str] = []

    try:
        await _send(msg("thinking"), finish=False)
        last_content = msg("thinking")

        if accumulate:
            pending = 0  # bytes accumulated since the last flush
            async for chunk in chunks:
                full.append(chunk)
                pending += len(chunk)
                if pending >= _FLUSH_BYTES:
                    text = "".join(full)
                    if text != last_content:
                        await _send(text, finish=False)
                        last_content = text
                    pending = 0
            last_content = "".join(full) or msg("no_reply")
        else:
            # Each chunk stands alone as the full bubble; WeCom replaces, so the
            # previous stage vanishes. The final chunk is re-sent as the finish
            # frame below (one redundant frame, harmless).
            async for chunk in chunks:
                if chunk and chunk != last_content:
                    await _send(chunk, finish=False)
                    last_content = chunk

        # Finish frame must carry the final text (an empty finish frame would
        # wipe the bubble).
        await _send(last_content or msg("no_reply"), finish=True)

    except Exception as exc:  # noqa: BLE001 — must not leave the bubble hanging
        logger.exception("Error while handling WeCom message")
        try:
            # Preserve whatever was generated: in accumulate mode that's the full
            # joined text (including sub-threshold bytes never flushed); in
            # replace mode it's the last stage shown.
            partial = "".join(full) if accumulate else last_content
            err_text = (
                f"{partial}\n\n[出错] {exc}".strip()
                if partial
                else f"[出错] {exc}"
            )
            await _send(err_text, finish=True)
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
        # One span covers receipt -> agent run -> LLM call so the whole turn
        # lands in a single trace (the "WeCom text from..." log and the
        # pydantic-ai agent span become children of this span).
        with logfire.span("WeCom text reply", session_id=session_id, text=text):
            logger.info("WeCom text from %s: %s", session_id, text)
            await _reply_streamed(ws_client, frame, handle_text(session_id, text))

    @ws_client.on("message.file")
    async def _on_file(frame: dict[str, Any]) -> None:
        session_id = _session_id_from_frame(frame)
        filename = _filename_from_frame(frame)
        file_url = _file_url_from_frame(frame)
        aes_key = _file_aeskey_from_frame(frame)
        with logfire.span("WeCom file processing", session_id=session_id, filename=filename):
            logger.info("WeCom file from %s: %s", session_id, filename)
            await _reply_streamed(
                ws_client, frame,
                _stream_file(ws_client, session_id, filename, file_url, aes_key),
                accumulate=False,
            )

    @ws_client.on("message.image")
    async def _on_image(frame: dict[str, Any]) -> None:
        # Images arrive as msgtype=image with image.{url,aeskey} (same encrypted
        # shape as files), but the pipeline has no OCR — acknowledge clearly
        # instead of silently dropping the message.
        session_id = _session_id_from_frame(frame)
        logger.info("WeCom image from %s (unsupported, no OCR)", session_id)

        async def _reply() -> AsyncIterator[str]:
            yield msg("image_not_supported")

        await _reply_streamed(ws_client, frame, _reply())

    @ws_client.on("event.enter_chat")
    async def _on_enter_chat(frame: dict[str, Any]) -> None:
        await ws_client.reply_welcome(
            frame, {"msgtype": "text", "text": {"content": _WELCOME_TEXT}}
        )


async def _stream_file(
    ws_client: WSClient,
    session_id: str,
    filename: str,
    file_url: str,
    aes_key: str,
) -> AsyncIterator[str]:
    """Download + decrypt the file via the SDK, then delegate to the app layer.

    Download and AES decryption are SDK wire-protocol concerns, so they live in
    this transport adapter; the app/pipeline layer only ever sees plaintext
    bytes. Yields progress chunks for the WeCom bubble.
    """
    if not file_url:
        yield msg("file_empty_url")
        return

    yield msg("file_downloading")
    try:
        file_data, real_name = await ws_client.download_file(file_url, aes_key)
    except Exception as exc:  # noqa: BLE001 — tell the user, don't hang the bubble
        yield msg("file_download_failed", error=exc)
        return

    # Prefer the SDK-provided filename (Content-Disposition); the frame filename
    # is often just a WeCom-assigned hash.
    name = real_name or filename or "unnamed"
    async for chunk in handle_file(session_id, name, file_data):
        yield chunk


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
    # Logging is configured by `nexo.observability.configure()` (called from
    # the CLI), which bridges stdlib logging into logfire/OTel — no basicConfig here.
    from nexo.observability import start_heartbeat_loop

    ws_client = build_client()
    heartbeat = start_heartbeat_loop(lambda: ws_client.is_connected)
    try:
        await ws_client.connect()
        # The SDK runs its receive loop as a background task on the running
        # loop; block here until stopped (Ctrl+C / signal).
        await asyncio.Event().wait()
    finally:
        heartbeat.cancel()
        ws_client.disconnect()
