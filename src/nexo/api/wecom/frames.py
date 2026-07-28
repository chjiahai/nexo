"""Frame-parsing helpers for the WeCom transport adapter.

Pure (or near-pure) functions that extract fields from inbound WeCom frames,
plus the session-id derivation and the best-effort debug-frame capture. Kept
separate from the streaming protocol and connection lifecycle so they can be
unit-tested in isolation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from nexo.config import DEBUG_FRAMES_PATH

logger = logging.getLogger("nexo.wecom")


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


def _user_id(session_id: str) -> str:
    """Strip the `wecom:` channel prefix to get the user/conversation id."""
    return session_id.split(":", 1)[1] if session_id.startswith("wecom:") else session_id


async def _dump_frame(frame: dict[str, Any]) -> None:
    """Append one inbound frame's body as a JSON line to DEBUG_FRAMES_PATH.

    Used to discover payload shapes for message types the SDK doesn't model.
    File I/O is offloaded to a thread so the event loop isn't blocked. Errors
    are logged but never raised — capture must not break message handling.
    """
    body = frame.get("body", {}) or {}
    msgtype = body.get("msgtype", "")
    line = json.dumps(body, ensure_ascii=False)

    def _write() -> None:
        DEBUG_FRAMES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_FRAMES_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    try:
        await asyncio.to_thread(_write)
        logger.info("Debug frame captured [%s]", msgtype)
    except Exception:  # noqa: BLE001 — capture is best-effort
        logger.exception("Failed to write debug frame for msgtype=%s", msgtype)
