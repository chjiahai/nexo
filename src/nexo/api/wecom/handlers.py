"""WeCom SDK event wiring + connection lifecycle.

Bridges the aibot WebSocket client to the application layer. The bot is now a
THIN process: it replies to the user (text via the inline LLM stream, media via
a short operator-configurable ack) and writes every message as an "intent" to
the local outbox. The heavy work — uploading media to OBS and publishing the
rich event to NATS — is done by the separate `nexo drain` process, so a crash
anywhere downstream is recoverable from the outbox (no orphan objects).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aibot import WSClient, WSClientOptions, generate_req_id

from nexo import outbox
from nexo.app import handle_text
from nexo.config import (
    DEBUG_FRAMES,
    MEDIA_ACK_TEXT,
    NEXO_ORG_ID,
    NEXO_STAGING_DIR,
    WECHAT_BOT_ID,
    WECHAT_BOT_SECRET,
    WECOM_REQUEST_TIMEOUT_MS,
)
from nexo.errors import retry
from nexo.media import sniff_file_ext
from nexo.observability import flush, start_heartbeat_loop, trace_span, trace_turn
from nexo.prompts import CHAT_SYSTEM_PROMPT, CHAT_SYSTEM_PROMPT_VERSION, msg

from nexo.api.wecom.frames import (
    _dump_frame,
    _media_aeskey,
    _media_field,
    _session_id_from_frame,
    _user_id,
    _user_text,
)
from nexo.api.wecom.streaming import _reply_streamed

logger = logging.getLogger("nexo.wecom")

_WELCOME_TEXT = msg("welcome")


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

    @ws_client.on("message")
    async def _on_message(frame: dict[str, Any]) -> None:
        """Catch-all router for msgtypes the SDK doesn't dispatch as typed events.

        The aibot SDK emits `message` for every inbound frame but only fires
        typed events (message.text/file/image/mixed/voice) for its 5 known
        msgtypes — others (video, ...) are dropped at a debug log and never
        reach a typed listener. This router catches the rest. Typed msgtypes
        are skipped here to avoid double-handling (they have their own typed
        listeners above, or are intentionally unhandled).
        """
        if DEBUG_FRAMES:
            await _dump_frame(frame)

        body = frame.get("body", {}) or {}
        msgtype = body.get("msgtype", "")

        if msgtype in {"text", "file", "image", "mixed", "voice"}:
            return  # handled by typed listeners (or intentionally unhandled)

        if msgtype == "video":
            await _handle_media(ws_client, frame, "video")
            return

        logger.info("Ignoring unsupported message type: %s", msgtype)

    @ws_client.on("message.text")
    async def _on_text(frame: dict[str, Any]) -> None:
        text = _user_text(frame)
        session_id = _session_id_from_frame(frame)
        with trace_turn(
            "WeCom text reply",
            session_id=session_id,
            user_id=_user_id(session_id),
            tags=["we-com", "text"],
            input=text,
        ) as root:
            logger.info("WeCom text from %s: %s", session_id, text)
            reply = await _reply_streamed(ws_client, frame, handle_text(session_id, text))
            if root is not None:
                root.update(
                    output=reply,
                    metadata={
                        "system_prompt": CHAT_SYSTEM_PROMPT,
                        "prompt_version": CHAT_SYSTEM_PROMPT_VERSION,
                    },
                )
            # Persist the turn as an outbox intent; drain publishes the rich
            # event (frame + reply_text) to NATS for archive/future subscribers.
            await outbox.enqueue_text(frame, reply, NEXO_ORG_ID, WECHAT_BOT_ID)

    @ws_client.on("message.file")
    async def _on_file(frame: dict[str, Any]) -> None:
        await _handle_media(ws_client, frame, "file")

    @ws_client.on("message.image")
    async def _on_image(frame: dict[str, Any]) -> None:
        await _handle_media(ws_client, frame, "image")

    @ws_client.on("event.enter_chat")
    async def _on_enter_chat(frame: dict[str, Any]) -> None:
        await ws_client.reply_welcome(
            frame, {"msgtype": "text", "text": {"content": _WELCOME_TEXT}}
        )


async def _handle_media(ws_client: WSClient, frame: dict[str, Any], kind: str) -> None:
    """Download media while the signed URL is fresh, stage it, enqueue, ack.

    The bot does NOT upload to OBS or publish — `nexo drain` does, from the
    staged bytes + outbox row. Staging now (URL fresh) decouples from drain
    availability: even if drain is down for hours, the bytes are on disk and
    drain uploads them on recovery. The user gets a short configurable ack.
    """
    session_id = _session_id_from_frame(frame)
    with trace_turn(
        f"WeCom {kind} processing",
        session_id=session_id,
        user_id=_user_id(session_id),
        tags=["we-com", kind],
    ):
        logger.info("WeCom %s from %s", session_id, kind)
        try:
            file_url = _media_field(frame, "url", kind)
            aes_key = _media_aeskey(frame, kind)
            if not file_url:
                await _reply_ack(ws_client, frame, "[出错] 无下载地址")
                return

            # Download is network I/O over a signed URL — a transient blip is
            # common, so retry before surfacing a failure to the user. Download
            # + AES decryption are the SDK's wire-protocol concerns.
            host = urlparse(file_url).hostname

            async def _download():
                return await ws_client.download_file(file_url, aes_key)

            with trace_span(f"download {kind}", input=None, metadata={"url_host": host}):
                data, dl_filename = await retry(_download, attempts=3, base_delay=0.5)

            staging_path = await _stage_bytes(frame, kind, data)
            filename = _resolve_filename(dl_filename, data)
            await outbox.enqueue_media(
                kind, frame, staging_path, NEXO_ORG_ID, WECHAT_BOT_ID, filename
            )
            await _reply_ack(ws_client, frame, MEDIA_ACK_TEXT)
        except Exception as exc:  # noqa: BLE001 — tell the user, don't hang the bubble
            logger.exception("WeCom %s handling failed", kind)
            await _reply_ack(ws_client, frame, f"[出错] {exc}")


def _safe_segment(value: str) -> str:
    """Scrub a string into a path-safe staging filename segment."""
    return value.replace("/", "_").replace(":", "_").strip() or "unknown"


def _resolve_filename(dl_filename: str | None, data: bytes) -> str | None:
    """Pick the filename to persist with the media intent.

    Prefer the SDK-reported name (parsed from the download's
    Content-Disposition). WeCom `file` frames carry no filename, so when the
    SDK didn't get one either, sniff the type from the downloaded bytes so the
    OBS object still gets a correct extension + content-type. None if unknown.
    """
    if dl_filename:
        return dl_filename
    ext = sniff_file_ext(data)
    return f"file.{ext}" if ext else None


async def _stage_bytes(frame: dict[str, Any], kind: str, data: bytes) -> str:
    """Write downloaded media bytes to the staging dir; returns the abs path.

    The filename only needs to be unique and traceable — drain derives the
    deterministic OBS key from the frame's msg_id, not from this filename.
    """
    body = frame.get("body", {}) or {}
    headers = frame.get("headers", {}) or {}
    msg_id = str(body.get("msgid") or headers.get("req_id") or "unknown")
    path = Path(NEXO_STAGING_DIR) / f"{_safe_segment(msg_id)}-{kind}"

    def _do() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    await asyncio.to_thread(_do)
    return str(path)


async def _reply_ack(ws_client: WSClient, frame: dict[str, Any], text: str) -> None:
    """Send a single finished bubble (the short media ack / error)."""
    stream_id = generate_req_id("stream")
    try:
        await ws_client.reply_stream(frame, stream_id, text, finish=True)
    except Exception:  # noqa: BLE001 — must not let an ack failure crash the handler
        logger.exception("Failed to send media ack to WeCom")


def build_client() -> WSClient:
    """Build a configured, handler-wired WSClient. Raises if creds missing."""
    if not WECHAT_BOT_ID or not WECHAT_BOT_SECRET:
        raise RuntimeError(
            "WeCom credentials missing: set WECHAT_BOT_ID and "
            "WECHAT_BOT_SECRET in your .env"
        )

    ws_client = WSClient(
        WSClientOptions(
            bot_id=WECHAT_BOT_ID,
            secret=WECHAT_BOT_SECRET,
            request_timeout=WECOM_REQUEST_TIMEOUT_MS,
        )
    )
    register_handlers(ws_client)
    return ws_client


async def run() -> None:
    """Connect to WeCom and serve until interrupted."""
    # Logging + Langfuse tracing are configured by `nexo.observability.configure()`
    # (called from the CLI) — no basicConfig here.
    await outbox.init()
    Path(NEXO_STAGING_DIR).mkdir(parents=True, exist_ok=True)
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
        flush()  # ship buffered Langfuse traces before the process exits
