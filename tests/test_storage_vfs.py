"""Tests for the local VFS storage backend (writes to NEXO_VFS_DIR).

Hermetic: no network, no real nexo-vfs mount. `NEXO_VFS_DIR` is pointed at a
tmp dir and `NEXO_ORG_ID` is monkeypatched, so we verify real on-disk writes —
path layout, content, ext sniffing, default names, and the config fast-fail —
without any external dependency. The pure helpers (`_sniff_image_ext`,
`_safe_name`, `_build_key`, `_safe_user_id`, `_safe_segment`) are tested
directly.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from nexo.storage import vfs


# --- pure helpers ----------------------------------------------------------

def test_sniff_image_ext_detects_common_formats():
    assert vfs._sniff_image_ext(b"\x89PNG\r\n\x1a\n....") == "png"
    assert vfs._sniff_image_ext(b"\xff\xd8\xff\xe0rest") == "jpg"
    assert vfs._sniff_image_ext(b"GIF89a...") == "gif"
    assert vfs._sniff_image_ext(b"GIF87a...") == "gif"


def test_sniff_image_ext_unknown_is_bin():
    assert vfs._sniff_image_ext(b"\x00\x01\x02unknown") == "bin"
    assert vfs._sniff_image_ext(b"") == "bin"


def test_safe_name_strips_directory_components():
    assert vfs._safe_name("../../etc/passwd") == "passwd"
    assert vfs._safe_name("sub/dir/report.pdf") == "report.pdf"
    assert vfs._safe_name("plain.pdf") == "plain.pdf"
    assert vfs._safe_name("") == "unnamed"


def test_safe_name_truncates_overlong_while_keeping_extension():
    very_long = "a" * 300 + ".pdf"
    safe = vfs._safe_name(very_long)
    assert len(safe) <= 180
    assert safe.endswith(".pdf")


def test_safe_segment_scrubs_separators_and_empties():
    assert vfs._safe_segment("a/b") == "a_b"
    assert vfs._safe_segment("a:b") == "a_b"
    assert vfs._safe_segment("plain") == "plain"
    assert vfs._safe_segment("") == "unknown"
    assert vfs._safe_segment("  ") == "unknown"


def test_safe_user_id_strips_channel_prefix_and_scrubs_separators():
    assert vfs._safe_user_id("wecom:2") == "2"
    assert vfs._safe_user_id("wecom:group-xyz") == "group-xyz"
    assert vfs._safe_user_id("plain") == "plain"
    assert vfs._safe_user_id("wecom:a/b") == "a_b"
    assert vfs._safe_user_id("wecom:a:b") == "a_b"
    assert vfs._safe_user_id("") == "unknown"


def test_build_key_groups_by_org_then_user_and_is_time_prefixed(monkeypatch):
    monkeypatch.setattr(vfs, "NEXO_ORG_ID", "org42")
    key = vfs._build_key("wecom:2", "report.pdf")
    parts = key.split("/")
    assert parts[0] == "org42"
    assert parts[1] == "2"  # channel prefix stripped
    assert len(parts) == 3
    assert re.match(r"^\d{8}-\d{6}-report\.pdf$", parts[2]), parts[2]


# --- config fixture: a real tmp VFS root + org id --------------------------

@pytest.fixture
def configured(tmp_path, monkeypatch):
    root = tmp_path / "vfs"
    root.mkdir()
    monkeypatch.setattr(vfs, "NEXO_VFS_DIR", str(root))
    monkeypatch.setattr(vfs, "NEXO_ORG_ID", "org42")
    return root


# --- upload_file / upload_image / upload_video -----------------------------

def test_upload_file_writes_under_org_user_folder(configured):
    rel = asyncio.run(vfs.upload_file("wecom:2", "报告.docx", b"doc-bytes"))
    parts = rel.split("/")
    assert parts[0] == "org42"
    assert parts[1] == "2"
    assert parts[2].endswith("报告.docx")
    written = configured / rel
    assert written.is_file()
    assert written.read_bytes() == b"doc-bytes"


def test_upload_image_sniffs_ext_into_path(configured):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    rel = asyncio.run(vfs.upload_image("wecom:2", None, png))
    assert rel.startswith("org42/2/")
    assert rel.endswith(".png")
    assert (configured / rel).read_bytes() == png


def test_upload_image_unknown_format_uses_bin(configured):
    rel = asyncio.run(vfs.upload_image("wecom:2", None, b"\x00\x01weird"))
    assert rel.endswith(".bin")


def test_upload_video_uses_default_name_when_none(configured):
    rel = asyncio.run(vfs.upload_video("wecom:2", None, b"\x00\x00"))
    assert rel.startswith("org42/2/")
    assert rel.endswith("video.mp4")
    assert (configured / rel).read_bytes() == b"\x00\x00"


def test_upload_creates_nested_user_folder(configured):
    # user folder must not pre-exist; mkdir(parents=True) should create it.
    rel = asyncio.run(vfs.upload_file("wecom:new-user", "f.txt", b"x"))
    assert (configured / rel).is_file()


# --- config fast-fail ------------------------------------------------------

def test_missing_vfs_dir_raises_before_write(monkeypatch):
    monkeypatch.setattr(vfs, "NEXO_VFS_DIR", "")
    monkeypatch.setattr(vfs, "NEXO_ORG_ID", "org42")
    with pytest.raises(RuntimeError, match="上传配置缺失"):
        asyncio.run(vfs.upload_file("wecom:2", "x.pdf", b"x"))


def test_missing_org_id_raises_before_write(monkeypatch, tmp_path):
    monkeypatch.setattr(vfs, "NEXO_VFS_DIR", str(tmp_path))
    monkeypatch.setattr(vfs, "NEXO_ORG_ID", "")
    with pytest.raises(RuntimeError, match="上传配置缺失"):
        asyncio.run(vfs.upload_file("wecom:2", "x.pdf", b"x"))
