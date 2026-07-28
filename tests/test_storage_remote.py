"""Tests for the remote-folder storage backend (scp via scripts/ship_media.sh).

Hermetic: no network. `_run_scp` (the subprocess call) is monkeypatched so we
verify path construction, temp-file lifecycle, error mapping, and retry
behavior without a real ssh/scp. The pure helpers (`_sniff_image_ext`,
`_safe_name`, `_build_key`, `_safe_user_id`) are tested directly.
"""

from __future__ import annotations

import asyncio
import re
import subprocess

import pytest

from nexo.storage import remote


# --- pure helpers ----------------------------------------------------------

def test_sniff_image_ext_detects_common_formats():
    assert remote._sniff_image_ext(b"\x89PNG\r\n\x1a\n....") == "png"
    assert remote._sniff_image_ext(b"\xff\xd8\xff\xe0rest") == "jpg"
    assert remote._sniff_image_ext(b"GIF89a...") == "gif"
    assert remote._sniff_image_ext(b"GIF87a...") == "gif"


def test_sniff_image_ext_unknown_is_bin():
    assert remote._sniff_image_ext(b"\x00\x01\x02unknown") == "bin"
    assert remote._sniff_image_ext(b"") == "bin"


def test_safe_name_strips_directory_components():
    assert remote._safe_name("../../etc/passwd") == "passwd"
    assert remote._safe_name("sub/dir/report.pdf") == "report.pdf"
    assert remote._safe_name("plain.pdf") == "plain.pdf"
    assert remote._safe_name("") == "unnamed"


def test_safe_name_truncates_overlong_while_keeping_extension():
    very_long = "a" * 300 + ".pdf"
    safe = remote._safe_name(very_long)
    assert len(safe) <= 180
    assert safe.endswith(".pdf")


def test_safe_user_id_strips_channel_prefix_and_scrubs_separators():
    assert remote._safe_user_id("wecom:2") == "2"
    assert remote._safe_user_id("wecom:group-xyz") == "group-xyz"
    assert remote._safe_user_id("plain") == "plain"
    assert remote._safe_user_id("wecom:a/b") == "a_b"
    assert remote._safe_user_id("wecom:a:b") == "a_b"
    assert remote._safe_user_id("") == "unknown"


def test_build_key_groups_by_user_and_is_time_prefixed():
    key = remote._build_key("docs", "wecom:2", "report.pdf")
    parts = key.split("/")
    assert parts[0] == "docs"
    assert parts[1] == "2"  # channel prefix stripped
    assert len(parts) == 3
    assert re.match(r"^\d{8}-\d{6}-report\.pdf$", parts[2]), parts[2]


# --- config fixture: pretend the env is configured -------------------------

@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(remote, "NEXO_UPLOAD_HOST", "host")
    monkeypatch.setattr(remote, "NEXO_UPLOAD_USER", "u")
    monkeypatch.setattr(remote, "NEXO_UPLOAD_DIR", "/data/up")
    monkeypatch.setattr(remote, "NEXO_UPLOAD_KEY", "")
    monkeypatch.setattr(remote, "NEXO_UPLOAD_PORT", "")


def _install_fake_scp(monkeypatch, *, fail_times: int = 0, fail_exc=None):
    """Replace _run_scp with a recorder. Optionally fail N times first."""
    calls: list[tuple[str, str]] = []
    attempts = {"n": 0}

    def fake_run(local_file: str, rel_path: str) -> None:
        attempts["n"] += 1
        if attempts["n"] <= fail_times:
            if fail_exc is not None:
                raise fail_exc
            raise subprocess.CalledProcessError(
                returncode=1, cmd=["bash", "ship_media.sh", local_file, rel_path],
                stderr="ssh: connection refused",
            )
        calls.append((local_file, rel_path))

    monkeypatch.setattr(remote, "_run_scp", fake_run)
    return calls


# --- upload_file / upload_image / upload_video ----------------------------

