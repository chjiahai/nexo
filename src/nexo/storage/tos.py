"""Volcengine TOS (火山引擎对象存储) client for user uploads.

The `tos` SDK's `TosClientV2` is synchronous (built on `requests`), while
the bot is fully async. Every TOS call here is wrapped in `asyncio.to_thread`
so the event loop never blocks on a network round-trip. The client itself is
built lazily once (module-level singleton) from config, mirroring how the
WeCom client is constructed in `wecom.build_client`.

Object keys are grouped by user (the WeCom conversation id), then
time-prefixed for ordering and collision avoidance:

    docs/<user_id>/YYYYMMDD-HHMMSS-<safe-name>   # original file uploads
    imgs/<user_id>/YYYYMMDD-HHMMSS-image.<ext>   # image uploads (ext sniffed)

`<user_id>` is derived from the bot's session id (e.g. `wecom:2` -> `2`).
The true original filename is preserved in object metadata
(`x-tos-meta-original-name`) because the key is rewritten.

API reference: https://www.volcengine.com/docs/6349
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from nexo.config import (
    TOS_ACCESS_KEY_ID,
    TOS_BUCKET,
    TOS_ENDPOINT,
    TOS_REGION,
    TOS_SECRET_ACCESS_KEY,
)

# Built once, on first use. `None` means "not yet built"; a missing-config
# error is raised at build time (not import time) so importing the module is
# always cheap and tests that monkeypatch the client don't need creds.
_client: object | None = None


def _get_client() -> object:
    """Return the lazily-built TOS singleton, or raise a clear config error."""
    global _client
    if _client is not None:
        return _client

    missing = [
        name
        for name, val in (
            ("TOS_ACCESS_KEY_ID", TOS_ACCESS_KEY_ID),
            ("TOS_SECRET_ACCESS_KEY", TOS_SECRET_ACCESS_KEY),
            ("TOS_ENDPOINT", TOS_ENDPOINT),
            ("TOS_REGION", TOS_REGION),
            ("TOS_BUCKET", TOS_BUCKET),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(
            "TOS 配置缺失，请在 .env 设置：" + " / ".join(missing)
        )

    from tos import TosClientV2  # imported lazily so the SDK is optional at import time

    _client = TosClientV2(
        ak=TOS_ACCESS_KEY_ID,
        sk=TOS_SECRET_ACCESS_KEY,
        endpoint=TOS_ENDPOINT,
        region=TOS_REGION,
    )
    return _client


def _safe_name(name: str) -> str:
    """Strip any directory component (path-traversal guard); cap length.

    TOS keys cap at 1024 bytes and UTF-8 Chinese filenames are multibyte, so
    we trim an over-long stem while preserving the extension.
    """
    base = Path(name).name or "unnamed"
    if len(base) <= 180:
        return base
    stem, dot, suffix = base.rpartition(".")
    if dot and len(suffix) <= 16:
        stem = stem[: 180 - len(suffix) - 1]
        return f"{stem}.{suffix}"
    return base[:180]


def _safe_user_id(user_id: str) -> str:
    """Normalize a session/user id into a path-safe TOS key segment.

    Session ids look like `wecom:<userid>` (single chat) or `wecom:<chatid>`
    (group). We drop the channel prefix to get the raw user/conversation id,
    then scrub path separators so it can never escape its folder.
    """
    raw = user_id.split(":", 1)[1] if ":" in user_id else user_id
    safe = raw.replace("/", "_").replace(":", "_").strip()
    return safe or "unknown"


def _build_key(prefix: str, user_id: str, leaf: str) -> str:
    """`prefix/<user_id>/YYYYMMDD-HHMMSS-<leaf>` — grouped by user, time-ordered."""
    now = datetime.now()
    return f"{prefix}/{_safe_user_id(user_id)}/{now:%Y%m%d-%H%M%S}-{leaf}"


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
    """Upload `data` to `key` and return the key. Raises on TOS error.

    `content_type` is sent explicitly when given; otherwise TOS infers it from
    the key's extension. `metadata` becomes `x-tos-meta-*` headers (passed to
    the SDK as the `meta` parameter).
    """
    client = _get_client()

    def _do():
        return client.put_object(  # type: ignore[union-attr]
            bucket=TOS_BUCKET,
            key=key,
            content=data,
            content_type=content_type,
            meta=metadata,
        )

    try:
        resp = await asyncio.to_thread(_do)
    except Exception as exc:  # TosClientError / TosServerError from the `tos` SDK
        raise RuntimeError(f"TOS 上传失败：{exc}") from exc

    status = getattr(resp, "status_code", 0) or 0
    # TOS returns 200 on a successful put_object; anything else is an error.
    if status >= 300:
        code = getattr(resp, "code", "") or ""
        message = getattr(resp, "message", "") or ""
        detail = f"TOS 上传失败 (status={status} {code})".rstrip()
        if message:
            detail += f": {message}"
        if status == 404:
            # TOS returns 404 (NoSuchBucket) when the bucket can't be found at
            # this endpoint — almost always an endpoint/region/bucket mismatch.
            detail += (
                "（404 通常是 bucket 在该 endpoint 下不存在：确认 TOS_ENDPOINT 是区域"
                "端点如 tos-cn-beijing.volces.com，而不是含 bucket 名的 URL；"
                "并核对 TOS_REGION 与 endpoint 对应、TOS_BUCKET 拼写无误）"
            )
        raise RuntimeError(detail)
    return key


async def upload_upload(user_id: str, filename: str, data: bytes) -> str:
    """Persist an original file upload under docs/<user_id>/; returns the key."""
    key = _build_key("docs", user_id, _safe_name(filename))
    # Preserve the real original name in metadata — the key is time-prefixed.
    await put_bytes(key, data, metadata={"original-name": filename})
    return key


async def upload_image(user_id: str, data: bytes) -> str:
    """Persist an image upload under imgs/<user_id>/; ext sniffed from bytes."""
    ext = _sniff_image_ext(data)
    key = _build_key("imgs", user_id, f"image.{ext}")
    await put_bytes(key, data, content_type=_IMAGE_CONTENT_TYPES[ext])
    return key


# Re-exported for tests that want to swap in a fake client.
def _set_client_for_test(client: object | None) -> None:
    """Test hook: inject a fake client (or reset to None to force rebuild)."""
    global _client
    _client = client


__all__ = [
    "put_bytes",
    "upload_upload",
    "upload_image",
    "_set_client_for_test",
]
