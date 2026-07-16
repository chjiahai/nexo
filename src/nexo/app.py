"""Application layer: request routing, session management, agent invocation.

Sits between the API layer (transport) and the Agent layer (pure agents).
Routing is deterministic by request type — there is no router agent. Each
request kind has its own `handle_*` entry point; the transport layer picks
the right one based on the message type (e.g. WeCom `msgtype`):

    text  -> handle_text  -> chat_agent (streamed)      [live]
    file  -> handle_file  -> TOS storage + ack          [live]
    image -> handle_image -> TOS storage + ack          [live]

This is the only layer that holds cross-turn state (session history, for the
text route). It does not import any transport code.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncIterator

from pydantic_ai.messages import ModelMessage

from nexo.agents.chat import chat_agent
from nexo.observability import trace_span

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
    """Handle an uploaded file: store the original to TOS, then acknowledge.

    `file_data` is the already-downloaded-and-decrypted file content (the
    transport layer handles download + AES decryption). The file is persisted
    to TOS under docs/<user_id>/; we reply with a short confirmation. No
    parsing/summarization yet — storage is the first step toward a later
    document-processing pipeline.
    """
    from nexo.prompts import msg
    from nexo.storage.tos import upload_upload

    try:
        yield msg("file_saving")
        with trace_span("TOS upload", input=filename):
            await upload_upload(session_id, filename, file_data)
        yield msg("file_saved")
    except Exception as exc:  # noqa: BLE001 — friendly message, bubble always gets a finish frame
        yield msg("file_save_failed", error=exc)


async def handle_image(session_id: str, image_data: bytes) -> AsyncIterator[str]:
    """Handle an uploaded image: store it to TOS, then acknowledge.

    `image_data` is the already-downloaded-and-decrypted image bytes (the
    transport layer handles download + AES decryption). The image is persisted
    to TOS under imgs/<user_id>/; we reply with a short confirmation. No
    OCR/vision yet — storage is the first step toward a later multimodal pipeline.
    """
    from nexo.prompts import msg
    from nexo.storage.tos import upload_image

    try:
        yield msg("image_saving")
        with trace_span("TOS upload"):
            await upload_image(session_id, image_data)
        yield msg("image_saved")
    except Exception as exc:  # noqa: BLE001 — friendly message, bubble always gets a finish frame
        yield msg("image_save_failed", error=exc)


def reset_session(session_id: str) -> None:
    """Clear a session's history (e.g., on an explicit reset request)."""
    _sessions.pop(session_id, None)
