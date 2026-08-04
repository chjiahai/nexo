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
