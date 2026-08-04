"""Local SQLite outbox — bot→drain durable handoff.

The bot writes every inbound WeCom message here as an "intent" row (BEFORE any
external side-effect), then `nexo drain` reads rows and performs the external
work (upload media to OBS, publish the rich event to NATS). Because the intent
is persisted first and the OBS PUT key is deterministic, a drain crash at any
point is recoverable on restart — no orphan objects, no reconciliation sweep.

State machine:
    text  : pending ──────────────────────────────▶ done   (publish only)
    media : pending ──▶ uploaded ──▶ done   (upload, then publish)

Only `nexo drain` mutates state (single drain process → no write contention).
The bot only inserts. SQLite WAL lets the bot insert while drain reads.

The DB lives at `NEXO_OUTBOX_PATH` (default `data/outbox.db`). Functions are
async — each wraps a short-lived `sqlite3` connection in `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

from nexo.config import NEXO_OUTBOX_PATH

# States. 'pending' = needs upload (media) or publish (text); 'uploaded' =
# media bytes are in OBS, obs_key set, needs publish; 'done' = published+acked.
_PENDING = "pending"
_UPLOADED = "uploaded"
_DONE = "done"


def _connect() -> sqlite3.Connection:
    """Open a short-lived connection with WAL + a busy timeout.

    WAL is persistent on the DB file; setting it per-connection is cheap and
    idempotent. `busy_timeout` makes a writer wait (not error) if another
    connection holds the lock — the bot and drain open separate connections.
    """
    conn = sqlite3.connect(NEXO_OUTBOX_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


async def init() -> None:
    """Create the outbox table if it doesn't exist. Safe to call on every start."""

    def _do() -> None:
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                  id            INTEGER PRIMARY KEY AUTOINCREMENT,
                  created_at    TEXT    NOT NULL,
                  kind          TEXT    NOT NULL,        -- text | file | image | video
                  frame         TEXT    NOT NULL,        -- raw frame JSON
                  reply_text    TEXT,                    -- text: LLM final output
                  staging_path  TEXT,                    -- media: abs path to staged bytes
                  org_id        TEXT,
                  bot_id        TEXT,
                  state         TEXT    NOT NULL DEFAULT 'pending',
                  obs_key       TEXT,                    -- media: filled after upload
                  error         TEXT,
                  updated_at    TEXT    NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_state ON outbox(state, id)"
            )

    await asyncio.to_thread(_do)


async def enqueue_text(
    frame: dict[str, Any], reply_text: str, org_id: str, bot_id: str
) -> int:
    """Insert a text message intent (state=pending). Returns the row id."""
    return await _enqueue(
        kind="text", frame=frame, reply_text=reply_text,
        staging_path=None, org_id=org_id, bot_id=bot_id,
    )


async def enqueue_media(
    kind: str, frame: dict[str, Any], staging_path: str, org_id: str, bot_id: str
) -> int:
    """Insert a media message intent (state=pending). Returns the row id."""
    return await _enqueue(
        kind=kind, frame=frame, reply_text=None,
        staging_path=staging_path, org_id=org_id, bot_id=bot_id,
    )


async def _enqueue(
    *,
    kind: str,
    frame: dict[str, Any],
    reply_text: str | None,
    staging_path: str | None,
    org_id: str,
    bot_id: str,
) -> int:
    now = _now()

    def _do() -> int:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO outbox
                  (created_at, kind, frame, reply_text, staging_path,
                   org_id, bot_id, state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (now, kind, json.dumps(frame, ensure_ascii=False), reply_text,
                 staging_path, org_id, bot_id, _PENDING, now),
            )
            return int(cur.lastrowid)

    return await asyncio.to_thread(_do)


async def next_pending() -> dict[str, Any] | None:
    """Return the oldest non-done row (state pending or uploaded), or None.

    `frame` is parsed back to a dict for the caller. The row stays claimed
    only by single-drain convention — there is no separate 'processing' state
    because a crash should resume the same row on restart.
    """

    def _do() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT id, kind, frame, reply_text, staging_path,
                       org_id, bot_id, state, obs_key, error
                FROM outbox
                WHERE state IN (?, ?)
                ORDER BY id
                LIMIT 1
                """,
                (_PENDING, _UPLOADED),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["frame"] = json.loads(d["frame"])
        return d

    return await asyncio.to_thread(_do)


async def mark_uploaded(row_id: int, obs_key: str) -> None:
    """Media uploaded to OBS — record the key, advance to 'uploaded'."""

    def _do() -> None:
        with _connect() as conn:
            conn.execute(
                "UPDATE outbox SET state=?, obs_key=?, error=NULL, updated_at=? WHERE id=?",
                (_UPLOADED, obs_key, _now(), row_id),
            )

    await asyncio.to_thread(_do)


async def mark_done(row_id: int) -> None:
    """Rich event published + acked — finalize the row."""

    def _do() -> None:
        with _connect() as conn:
            conn.execute(
                "UPDATE outbox SET state=?, error=NULL, updated_at=? WHERE id=?",
                (_DONE, _now(), row_id),
            )

    await asyncio.to_thread(_do)


async def mark_error(row_id: int, error: str) -> None:
    """Record a failure on the row; state is left as-is so drain retries it."""

    def _do() -> None:
        with _connect() as conn:
            conn.execute(
                "UPDATE outbox SET error=?, updated_at=? WHERE id=?",
                (error, _now(), row_id),
            )

    await asyncio.to_thread(_do)


def _now() -> str:
    """UTC ISO-8601 timestamp (stdlib only; `datetime.now` is fine outside the workflow sandbox)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
