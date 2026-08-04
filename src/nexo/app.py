"""Application layer: request routing, session management, agent invocation.

Sits between the API layer (transport) and the Agent layer (pure agents).
Routing is deterministic by request type — there is no router agent. Each
request kind has its own `handle_*` entry point; the transport layer picks
the right one based on the message type (e.g. WeCom `msgtype`):

    text  -> handle_text  -> chat_agent (streamed)      [live]

This is the only layer that holds cross-turn state (session history, for the
text route), backed by a pluggable `SessionStore` (see `nexo.sessions`). It
does not import any transport code.

Media (file/image/video) no longer flows through the app layer — the bot
downloads + stages it and `nexo drain` uploads to OBS, so there is no
`handle_media` here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from nexo.agents.chat import chat_agent
from nexo.sessions import InMemorySessionStore, SessionStore

# Per-session conversation history. The default is an in-memory LRU-bounded
# store; swap via `set_session_store` (e.g. a Redis-backed store for
# multi-process deployments) without touching the rest of the app layer.
_store: SessionStore = InMemorySessionStore()


def set_session_store(store: SessionStore) -> None:
    """Replace the active session store (tests / multi-process backends)."""
    global _store
    _store = store


async def handle_text(session_id: str, text: str) -> AsyncIterator[str]:
    """Run the chat agent within a session, yielding streamed text tokens.

    The agent itself is stateless; we own the conversation history here and
    pass it in on every turn so the model sees prior context.
    """
    history = _store.get(session_id)

    async with chat_agent.run_stream(text, message_history=history) as result:
        async for chunk in result.stream_text(delta=True):
            if chunk:
                yield chunk

    # Persist the full conversation (history + this turn) for the next turn.
    _store.set(session_id, result.all_messages())


def reset_session(session_id: str) -> None:
    """Clear a session's history (e.g., on an explicit reset request)."""
    _store.drop(session_id)
