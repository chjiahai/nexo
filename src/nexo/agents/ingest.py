"""Ingest agent: raw document text -> structured summary.

Single-agent pattern: one role, one system prompt, one structured output type.
The output schema (`IngestResult`) mirrors the summary template rendered to
`data/processed/<stem>.md` by `nexo.documents.pipeline._format_markdown`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

from nexo.config import MODEL_NAME
from nexo.prompts import INGEST_SYSTEM_PROMPT


class KeyPoint(BaseModel):
    """One bullet of the「核心要点提炼」section."""

    headline: str = Field(description="核心词或小标题，简短")
    detail: str = Field(description="具体解释，含关键数据/事实/论据")


class IngestResult(BaseModel):
    """Structured summary of a document, following the summary template."""

    core_summary: str = Field(description="一句话核心摘要，不超过50字")
    key_points: list[KeyPoint] = Field(description="3-5 个核心要点")
    tags: list[str] = Field(description="3-5 个关键词/标签")
    background: str | None = Field(
        default=None, description="背景/上下文，文档无明确背景则留空"
    )
    action_items: list[str] = Field(
        default_factory=list, description="下步行动/待办事项，文档不涉及则留空"
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
    """Run the ingest agent on a document and return the structured summary."""
    result = await ingest_agent.run(text, model_settings=_NO_THINK_SETTINGS)
    return result.output
