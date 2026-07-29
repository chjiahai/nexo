"""Local VFS storage for user uploads (writes to the nexo-vfs mount).

Received media (already downloaded + decrypted by the transport layer) is
written directly to the nexo-vfs distributed filesystem mounted at
`NEXO_VFS_DIR`. Storage is flat — no type subfolder — every file lands under
its owner's folder, grouped by organization:

    <NEXO_VFS_DIR>/<org_id>/<user_id>/YYYYMMDD-HHMMSS-<safe-name>

`<org_id>` comes from config (`NEXO_ORG_ID`); `<user_id>` is derived from the
bot's session id (e.g. `wecom:2` -> `2`). Time-prefixed leaf names preserve
upload order and avoid collisions.

Writes are blocking filesystem calls wrapped in `asyncio.to_thread` so the
event loop never blocks on I/O. Missing config fails fast as a permanent
`RuntimeError` — a misconfigured deploy surfaces on the first upload rather
than silently dropping files.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from nexo.config import NEXO_ORG_ID, NEXO_VFS_DIR


def _require_config() -> None:
    """Raise a clear config error if the VFS root or org id is missing.

    Permanent (not retried) so a misconfigured deploy surfaces on the first
    upload rather than silently writing to the wrong place.
    """
    missing = [
        name
        for name, val in (
            ("NEXO_VFS_DIR", NEXO_VFS_DIR),
            ("NEXO_ORG_ID", NEXO_ORG_ID),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(
            "上传配置缺失，请在 .env 设置：" + " / ".join(missing)
        )


def _safe_name(name: str) -> str:
    """Strip any directory component (path-traversal guard); cap length.

    Paths cap well under any filesystem limit, but UTF-8 Chinese filenames are
    multibyte, so we trim an over-long stem while preserving the extension.
    """
    base = Path(name).name or "unnamed"
    if len(base) <= 180:
        return base
    stem, dot, suffix = base.rpartition(".")
    if dot and len(suffix) <= 16:
        stem = stem[: 180 - len(suffix) - 1]
        return f"{stem}.{suffix}"
    return base[:180]


def _safe_segment(value: str) -> str:
    """Normalize a string into a path-safe folder segment.

    Scrubs path separators so an org/user id can never escape its folder.
    """
    safe = value.replace("/", "_").replace(":", "_").strip()
    return safe or "unknown"


def _safe_user_id(user_id: str) -> str:
    """Normalize a session/user id into a path-safe folder segment.

    Session ids look like `wecom:<userid>` (single chat) or `wecom:<chatid>`
    (group). We drop the channel prefix to get the raw user/conversation id,
    then scrub path separators so it can never escape its folder.
    """
    raw = user_id.split(":", 1)[1] if ":" in user_id else user_id
    return _safe_segment(raw)


def _build_key(user_id: str, leaf: str) -> str:
    """`<org_id>/<user_id>/YYYYMMDD-HHMMSS-<leaf>` — grouped by org then user, time-ordered."""
    now = datetime.now()
    return f"{_safe_segment(NEXO_ORG_ID)}/{_safe_user_id(user_id)}/{now:%Y%m%d-%H%M%S}-{leaf}"


# --- image format sniffing --------------------------------------------------
# WeCom images arrive as raw bytes with no reliable filename, so the extension
# is inferred from the magic bytes (gives the file a proper suffix).
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def _sniff_image_ext(data: bytes) -> str:
    """Detect png/jpg/gif from leading magic bytes; fall back to 'bin'."""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    return "bin"


async def _write(rel_path: str, data: bytes) -> str:
    """Write `data` to `<NEXO_VFS_DIR>/<rel_path>`, creating parents as needed.

    Returns `rel_path`. Missing config is permanent (raised before any work).
    """
    _require_config()
    dest = Path(NEXO_VFS_DIR).expanduser() / rel_path

    def _do() -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            f.write(data)

    await asyncio.to_thread(_do)
    return rel_path


async def upload_file(user_id: str, filename: str | None, data: bytes) -> str:
    """Persist a file upload under <org>/<user>/; returns the rel path."""
    name = filename or "unnamed"
    rel = _build_key(user_id, _safe_name(name))
    await _write(rel, data)
    return rel


async def upload_image(user_id: str, filename: str | None, data: bytes) -> str:
    """Persist an image upload under <org>/<user>/; ext sniffed from bytes.

    `filename` is unused (images arrive as raw bytes with no reliable name) but
    accepted so the signature matches the other media uploads.
    """
    ext = _sniff_image_ext(data)
    rel = _build_key(user_id, f"image.{ext}")
    await _write(rel, data)
    return rel


async def upload_video(user_id: str, filename: str | None, data: bytes) -> str:
    """Persist a video upload under <org>/<user>/; returns the rel path."""
    name = filename or "video.mp4"
    rel = _build_key(user_id, _safe_name(name))
    await _write(rel, data)
    return rel


__all__ = [
    "upload_file",
    "upload_image",
    "upload_video",
]
