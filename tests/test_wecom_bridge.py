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

    def __init__(self, downloads=None) -> None:
        self.stream_calls: list[tuple[str, str, bool]] = []
        self.welcome_calls: list[dict] = []
        self.download_calls: list[tuple[str, str]] = []
        # If an Exception, download_file raises it; if a list, pops next (bytes, name).
        self.downloads = downloads if downloads is not None else []

    async def reply_stream(self, frame, stream_id, content, finish=False, **_):
        self.stream_calls.append((stream_id, content, finish))

    async def reply_welcome(self, frame, body):
        self.welcome_calls.append(body)

    async def download_file(self, url, aes_key=None):
        self.download_calls.append((url, aes_key))
        if isinstance(self.downloads, Exception):
            raise self.downloads
        if not self.downloads:
            raise AssertionError("no fake download queued")
        return self.downloads.pop(0)


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


def _image_frame(url: str = "https://i/p.png", aeskey: str = "img-key=", **extra) -> dict:
    body = {"msgtype": "image", "image": {"url": url, "aeskey": aeskey}, **extra}
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


def test_media_field_file_kind_returns_url():
    frame = _file_frame({"file": {"filename": "report.pdf", "url": "https://x/y"}})
    assert wecom._media_field(frame, "url", "file") == "https://x/y"


def test_media_field_missing_is_empty():
    """No url in the file payload -> empty string (pipeline surfaces a clear error)."""
    frame = {"body": {"msgtype": "file", "file": {"filename": "a.pdf"}}}
    assert wecom._media_field(frame, "url", "file") == ""
    assert wecom._media_field({}, "url", "file") == ""


def test_media_aeskey_file_kind_returns_key():
    frame = _file_frame({"file": {"url": "https://x/y", "aeskey": "base64key=="}})
    assert wecom._media_aeskey(frame, "file") == "base64key=="


def test_media_aeskey_missing_is_empty():
    """No aeskey -> empty; without it the downloaded bytes stay encrypted."""
    assert wecom._media_aeskey(_file_frame(), "file") == ""
    assert wecom._media_aeskey({}, "file") == ""


def test_media_field_image_kind_reads_body_image():
    """Image payloads live under body.image.{url,aeskey} — same shape as files."""
    frame = {"body": {"msgtype": "image", "image": {"url": "https://i/p", "aeskey": "k="}}}
    assert wecom._media_field(frame, "url", "image") == "https://i/p"
    assert wecom._media_aeskey(frame, "image") == "k="


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


def test_reply_streamed_time_flushes_small_slow_chunks(monkeypatch):
    """Small chunks under _FLUSH_BYTES still surface once _FLUSH_INTERVAL elapses,
    so a slow model doesn't leave the bubble stuck on 'thinking'. Nothing is lost:
    the finish frame carries the full concatenated text.

    Guards the queue+pump design: timeouts must fire on queue.get() (safe to
    cancel), never on the generator's __anext__ (which would close it)."""
    monkeypatch.setattr(wecom, "_FLUSH_INTERVAL", 0.02)
    fake = FakeWSClient()
    frame = _text_frame("hi", body_extra={"chatid": "group-1"})

    async def gen():
        for tok in ("one ", "two ", "three"):
            await asyncio.sleep(0.05)  # slower than the 0.02s interval
            yield tok

    asyncio.run(wecom._reply_streamed(fake, frame, gen()))

    contents = [c for _, c, _ in fake.stream_calls]
    # Each token is well under _FLUSH_BYTES(64); without time-flush the bubble
    # would sit on 'thinking' until the finish frame. Here each prefix surfaces.
    assert "one " in contents
    assert "one two " in contents
    assert "one two three" in contents
    # Finish frame carries the full text — nothing dropped by the timeouts.
    assert contents[-1] == "one two three"
    assert fake.stream_calls[-1][2] is True


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


def test_reply_streamed_replace_mode_each_chunk_stands_alone():
    """accumulate=False: each chunk is sent as the FULL bubble content (replace),
    not concatenated with prior stages. Progress messages wipe each other."""
    fake = FakeWSClient()
    frame = _text_frame("hi", body_extra={"chatid": "group-1"})

    async def gen():
        yield "正在下载文件…"
        yield "已下载到 data/uploads/x.docx，正在解析…"
        yield "已解析，正在生成摘要…"
        yield "# 摘要\n\n正文"

    asyncio.run(wecom._reply_streamed(fake, frame, gen(), accumulate=False))

    contents = [c for _, c, _ in fake.stream_calls]
    # placeholder, then each stage as its own frame (no concatenation).
    assert contents[0] == "正在思考…"
    assert contents[1] == "正在下载文件…"
    assert contents[2] == "已下载到 data/uploads/x.docx，正在解析…"
    assert contents[3] == "已解析，正在生成摘要…"
    # No frame ever carries two stages glued together.
    assert not any("正在下载" in c and "正在解析" in c for c in contents)


