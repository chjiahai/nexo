"""WeCom SDK event wiring + connection lifecycle.

Bridges the aibot WebSocket client to the application layer: dispatches by
WeCom message type, streams replies back via the streaming protocol, downloads
+ AES-decrypts media (SDK wire-protocol concerns), and owns connect/disconnect
plus the liveness heartbeat.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

from aibot import WSClient, WSClientOptions

from nexo.app import handle_media, handle_text
from nexo.config import (
    DEBUG_FRAMES,
    WECHAT_BOT_ID,
    WECHAT_BOT_SECRET,
    WECOM_REQUEST_TIMEOUT_MS,
)
from nexo.errors import retry
from nexo.media import ROUTES, MediaRoute
from nexo.observability import flush, start_heartbeat_loop, trace_span, trace_turn
from nexo.prompts import CHAT_SYSTEM_PROMPT, CHAT_SYSTEM_PROMPT_VERSION, msg

from nexo.api.wecom.frames import (
    _dump_frame,
    _filename_from_frame,
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
        body = frame.get("body", {}) or {}
        msgtype = body.get("msgtype", "")

        if DEBUG_FRAMES:
            await _dump_frame(frame)

        if msgtype in {"text", "file", "image", "mixed", "voice"}:
            return  # handled by typed listeners (or intentionally unhandled)

        if msgtype == "video":
            await _handle_video_frame(ws_client, frame)
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
                # Surface the prompt + version and the final reply at the trace
                # root (the prompt is also captured inside the generation span).
                root.update(
                    output=reply,
                    metadata={
                        "system_prompt": CHAT_SYSTEM_PROMPT,
                        "prompt_version": CHAT_SYSTEM_PROMPT_VERSION,
                    },
                )

    @ws_client.on("message.file")
    async def _on_file(frame: dict[str, Any]) -> None:
        session_id = _session_id_from_frame(frame)
        filename = _filename_from_frame(frame)
        file_url = _media_field(frame, "url", "file")
        aes_key = _media_aeskey(frame, "file")
        with trace_turn(
            "WeCom file processing",
            session_id=session_id,
            user_id=_user_id(session_id),
            tags=["we-com", "file"],
            input=filename,
        ):
            logger.info("WeCom file from %s: %s", session_id, filename)
            await _reply_streamed(
                ws_client, frame,
                _stream_media(ws_client, session_id, ROUTES["file"], file_url, aes_key, filename),
                accumulate=False,
            )

    @ws_client.on("message.image")
    async def _on_image(frame: dict[str, Any]) -> None:
        # Images arrive as msgtype=image with image.{url,aeskey} (same encrypted
        # shape as files). Download via the SDK, ship to the remote folder,
        # acknowledge — no OCR/vision yet, but storage is the first step toward
        # multimodal.
        session_id = _session_id_from_frame(frame)
        file_url = _media_field(frame, "url", "image")
        aes_key = _media_aeskey(frame, "image")
        with trace_turn(
            "WeCom image processing",
            session_id=session_id,
            user_id=_user_id(session_id),
            tags=["we-com", "image"],
        ):
            logger.info("WeCom image from %s", session_id)
            await _reply_streamed(
                ws_client, frame,
                _stream_media(ws_client, session_id, ROUTES["image"], file_url, aes_key),
                accumulate=False,
            )

    @ws_client.on("event.enter_chat")
    async def _on_enter_chat(frame: dict[str, Any]) -> None:
        await ws_client.reply_welcome(
            frame, {"msgtype": "text", "text": {"content": _WELCOME_TEXT}}
        )


async def _stream_media(
    ws_client: WSClient,
    session_id: str,
    route: MediaRoute,
    file_url: str,
    aes_key: str,
    filename: str | None = None,
) -> AsyncIterator[str]:
    """Download + decrypt a media payload via the SDK, then delegate to the app layer.

    Download and AES decryption are SDK wire-protocol concerns, so they live in
    this transport adapter; the app/storage layer only ever sees plaintext bytes.
    Everything that varies by media type (progress strings, default filename,
    the app-layer route) rides on `route`. Yields progress chunks for the bubble.
    """
    if not file_url:
        yield msg(route.empty_url)
        return

    yield msg(route.downloading)
    try:
        # Record only the URL host (not the signed path) to diagnose reachability.
        host = urlparse(file_url).hostname
        with trace_span(f"download {route.kind}", input=filename, metadata={"url_host": host}):
            # Download is network I/O over a signed URL — a transient blip is
            # common, so retry before surfacing a failure to the user.
            async def _download():
                return await ws_client.download_file(file_url, aes_key)

            data, real_name = await retry(_download, attempts=3, base_delay=0.5)
    except Exception as exc:  # noqa: BLE001 — tell the user, don't hang the bubble
        yield msg(route.download_failed, error=exc)
        return

    # Prefer the SDK-provided filename (Content-Disposition); the frame filename
    # is often just a WeCom-assigned hash. Fall back to the route's default.
    name = real_name or filename or route.default_name
    async for chunk in handle_media(session_id, route, name, data):
        yield chunk


async def _handle_video_frame(ws_client: WSClient, frame: dict[str, Any]) -> None:
    """Route a `video` frame: download -> remote folder -> acknowledge (mirrors _on_file).

    Video arrives via the catch-all `message` router (the SDK has no
    `message.video` typed event), not a dedicated listener.
    """
    session_id = _session_id_from_frame(frame)
    file_url = _media_field(frame, "url", "video")
    aes_key = _media_aeskey(frame, "video")
    with trace_turn(
        "WeCom video processing",
        session_id=session_id,
        user_id=_user_id(session_id),
        tags=["we-com", "video"],
    ):
        logger.info("WeCom video from %s", session_id)
        await _reply_streamed(
            ws_client, frame,
            _stream_media(ws_client, session_id, ROUTES["video"], file_url, aes_key),
            accumulate=False,
        )


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
