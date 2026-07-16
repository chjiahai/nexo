"""WeCom (企业微信) AI bot transport adapter.

The wecom-aibot-python-sdk is a WebSocket *client* that connects out to
wss://openws.work.weixin.qq.com. This module owns that connection and
bridges it to the application layer, dispatching by WeCom message type
(deterministic routing — no router agent):

    message.text  ──> app.handle_text   ──> chat_agent (streamed)
    message.file  ──> app.handle_file   ──> TOS storage + ack (download+decrypt here)
    message.image ──> app.handle_image  ──> TOS storage + ack (download+decrypt here)

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
from urllib.parse import urlparse

from aibot import WSClient, WSClientOptions, generate_req_id

from nexo.app import handle_file, handle_image, handle_text
from nexo.config import WECHAT_BOT_ID, WECHAT_BOT_SECRET, WECOM_REQUEST_TIMEOUT_MS
from nexo.observability import flush, start_heartbeat_loop, trace_span, trace_turn
from nexo.prompts import CHAT_SYSTEM_PROMPT, CHAT_SYSTEM_PROMPT_VERSION, msg

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


def _media_field(frame: dict[str, Any], field: str, kind: str = "file") -> str:
    """Extract a single field from a media payload in the frame.

    Files carry their payload under `body.file`; images under `body.image`
    (same encrypted shape — url + aeskey). `field` is the key inside that
    payload (e.g. "url", "filename"). Returns "" if absent — the caller then
    surfaces a clear error to the user.
    """
    body = frame.get("body", {}) or {}
    payload = body.get(kind) or {}
    val = payload.get(field)
    return str(val) if val else ""


def _media_aeskey(frame: dict[str, Any], kind: str = "file") -> str:
    """Extract the AES key for decrypting a media payload (file or image).

    WeCom media URLs serve AES-256-CBC-encrypted ciphertext; the Base64 key
    rides in `body.<kind>.aeskey` (some payloads spell it `aes_key`). Without
    it the downloaded bytes are gibberish. Returns "" if absent.
    """
    body = frame.get("body", {}) or {}
    payload = body.get(kind) or {}
    aeskey = payload.get("aeskey") or payload.get("aes_key")
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
) -> str:
    """Stream an async generator of reply chunks back to WeCom.

    WeCom's stream model REPLACES the bubble content on each refresh (it does
    not append) — every frame carries the FULL text the bubble should show.

    Two modes:
    - accumulate=True (chat): each frame carries the GROWING accumulated text,
      so the streamed reply reads like a growing prefix. The finish frame carries
      the full reply.
    - accumulate=False (file/image route): each chunk REPLACES the previous one,
      so the staged progress messages ("正在下载…" -> "正在保存…") show transiently
      and are wiped when the next stage appears. The final chunk replaces all
      progress, exactly like a chat reply.

    On error, send a finish frame preserving whatever was last shown. Returns
    the final text sent to the bubble (the full reply, or the error text) so
    the caller can attach it as the trace root's output.
    """
    stream_id = generate_req_id("stream")

    async def _send(content: str, finish: bool) -> None:
        await ws_client.reply_stream(frame, stream_id, content, finish=finish)

    # `last_content` is whatever the bubble currently shows — used both to skip
    # redundant frames and to preserve partial text on error.
    last_content = ""
    full: list[str] = []
    final_text = ""

    try:
        await _send(msg("thinking"), finish=False)
        last_content = msg("thinking")

        if accumulate:
            pending = 0  # bytes accumulated since the last flush
            queue: asyncio.Queue = asyncio.Queue()
            eos = object()  # end-of-stream sentinel pushed by the pump

            async def _pump() -> None:
                # Own the chunks generator's lifetime so a slow producer is never
                # cancelled: timeouts fire on queue.get() (safe to cancel) rather
                # than on __anext__ — cancelling __anext__ closes the async
                # generator and loses the rest of the stream.
                try:
                    async for chunk in chunks:
                        await queue.put(chunk)
                except Exception as exc:  # surface producer errors to the consumer
                    await queue.put(exc)
                finally:
                    await queue.put(eos)

            pump = asyncio.create_task(_pump())
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(
                            queue.get(), timeout=_FLUSH_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        # Time-based flush: surface accumulated bytes before they
                        # reach _FLUSH_BYTES, so a slow-streaming model doesn't
                        # leave the bubble stuck on the previous frame.
                        if pending > 0:
                            text = "".join(full)
                            if text != last_content:
                                await _send(text, finish=False)
                                last_content = text
                            pending = 0
                        continue
                    if item is eos:
                        break
                    if isinstance(item, Exception):
                        raise item
                    full.append(item)
                    pending += len(item)
                    if pending >= _FLUSH_BYTES:
                        text = "".join(full)
                        if text != last_content:
                            await _send(text, finish=False)
                            last_content = text
                        pending = 0
            finally:
                # If the consumer exited early (e.g. _send failed), stop the pump
                # so it doesn't leak; otherwise it has already finished. Swallow
                # whatever the cancelled pump raises so it can't mask the
                # exception already propagating from the consumer.
                if not pump.done():
                    pump.cancel()
                    try:
                        await pump
                    except BaseException:  # noqa: BLE001 — see comment above
                        pass
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
        final_text = last_content or msg("no_reply")
        await _send(final_text, finish=True)

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
            final_text = err_text
        except Exception:
            logger.exception("Failed to send error frame to WeCom")

    return final_text


def _user_id(session_id: str) -> str:
    """Strip the `wecom:` channel prefix to get the user/conversation id."""
    return session_id.split(":", 1)[1] if session_id.startswith("wecom:") else session_id


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
                _stream_file(ws_client, session_id, filename, file_url, aes_key),
                accumulate=False,
            )

    @ws_client.on("message.image")
    async def _on_image(frame: dict[str, Any]) -> None:
        # Images arrive as msgtype=image with image.{url,aeskey} (same encrypted
        # shape as files). Download via the SDK, persist to TOS, acknowledge —
        # no OCR/vision yet, but storage is the first step toward multimodal.
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
                _stream_image(ws_client, session_id, file_url, aes_key),
                accumulate=False,
            )

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
    this transport adapter; the app/storage layer only ever sees plaintext
    bytes. Yields progress chunks for the WeCom bubble.
    """
    if not file_url:
        yield msg("file_empty_url")
        return

    yield msg("file_downloading")
    try:
        # Record only the URL host (not the signed path) to diagnose reachability.
        host = urlparse(file_url).hostname
        with trace_span("download file", input=filename, metadata={"url_host": host}):
            file_data, real_name = await ws_client.download_file(file_url, aes_key)
    except Exception as exc:  # noqa: BLE001 — tell the user, don't hang the bubble
        yield msg("file_download_failed", error=exc)
        return

    # Prefer the SDK-provided filename (Content-Disposition); the frame filename
    # is often just a WeCom-assigned hash.
    name = real_name or filename or "unnamed"
    async for chunk in handle_file(session_id, name, file_data):
        yield chunk


async def _stream_image(
    ws_client: WSClient,
    session_id: str,
    file_url: str,
    aes_key: str,
) -> AsyncIterator[str]:
    """Download + decrypt an image via the SDK, then delegate to the app layer.

    Mirrors `_stream_file`: download and AES decryption are SDK wire-protocol
    concerns, so they live in this transport adapter; the app/storage layer
    only ever sees plaintext bytes. The download returns `(data, real_name)`;
    for images the name is unused. Yields progress chunks for the WeCom bubble.
    """
    if not file_url:
        yield msg("image_empty_url")
        return

    yield msg("image_downloading")
    try:
        host = urlparse(file_url).hostname
        with trace_span("download image", metadata={"url_host": host}):
            image_data, _ = await ws_client.download_file(file_url, aes_key)
    except Exception as exc:  # noqa: BLE001 — tell the user, don't hang the bubble
        yield msg("image_download_failed", error=exc)
        return

    async for chunk in handle_image(session_id, image_data):
        yield chunk


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
