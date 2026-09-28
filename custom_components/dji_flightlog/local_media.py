"""Recordings in a folder Home Assistant can read.

Covers network shares too: Home Assistant mounts SMB/CIFS and NFS shares
itself (Settings → System → Storage → Add network storage, usage "Media"),
they appear under ``/media/<name>``. The integration only reads files.

Compared to OneDrive, three things are done here instead of by the server:

* Changes: a full scan per sync; files with unchanged size and mtime keep
  what was read from them before.
* Duration and aircraft model: read from the MP4 header (``moov/mvhd``,
  ``moov/udta/meta/ilst/©too``), which is enough to match a recording to
  a flight. Only new or changed files are opened.
* Thumbnails: rendered with ffmpeg from the cover image, the proxy or the
  original, scaled down.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import struct
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from homeassistant.core import HomeAssistant

from .media import (
    KIND_PHOTO,
    MP4_EXTENSIONS,
    MP4_MODEL_PATH,
    ROLE_COVER,
    ROLE_EQUIRECT,
    ROLE_ORIGINAL,
    ROLE_PROXY,
    box_header,
    classify_name,
    is_flight_record,
    meta_payload_start,
    mp4_model,
)
from .media_backend import MediaError, MediaNotFound

_LOGGER = logging.getLogger(__name__)
# NAS housekeeping: Synology @eaDir thumbnails, recycle bins, hidden folders.
_SKIP_DIR_PREFIXES = (".", "@", "#", "$")

THUMB_WIDTH = 480
_FFMPEG_TIMEOUT_S = 30
# Without ffmpeg, cover images up to this size are sent as they are.
_MAX_RAW_THUMB_BYTES = 5 * 1024 * 1024

_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".lrf": "video/mp4",
    ".mov": "video/quicktime",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".heic": "image/heic",
    ".dng": "image/x-adobe-dng",
    ".srt": "text/plain; charset=utf-8",
}


def content_type(name: str) -> str:
    return _CONTENT_TYPES.get(PurePosixPath(name).suffix.lower(), "application/octet-stream")


# -- MP4 header ------------------------------------------------------------------


def _find_box(fh: BinaryIO, start: int, end: int, name: bytes) -> tuple[int, int] | None:
    """Payload range of the first box ``name`` in ``[start, end)``, or None."""
    pos = start
    for _ in range(10_000):
        if pos + 8 > end:
            return None
        fh.seek(pos)
        box = box_header(fh.read(16), pos, end)
        if box is None:
            return None
        kind, payload, pos = box
        if kind == name:
            return payload, pos
    return None


def _mp4_model(fh: BinaryIO, moov: tuple[int, int]) -> str | None:
    """Aircraft model from ``moov/udta/meta/ilst/©too`` (see ``media.mp4_model``)."""
    box: tuple[int, int] | None = moov
    for name in MP4_MODEL_PATH[1:]:
        box = _find_box(fh, *box, name)
        if box is None:
            return None
        if name == b"meta":
            fh.seek(box[0])
            box = (meta_payload_start(fh.read(12), box[0]), box[1])
    fh.seek(box[0])
    return mp4_model(fh.read(min(box[1] - box[0], 256)))


def mp4_info(path: Path) -> tuple[int | None, str | None]:
    """Duration (ms) and aircraft model from the movie header.

    The ``moov`` box may sit at the end (no faststart).
    """
    duration_ms = model = None
    try:
        with path.open("rb") as fh:
            end = os.fstat(fh.fileno()).st_size
            moov = _find_box(fh, 0, end, b"moov")
            if moov is None:
                return None, None
            if mvhd := _find_box(fh, *moov, b"mvhd"):
                fh.seek(mvhd[0])
                head = fh.read(32)
                if head[:1] == b"\x01":
                    timescale, duration = struct.unpack(">IQ", head[20:32])
                else:
                    timescale, duration = struct.unpack(">II", head[12:20])
                if timescale and duration not in (0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
                    duration_ms = round(duration * 1000 / timescale)
            model = _mp4_model(fh, moov)
    except (OSError, struct.error):
        pass
    return duration_ms, model


# -- scan ---------------------------------------------------------------------------


def item_id(root: Path, rel: str) -> str:
    """Stable id of a file; hex only, so it can go into URL paths unquoted."""
    return hashlib.sha1(f"{root}\0{rel}".encode()).hexdigest()[:20]


def scan_folder(root: Path, previous: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """All media files and flight records below ``root`` as items for ``media.build_recordings``.

    Blocking (runs in the executor). Raises :class:`MediaNotFound` if ``root``
    is missing and :class:`MediaError` if it is empty although files were
    found before, which is what an unmounted network share looks like.
    """
    if not root.is_dir():
        raise MediaNotFound(f"folder {root} not found")
    items: dict[str, dict[str, Any]] = {}
    seen_any = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(_SKIP_DIR_PREFIXES)]
        seen_any = seen_any or bool(dirnames or filenames)
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        for name in filenames:
            if name.startswith(".") or (classify_name(name) is None and not is_flight_record(name)):
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue  # removed while scanning
            iid = item_id(root, rel)
            is_mp4 = PurePosixPath(name).suffix.lower() in MP4_EXTENSIONS
            old = previous.get(iid)
            if (
                old
                and old.get("size") == st.st_size
                and old.get("mtime") == st.st_mtime_ns
                # Items stored before the model was read get read once more.
                and (not is_mp4 or "model" in old)
            ):
                items[iid] = old
                continue
            duration = model = None
            if is_mp4:
                duration, model = mp4_info(Path(dirpath, name))
            items[iid] = {
                "id": iid,
                "name": name,
                "size": st.st_size,
                "web_url": None,
                "folder": rel_dir,
                "taken_at": None,
                "duration_ms": duration,
                "model": model,
                "width": None,
                "height": None,
                "path": rel,
                "mtime": st.st_mtime_ns,
            }
    if not seen_any and previous:
        raise MediaError(f"folder {root} is empty; is the network storage mounted?")
    return items


# -- thumbnails ---------------------------------------------------------------------


def _ffmpeg_binary(hass: HomeAssistant) -> str:
    """The binary configured for HA's ffmpeg integration, else the one on PATH."""
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        return get_ffmpeg_manager(hass).binary
    except (ImportError, ValueError):
        return "ffmpeg"


