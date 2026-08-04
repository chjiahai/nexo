"""Huawei Cloud OBS (对象存储) client for user uploads.

`esdk-obs-python`'s `ObsClient` is synchronous (built on `requests`); every OBS
call here is wrapped in `asyncio.to_thread` so the drain process's event loop
never blocks on a network round-trip. The client is built lazily once
(module-level singleton) from config.

Object keys are **deterministic** — derived from the org, user, and the WeCom
`msg_id` (NOT a timestamp) — so a drain crash-and-replay re-uploads to the
SAME key (idempotent: same object, no orphan). Layout:

    <org>/<user>/<msg_id>-<safe-name>     # file / video
    <org>/<user>/<msg_id>.<ext>           # image (ext sniffed from magic bytes)

The real original filename is preserved in object metadata (`x-obs-meta-original-name`)
because the key is rewritten with the msg_id. drain calls these after pulling
the already-downloaded bytes from the staging file (the bot downloaded them
while the WeCom signed URL was still fresh).

API reference: https://support.huaweicloud.com/sdk-python-devg-obs/obs_22_0500.html
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from nexo.config import (
    NEXO_ORG_ID,
    OBS_ACCESS_KEY_ID,
    OBS_BUCKET,
    OBS_ENDPOINT,
    OBS_SECRET_ACCESS_KEY,
)

# Built once, on first use. `None` means "not yet built"; a missing-config
# error is raised at build time (not import time) so importing the module is
# always cheap and tests that monkeypatch the client don't need creds.
_client: object | None = None


def _get_client() -> object:
    """Return the lazily-built OBS singleton, or raise a clear config error."""
    global _client
    if _client is not None:
        return _client

    missing = [
        name
        for name, val in (
            ("OBS_ACCESS_KEY_ID", OBS_ACCESS_KEY_ID),
            ("OBS_SECRET_ACCESS_KEY", OBS_SECRET_ACCESS_KEY),
            ("OBS_ENDPOINT", OBS_ENDPOINT),
            ("OBS_BUCKET", OBS_BUCKET),
        )
        if not val
    ]
    if missing:
        raise RuntimeError("OBS 配置缺失，请在 .env 设置：" + " / ".join(missing))

    from obs import ObsClient  # imported lazily so the SDK is optional at import time

    _client = ObsClient(
        access_key_id=OBS_ACCESS_KEY_ID,
        secret_access_key=OBS_SECRET_ACCESS_KEY,
        server=OBS_ENDPOINT,
    )
    return _client


def _safe_segment(value: str) -> str:
    """Normalize a string into a path-safe OBS key segment (no separators)."""
    safe = value.replace("/", "_").replace(":", "_").strip()
    return safe or "unknown"


def _safe_user_id(user_id: str) -> str:
    """Drop the `wecom:` channel prefix and scrub separators (mirrors vfs)."""
    raw = user_id.split(":", 1)[1] if ":" in user_id else user_id
    return _safe_segment(raw)


def _safe_name(name: str) -> str:
    """Strip any directory component (path-traversal guard); cap length."""
    base = Path(name).name or "unnamed"
    if len(base) <= 180:
        return base
    stem, dot, suffix = base.rpartition(".")
    if dot and len(suffix) <= 16:
        stem = stem[: 180 - len(suffix) - 1]
        return f"{stem}.{suffix}"
    return base[:180]


def _build_key(user_id: str, msg_id: str, leaf: str) -> str:
    """`<org>/<user>/<msg_id>-<leaf>` — deterministic, idempotent on replay."""
    return f"{_safe_segment(NEXO_ORG_ID)}/{_safe_user_id(user_id)}/{_safe_segment(msg_id)}-{leaf}"


# --- image format sniffing --------------------------------------------------
# WeCom images arrive as raw bytes with no reliable filename, so the extension
# (and thus content-type) is inferred from the magic bytes.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)

_IMAGE_CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "bin": "application/octet-stream",
}


def _sniff_image_ext(data: bytes) -> str:
    """Detect png/jpg/gif from leading magic bytes; fall back to 'bin'."""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    return "bin"


async def put_bytes(
    key: str,
    data: bytes,
    *,
    content_type: str | None = None,
    metadata: dict[str, str] | None = None,
) -> str:
    """Upload `data` to `key` and return the key. Raises on OBS error.

    `content_type` is sent explicitly when given; otherwise OBS infers it from
    the key's extension. `metadata` becomes `x-obs-meta-*` headers.
    """
    client = _get_client()
    headers = {"contentType": content_type} if content_type else None

    def _do():
        return client.putContent(  # type: ignore[union-attr]
            OBS_BUCKET,
            key,
            content=data,
            metadata=metadata,
            headers=headers,
        )

    resp = await asyncio.to_thread(_do)
    status = getattr(resp, "status", 0) or 0
    if status >= 300:
        code = getattr(resp, "errorCode", "") or ""
        message = getattr(resp, "errorMessage", "") or ""
        detail = f"OBS 上传失败 (status={status} {code})".rstrip()
        if message:
            detail += f": {message}"
        if status == 404:
            # OBS returns a bare 404 (no error code) when the bucket can't be
            # found at this endpoint — almost always an endpoint/bucket
            # mismatch. Make that legible instead of a cryptic empty error.
            detail += (
                "（404 通常是 bucket 在该 endpoint 下不存在：确认 OBS_ENDPOINT 是区域"
                "端点如 obs.cn-south-1.myhuaweicloud.com，而不是含 bucket 名的 URL；"
                "并核对 OBS_BUCKET 拼写与 bucket 所在区域）"
            )
        raise RuntimeError(detail)
    return key


async def upload_file(user_id: str, msg_id: str, filename: str | None, data: bytes) -> str:
    """Persist a file upload to a deterministic key; returns the OBS object key."""
    name = _safe_name(filename or "unnamed")
    key = _build_key(user_id, msg_id, name)
    await put_bytes(key, data, metadata={"original-name": filename or "unnamed"})
    return key


async def upload_image(user_id: str, msg_id: str, filename: str | None, data: bytes) -> str:
    """Persist an image upload (ext sniffed from bytes); returns the OBS key.

    `filename` is unused (images arrive as raw bytes) but accepted so the
    signature matches the other media uploads (drain calls them uniformly).
    """
    ext = _sniff_image_ext(data)
    key = _build_key(user_id, msg_id, f"image.{ext}")
    await put_bytes(key, data, content_type=_IMAGE_CONTENT_TYPES[ext])
    return key


async def upload_video(user_id: str, msg_id: str, filename: str | None, data: bytes) -> str:
    """Persist a video upload to a deterministic key; returns the OBS object key."""
    name = _safe_name(filename or "video.mp4")
    key = _build_key(user_id, msg_id, name)
    await put_bytes(key, data, metadata={"original-name": filename or "video.mp4"})
    return key


# Re-exported for tests that want to swap in a fake client.
def _set_client_for_test(client: object | None) -> None:
    """Test hook: inject a fake client (or reset to None to force rebuild)."""
    global _client
    _client = client


__all__ = [
    "put_bytes",
    "upload_file",
    "upload_image",
    "upload_video",
    "_set_client_for_test",
]
