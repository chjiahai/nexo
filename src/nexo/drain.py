"""`nexo drain` — outbox orchestration: upload media to OBS, publish rich events.

An independent process that reads the local SQLite outbox `nexo bot` wrote and
performs the external side-effects: upload staged media bytes to Huawei Cloud
OBS, then publish a "rich event" (frame + result) to NATS JetStream. Only drain
touches OBS and NATS-publish, so the bot stays a thin WS/reply loop.

Crash safety (the point of the design): the outbox row is persisted BEFORE any
external side-effect, and the OBS object key is deterministic. So a drain crash
at any point resumes cleanly on restart:

    pending  ──upload──▶  uploaded  ──publish──▶  done
      (re-upload is idempotent: same key → same object)
                            (re-publish is idempotent: consumer dedups on nats_seq)

No orphan objects, no reconciliation sweep. A failed step marks `error` on the
row and leaves the state for retry on the next loop (with a short backoff so a
persistent failure doesn't hot-spin).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from nexo import outbox
from nexo.api.wecom.frames import _filename_from_frame, _session_id_from_frame, _user_id
from nexo.config import NEXO_STAGING_DIR
from nexo.media import ROUTES
from nexo.messaging.publisher import EventPublisher
from nexo.observability import flush, trace_span
from nexo.storage import obs

logger = logging.getLogger("nexo.drain")

_NO_ROW_POLL = 1.0      # seconds to idle when the outbox is empty
_ERR_BACKOFF = 2.0      # seconds to back off after a failed row


def _msg_id(frame: dict[str, Any]) -> str:
    """Stable per-message id for the deterministic OBS key (msgid, fallback req_id)."""
    body = frame.get("body", {}) or {}
    mid = body.get("msgid")
    if mid:
        return str(mid)
    return str(frame.get("headers", {}).get("req_id") or "unknown")


async def _read_staging(path: str | None) -> bytes:
    if not path:
        raise RuntimeError("media outbox row has no staging_path")
    p = Path(path)

    def _do() -> bytes:
        if not p.exists():
            raise RuntimeError(f"staging file missing: {path}")
        return p.read_bytes()

    return await asyncio.to_thread(_do)


def _delete_staging(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.warning("could not delete staging file %s", path, exc_info=True)


async def _upload_media(kind: str, row: dict[str, Any], data: bytes) -> str:
    """Upload staged bytes to OBS by media kind; returns the OBS object key."""
    frame = row["frame"]
    route = ROUTES[kind]
    user_id = _user_id(_session_id_from_frame(frame))
    msg_id = _msg_id(frame)
    # Prefer the filename the bot captured (SDK-reported or sniffed); fall back
    # to the frame only for older rows that predate the `filename` column.
    name = (row.get("filename") or _filename_from_frame(frame)) if kind == "file" else None
    with trace_span(f"obs upload {kind}", input=name):
        return await getattr(obs, route.upload_attr)(user_id, msg_id, name, data)


def _build_event(
    row: dict[str, Any], *, obs_key: str | None, error: str | None
) -> dict[str, Any]:
    return {
        "frame": row["frame"],
        "reply_text": row.get("reply_text"),
        "obs_key": obs_key,
        "error": error,
        "org_id": row.get("org_id"),
        "bot_id": row.get("bot_id"),
        "filename": row.get("filename"),
    }


async def _process(row: dict[str, Any], publisher: EventPublisher) -> None:
    """Advance one outbox row to `done` (upload + publish), or raise to retry."""
    kind = row["kind"]

    if kind == "text":
        # text has no bucket step — publish directly.
        await publisher.publish(_build_event(row, obs_key=None, error=None))
        await outbox.mark_done(row["id"])
        return

    # media: ensure uploaded, then publish.
    obs_key = row.get("obs_key")
    if row["state"] == "pending":
        data = await _read_staging(row.get("staging_path"))
        obs_key = await _upload_media(kind, row, data)
        await outbox.mark_uploaded(row["id"], obs_key)

    await publisher.publish(_build_event(row, obs_key=obs_key, error=None))
    await outbox.mark_done(row["id"])
    _delete_staging(row.get("staging_path"))


async def run() -> None:
    """Drain the outbox → OBS + NATS until interrupted."""
    await outbox.init()
    Path(NEXO_STAGING_DIR).mkdir(parents=True, exist_ok=True)

    publisher = EventPublisher()
    await publisher.connect()
    logger.info("drain ready (outbox + OBS + NATS %s)", "connected")

    try:
        while True:
            row = await outbox.next_pending()
            if row is None:
                await asyncio.sleep(_NO_ROW_POLL)
                continue
            try:
                await _process(row, publisher)
                logger.debug("drained row %s (%s)", row["id"], row["kind"])
            except Exception as exc:  # noqa: BLE001 — mark + backoff, retry next loop
                logger.exception("row %s failed: %s", row["id"], exc)
                await outbox.mark_error(row["id"], str(exc))
                await asyncio.sleep(_ERR_BACKOFF)
    finally:
        await publisher.close()
        flush()  # ship buffered Langfuse traces before the process exits
