"""Application layer: request routing, session management, agent invocation.

Sits between the API layer (transport) and the Agent layer (pure agents).
Routing is deterministic by request type — there is no router agent. Each
request kind has its own `handle_*` entry point; the transport layer picks
the right one based on the message type (e.g. WeCom `msgtype`):

    text -> handle_text -> chat_agent (streamed)        [live]
    file -> handle_file -> documents pipeline           [live]

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


async def handle_file(session_id: str, filename: str, file_data: bytes) -> AsyncIterator[str]:
    """Handle an uploaded file: extract text, summarize, persist.

    `file_data` is the already-downloaded-and-decrypted file content (the
    transport layer handles download + AES decryption). Streams progress chunks
    to the transport. The documents pipeline owns the heavy lifting; we wrap it
    so a failure still yields a friendly Chinese message and the WeCom bubble
    always receives a finish frame.
    """
    from nexo.documents.pipeline import process_file  # lazy: avoid import overhead at bot startup
    from nexo.prompts import msg

    try:
        async for chunk in process_file(file_data, filename):
            yield chunk
    except Exception as exc:  # noqa: BLE001 — surface a readable message to the user
        yield msg("file_handle_failed", error=exc)


def reset_session(session_id: str) -> None:
    """Clear a session's history (e.g., on an explicit reset request)."""
    _sessions.pop(session_id, None)
