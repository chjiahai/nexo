"""Minimal ingest agent: raw document text -> structured memory.

This is the first agent in Nexo's pipeline. It demonstrates the single-agent
pattern: one role, one system prompt, one structured output type. Orchestration
(sequencing / delegating to other agents) comes later in `app.py`.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from nexo.config import MODEL_NAME


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
    system_prompt=(
        "You are Nexo's ingest agent. Given raw document text, produce a "
        "structured memory representation: a title, a short summary, top "
        "keywords, and the document split into coherent, queryable memory "
        "nodes. Be precise and faithful to the source — do not invent facts."
    ),
    retries=1,
    defer_model_check=True,
)


async def run(text: str) -> IngestResult:
    """Run the ingest agent on a document and return structured memory."""
    result = await ingest_agent.run(text)
    return result.output


if __name__ == "__main__":
    _sample = (
        "PydanticAI is a Python agent framework that lets you build "
        "production-grade generative AI applications. It is model-agnostic, "
        "type-safe, and built on Pydantic. Agents are defined with a model, "
        "system prompt, and tools, and return structured, validated output."
    )
    out = asyncio.run(run(_sample))
    print(out.model_dump_json(indent=2))
