"""Tests for the local SQLite outbox (bot→drain durable handoff).

Hermetic: NEXO_OUTBOX_PATH is pointed at a tmp db. Verifies the state machine
(pending → uploaded → done), that next_pending returns the oldest non-done row,
and that mark_error leaves state for retry.
"""

from __future__ import annotations

import asyncio

import pytest

from nexo import outbox


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "NEXO_OUTBOX_PATH", str(tmp_path / "outbox.db"))
    asyncio.run(outbox.init())
    return outbox


def _frame(msgtype: str = "text", msgid: str = "m1") -> dict:
    return {"headers": {"req_id": "r1"}, "body": {"msgtype": msgtype, "msgid": msgid}}


def test_enqueue_text_then_next_pending(db):
    """A text intent lands as pending; next_pending returns it with frame parsed."""
    asyncio.run(db.enqueue_text(_frame(), "hello", "org1", "bot1"))

    row = asyncio.run(db.next_pending())
    assert row is not None
    assert row["kind"] == "text"
    assert row["state"] == "pending"
    assert row["reply_text"] == "hello"
    assert row["org_id"] == "org1"
    assert row["bot_id"] == "bot1"
    assert row["frame"]["body"]["msgid"] == "m1"


def test_next_pending_returns_oldest_first(db):
    asyncio.run(db.enqueue_text(_frame(msgid="a"), "1", "o", "b"))
    asyncio.run(db.enqueue_text(_frame(msgid="b"), "2", "o", "b"))
    row = asyncio.run(db.next_pending())
    assert row["frame"]["body"]["msgid"] == "a"


def test_next_pending_skips_done(db):
    """Done rows are not returned."""
    asyncio.run(db.enqueue_text(_frame(), "x", "o", "b"))
    row = asyncio.run(db.next_pending())
    asyncio.run(db.mark_done(row["id"]))
    assert asyncio.run(db.next_pending()) is None


def test_text_advance_to_done(db):
    """text: pending → done (no upload step)."""
    asyncio.run(db.enqueue_text(_frame(), "reply", "o", "b"))
    row = asyncio.run(db.next_pending())
    asyncio.run(db.mark_done(row["id"]))
    assert asyncio.run(db.next_pending()) is None


def test_media_advance_pending_uploaded_done(db):
    """media: pending → uploaded (obs_key set) → done."""
    asyncio.run(db.enqueue_media("image", _frame("image", "m2"), "/tmp/staged", "o", "b"))
    row = asyncio.run(db.next_pending())
    assert row["kind"] == "image"
    assert row["state"] == "pending"
    assert row["staging_path"] == "/tmp/staged"

    asyncio.run(db.mark_uploaded(row["id"], "org/u/m2-image.png"))
    # next_pending still returns it (uploaded is not done).
    row2 = asyncio.run(db.next_pending())
    assert row2["id"] == row["id"]
    assert row2["state"] == "uploaded"
    assert row2["obs_key"] == "org/u/m2-image.png"

    asyncio.run(db.mark_done(row["id"]))
    assert asyncio.run(db.next_pending()) is None


def test_mark_error_leaves_state_for_retry(db):
    """mark_error records the error but does not advance state — drain retries."""
    asyncio.run(db.enqueue_text(_frame(), "x", "o", "b"))
    row = asyncio.run(db.next_pending())
    asyncio.run(db.mark_error(row["id"], "boom"))
    # Still pending, still returned.
    row2 = asyncio.run(db.next_pending())
    assert row2["id"] == row["id"]
    assert row2["state"] == "pending"
    assert row2["error"] == "boom"
