"""Where recordings come from: the interface of a media backend.

The coordinator keeps the listing, the thumbnail cache and the link to the
flights; a backend only knows its storage:

* OneDrive (``onedrive.py``): Microsoft Graph, delta queries.
* Local folder (``local_media.py``): any directory Home Assistant can read,
  including SMB/NFS shares mounted as network storage under ``/media``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class MediaError(Exception):
    """Reading the recordings failed."""


class MediaAuthError(MediaError):
    """The sign-in expired or was revoked; the user has to sign in again."""


class MediaNotFound(MediaError):
    """The folder or file does not exist."""


class MediaBackend(Protocol):
    kind: str  # "onedrive" | "local": device model, entity unique ids
    label: str  # for log and error messages

    @property
    def folder_display(self) -> str:
        """The folder as the user sees it (panel, sensor attribute)."""

    def load_state(self, data: dict[str, Any]) -> None:
        """Restore the sync state saved next to the items (see ``dump_state``)."""

    def dump_state(self) -> dict[str, Any]:
        """Sync state to persist (delta link, ...); stored flat next to ``items``."""

    async def async_sync(self, items: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Return the current items (id -> normalized file, see ``media.build_recordings``).

        Items are media files and DJI flight records (``media.is_flight_record``).

        ``items`` is the previous result; the backend may reuse or update it.
        """

    async def async_thumbnail(self, rec: dict[str, Any]) -> bytes | None:
        """JPEG preview of a recording, or None."""

    async def async_file(self, ref: dict[str, Any]) -> str | Path | None:
        """One file of a recording: a URL to redirect to, or a local path to serve."""

    async def async_read(self, item: dict[str, Any], max_bytes: int) -> bytes | None:
        """Content of a stored item (flight records), or None if it is larger than ``max_bytes``."""
