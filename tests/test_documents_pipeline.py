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
    return IngestResult(
        title="Sample Title",
        summary="A short summary of the document.",
        keywords=["alpha", "beta"],
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

def test_format_markdown_includes_title_summary_keywords():
    md = pipeline._format_markdown(_fake_ingest_result())
    assert "# Sample Title" in md
    assert "A short summary of the document." in md
    assert "alpha, beta" in md


def test_format_markdown_handles_empty_keywords():
    result = IngestResult(title="T", summary="S", keywords=[])
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
    assert chunks[-1].startswith("# Sample Title")

    # Original file persisted under uploads/, summary under processed/.
    assert (pipeline.UPLOADS_DIR / "note.txt").read_bytes() == b"the quick brown fox"
    out_md = (pipeline.PROCESSED_DIR / "note.md").read_text(encoding="utf-8")
    assert "Sample Title" in out_md


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
