"""Tests for the Huawei Cloud OBS storage backend (deterministic keys).

Hermetic: a fake OBS client records putContent calls. Verifies the deterministic
key derivation (msg_id-based, not time-based — idempotent on replay), image ext
sniffing, and the config fast-fail.
"""

from __future__ import annotations

import asyncio

import pytest

from nexo.storage import obs


class FakeResp:
    def __init__(self, status: int = 200) -> None:
        self.status = status


class FakeClient:
    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes, dict | None, dict | None]] = []

    def putContent(self, bucket, key, content=None, metadata=None, headers=None):
        self.puts.append((key, content, metadata, headers))
        return FakeResp(200)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(obs, "OBS_ACCESS_KEY_ID", "ak")
    monkeypatch.setattr(obs, "OBS_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setattr(obs, "OBS_ENDPOINT", "obs.cn-south-1.myhuaweicloud.com")
    monkeypatch.setattr(obs, "OBS_BUCKET", "bucket")
    monkeypatch.setattr(obs, "NEXO_ORG_ID", "org42")
    fake = FakeClient()
    obs._set_client_for_test(fake)
    yield fake
    obs._set_client_for_test(None)


def test_build_key_is_deterministic_and_msg_id_based():
    """Same (org, user, msg_id, name) → same key; no timestamp."""
    k1 = obs._build_key("wecom:2", "mid-1", "report.pdf")
    k2 = obs._build_key("wecom:2", "mid-1", "report.pdf")
    assert k1 == k2 == "org42/2/mid-1-report.pdf"


def test_build_key_strips_channel_prefix_and_groups_by_org():
    assert obs._build_key("wecom:group-x", "m", "f.txt") == "org42/group-x/m-f.txt"


def test_upload_file_uses_deterministic_key_and_preserves_name(configured):
    key = asyncio.run(obs.upload_file("wecom:2", "mid-1", "report.pdf", b"data"))
    assert key == "org42/2/mid-1-report.pdf"
    assert configured.puts[0][0] == key
    assert configured.puts[0][1] == b"data"
    assert configured.puts[0][2] == {"original-name": "report.pdf"}


def test_upload_image_sniffs_ext_into_key(configured):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    key = asyncio.run(obs.upload_image("wecom:2", "mid-img", None, png))
    assert key == "org42/2/mid-img-image.png"
    assert configured.puts[0][3] == {"contentType": "image/png"}


def test_upload_image_unknown_format_uses_bin(configured):
    key = asyncio.run(obs.upload_image("wecom:2", "mid-img", None, b"\x00\x01weird"))
    assert key.endswith("image.bin")


def test_upload_video_defaults_name_when_none(configured):
    key = asyncio.run(obs.upload_video("wecom:2", "mid-vid", None, b"\x00\x00"))
    assert key == "org42/2/mid-vid-video.mp4"


def test_put_bytes_raises_on_obs_error(monkeypatch):
    """A non-2xx OBS response raises a RuntimeError with the status."""
    monkeypatch.setattr(obs, "OBS_ACCESS_KEY_ID", "ak")
    monkeypatch.setattr(obs, "OBS_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setattr(obs, "OBS_ENDPOINT", "obs.cn-south-1.myhuaweicloud.com")
    monkeypatch.setattr(obs, "OBS_BUCKET", "bucket")

    class FailClient:
        def putContent(self, *a, **k):
            return FakeResp(403)
    obs._set_client_for_test(FailClient())
    try:
        with pytest.raises(RuntimeError, match="403"):
            asyncio.run(obs.put_bytes("k", b"x"))
    finally:
        obs._set_client_for_test(None)


def test_missing_config_raises_before_upload(monkeypatch):
    obs._set_client_for_test(None)
    monkeypatch.setattr(obs, "OBS_ACCESS_KEY_ID", "")
    monkeypatch.setattr(obs, "OBS_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setattr(obs, "OBS_ENDPOINT", "ep")
    monkeypatch.setattr(obs, "OBS_BUCKET", "b")
    with pytest.raises(RuntimeError, match="OBS 配置缺失"):
        asyncio.run(obs.upload_file("wecom:2", "m", "f.pdf", b"x"))
