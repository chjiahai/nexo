"""Tests for `nexo drain` — outbox orchestration (upload + publish + cleanup).

Drives `_process` with a fake publisher, a real tmp outbox, and fake OBS uploads.
Verifies the crash-recoverable state machine: text publishes directly; media
uploads (idempotent) then publishes; a 'uploaded' row publishes without
re-uploading; failures leave the row un-advanced for retry.
"""

from __future__ import annotations

import asyncio

import pytest

from nexo import drain, outbox


class FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[dict] = []
        self.fail = fail

    async def publish(self, event):
        if self.fail:
            raise RuntimeError("pub fail")
        self.events.append(event)
        return 1


def _frame(msgtype: str, msgid: str = "m1") -> dict:
    return {"headers": {"req_id": "r1"},
            "body": {"msgtype": msgtype, "msgid": msgid,
                     "chattype": "single", "from": {"userid": "u1"}}}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "NEXO_OUTBOX_PATH", str(tmp_path / "outbox.db"))
    monkeypatch.setattr(drain.outbox, "NEXO_OUTBOX_PATH", str(tmp_path / "outbox.db"))
    asyncio.run(outbox.init())
    return outbox


@pytest.fixture
def fake_uploads(monkeypatch):
    """Replace OBS uploads with recorders returning a deterministic key."""
    calls: list[tuple[str, str, str | None, bytes]] = []

    async def _file(user_id, msg_id, filename, data):
        calls.append(("file", msg_id, filename, data))
        return f"org/{user_id}/{msg_id}-{filename}"

    async def _image(user_id, msg_id, filename, data):
        calls.append(("image", msg_id, filename, data))
        return f"org/{user_id}/{msg_id}-image.png"

    async def _video(user_id, msg_id, filename, data):
        calls.append(("video", msg_id, filename, data))
        return f"org/{user_id}/{msg_id}-video.mp4"

    monkeypatch.setattr(drain.obs, "upload_file", _file)
    monkeypatch.setattr(drain.obs, "upload_image", _image)
    monkeypatch.setattr(drain.obs, "upload_video", _video)
    return calls


def test_process_text_publishes_and_marks_done(db, fake_uploads):
    """text has no bucket step — publish the rich event, then mark done."""
    asyncio.run(db.enqueue_text(_frame("text"), "the reply", "org1", "bot1"))
    row = asyncio.run(db.next_pending())

    pub = FakePublisher()
    asyncio.run(drain._process(row, pub))

    assert len(pub.events) == 1
    ev = pub.events[0]
    assert ev["frame"]["body"]["msgtype"] == "text"
    assert ev["reply_text"] == "the reply"
    assert ev["obs_key"] is None
    assert ev["org_id"] == "org1"
    assert ev["bot_id"] == "bot1"
    assert fake_uploads == []  # no upload for text
    # Row is done — not returned again.
    assert asyncio.run(db.next_pending()) is None


def test_process_media_pending_uploads_publishes_done_deletes_staging(db, fake_uploads, tmp_path):
    """media pending → upload → mark_uploaded → publish → mark_done → delete staging."""
    staging = tmp_path / "staged.png"
    staging.write_bytes(b"png-bytes")
    asyncio.run(db.enqueue_media("image", _frame("image", "mid-img"), str(staging), "o", "b"))
    row = asyncio.run(db.next_pending())
    assert row["state"] == "pending"

    pub = FakePublisher()
    asyncio.run(drain._process(row, pub))

    # Uploaded with the staged bytes.
    assert fake_uploads[0][0] == "image"
    assert fake_uploads[0][3] == b"png-bytes"
    # Event carries the obs_key returned by the upload.
    assert pub.events[0]["obs_key"] == "org/u1/mid-img-image.png"
    assert pub.events[0]["reply_text"] is None
    # Staging file cleaned up.
    assert not staging.exists()
    # Row is done.
    assert asyncio.run(db.next_pending()) is None


def test_process_media_uploaded_publishes_without_reupload(db, fake_uploads, tmp_path):
    """A resumed 'uploaded' row publishes without re-uploading (idempotent skip)."""
    staging = tmp_path / "staged.mp4"
    staging.write_bytes(b"mp4")
    asyncio.run(db.enqueue_media("video", _frame("video", "mid-v"), str(staging), "o", "b"))
    row = asyncio.run(db.next_pending())
    # Simulate a prior run that uploaded but crashed before publishing.
    asyncio.run(db.mark_uploaded(row["id"], "org/u1/mid-v-video.mp4"))
    row = asyncio.run(db.next_pending())
    assert row["state"] == "uploaded"

    pub = FakePublisher()
    asyncio.run(drain._process(row, pub))

    assert fake_uploads == []  # no re-upload
    assert pub.events[0]["obs_key"] == "org/u1/mid-v-video.mp4"
    assert not staging.exists()  # cleaned up
    assert asyncio.run(db.next_pending()) is None


def test_process_upload_failure_leaves_row_pending(db, monkeypatch, tmp_path):
    """An upload error raises; the row stays pending for retry (idempotent re-upload)."""
    staging = tmp_path / "staged"
    staging.write_bytes(b"x")
    asyncio.run(db.enqueue_media("file", _frame("file", "mid-f"), str(staging), "o", "b"))

    async def boom(user_id, msg_id, filename, data):
        raise RuntimeError("obs down")
    monkeypatch.setattr(drain.obs, "upload_file", boom)

    row = asyncio.run(db.next_pending())
    with pytest.raises(RuntimeError, match="obs down"):
        asyncio.run(drain._process(row, FakePublisher()))

    # Row still pending → drain retries it next loop.
    row2 = asyncio.run(db.next_pending())
    assert row2["id"] == row["id"]
    assert row2["state"] == "pending"


def test_process_publish_failure_after_upload_leaves_row_uploaded(db, fake_uploads, tmp_path):
    """publish fails after upload → row is 'uploaded' (not done); retry publishes only."""
    staging = tmp_path / "staged"
    staging.write_bytes(b"x")
    asyncio.run(db.enqueue_media("file", _frame("file", "mid-f"), str(staging), "o", "b"))
    row = asyncio.run(db.next_pending())

    pub = FakePublisher(fail=True)
    with pytest.raises(RuntimeError, match="pub fail"):
        asyncio.run(drain._process(row, pub))

    # Upload happened, row advanced to 'uploaded' with obs_key — but not done.
    assert fake_uploads  # upload succeeded
    row2 = asyncio.run(db.next_pending())
    assert row2["id"] == row["id"]
    assert row2["state"] == "uploaded"
    assert row2["obs_key"]  # recorded

    # Retry: publishes (no re-upload) and completes.
    pub2 = FakePublisher()
    asyncio.run(drain._process(row2, pub2))
    assert len(fake_uploads) == 1  # still only one upload total
    assert asyncio.run(db.next_pending()) is None
