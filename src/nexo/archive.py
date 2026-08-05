"""`nexo archive` — JetStream consumer that persists rich events to MySQL.

Runs on the host with internal-network access to the cloud MySQL. Pulls every
rich event `nexo drain` published to the `WECOM_MSG` stream, extracts the key
fields into typed columns (plus the full frame as JSON), and `INSERT IGNORE`s
the row. The `(nats_stream, nats_seq)` unique key makes this idempotent:
JetStream redelivery (at-least-once) never produces duplicate rows.

Each message is acked only after the row is committed. Permanent failures
(malformed payload, or a row that violates the schema) are acked + logged once
so JetStream stops redelivering — a poison message that naks forever would spam
the log every few seconds and never recover. Transient failures (connection,
deadlock, lock wait) are naked with a backoff so JetStream redelivers them.

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
from pymysql.err import DataError, IntegrityError, OperationalError

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

# MySQL error codes that are transient (retryable): the message is fine, the
# infrastructure isn't (temporarily). Other OperationalErrors are treated as
# non-transient → still nak (don't drop data on ambiguous operational states
# like a missing table, which ops can fix without losing in-flight messages).
_TRANSIENT_MYSQL_CODES = frozenset({
    1040,           # too many connections
    1205,           # lock wait timeout
    1213,           # deadlock
    1226,           # user limit reached
    1317,           # query interrupted
    2002, 2003,     # can't connect to server
    2006,           # server has gone away
    2013,           # lost connection during query
})

# Errors that are the message's fault — the row's data violates the schema
# deterministically, so redelivery will fail identically. Ack + log once
# instead of nak-ing forever (a poison message that naks every _NAK_DELAY
# seconds spams the log with a traceback and never recovers).
_PERMANENT_MYSQL_ERRORS = (IntegrityError, DataError)

# Bytes of a poison payload to log for post-mortem inspection.
_POISON_PREVIEW = 200


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
            fn = event.get("filename") or _filename_from_frame(frame)
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
    meta = msg.metadata
    nats_stream = meta.stream
    nats_seq = meta.sequence.stream

    # Phase 1 — parse + extract. A failure here is permanent: the message is
    # malformed and redelivery will fail identically. Ack + log once (with a
    # payload preview for diagnosis) so JetStream stops redelivering.
    try:
        event = json.loads(msg.data.decode("utf-8"))
        row = _row_from_event(event, nats_stream, nats_seq)
    except Exception as exc:  # noqa: BLE001 — any parse/extract failure is permanent
        logger.warning(
            "Dropping unparseable message (stream=%s seq=%s subject=%s): %s; "
            "payload=%r",
            nats_stream, nats_seq, msg.subject, exc, msg.data[:_POISON_PREVIEW],
        )
        await msg.ack()
        return

    # Phase 2 — persist. Transient DB errors (connection/lock/deadlock) → nak
    # for redelivery. Permanent data errors (row violates schema) → ack + log.
    # Anything else → nak (conservative: don't drop data on ambiguous or
    # operational failures like a missing table or a SQL bug — those are
    # fixable, and the redelivery log surfaces them as a signal to fix them).
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_INSERT_SQL, row)
    except _PERMANENT_MYSQL_ERRORS as exc:
        logger.warning(
            "Dropping message with unrecoverable DB error (stream=%s seq=%s): "
            "%s; row=%r",
            nats_stream, nats_seq, exc, row,
        )
        await msg.ack()
        return
    except OperationalError as exc:
        code = exc.args[0] if exc.args else None
        if isinstance(code, int) and code in _TRANSIENT_MYSQL_CODES:
            logger.info(
                "Transient DB error (stream=%s seq=%s code=%s); naking for redelivery",
                nats_stream, nats_seq, code,
            )
        else:
            logger.error(
                "DB operational error (stream=%s seq=%s code=%s); naking for "
                "redelivery: %s",
                nats_stream, nats_seq, code, exc,
            )
        await msg.nak(delay=_NAK_DELAY)
        return
    except Exception:  # noqa: BLE001 — unknown failure; don't drop the message
        logger.exception(
            "Unexpected error archiving (stream=%s seq=%s); naking for redelivery",
            nats_stream, nats_seq,
        )
        await msg.nak(delay=_NAK_DELAY)
        return

    await msg.ack()
    logger.debug("Archived %s seq=%s", nats_stream, nats_seq)


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
