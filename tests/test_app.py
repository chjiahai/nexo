"""Unit tests for the application layer.

Uses pydantic-ai's TestModel — a fake model that needs no real LLM or API
key. These tests are hermetic, deterministic, and fast, which is exactly
what `tests/` is for.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.models.test import TestModel

from nexo import app
from nexo.agents.chat import chat_agent
from nexo import media


@pytest.fixture(autouse=True)
def _clear_sessions():
    """Isolate tests from each other by wiping the in-memory session store."""
    app._store.clear()
    yield
    app._store.clear()


@pytest.fixture
def test_model():
    return TestModel(custom_output_text="hello world")


async def _collect(session_id: str, text: str) -> str:
    """Drive the async generator and concatenate its streamed chunks."""
    chunks: list[str] = []
    async for chunk in app.handle_text(session_id, text):
        chunks.append(chunk)
    return "".join(chunks)


def test_handle_text_streams_text(test_model):
    """The agent's reply is streamed out as text chunks."""
    with chat_agent.override(model=test_model):
        out = asyncio.run(_collect("s1", "hi"))

    assert "hello" in out
    assert "world" in out


def test_session_history_grows_across_turns(test_model):
    """Cross-turn state lives in the app layer: history accumulates per session."""
    with chat_agent.override(model=test_model):
        asyncio.run(_collect("s2", "first turn"))
        after_turn_1 = len(app._store.get("s2"))

        asyncio.run(_collect("s2", "second turn"))
        after_turn_2 = len(app._store.get("s2"))

    assert after_turn_1 > 0
    assert after_turn_2 > after_turn_1


def test_sessions_are_isolated(test_model):
    """Different session ids keep independent histories."""
    with chat_agent.override(model=test_model):
        asyncio.run(_collect("s3", "hi"))
        asyncio.run(_collect("s4", "hi"))

    assert "s3" in app._store
    assert "s4" in app._store
    # Each session only saw its own single turn.
    assert len(app._store.get("s3")) == len(app._store.get("s4"))


def test_reset_session_clears_history(test_model):
    """reset_session drops a session's history."""
    with chat_agent.override(model=test_model):
        asyncio.run(_collect("s5", "hi"))
    assert "s5" in app._store

    app.reset_session("s5")
    assert "s5" not in app._store


def test_in_memory_store_evicts_least_recently_used():
    """The default store caps concurrent sessions, evicting the LRU entry so a
    long-running bot can't leak memory proportional to distinct users."""
    store = app.InMemorySessionStore(max_sessions=2)
    store.set("a", ["msg-a"])
    store.set("b", ["msg-b"])
    # Touch "a" so "b" becomes the least-recently-used.
    assert store.get("a") == ["msg-a"]
    store.set("c", ["msg-c"])

    assert "a" in store  # touched recently -> survives
    assert "c" in store
    assert "b" not in store  # LRU victim


def test_handle_media_file_stores_and_acks(monkeypatch):
    """handle_media ships a file to the remote folder and replies with the saved message."""
    uploaded: list[tuple[str, str | None, bytes]] = []

    async def fake_upload(user_id: str, filename: str | None, data: bytes) -> str:
        uploaded.append((user_id, filename, data))
        return "docs/fake/key"

    monkeypatch.setattr("nexo.storage.remote.upload_file", fake_upload)
    out = asyncio.run(_collect_media("s6", media.FILE, "report.pdf", b"hello"))
    assert uploaded == [("s6", "report.pdf", b"hello")]
    assert "正在保存文件" in out
    assert "已保存" in out


def test_handle_media_file_wraps_upload_errors(monkeypatch):
    """An upload failure becomes a friendly Chinese message, not a crash."""
    async def boom(user_id: str, filename: str | None, data: bytes) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr("nexo.storage.remote.upload_file", boom)
    out = asyncio.run(_collect_media("s6", media.FILE, "report.pdf", b"hello"))
    assert "文件保存失败" in out
    assert "boom" in out


def test_handle_media_image_stores_and_acks(monkeypatch):
    """handle_media ships an image to the remote folder and replies with the saved message."""
    uploaded: list[tuple[str, str | None, bytes]] = []

    async def fake_upload(user_id: str, filename: str | None, data: bytes) -> str:
        uploaded.append((user_id, filename, data))
        return "imgs/fake/key"

    monkeypatch.setattr("nexo.storage.remote.upload_image", fake_upload)
    out = asyncio.run(_collect_media("s7", media.IMAGE, None, b"\x89PNG\r\n\x1a\n..."))
    assert uploaded == [("s7", None, b"\x89PNG\r\n\x1a\n...")]
    assert "图片已收到" in out


def test_handle_media_image_wraps_upload_errors(monkeypatch):
    """An upload failure becomes a friendly Chinese message, not a crash."""
    async def boom(user_id: str, filename: str | None, data: bytes) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr("nexo.storage.remote.upload_image", boom)
    out = asyncio.run(_collect_media("s7", media.IMAGE, None, b"x"))
    assert "图片保存失败" in out
    assert "boom" in out


def test_handle_media_video_stores_and_acks(monkeypatch):
    """handle_media ships a video to the remote folder and replies with the saved message."""
    uploaded: list[tuple[str, str | None, bytes]] = []

    async def fake_upload(user_id: str, filename: str | None, data: bytes) -> str:
        uploaded.append((user_id, filename, data))
        return "videos/fake/key"

    monkeypatch.setattr("nexo.storage.remote.upload_video", fake_upload)
    out = asyncio.run(_collect_media("s8", media.VIDEO, "clip.mp4", b"\x00\x00\x00..."))
    assert uploaded == [("s8", "clip.mp4", b"\x00\x00\x00...")]
    assert "正在保存视频" in out
    assert "已保存" in out


def test_handle_media_video_wraps_upload_errors(monkeypatch):
    """An upload failure becomes a friendly Chinese message, not a crash."""
    async def boom(user_id: str, filename: str | None, data: bytes) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr("nexo.storage.remote.upload_video", boom)
    out = asyncio.run(_collect_media("s8", media.VIDEO, "clip.mp4", b"x"))
    assert "视频保存失败" in out
    assert "boom" in out


async def _collect_media(
    session_id: str, route: media.MediaRoute, filename: str | None, data: bytes
) -> str:
    chunks: list[str] = []
    async for chunk in app.handle_media(session_id, route, filename, data):
        chunks.append(chunk)
    return "".join(chunks)
