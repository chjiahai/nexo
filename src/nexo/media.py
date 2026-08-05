"""Media-type route registry.

Each kind of media the bot accepts (file / image / video) differs only in a
few details: which OBS upload function persists it, the default filename when
none is known, and (legacy) six user-facing message keys. Capturing those
differences in one `MediaRoute` value lets `nexo.drain._upload_media` be
written once instead of copied per type — adding a new media type is now one
route + one upload function, with no new drain code.

The upload function is referenced by attribute name (not captured as a
callable) so drain resolves it lazily via `getattr(obs, ...)` on
`nexo.storage.obs`. (`nexo.storage.vfs` was retired when uploads moved to OBS.)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MediaRoute:
    """Everything that differs between the file / image / video routes."""

    kind: str
    upload_attr: str  # name of the upload function on `nexo.storage.obs`
    default_name: str | None  # filename when neither SDK nor frame provides one
    # Six user-facing message keys (see prompts.toml [messages]).
    empty_url: str
    downloading: str
    download_failed: str
    saving: str
    saved: str
    save_failed: str


FILE = MediaRoute(
    kind="file",
    upload_attr="upload_file",
    default_name="unnamed",
    empty_url="file_empty_url",
    downloading="file_downloading",
    download_failed="file_download_failed",
    saving="file_saving",
    saved="file_saved",
    save_failed="file_save_failed",
)

IMAGE = MediaRoute(
    kind="image",
    upload_attr="upload_image",
    default_name=None,  # images carry no filename; the name is unused
    empty_url="image_empty_url",
    downloading="image_downloading",
    download_failed="image_download_failed",
    saving="image_saving",
    saved="image_saved",
    save_failed="image_save_failed",
)

VIDEO = MediaRoute(
    kind="video",
    upload_attr="upload_video",
    default_name="video.mp4",  # WeCom videos are mp4; frames carry no filename
    empty_url="video_empty_url",
    downloading="video_downloading",
    download_failed="video_download_failed",
    saving="video_saving",
    saved="video_saved",
    save_failed="video_save_failed",
)

ROUTES: dict[str, MediaRoute] = {"file": FILE, "image": IMAGE, "video": VIDEO}


# --- file-type sniffing (fallback when no filename is available) ------------
# WeCom `message.file` frames carry only `url` + `aeskey` — no filename — and
# the download URL's Content-Disposition is not guaranteed. When the SDK didn't
# report a name, sniff the type from the downloaded bytes so the OBS object
# still gets a correct extension + content-type (e.g. an xlsx is recognizable
# instead of an opaque binary/octet-stream). Images are sniffed separately in
# `nexo.storage.obs` (they never carry a filename).

def sniff_file_ext(data: bytes) -> str | None:
    """Best-effort extension from magic bytes. Returns None if unknown."""
    if data.startswith(b"PK\x03\x04"):
        # ZIP-based: xlsx/docx/pptx are all zip — tell them apart by members.
        import io
        import zipfile

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
        except Exception:  # noqa: BLE001 — corrupt/truncated zip → treat as plain zip
            return "zip"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if any(n.startswith("word/") for n in names):
            return "docx"
        if any(n.startswith("ppt/") for n in names):
            return "pptx"
        return "zip"
    if data.startswith(b"%PDF"):
        return "pdf"
    return None
