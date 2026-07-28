"""Pluggable conversation-history store for the text route.

The app layer owns cross-turn state: each session's full pydantic-ai message
history, replayed into the agent on every turn. This module defines the
contract (`SessionStore`) and a default in-memory implementation with an LRU
cap, so the bot no longer leaks memory proportional to distinct users and so
"swap for Redis when multi-process" is a matter of implementing the Protocol
rather than a TODO comment.

A store only needs `get` / `set` / `drop`. The default `InMemorySessionStore`
also exposes `clear` and `__contains__` for test convenience.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Protocol

from pydantic_ai.messages import ModelMessage


class SessionStore(Protocol):
    """Per-session conversation history, keyed by session id."""

    def get(self, session_id: str) -> list[ModelMessage]:
        """Return the session's history (empty list if unknown)."""
        ...

    def set(self, session_id: str, messages: list[ModelMessage]) -> None:
        """Persist the full history for a session (replaces prior)."""
        ...

    def drop(self, session_id: str) -> None:
        """Clear a session's history."""
        ...


class InMemorySessionStore:
    """Default store: an LRU-bounded OrderedDict.

    `max_sessions` caps distinct concurrent sessions; the least-recently-used
    is evicted when the cap is exceeded, so a long-running single-process bot
    can't grow without bound. `get` touches the LRU order so active sessions
    survive.
    """

    def __init__(self, max_sessions: int = 256) -> None:
        self._max = max_sessions
        self._data: OrderedDict[str, list[ModelMessage]] = OrderedDict()

    def get(self, session_id: str) -> list[ModelMessage]:
        msgs = self._data.get(session_id)
        if msgs is None:
            return []
        self._data.move_to_end(session_id)  # LRU touch on read
        return msgs

    def set(self, session_id: str, messages: list[ModelMessage]) -> None:
        self._data[session_id] = messages
        self._data.move_to_end(session_id)
        while len(self._data) > self._max:
            self._data.popitem(last=False)  # evict least-recently-used

    def drop(self, session_id: str) -> None:
        self._data.pop(session_id, None)

    def clear(self) -> None:
        self._data.clear()

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._data