async def ffmpeg_thumbnail(binary: str, path: Path, *, seek_s: float | None) -> bytes | None:
    """One frame of ``path`` as a JPEG, ``THUMB_WIDTH`` wide; None if ffmpeg fails."""
    args = [binary, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if seek_s:
        args += ["-ss", str(seek_s)]
    args += [
        "-i", str(path),
        "-frames:v", "1",
        "-vf", f"scale='min({THUMB_WIDTH},iw)':-2",
        "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5",
        "pipe:1",
    ]  # fmt: skip
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as err:
        _LOGGER.debug("ffmpeg not available (%s): %s", binary, err)
        return None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), _FFMPEG_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        _LOGGER.debug("ffmpeg timed out on %s", path)
        return None
    if proc.returncode != 0 or not out:
        _LOGGER.debug("ffmpeg failed on %s: %s", path, err.decode(errors="replace").strip()[-300:])
        return None
    return out


def _read_small(path: Path, limit: int, strict: bool = False) -> bytes | None:
    """File content, or None if larger than ``limit``; read errors raise only if ``strict``."""
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_bytes()
    except OSError:
        if strict:
            raise
        return None


class LocalFolderMedia:
    """Media backend for a directory (local disk or mounted network share)."""

    kind = "local"

    def __init__(self, hass: HomeAssistant, folder: str) -> None:
        self.hass = hass
        self.root = Path(folder)
        self.label = folder

    @property
    def folder_display(self) -> str:
        return str(self.root)

    def load_state(self, data: dict[str, Any]) -> None:
        """Nothing beyond the items themselves (size/mtime per file)."""

    def dump_state(self) -> dict[str, Any]:
        return {}

    async def async_sync(self, items: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        try:
            return await self.hass.async_add_executor_job(scan_folder, self.root, items)
        except OSError as err:
            raise MediaError(str(err)) from err

    def path(self, ref: dict[str, Any]) -> Path | None:
        rel = ref.get("path")
        return self.root / rel if rel else None

    async def async_thumbnail(self, rec: dict[str, Any]) -> bytes | None:
        binary = _ffmpeg_binary(self.hass)
        if (cover := rec.get(ROLE_COVER)) and (path := self.path(cover)):
            data = await ffmpeg_thumbnail(binary, path, seek_s=None)
            if data:
                return data
            data = await self.hass.async_add_executor_job(_read_small, path, _MAX_RAW_THUMB_BYTES)
            if data:
                return data
        # The 360° render shows a panorama instead of two fisheye circles.
        for role in (ROLE_EQUIRECT, ROLE_PROXY, ROLE_ORIGINAL):
            if (ref := rec.get(role)) and (path := self.path(ref)):
                seek = None if rec.get("kind") == KIND_PHOTO and role == ROLE_ORIGINAL else 1.0
                data = await ffmpeg_thumbnail(binary, path, seek_s=seek)
                if data is None and seek:
                    # Shorter than the seek position.
                    data = await ffmpeg_thumbnail(binary, path, seek_s=None)
                if data:
                    return data
        return None

    async def async_read(self, item: dict[str, Any], max_bytes: int) -> bytes | None:
        path = self.path(item)
        if path is None:
            return None
        try:
            return await self.hass.async_add_executor_job(_read_small, path, max_bytes, True)
        except OSError as err:
            raise MediaError(str(err)) from err

    async def async_file(self, ref: dict[str, Any]) -> Path | None:
        path = self.path(ref)
        if path is None or not await self.hass.async_add_executor_job(path.is_file):
            return None
        return path
