"""Unit tests for the WeCom transport bridge.

Mocks the WSClient so no real WeCom connection or LLM is needed. The app
layer is driven by pydantic-ai's TestModel via `chat_agent.override`, the
same hermetic pattern used in test_app.py.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.models.test import TestModel

from nexo import app
from nexo.agents.chat import chat_agent
from nexo.api import wecom


class FakeWSClient:
    """Records reply_stream / reply_welcome calls instead of sending them."""

    def __init__(self) -> None:
        self.stream_calls: list[tuple[str, str, bool]] = []
        self.welcome_calls: list[dict] = []

    async def reply_stream(self, frame, stream_id, content, finish=False, **_):
        self.stream_calls.append((stream_id, content, finish))

    async def reply_welcome(self, frame, body):
        self.welcome_calls.append(body)


def _text_frame(content: str, body_extra: dict | None = None) -> dict:
    body = {"msgtype": "text", "text": {"content": content}}
    if body_extra:
        body.update(body_extra)
    return {"cmd": "aibot_msg_callback", "headers": {"req_id": "req-123"}, "body": body}


def _file_frame(body_extra: dict | None = None) -> dict:
    body = {"msgtype": "file", "file": {"url": "https://x/y.pdf"}, "msgid": "mid-1"}
    if body_extra:
        body.update(body_extra)
    return {"cmd": "aibot_msg_callback", "headers": {"req_id": "req-123"}, "body": body}


@pytest.fixture(autouse=True)
def _clear_sessions():
    app._sessions.clear()
    yield
    app._sessions.clear()


def test_user_text_extracts_content():
    frame = _text_frame("hello there")
    assert wecom._user_text(frame) == "hello there"


def test_user_text_missing_fields_is_empty():
    assert wecom._user_text({}) == ""
    assert wecom._user_text({"body": {}}) == ""


def test_session_id_single_chat_uses_from_userid():
    """Confirmed real shape: single chat carries the user under body.from.userid."""
    frame = _text_frame("hi", body_extra={"chattype": "single", "from": {"userid": "2"}})
    assert wecom._session_id_from_frame(frame) == "wecom:2"


def test_session_id_group_chat_uses_chatid():
    """Group chat uses the top-level body.chatid as the conversation identity."""
    frame = _text_frame("hi", body_extra={"chattype": "group", "chatid": "group-xyz"})
    assert wecom._session_id_from_frame(frame) == "wecom:group-xyz"


def test_session_id_falls_back_to_req_id(caplog):
    """Unknown shape with no identity falls back to req_id (with a warning)."""
    frame = _text_frame("hi")  # no chattype / from / chatid
    with caplog.at_level("WARNING", logger="nexo.wecom"):
        sid = wecom._session_id_from_frame(frame)
    assert sid == "wecom:req-123"
    assert "No conversation id" in caplog.text


def test_filename_from_frame_uses_file_filename():
    frame = _file_frame({"file": {"filename": "report.pdf", "url": "https://x/y"}})
    assert wecom._filename_from_frame(frame) == "report.pdf"


def test_filename_from_frame_falls_back_to_msgid():
    """No filename field -> fall back to body.msgid."""
    frame = _file_frame()  # file has only url
    assert wecom._filename_from_frame(frame) == "mid-1"


def test_reply_streamed_streams_and_finishes():
    """Frames carry the FULL accumulated text (WeCom replaces, not appends);
    the finish frame repeats the full text (never empty, or the bubble clears)."""
    fake = FakeWSClient()
    frame = _text_frame("hi", body_extra={"chatid": "group-1"})

    with chat_agent.override(model=TestModel(custom_output_text="hello world")):
        chunks = wecom.handle_text("wecom:group-1", "hi")
        asyncio.run(wecom._reply_streamed(fake, frame, chunks))

    # Every call must share one stream_id.
    stream_ids = {sid for sid, _, _ in fake.stream_calls}
    assert len(stream_ids) == 1

    # First frame is the 'thinking' placeholder, not finished.
    assert fake.stream_calls[0][1] == "正在思考…"
    assert fake.stream_calls[0][2] is False

    # No frame ever carries empty content (that would clear the bubble).
    assert all(content for _, content, _ in fake.stream_calls)

    # Last frame is the finish frame and carries the full reply text.
    last_content, last_finish = fake.stream_calls[-1][1], fake.stream_calls[-1][2]
    assert last_finish is True
    assert "hello world" in last_content


def test_reply_streamed_sends_full_text_each_frame():
    """Each refresh frame contains the accumulated full text so far (growing
    prefix), per WeCom's replace-on-refresh stream model."""
    fake = FakeWSClient()
    frame = _text_frame("hi", body_extra={"chatid": "group-1"})

    async def gen():
        yield "AAAA" * 20  # 80 chars > _FLUSH_BYTES(64) -> flush
        yield "BBBB" * 20  # another 80 -> flush

    asyncio.run(wecom._reply_streamed(fake, frame, gen()))

    contents = [c for _, c, _ in fake.stream_calls]
    # placeholder, then full-after-chunk1, then full-after-chunk2 (finish)
    assert contents[0] == "正在思考…"
    assert contents[1] == "AAAA" * 20
    assert contents[2] == "AAAA" * 20 + "BBBB" * 20
    # finish frame repeats the full text.
    assert fake.stream_calls[-1][2] is True
    assert contents[-1] == "AAAA" * 20 + "BBBB" * 20


def test_reply_streamed_sends_finish_on_error():
    """If the chunk generator raises, a finish frame is still sent so the bubble
    unblocks; any partial text is preserved alongside the error."""
    fake = FakeWSClient()
    frame = _text_frame("hi", body_extra={"chatid": "group-1"})

    async def _boom():
        yield "partial"
        raise RuntimeError("boom")

    asyncio.run(wecom._reply_streamed(fake, frame, _boom()))

    # Last frame must be a finish frame containing both partial text and error.
    last_content, last_finish = fake.stream_calls[-1][1], fake.stream_calls[-1][2]
    assert last_finish is True
    assert "boom" in last_content
    assert "partial" in last_content


def test_register_handlers_wires_events():
    """register_handlers attaches listeners for text + file + enter_chat."""
    from pyee.asyncio import AsyncIOEventEmitter

    class StubClient(AsyncIOEventEmitter):
        async def reply_welcome(self, frame, body):
            pass

    stub = StubClient()
    wecom.register_handlers(stub)
    assert stub.listeners("message.text")
    assert stub.listeners("message.file")
    assert stub.listeners("event.enter_chat")
