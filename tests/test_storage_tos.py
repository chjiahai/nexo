"""Tests for the TOS storage wrapper.

Hermetic: no network. `put_bytes`/`upload_*` are driven against an injected
fake `TosClientV2` (via the module's `_set_client_for_test` hook), so we verify
key construction, metadata/content-type handling, and error mapping without a
real bucket. The pure helpers (`_sniff_image_ext`, `_safe_name`, `_build_key`)
are tested directly.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from nexo.storage import tos


class _FakeResp:
    def __init__(self, status_code=200, code="", message=""):
        self.status_code = status_code
        self.code = code
        self.message = message


class FakeTosClient:
    def __init__(self) -> None:
        self.puts: list[dict] = []
        self.next_resp = _FakeResp()

    def put_object(self, bucket, key, content=None, content_type=None, meta=None, **_):
        self.puts.append(
            dict(bucket=bucket, key=key, content=content, content_type=content_type, meta=meta)
        )
        return self.next_resp


@pytest.fixture
def fake_client():
    client = FakeTosClient()
    tos._set_client_for_test(client)
    yield client
    tos._set_client_for_test(None)


# --- pure helpers ----------------------------------------------------------

def test_sniff_image_ext_detects_common_formats():
    assert tos._sniff_image_ext(b"\x89PNG\r\n\x1a\n....") == "png"
    assert tos._sniff_image_ext(b"\xff\xd8\xff\xe0rest") == "jpg"
    assert tos._sniff_image_ext(b"GIF89a...") == "gif"
    assert tos._sniff_image_ext(b"GIF87a...") == "gif"


def test_sniff_image_ext_unknown_is_bin():
    assert tos._sniff_image_ext(b"\x00\x01\x02unknown") == "bin"
    assert tos._sniff_image_ext(b"") == "bin"


def test_safe_name_strips_directory_components():
    assert tos._safe_name("../../etc/passwd") == "passwd"
    assert tos._safe_name("sub/dir/report.pdf") == "report.pdf"
    assert tos._safe_name("plain.pdf") == "plain.pdf"
    assert tos._safe_name("") == "unnamed"


def test_safe_name_truncates_overlong_while_keeping_extension():
    very_long = "a" * 300 + ".pdf"
    safe = tos._safe_name(very_long)
    assert len(safe) <= 180
    assert safe.endswith(".pdf")


def test_safe_user_id_strips_channel_prefix_and_scrubs_separators():
    assert tos._safe_user_id("wecom:2") == "2"
    assert tos._safe_user_id("wecom:group-xyz") == "group-xyz"
    assert tos._safe_user_id("plain") == "plain"  # no channel prefix -> kept whole
    # Separators inside the id portion are scrubbed (path-traversal guard).
    assert tos._safe_user_id("wecom:a/b") == "a_b"
    assert tos._safe_user_id("wecom:a:b") == "a_b"
    assert tos._safe_user_id("") == "unknown"


def test_build_key_groups_by_user_and_is_time_prefixed():
    key = tos._build_key("docs", "wecom:2", "report.pdf")
    # prefix/<user_id>/YYYYMMDD-HHMMSS-leaf
    parts = key.split("/")
    assert parts[0] == "docs"
    assert parts[1] == "2"  # channel prefix stripped from the session id
    assert len(parts) == 3  # no extra nesting beyond the user folder
    assert re.match(r"^\d{8}-\d{6}-report\.pdf$", parts[2]), parts[2]


# --- put_bytes via injected fake client ------------------------------------

def test_put_bytes_success_returns_key(fake_client):
    key = asyncio.run(tos.put_bytes("uploads/x/y.txt", b"hello"))
    assert key == "uploads/x/y.txt"
    assert len(fake_client.puts) == 1
    put = fake_client.puts[0]
    assert put["content"] == b"hello"
    assert put["content_type"] is None
    assert put["meta"] is None


def test_put_bytes_passes_content_type_and_metadata(fake_client):
    asyncio.run(
        tos.put_bytes(
            "images/x/y.png", b"\x89PNG", content_type="image/png", metadata={"k": "v"}
        )
    )
    put = fake_client.puts[0]
    assert put["content_type"] == "image/png"
    assert put["meta"] == {"k": "v"}


def test_put_bytes_raises_on_tos_error(fake_client):
    fake_client.next_resp = _FakeResp(status_code=403, code="AccessDenied",
                                      message="no permission")
    with pytest.raises(RuntimeError, match="TOS 上传失败"):
        asyncio.run(tos.put_bytes("k", b"x"))


def test_put_bytes_maps_sdk_exception_to_runtime_error(fake_client):
    """A raised SDK exception (e.g. TosClientError) becomes a readable RuntimeError."""
    def boom(**_):
        raise RuntimeError("network down")

    fake_client.put_object = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="TOS 上传失败：network down"):
        asyncio.run(tos.put_bytes("k", b"x"))


# --- upload_upload / upload_image ------------------------------------------

def test_upload_upload_builds_key_under_docs_user_folder(fake_client):
    key = asyncio.run(tos.upload_upload("wecom:2", "报告.docx", b"doc-bytes"))
    assert key.startswith("docs/2/")
    assert key.endswith("报告.docx")
    put = fake_client.puts[0]
    assert put["content"] == b"doc-bytes"
    assert put["meta"] == {"original-name": "报告.docx"}


def test_upload_image_sniffs_ext_and_sets_content_type(fake_client):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    key = asyncio.run(tos.upload_image("wecom:2", png))
    assert key.startswith("imgs/2/")
    assert key.endswith(".png")
    put = fake_client.puts[0]
    assert put["content_type"] == "image/png"
    assert put["content"] == png


def test_upload_image_unknown_format_uses_bin(fake_client):
    key = asyncio.run(tos.upload_image("wecom:2", b"\x00\x01weird"))
    assert key.startswith("imgs/2/")
    assert key.endswith(".bin")
    assert fake_client.puts[0]["content_type"] == "application/octet-stream"


# --- config-missing fails fast ---------------------------------------------

def test_get_client_raises_when_config_missing(monkeypatch):
    monkeypatch.setattr(tos, "TOS_ACCESS_KEY_ID", "")
    monkeypatch.setattr(tos, "TOS_SECRET_ACCESS_KEY", "")
    monkeypatch.setattr(tos, "TOS_ENDPOINT", "")
    monkeypatch.setattr(tos, "TOS_REGION", "")
    monkeypatch.setattr(tos, "TOS_BUCKET", "")
    tos._set_client_for_test(None)  # force rebuild
    with pytest.raises(RuntimeError, match="TOS 配置缺失"):
        asyncio.run(tos.put_bytes("k", b"x"))
