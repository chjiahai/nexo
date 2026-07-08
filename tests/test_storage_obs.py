"""Tests for the OBS storage wrapper.

Hermetic: no network. `put_bytes`/`upload_*` are driven against an injected
fake `ObsClient` (via the module's `_set_client_for_test` hook), so we verify
key construction, metadata/content-type handling, and error mapping without a
real bucket. The pure helpers (`_sniff_image_ext`, `_safe_name`, `_build_key`)
are tested directly.
"""

from __future__ import annotations

import asyncio

import pytest

from nexo.storage import obs


class _FakeResp:
    def __init__(self, status=200, errorCode="", errorMessage=""):
        self.status = status
        self.errorCode = errorCode
        self.errorMessage = errorMessage


class FakeObsClient:
    def __init__(self) -> None:
        self.puts: list[dict] = []
        self.next_resp = _FakeResp()

    def putContent(self, bucket, key, content=None, metadata=None, headers=None, **_):
        self.puts.append(
            dict(bucket=bucket, key=key, content=content, metadata=metadata, headers=headers)
        )
        return self.next_resp


@pytest.fixture
def fake_client():
    client = FakeObsClient()
    obs._set_client_for_test(client)
    yield client
    obs._set_client_for_test(None)


# --- pure helpers ----------------------------------------------------------

def test_sniff_image_ext_detects_common_formats():
    assert obs._sniff_image_ext(b"\x89PNG\r\n\x1a\n....") == "png"
    assert obs._sniff_image_ext(b"\xff\xd8\xff\xe0rest") == "jpg"
    assert obs._sniff_image_ext(b"GIF89a...") == "gif"
    assert obs._sniff_image_ext(b"GIF87a...") == "gif"


def test_sniff_image_ext_unknown_is_bin():
    assert obs._sniff_image_ext(b"\x00\x01\x02unknown") == "bin"
    assert obs._sniff_image_ext(b"") == "bin"


def test_safe_name_strips_directory_components():
    assert obs._safe_name("../../etc/passwd") == "passwd"
    assert obs._safe_name("sub/dir/report.pdf") == "report.pdf"
    assert obs._safe_name("plain.pdf") == "plain.pdf"
    assert obs._safe_name("") == "unnamed"


def test_safe_name_truncates_overlong_while_keeping_extension():
    very_long = "a" * 300 + ".pdf"
    safe = obs._safe_name(very_long)
    assert len(safe) <= 180
    assert safe.endswith(".pdf")


def test_build_key_is_time_prefixed_and_sortable():
    key = obs._build_key("uploads", "report.pdf")
    # prefix/YYYYMMDD/HHMMSS-leaf
    parts = key.split("/")
    assert parts[0] == "uploads"
    assert parts[1].isdigit() and len(parts[1]) == 8  # YYYYMMDD
    assert parts[2].endswith("-report.pdf")
    assert len(parts[2].split("-")[0]) == 6  # HHMMSS


# --- put_bytes via injected fake client ------------------------------------

def test_put_bytes_success_returns_key(fake_client):
    key = asyncio.run(obs.put_bytes("uploads/x/y.txt", b"hello"))
    assert key == "uploads/x/y.txt"
    assert len(fake_client.puts) == 1
    put = fake_client.puts[0]
    assert put["content"] == b"hello"
    assert put["metadata"] is None
    assert put["headers"] is None  # no explicit content-type


def test_put_bytes_passes_content_type_and_metadata(fake_client):
    asyncio.run(
        obs.put_bytes(
            "images/x/y.png", b"\x89PNG", content_type="image/png", metadata={"k": "v"}
        )
    )
    put = fake_client.puts[0]
    assert put["headers"] == {"contentType": "image/png"}
    assert put["metadata"] == {"k": "v"}


def test_put_bytes_raises_on_obs_error(fake_client):
    fake_client.next_resp = _FakeResp(status=403, errorCode="AccessDenied",
                                      errorMessage="no permission")
    with pytest.raises(RuntimeError, match="OBS 上传失败"):
        asyncio.run(obs.put_bytes("k", b"x"))


# --- upload_upload / upload_image ------------------------------------------

def test_upload_upload_builds_key_and_keeps_original_name(fake_client):
    key = asyncio.run(obs.upload_upload("报告.docx", b"doc-bytes"))
    assert key.startswith("uploads/")
    assert key.endswith("报告.docx")
    put = fake_client.puts[0]
    assert put["content"] == b"doc-bytes"
    assert put["metadata"] == {"original-name": "报告.docx"}


def test_upload_image_sniffs_ext_and_sets_content_type(fake_client):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    key = asyncio.run(obs.upload_image(png))
    assert key.startswith("images/")
    assert key.endswith(".png")
    put = fake_client.puts[0]
    assert put["headers"] == {"contentType": "image/png"}
    assert put["content"] == png


def test_upload_image_unknown_format_uses_bin(fake_client):
    key = asyncio.run(obs.upload_image(b"\x00\x01weird"))
    assert key.endswith(".bin")
    assert fake_client.puts[0]["headers"] == {
        "contentType": "application/octet-stream"
    }


# --- config-missing fails fast ---------------------------------------------

def test_get_client_raises_when_config_missing(monkeypatch):
    monkeypatch.setattr(obs, "OBS_ACCESS_KEY_ID", "")
    monkeypatch.setattr(obs, "OBS_SECRET_ACCESS_KEY", "")
    monkeypatch.setattr(obs, "OBS_ENDPOINT", "")
    monkeypatch.setattr(obs, "OBS_BUCKET", "")
    obs._set_client_for_test(None)  # force rebuild
    with pytest.raises(RuntimeError, match="OBS 配置缺失"):
        asyncio.run(obs.put_bytes("k", b"x"))
