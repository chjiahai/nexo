"""Tests for the centralized prompts loader.

`prompts.toml` is the source of truth for every system prompt and user-facing
string. These tests guard the contract: the file must parse, the agent prompts
must be non-empty, and `msg()` must format placeholders (and fail loud on a
bad key so a typo can't silently ship an empty bubble).
"""

from __future__ import annotations

import pytest

from nexo.prompts import (
    CHAT_SYSTEM_PROMPT,
    INGEST_SYSTEM_PROMPT,
    msg,
)


def test_chat_system_prompt_is_nonempty():
    assert CHAT_SYSTEM_PROMPT.strip()
    assert "Nexo" in CHAT_SYSTEM_PROMPT


def test_ingest_system_prompt_is_nonempty():
    assert INGEST_SYSTEM_PROMPT.strip()
    # The new prompt drives the summary template — it must mention the key
    # contract terms (faithfulness + the core-summary length cap).
    assert "忠于" in INGEST_SYSTEM_PROMPT
    assert "core_summary" in INGEST_SYSTEM_PROMPT
    assert "500" in INGEST_SYSTEM_PROMPT


def test_msg_returns_plain_string_without_placeholders():
    assert msg("thinking") == "正在思考…"
    assert msg("file_downloading") == "正在下载文件…"


def test_msg_formats_placeholders():
    out = msg("file_download_failed", error="bad aeskey")
    assert out == "（下载失败：bad aeskey）"
    out2 = msg("file_parsing", name="report.pdf")
    assert out2 == "已下载 report.pdf，正在解析…"


def test_msg_raises_on_unknown_key():
    """A typo'd key is a bug — fail loud, don't return a silent empty string."""
    with pytest.raises(KeyError):
        msg("nonexistent_key")


def test_msg_raises_on_missing_placeholder():
    """A template with {placeholder} must be given matching kwargs."""
    with pytest.raises(KeyError):
        msg("file_download_failed")  # missing `error`
