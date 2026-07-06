"""Tests for the documents processing pipeline.

Hermetic: no network, no real LLM. `ingest_agent_run` is monkeypatched;
UPLOADS_DIR / PROCESSED_DIR are redirected to tmp_path so the tests never
touch the real data/ tree. The pipeline receives already-decrypted bytes
(download + AES decrypt is the transport layer's job).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexo.agents.ingest import IngestResult
from nexo.documents import pipeline


@pytest.fixture(autouse=True)
def _redirect_dirs(tmp_path: Path, monkeypatch):
    """Keep file writes inside tmp_path."""
    uploads = tmp_path / "uploads"
    processed = tmp_path / "processed"
    uploads.mkdir()
    processed.mkdir()
    monkeypatch.setattr(pipeline, "UPLOADS_DIR", uploads)
    monkeypatch.setattr(pipeline, "PROCESSED_DIR", processed)
    yield


# --- helpers ---------------------------------------------------------------

async def _collect(file_data: bytes, filename: str) -> list[str]:
    chunks: list[str] = []
    async for chunk in pipeline.process_file(file_data, filename):
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


# --- _safe_filename --------------------------------------------------------

def test_safe_filename_strips_directory_components():
    """Path traversal attempts are reduced to a bare basename."""
    assert pipeline._safe_filename("../../etc/passwd") == "passwd"
    assert pipeline._safe_filename("sub/dir/report.pdf") == "report.pdf"
    assert pipeline._safe_filename("plain.pdf") == "plain.pdf"


# --- _extract_text ---------------------------------------------------------

def test_extract_text_txt(tmp_path: Path):
    f = tmp_path / "note.txt"
    f.write_text("hello world", encoding="utf-8")
    assert pipeline._extract_text(f) == "hello world"


def test_extract_text_docx(tmp_path: Path):
    docx = pytest.importorskip("docx")
    f = tmp_path / "doc.docx"
    document = docx.Document()
    document.add_paragraph("First paragraph.")
    document.add_paragraph("Second paragraph.")
    document.save(str(f))
    text = pipeline._extract_text(f)
    assert "First paragraph." in text
    assert "Second paragraph." in text


def test_extract_text_doc_raises_not_implemented(tmp_path: Path):
    f = tmp_path / "old.doc"
    f.write_bytes(b"dummy")
    with pytest.raises(NotImplementedError):
        pipeline._extract_text(f)


def test_extract_text_unsupported_type(tmp_path: Path):
    f = tmp_path / "image.png"
    f.write_bytes(b"dummy")
    with pytest.raises(ValueError, match="不支持的文件类型"):
        pipeline._extract_text(f)


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
    """Persist bytes -> extract (txt) -> summarize (faked) -> write summary."""
    monkeypatch.setattr(pipeline, "ingest_agent_run", _async_ingest)

    chunks = asyncio.run(_collect(b"the quick brown fox", "note.txt"))

    # Progress + final summary streamed out.
    assert any("正在解析" in c for c in chunks)
    assert any("正在生成摘要" in c for c in chunks)
    assert chunks[-1].startswith("## 📌 一句话核心摘要")

    # Original file persisted under uploads/, summary under processed/.
    assert (pipeline.UPLOADS_DIR / "note.txt").read_bytes() == b"the quick brown fox"
    out_md = (pipeline.PROCESSED_DIR / "note.md").read_text(encoding="utf-8")
    assert "这是一份关于示例文档的核心摘要。" in out_md


def test_process_file_empty_text_short_circuits(monkeypatch):
    """When no text can be extracted, the pipeline says so and skips summary."""
    async def fail_ingest(_):
        raise AssertionError("ingest should not run on empty text")
    monkeypatch.setattr(pipeline, "ingest_agent_run", fail_ingest)

    chunks = asyncio.run(_collect(b"   ", "blank.txt"))
    joined = "".join(chunks)
    assert "未能从文件中提取出文本" in joined
    # No summary written.
    assert not (pipeline.PROCESSED_DIR / "blank.md").exists()


def test_process_file_empty_bytes_raises():
    """Empty file content is a contract violation — raise, don't pretend."""
    with pytest.raises(ValueError, match="文件内容为空"):
        asyncio.run(_collect(b"", "empty.txt"))


# --- helper for monkeypatching the async ingest agent ----------------------

async def _async_ingest(_text: str) -> IngestResult:
    return _fake_ingest_result()
