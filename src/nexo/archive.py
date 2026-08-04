"""`nexo archive` — JetStream consumer that persists rich events to MySQL.

Runs on the host with internal-network access to the cloud MySQL. Pulls every
rich event `nexo drain` published to the `WECOM_MSG` stream, extracts the key
fields into typed columns (plus the full frame as JSON), and `INSERT IGNORE`s
the row. The `(nats_stream, nats_seq)` unique key makes this idempotent:
JetStream redelivery (at-least-once) never produces duplicate rows.

Each message is acked only after the row is committed; on any failure it is
naked with a backoff so JetStream redelivers it.

A rich event is `{frame, reply_text, obs_key, error, org_id, bot_id}` — the
frame is the raw WeCom frame; the rest is the processing result drain attaches.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiomysql
import nats
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext

from nexo.api.wecom.frames import (
    _filename_from_frame,
    _media_aeskey,
    _media_field,
    _session_id_from_frame,
    _user_text,
)
from nexo.config import (
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
    NATS_ARCHIVE_DURABLE,
    NATS_STREAM,
    NATS_SUBJECT_PREFIX,
    NATS_URL,
)

logger = logging.getLogger("nexo.archive")

# 18 column values; received_at is filled by NOW(3) in the SQL itself.
# reply_at mirrors NOW(3) only when a reply exists (reply_text non-null).
_INSERT_SQL = """\
INSERT IGNORE INTO wecom_messages
    (msgid, msgtype, chattype, session_id, user_id, chat_id, content,
     media_url, media_aeskey, filename, req_id, raw_payload,
     org_id, bot_id, obs_key, reply_text, reply_at,
     nats_stream, nats_seq, received_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, CASE WHEN %s IS NULL THEN NULL ELSE NOW(3) END,
        %s, %s, NOW(3))
"""

_BATCH = 16
_FETCH_TIMEOUT = 5  # seconds; a short timeout lets the loop poll shutdown flags
_NAK_DELAY = 5  # seconds before JetStream redelivers a failed message


def _row_from_event(
    event: dict[str, Any], nats_stream: str, nats_seq: int
) -> tuple[Any, ...]:
    """Extract the typed columns from a rich event, in INSERT-SQL order."""
    frame = event.get("frame", {}) or {}
    body = frame.get("body", {}) or {}
    headers = frame.get("headers", {}) or {}
    msgtype = body.get("msgtype", "") or ""
    chattype = body.get("chattype", "") or ""
    session_id = _session_id_from_frame(frame)

    user_id = chat_id = None
    if chattype == "single":
        uid = (body.get("from") or {}).get("userid")
        user_id = str(uid) if uid else None
    elif chattype == "group":
        cid = body.get("chatid")
        chat_id = str(cid) if cid else None

    content = _user_text(frame) or None

    media_url = media_aeskey = filename = None
    if msgtype in {"file", "image", "video"}:
        media_url = _media_field(frame, "url", msgtype) or None
        media_aeskey = _media_aeskey(frame, msgtype) or None
        if msgtype == "file":
            fn = _filename_from_frame(frame)
            filename = fn if fn and fn != "unknown-file" else None

    req_id = headers.get("req_id")
    raw = json.dumps(frame, ensure_ascii=False)
    reply_text = event.get("reply_text")
    return (
        body.get("msgid"),
        msgtype,
        chattype,
        session_id,
        user_id,
        chat_id,
        content,
        media_url,
        media_aeskey,
        filename,
        req_id,
        raw,
        event.get("org_id"),
        event.get("bot_id"),
        event.get("obs_key"),
        reply_text,
        reply_text,  # CASE WHEN %s IS NULL ...
        nats_stream,
        nats_seq,
    )


async def _handle(msg: Any, pool: aiomysql.Pool) -> None:
    try:
        event = json.loads(msg.data.decode("utf-8"))
        meta = msg.metadata
        nats_stream = meta.stream
        nats_seq = meta.sequence.stream
        row = _row_from_event(event, nats_stream, nats_seq)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_INSERT_SQL, row)
        await msg.ack()
        logger.debug("Archived %s seq=%s", nats_stream, nats_seq)
    except Exception:  # noqa: BLE001 — nack so JetStream redelivers
        logger.exception("Failed to archive message; naking for redelivery")
        await msg.nak(delay=_NAK_DELAY)


async def run() -> None:
    """Consume WECOM_MSG → MySQL until interrupted."""
    if not (MYSQL_HOST and MYSQL_USER and MYSQL_DATABASE):
        raise RuntimeError(
            "MySQL config missing: set MYSQL_HOST, MYSQL_USER, MYSQL_DATABASE "
            "in your .env"
        )

    pool = await aiomysql.create_pool(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD or None,
        db=MYSQL_DATABASE,
        autocommit=True,
        minsize=1,
        maxsize=4,
    )
    nc: NatsClient = await nats.connect(NATS_URL)
    js: JetStreamContext = nc.jetstream()
    # Binds to (or auto-creates) the durable pull consumer filtering
    # `nexo.wecom.msg.>`; deliver-all + explicit ack.
    sub = await js.pull_subscribe(
        f"{NATS_SUBJECT_PREFIX}.>", durable=NATS_ARCHIVE_DURABLE, stream=NATS_STREAM
    )
    logger.info(
        "archive consumer ready (stream=%s, durable=%s) → MySQL %s/%s",
        NATS_STREAM, NATS_ARCHIVE_DURABLE, MYSQL_HOST, MYSQL_DATABASE,
    )
    try:
        while True:
            try:
                msgs = await sub.fetch(batch=_BATCH, timeout=_FETCH_TIMEOUT)
            except TimeoutError:
                continue
            for msg in msgs:
                await _handle(msg, pool)
    finally:
        await nc.drain()
        pool.close()
        await pool.wait_closed()
