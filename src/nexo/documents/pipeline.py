"""File processing pipeline: extract text -> summarize.

Wired into the WeCom file route via `nexo.app.handle_file`. The transport
layer (wecom.py) owns download + AES decryption via the SDK; this module only
sees the already-decrypted file bytes. The pipeline is an `AsyncIterator[str]`
so each stage yields a progress chunk that the transport streams back to the
WeCom bubble:

    "正在解析…" -> "正在生成摘要…" -> <summary markdown>

Original files are persisted under `data/uploads/`; generated summary markdown
under `data/processed/<stem>.md`. Intermediate full-text is held in memory only.

DeepSeek (the configured OpenAI-compatible model) has no file-upload endpoint,
so text is always extracted client-side before being handed to `ingest_agent`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from nexo.agents.ingest import IngestResult, run as ingest_agent_run
from nexo.config import PROCESSED_DIR, UPLOADS_DIR
from nexo.prompts import INGEST_SUMMARY_TEMPLATE, msg


async def process_file(file_data: bytes, filename: str) -> AsyncIterator[str]:
    """Persist `file_data`, extract text, summarize, persist results.

    `file_data` is the already-decrypted file content (download + AES decrypt
    happens in the transport layer). Yields progress messages and finally the
    summary markdown. Original bytes are written to UPLOADS_DIR; summary `.md`
    to PROCESSED_DIR/<stem>.md.
    """
    if not file_data:
        raise ValueError("文件内容为空，无法处理")

    # 1. Persist the original.
    dest = UPLOADS_DIR / _safe_filename(filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(file_data)

    # 2. Extract text.
    yield msg("file_parsing", name=dest.name)
    text = _extract_text(dest)
    if not text.strip():
        yield msg("file_extract_empty")
        return

    # 3. Summarize via the existing ingest agent.
    yield msg("file_summarizing")
    result = await ingest_agent_run(text)

    # 4. Persist summary + reply.
    md = _format_markdown(result)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PROCESSED_DIR / f"{dest.stem}.md"
    out_path.write_text(md, encoding="utf-8")
    yield md


def _safe_filename(name: str) -> str:
    """Strip any directory component to prevent path traversal, keep it sane."""
    base = Path(name).name or "unnamed"
    return base


def _extract_text(path: Path) -> str:
    """Extract plain text from a file, dispatching by extension."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".doc":
        raise NotImplementedError("旧版 .doc 暂不支持，请另存为 .docx 后重试")
    if suffix in (".txt", ".md", ".markdown", ".csv", ".log"):
        return _read_text(path)
    raise ValueError(f"不支持的文件类型: {suffix or '(无扩展名)'}")


def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


def _extract_docx(path: Path) -> str:
    import docx  # python-docx

    document = docx.Document(str(path))
    parts: list[str] = [p.text for p in document.paragraphs if p.text]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _read_text(path: Path) -> str:
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return ""


def _format_markdown(result: IngestResult) -> str:
    """Render an IngestResult as a self-contained summary markdown document."""
    keywords = ", ".join(result.keywords) if result.keywords else msg("no_keywords")
    return INGEST_SUMMARY_TEMPLATE.format(
        title=result.title, summary=result.summary, keywords=keywords
    )
