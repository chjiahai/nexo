"""Tests for the documents processing pipeline.

Hermetic: no network, no real LLM, no real OBS. `ingest_agent_run` and
`upload_upload` are monkeypatched, so the pipeline never touches the real OBS
service or any model. The pipeline receives already-decrypted bytes (download
+ AES decrypt is the transport layer's job); text is now extracted in memory,
so nothing is written to disk anymore.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from nexo.agents.ingest import IngestResult
from nexo.documents import pipeline


@pytest.fixture(autouse=True)
def _stub_obs(monkeypatch):
    """Stub the OBS upload so pipeline tests never hit the network."""
    async def fake_upload(user_id: str, filename: str, data: bytes) -> str:
        return f"docs/fake/{user_id}/{filename}"

    monkeypatch.setattr(pipeline, "upload_upload", fake_upload)
    yield


# --- helpers ---------------------------------------------------------------

async def _collect(file_data: bytes, filename: str, user_id: str = "wecom:u1") -> list[str]:
    chunks: list[str] = []
    async for chunk in pipeline.process_file(file_data, filename, user_id):
        chunks.append(chunk)
    return chunks


def _fake_ingest_result() -> IngestResult:
    from nexo.agents.ingest import KeyPoint

    return IngestResult(
        core_summary="这是一份关于示例文档的核心摘要。",
        key_points=[
            KeyPoint(headline="要点甲", detail="含关键数据 100 的解释。"),
            KeyPoint(headline="要点乙", detail="另一条带事实的说明。"),
        ],
        tags=["alpha", "beta"],
        background=None,
        action_items=[],
    )


# --- _extract_text (now reads bytes in memory) -----------------------------

def test_extract_text_txt():
    assert pipeline._extract_text("note.txt", b"hello world") == "hello world"


def test_extract_text_docx():
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("First paragraph.")
    document.add_paragraph("Second paragraph.")
    buf = io.BytesIO()
    document.save(buf)
    text = pipeline._extract_text("doc.docx", buf.getvalue())
    assert "First paragraph." in text
    assert "Second paragraph." in text


def test_extract_text_doc_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        pipeline._extract_text("old.doc", b"dummy")


def test_extract_text_unsupported_type():
    with pytest.raises(ValueError, match="不支持的文件类型"):
        pipeline._extract_text("image.png", b"dummy")


# --- _format_markdown ------------------------------------------------------

def test_format_markdown_renders_required_sections():
    md = pipeline._format_markdown(_fake_ingest_result())
    # Required sections present.
    assert "## 📌 一句话核心摘要" in md
    assert "这是一份关于示例文档的核心摘要。" in md
    assert "## 🎯 核心要点提炼" in md
    # Key points render as **headline**：detail.
    assert "**要点甲**：含关键数据 100 的解释。" in md
    assert "**要点乙**：另一条带事实的说明。" in md
    # Tags render as #tag.
    assert "## 🔑 关键词/标签" in md
    assert "#alpha #beta" in md
    # Optional sections absent when not filled.
    assert "背景/上下文" not in md
    assert "下步行动" not in md


def test_format_markdown_omits_optional_sections_when_empty():
    from nexo.agents.ingest import KeyPoint

    result = IngestResult(
        core_summary="只有摘要。",
        key_points=[KeyPoint(headline="唯一", detail="说明。")],
        tags=["x"],
        background=None,
        action_items=[],
    )
    md = pipeline._format_markdown(result)
    assert "背景/上下文" not in md
    assert "下步行动" not in md


def test_format_markdown_includes_optional_sections_when_filled():
    from nexo.agents.ingest import KeyPoint

    result = IngestResult(
        core_summary="带可选节的摘要。",
        key_points=[KeyPoint(headline="要点", detail="说明。")],
        tags=["x"],
        background="文档成文于某次会议之后。",
        action_items=["张三 8 月底前完成方案", "复核数据"],
    )
    md = pipeline._format_markdown(result)
    assert "## 🎬 背景/上下文" in md
    assert "文档成文于某次会议之后。" in md
    assert "## 🛠️ 下步行动/待办事项" in md
    assert "- 张三 8 月底前完成方案" in md
    assert "- 复核数据" in md


def test_format_markdown_handles_empty_tags():
    from nexo.agents.ingest import KeyPoint

    result = IngestResult(
        core_summary="x",
        key_points=[KeyPoint(headline="h", detail="d")],
        tags=[],
    )
    md = pipeline._format_markdown(result)
    assert "（无）" in md


# --- process_file end-to-end ----------------------------------------------

def test_process_file_end_to_end(monkeypatch):
    """Upload original to OBS -> extract (txt) -> summarize (faked) -> reply."""
    monkeypatch.setattr(pipeline, "ingest_agent_run", _async_ingest)

    uploaded: list[tuple[str, str, bytes]] = []

    async def record_upload(user_id: str, filename: str, data: bytes) -> str:
        uploaded.append((user_id, filename, data))
        return "docs/fake/u1/note.txt"

    monkeypatch.setattr(pipeline, "upload_upload", record_upload)

    chunks = asyncio.run(_collect(b"the quick brown fox", "note.txt", "wecom:u1"))

    # Progress + final summary streamed out.
    assert any("正在解析" in c for c in chunks)
    assert any("正在生成摘要" in c for c in chunks)
    assert chunks[-1].startswith("## 📌 一句话核心摘要")

    # Original bytes uploaded to OBS once, under the user's folder; nothing local.
    assert uploaded == [("wecom:u1", "note.txt", b"the quick brown fox")]


def test_process_file_upload_failure_surfaces_error(monkeypatch):
    """An OBS upload error becomes a readable message; summarization is skipped."""
    async def fail_upload(user_id: str, filename: str, data: bytes) -> str:
        raise RuntimeError("obs down")

    async def fail_ingest(_):  # pragma: no cover — must not run
        raise AssertionError("ingest should not run when upload fails")

    monkeypatch.setattr(pipeline, "upload_upload", fail_upload)
    monkeypatch.setattr(pipeline, "ingest_agent_run", fail_ingest)

    chunks = asyncio.run(_collect(b"the quick brown fox", "note.txt"))
    assert any("保存到对象存储失败" in c for c in chunks)
    assert any("obs down" in c for c in chunks)


def test_process_file_empty_text_short_circuits(monkeypatch):
    """When no text can be extracted, the pipeline says so and skips summary."""
    async def fail_ingest(_):
        raise AssertionError("ingest should not run on empty text")

    monkeypatch.setattr(pipeline, "ingest_agent_run", fail_ingest)

    chunks = asyncio.run(_collect(b"   ", "blank.txt"))
    joined = "".join(chunks)
    assert "未能从文件中提取出文本" in joined


def test_process_file_empty_bytes_raises():
    """Empty file content is a contract violation — raise, don't pretend."""
    with pytest.raises(ValueError, match="文件内容为空"):
        asyncio.run(_collect(b"", "empty.txt"))


# --- helper for monkeypatching the async ingest agent ----------------------

async def _async_ingest(_text: str) -> IngestResult:
    return _fake_ingest_result()
