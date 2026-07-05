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


@pytest.fixture(autouse=True)
def _clear_sessions():
    """Isolate tests from each other by wiping the in-memory session store."""
    app._sessions.clear()
    yield
    app._sessions.clear()


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
        after_turn_1 = len(app._sessions["s2"])

        asyncio.run(_collect("s2", "second turn"))
        after_turn_2 = len(app._sessions["s2"])

    assert after_turn_1 > 0
    assert after_turn_2 > after_turn_1


def test_sessions_are_isolated(test_model):
    """Different session ids keep independent histories."""
    with chat_agent.override(model=test_model):
        asyncio.run(_collect("s3", "hi"))
        asyncio.run(_collect("s4", "hi"))

    assert "s3" in app._sessions
    assert "s4" in app._sessions
    # Each session only saw its own single turn.
    assert len(app._sessions["s3"]) == len(app._sessions["s4"])


def test_reset_session_clears_history(test_model):
    """reset_session drops a session's history."""
    with chat_agent.override(model=test_model):
        asyncio.run(_collect("s5", "hi"))
    assert "s5" in app._sessions

    app.reset_session("s5")
    assert "s5" not in app._sessions


def test_handle_file_surfaces_pipeline_chunks(monkeypatch):
    """handle_file streams whatever the documents pipeline yields."""
    async def fake_process(file_data, filename):
        yield f"fake:{filename}:{len(file_data)}"

    monkeypatch.setattr("nexo.documents.pipeline.process_file", fake_process)
    out = asyncio.run(_collect_file("s6", "report.pdf", b"hello"))
    assert "fake:report.pdf:5" in out


def test_handle_file_wraps_pipeline_errors(monkeypatch):
    """A pipeline failure becomes a friendly Chinese message, not a crash."""
    async def boom(file_data, filename):
        raise RuntimeError("boom")
        yield  # pragma: no cover — make it an async generator

    monkeypatch.setattr("nexo.documents.pipeline.process_file", boom)
    out = asyncio.run(_collect_file("s6", "report.pdf", b"hello"))
    assert "处理失败" in out
    assert "boom" in out


async def _collect_file(session_id: str, filename: str, file_data: bytes) -> str:
    chunks: list[str] = []
    async for chunk in app.handle_file(session_id, filename, file_data):
        chunks.append(chunk)
    return "".join(chunks)
