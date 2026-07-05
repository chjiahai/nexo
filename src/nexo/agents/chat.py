"""Minimal chat agent: text-in -> streamed text-out.

The natural fit for a chat bot over WebSocket — streams raw text tokens.
Contrast with `ingest.py`, which returns structured memory nodes (and is
therefore not suited to streaming chat replies).
"""

from __future__ import annotations

from pydantic_ai import Agent

from nexo.config import MODEL_NAME
from nexo.prompts import CHAT_SYSTEM_PROMPT

chat_agent = Agent(
    model=MODEL_NAME,
    output_type=str,
    system_prompt=CHAT_SYSTEM_PROMPT,
    defer_model_check=True,
)