def test_upload_file_ships_bytes_and_returns_rel_path(configured, monkeypatch):
    calls = _install_fake_scp(monkeypatch)
    rel = asyncio.run(remote.upload_file("wecom:2", "报告.docx", b"doc-bytes"))
    assert rel.startswith("docs/2/")
    assert rel.endswith("报告.docx")
    assert len(calls) == 1
    local_file, shipped_rel = calls[0]
    assert shipped_rel == rel
    # temp file cleaned up
    import os
    assert not os.path.exists(local_file)


def test_upload_image_sniffs_ext_into_rel_path(configured, monkeypatch):
    calls = _install_fake_scp(monkeypatch)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    rel = asyncio.run(remote.upload_image("wecom:2", None, png))
    assert rel.startswith("imgs/2/")
    assert rel.endswith(".png")
    assert len(calls) == 1
    assert calls[0][1] == rel


def test_upload_image_unknown_format_uses_bin(configured, monkeypatch):
    _install_fake_scp(monkeypatch)
    rel = asyncio.run(remote.upload_image("wecom:2", None, b"\x00\x01weird"))
    assert rel.endswith(".bin")


def test_upload_video_uses_default_name_when_none(configured, monkeypatch):
    calls = _install_fake_scp(monkeypatch)
    rel = asyncio.run(remote.upload_video("wecom:2", None, b"\x00\x00"))
    assert rel.startswith("videos/2/")
    assert rel.endswith("video.mp4")
    assert len(calls) == 1


# --- error mapping + retry -------------------------------------------------

def test_missing_config_raises_before_subprocess(monkeypatch):
    """Missing config is permanent: RuntimeError, no subprocess call."""
    monkeypatch.setattr(remote, "NEXO_UPLOAD_HOST", "")
    monkeypatch.setattr(remote, "NEXO_UPLOAD_USER", "")
    monkeypatch.setattr(remote, "NEXO_UPLOAD_DIR", "")
    called = {"n": 0}
    monkeypatch.setattr(remote, "_run_scp", lambda *a: called.__setitem__("n", called["n"] + 1))
    with pytest.raises(RuntimeError, match="上传配置缺失"):
        asyncio.run(remote.upload_file("wecom:2", "x.pdf", b"x"))
    assert called["n"] == 0


def test_scp_failure_is_transient_and_retried(configured, monkeypatch):
    """A non-zero scp exit is TransientError; retry re-issues until it succeeds."""
    from nexo.errors import retry as real_retry

    async def fast_retry(factory, **kw):
        return await real_retry(factory, attempts=kw.get("attempts", 3), base_delay=0)

    monkeypatch.setattr(remote, "retry", fast_retry)
    calls = _install_fake_scp(monkeypatch, fail_times=2)  # 2 fails, then success
    rel = asyncio.run(remote.upload_file("wecom:2", "r.pdf", b"x"))
    assert rel.endswith("r.pdf")
    assert len(calls) == 1  # one success recorded after 2 failures


def test_scp_failure_exhausts_retries_and_raises_transient(configured, monkeypatch):
    from nexo.errors import TransientError, retry as real_retry

    async def fast_retry(factory, **kw):
        return await real_retry(factory, attempts=kw.get("attempts", 3), base_delay=0)

    monkeypatch.setattr(remote, "retry", fast_retry)
    _install_fake_scp(monkeypatch, fail_times=99)  # always fails
    with pytest.raises(TransientError, match="scp 传输失败"):
        asyncio.run(remote.upload_file("wecom:2", "r.pdf", b"x"))


def test_temp_file_cleaned_up_even_on_failure(configured, monkeypatch):
    from nexo.errors import retry as real_retry

    async def fast_retry(factory, **kw):
        return await real_retry(factory, attempts=kw.get("attempts", 3), base_delay=0)

    monkeypatch.setattr(remote, "retry", fast_retry)
    seen: list[str] = []

    def fake_run(local_file: str, rel_path: str) -> None:
        seen.append(local_file)
        raise subprocess.CalledProcessError(1, ["bash", "ship_media.sh"])

    monkeypatch.setattr(remote, "_run_scp", fake_run)
    import os
    with pytest.raises(Exception):
        asyncio.run(remote.upload_file("wecom:2", "r.pdf", b"x"))
    # every temp path created was cleaned up despite the failure
    assert all(not os.path.exists(p) for p in seen)
