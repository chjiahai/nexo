"""Remote-folder storage for user uploads (scp over ssh).

Replaces the former Volcengine TOS backend. Received media (already
downloaded + decrypted by the transport layer) is written to a temp file and
shipped to a specified folder on a remote machine via `scripts/ship_media.sh`
(scp). The remote path mirrors the old TOS key layout, grouped by user and
time-prefixed for ordering / collision avoidance:

    docs/<user_id>/YYYYMMDD-HHMMSS-<safe-name>   # original file uploads
    imgs/<user_id>/YYYYMMDD-HHMMSS-image.<ext>   # image uploads (ext sniffed)
    videos/<user_id>/YYYYMMDD-HHMMSS-<safe-name> # video uploads

`<user_id>` is derived from the bot's session id (e.g. `wecom:2` -> `2`).

The script is a blocking subprocess; every call is wrapped in
`asyncio.to_thread` so the event loop never blocks on the network. Transient
failures (scp non-zero exit — network/ssh blips) are retried with backoff;
missing config fails fast as a permanent `RuntimeError`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from nexo.config import (
    NEXO_UPLOAD_DIR,
    NEXO_UPLOAD_HOST,
    NEXO_UPLOAD_KEY,
    NEXO_UPLOAD_PORT,
    NEXO_UPLOAD_USER,
)
from nexo.errors import TransientError, retry

# scripts/ship_media.sh lives at <repo root>/scripts/. remote.py is at
# <root>/src/nexo/storage/remote.py -> parents[3] is the root.
_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "ship_media.sh"


def _require_config() -> None:
    """Raise a clear config error if any required upload var is missing.

    Permanent (not retried) — mirrors the old TOS fast-fail so a misconfigured
    deploy surfaces on the first upload rather than burning the retry budget.
    """
    missing = [
        name
        for name, val in (
            ("NEXO_UPLOAD_HOST", NEXO_UPLOAD_HOST),
            ("NEXO_UPLOAD_USER", NEXO_UPLOAD_USER),
            ("NEXO_UPLOAD_DIR", NEXO_UPLOAD_DIR),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(
            "上传配置缺失，请在 .env 设置：" + " / ".join(missing)
        )


def _safe_name(name: str) -> str:
    """Strip any directory component (path-traversal guard); cap length.

    Remote paths cap well under any filesystem limit, but UTF-8 Chinese
    filenames are multibyte, so we trim an over-long stem while preserving the
    extension.
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
    """Normalize a session/user id into a path-safe folder segment.

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
# is inferred from the magic bytes (gives the remote file a proper suffix).
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


def _run_scp(local_file: str, rel_path: str) -> None:
    """Invoke scripts/ship_media.sh to ship one file. Raises CalledProcessError on non-zero exit."""
    env = {
        **os.environ,
        "NEXO_UPLOAD_HOST": NEXO_UPLOAD_HOST,
        "NEXO_UPLOAD_USER": NEXO_UPLOAD_USER,
        "NEXO_UPLOAD_DIR": NEXO_UPLOAD_DIR,
        "NEXO_UPLOAD_SSH_KEY": NEXO_UPLOAD_KEY,
        "NEXO_UPLOAD_SSH_PORT": NEXO_UPLOAD_PORT,
    }
    subprocess.run(
        ["bash", str(_SCRIPT), local_file, rel_path],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


async def _ship(rel_path: str, data: bytes) -> str:
    """Write `data` to a temp file, scp it to `<NEXO_UPLOAD_DIR>/<rel_path>`, clean up.

    Returns `rel_path`. scp failures are transient (retried); missing config
    is permanent (raised before any work).
    """
    _require_config()

    def _write_tmp() -> str:
        # delete=False — we unlink ourselves in the finally below (scp needs a
        # closed file on some platforms, and we want one temp per call).
        with tempfile.NamedTemporaryFile(prefix="nexo-upload-", delete=False) as f:
            f.write(data)
            return f.name

    tmp = await asyncio.to_thread(_write_tmp)
    try:

        async def _attempt() -> None:
            try:
                await asyncio.to_thread(_run_scp, tmp, rel_path)
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                detail = f"scp 传输失败 (exit={exc.returncode})"
                if stderr:
                    detail += f": {stderr}"
                raise TransientError(detail) from exc

        await retry(_attempt, attempts=3, base_delay=0.5)
    finally:
        await asyncio.to_thread(os.unlink, tmp)
    return rel_path


async def upload_file(user_id: str, filename: str | None, data: bytes) -> str:
    """Ship an original file upload under docs/<user_id>/; returns the rel path."""
    name = filename or "unnamed"
    rel = _build_key("docs", user_id, _safe_name(name))
    await _ship(rel, data)
    return rel


async def upload_image(user_id: str, filename: str | None, data: bytes) -> str:
    """Ship an image upload under imgs/<user_id>/; ext sniffed from bytes.

    `filename` is unused (images arrive as raw bytes with no reliable name) but
    accepted so the signature matches the other media uploads.
    """
    ext = _sniff_image_ext(data)
    rel = _build_key("imgs", user_id, f"image.{ext}")
    await _ship(rel, data)
    return rel


async def upload_video(user_id: str, filename: str | None, data: bytes) -> str:
    """Ship a video upload under videos/<user_id>/; returns the rel path."""
    name = filename or "video.mp4"
    rel = _build_key("videos", user_id, _safe_name(name))
    await _ship(rel, data)
    return rel


__all__ = [
    "upload_file",
    "upload_image",
    "upload_video",
]
