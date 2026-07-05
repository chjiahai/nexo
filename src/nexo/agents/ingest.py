"""Minimal ingest agent: raw document text -> structured memory.

This is the first agent in Nexo's pipeline. It demonstrates the single-agent
pattern: one role, one system prompt, one structured output type. Orchestration
(sequencing / delegating to other agents) comes later in `app.py`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

from nexo.config import MODEL_NAME
from nexo.prompts import INGEST_SYSTEM_PROMPT


class MemoryNode(BaseModel):
    """A single chunk of extracted, queryable memory."""

    text: str = Field(description="The extracted text chunk")
    topic: str = Field(description="Short topic label for this chunk")


class IngestResult(BaseModel):
    """Structured output of the ingest agent — the document as memory."""

    title: str = Field(description="Concise title for the document")
    summary: str = Field(description="2-3 sentence summary")
    keywords: list[str] = Field(default_factory=list, description="Top keywords")
    memory_nodes: list[MemoryNode] = Field(
        default_factory=list,
        description="Document split into coherent, queryable memory chunks",
    )


ingest_agent = Agent(
    model=MODEL_NAME,
    output_type=IngestResult,
    system_prompt=INGEST_SYSTEM_PROMPT,
    retries=1,
    defer_model_check=True,
)

# Structured output is realized via tool calling (tool_choice), which DeepSeek's
# thinking models reject ("Thinking mode does not support this tool_choice").
# Disable thinking for this agent — summarization doesn't need it, and it's
# incompatible with the forced tool call either way. chat_agent (output_type=str)
# has no tool_choice, so thinking stays on there.
_NO_THINK_SETTINGS = ModelSettings(extra_body={"thinking": {"type": "disabled"}})


async def run(text: str) -> IngestResult:
    """Run the ingest agent on a document and return structured memory."""
    result = await ingest_agent.run(text, model_settings=_NO_THINK_SETTINGS)
    return result.output
