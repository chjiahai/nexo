"""Application layer: request routing, session management, agent invocation.

Sits between the API layer (transport) and the Agent layer (pure agents).
Routing is deterministic by request type — there is no router agent. Each
request kind has its own `handle_*` entry point; the transport layer picks
the right one based on the message type (e.g. WeCom `msgtype`):

    text -> handle_text -> chat_agent (streamed)        [live]
    file -> handle_file -> ingest pipeline (placeholder) [skeleton]

This is the only layer that holds cross-turn state (session history, for the
text route). It does not import any transport code.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncIterator

from pydantic_ai.messages import ModelMessage

from nexo.agents.chat import chat_agent

# session_id -> full message history. Swap for Redis when multi-process.
_sessions: dict[str, list[ModelMessage]] = defaultdict(list)


async def handle_text(session_id: str, text: str) -> AsyncIterator[str]:
    """Run the chat agent within a session, yielding streamed text tokens.

    The agent itself is stateless; we own the conversation history here and
    pass it in on every turn so the model sees prior context.
    """
    history = _sessions[session_id]

    async with chat_agent.run_stream(text, message_history=history) as result:
        async for chunk in result.stream_text(delta=True):
            if chunk:
                yield chunk

    # Persist the full conversation (history + this turn) for the next turn.
    _sessions[session_id] = result.all_messages()


async def handle_file(session_id: str, filename: str) -> AsyncIterator[str]:
    """Handle an uploaded file.

    Placeholder until the documents/ (parse) + memory/ (store) pipeline is
    built: just acknowledge receipt. Once the pipeline exists, this will run
    `ingest_agent` on the parsed text and persist the resulting memory nodes.
    """
    yield f"（文档摄取功能开发中，已收到：{filename}）"


def reset_session(session_id: str) -> None:
    """Clear a session's history (e.g., on an explicit reset request)."""
    _sessions.pop(session_id, None)
