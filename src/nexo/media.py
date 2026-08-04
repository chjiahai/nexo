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