def test_reply_streamed_replace_mode_finish_is_summary_only():
    """accumulate=False: the finish frame carries ONLY the summary — all progress
    is wiped, exactly like a chat reply."""
    fake = FakeWSClient()
    frame = _text_frame("hi", body_extra={"chatid": "group-1"})

    async def gen():
        yield "正在下载文件…"
        yield "正在解析…"
        yield "# 摘要正文"

    asyncio.run(wecom._reply_streamed(fake, frame, gen(), accumulate=False))

    last_content, last_finish = fake.stream_calls[-1][1], fake.stream_calls[-1][2]
    assert last_finish is True
    assert last_content == "# 摘要正文"
    # Progress must be gone from the final bubble.
    assert "正在下载" not in last_content
    assert "正在解析" not in last_content


def test_reply_streamed_replace_mode_error_preserves_last():
    """accumulate=False: on mid-stream error, the finish frame preserves the last
    shown stage alongside the error message."""
    fake = FakeWSClient()
    frame = _text_frame("hi", body_extra={"chatid": "group-1"})

    async def _boom():
        yield "正在下载文件…"
        yield "正在解析…"
        raise RuntimeError("boom")

    asyncio.run(wecom._reply_streamed(fake, frame, _boom(), accumulate=False))

    last_content, last_finish = fake.stream_calls[-1][1], fake.stream_calls[-1][2]
    assert last_finish is True
    assert "boom" in last_content
    assert "正在解析" in last_content


def test_register_handlers_wires_events():
    """register_handlers attaches listeners for text + image + file + enter_chat."""
    from pyee.asyncio import AsyncIOEventEmitter

    class StubClient(AsyncIOEventEmitter):
        async def reply_welcome(self, frame, body):
            pass

    stub = StubClient()
    wecom.register_handlers(stub)
    assert stub.listeners("message.text")
    assert stub.listeners("message.image")
    assert stub.listeners("message.file")
    assert stub.listeners("event.enter_chat")


def test_image_message_downloads_and_delegates(monkeypatch):
    """An image is downloaded via the SDK, then handed to handle_image (which
    stores it). No more silent 'not supported' rejection."""
    from pyee.asyncio import AsyncIOEventEmitter

    class EmitterClient(AsyncIOEventEmitter):
        def __init__(self, downloads=None) -> None:
            super().__init__()
            self.stream_calls: list[tuple[str, str, bool]] = []
            self.download_calls: list[tuple[str, str]] = []
            self.downloads = downloads if downloads is not None else []

        async def reply_stream(self, frame, stream_id, content, finish=False, **_):
            self.stream_calls.append((stream_id, content, finish))

        async def reply_welcome(self, frame, body):
            pass

        async def download_file(self, url, aes_key=None):
            self.download_calls.append((url, aes_key))
            if not self.downloads:
                raise AssertionError("no fake download queued")
            return self.downloads.pop(0)

    fake = EmitterClient(downloads=[(b"\x89PNG-bytes", None)])
    wecom.register_handlers(fake)

    captured: dict = {}

    async def fake_handle_image(session_id, image_data):
        captured.update(session_id=session_id, image_data=image_data)
        yield "（图片已收到，已保存到对象存储。）"

    monkeypatch.setattr(wecom, "handle_image", fake_handle_image)

    frame = _image_frame(url="https://i/p.png", aeskey="img-key=",
                         chatid="group-1")

    async def _go():
        # emit() is sync; it schedules the async handler on this loop. Yield
        # once so the scheduled task actually runs to completion.
        fake.emit("message.image", frame)
        await asyncio.sleep(0.05)

    asyncio.run(_go())

    # Image downloaded with the frame's url + aeskey, then handed to handle_image.
    assert fake.download_calls == [("https://i/p.png", "img-key=")]
    assert captured["image_data"] == b"\x89PNG-bytes"
    contents = [c for _, c, _ in fake.stream_calls]
    assert contents, "expected a reply for image messages"
    assert "图片已收到" in contents[-1]
    # Last frame is the finish frame.
    assert fake.stream_calls[-1][2] is True


def test_stream_image_downloads_and_delegates(monkeypatch):
    """_stream_image: SDK download+decrypt yields plaintext bytes, then hands off
    to handle_image."""
    fake = FakeWSClient()
    fake.downloads = [(b"png-bytes", None)]

    captured: dict = {}

    async def fake_handle(session_id, image_data):
        captured.update(session_id=session_id, image_data=image_data)
        yield "ok:saved"

    monkeypatch.setattr(wecom, "handle_image", fake_handle)

    async def gen():
        async for c in wecom._stream_image(fake, "wecom:u1", "https://i/p", "k"):
            yield c

    chunks = asyncio.run(_collect(gen()))

    # SDK download called with url + aes_key.
    assert fake.download_calls == [("https://i/p", "k")]
    # handle_image got the decrypted bytes.
    assert captured["image_data"] == b"png-bytes"
    assert captured["session_id"] == "wecom:u1"
    # Progress + delegated chunk streamed out.
    assert chunks[0] == "正在接收图片…"
    assert chunks[-1] == "ok:saved"


def test_stream_image_empty_url_yields_error():
    """No download URL -> a clear user-facing message, no SDK call."""
    fake = FakeWSClient()

    async def gen():
        async for c in wecom._stream_image(fake, "wecom:u1", "", "k"):
            yield c

    chunks = asyncio.run(_collect(gen()))
    assert fake.downloads == []
    assert "下载链接为空" in chunks[-1]


def test_stream_image_download_failure_surfaces_error(monkeypatch):
    """A download/decrypt error becomes a readable message, not a crash."""
    fake = FakeWSClient(downloads=RuntimeError("bad aeskey"))

    async def fail_handle(*a, **k):  # pragma: no cover — must not run
        yield "should not reach"

    monkeypatch.setattr(wecom, "handle_image", fail_handle)

    async def gen():
        async for c in wecom._stream_image(fake, "wecom:u1", "https://i/p", "k"):
            yield c

    chunks = asyncio.run(_collect(gen()))
    assert "下载失败" in chunks[-1]
    assert "bad aeskey" in chunks[-1]


def test_stream_file_downloads_and_delegates(monkeypatch):
    """_stream_file: SDK download+decrypt yields plaintext bytes, then hands off
    to handle_file using the SDK-provided filename over the frame hash."""
    fake = FakeWSClient()
    fake.downloads = [(b"plaintext-bytes", "real-name.docx")]

    captured: dict = {}

    async def fake_handle(session_id, filename, file_data):
        captured.update(session_id=session_id, filename=filename, file_data=file_data)
        yield f"ok:{filename}"

    monkeypatch.setattr(wecom, "handle_file", fake_handle)

    async def gen():
        async for c in wecom._stream_file(fake, "wecom:u1", "hash.docx", "https://x/y", "k"):
            yield c

    chunks = asyncio.run(_collect(gen()))

    # SDK download called with url + aes_key.
    assert fake.download_calls == [("https://x/y", "k")]
    # handle_file got the decrypted bytes and the SDK filename (not the hash).
    assert captured["file_data"] == b"plaintext-bytes"
    assert captured["filename"] == "real-name.docx"
    assert captured["session_id"] == "wecom:u1"
    # Progress + delegated chunk streamed out.
    assert chunks[0] == "正在下载文件…"
    assert chunks[-1] == "ok:real-name.docx"


def test_stream_file_empty_url_yields_error():
    """No download URL -> a clear user-facing message, no SDK call."""
    fake = FakeWSClient()

    async def gen():
        async for c in wecom._stream_file(fake, "wecom:u1", "x.docx", "", "k"):
            yield c

    chunks = asyncio.run(_collect(gen()))
    assert fake.downloads == []
    assert "下载链接为空" in chunks[-1]


def test_stream_file_download_failure_surfaces_error(monkeypatch):
    """A download/decrypt error becomes a readable message, not a crash."""
    fake = FakeWSClient(downloads=RuntimeError("bad aeskey"))

    async def fail_handle(*a, **k):  # pragma: no cover — must not run
        yield "should not reach"
    monkeypatch.setattr(wecom, "handle_file", fail_handle)

    async def gen():
        async for c in wecom._stream_file(fake, "wecom:u1", "x.docx", "https://x/y", "k"):
            yield c

    chunks = asyncio.run(_collect(gen()))
    assert "下载失败" in chunks[-1]
    assert "bad aeskey" in chunks[-1]


async def _collect(gen):
    out: list[str] = []
    async for c in gen:
        out.append(c)
    return out
