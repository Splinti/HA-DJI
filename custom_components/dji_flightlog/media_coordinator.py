"""Keeps the list of recordings of one media source (OneDrive, local folder) up to date."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTRY_TYPE,
    CONF_MATCH_TOLERANCE,
    CONF_MEDIA_SCAN_INTERVAL,
    DEFAULT_MATCH_TOLERANCE,
    DEFAULT_MEDIA_SCAN_INTERVAL,
    DOMAIN,
    ENTRY_TYPE_LOCAL,
    MEDIA_STORAGE_KEY,
    MEDIA_STORAGE_VERSION,
    STORAGE_SUBDIR,
)
from .media import (
    KIND_360,
    ROLE_COVER,
    ROLE_ORIGINAL,
    ROLE_PROXY,
    ROLE_RAW,
    build_recordings,
    match_recordings,
)
from .media_backend import MediaAuthError, MediaBackend, MediaError, MediaNotFound

_LOGGER = logging.getLogger(__name__)


@dataclass
class MediaData:
    recordings: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_sync: str | None = None
    folder: str = ""


class MediaCoordinator(DataUpdateCoordinator[MediaData]):
    """Polls one media backend and groups its files into recordings."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, backend: MediaBackend) -> None:
        opts = {**entry.data, **entry.options}
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_media",
            update_interval=timedelta(
                seconds=int(opts.get(CONF_MEDIA_SCAN_INTERVAL, DEFAULT_MEDIA_SCAN_INTERVAL))
            ),
            config_entry=entry,
        )
        self.backend = backend
        self.tolerance_s = int(opts.get(CONF_MATCH_TOLERANCE, DEFAULT_MATCH_TOLERANCE))
        self._store: Store[dict[str, Any]] = Store(
            hass, MEDIA_STORAGE_VERSION, f"{MEDIA_STORAGE_KEY}.{entry.entry_id}"
        )
        self._thumb_dir = Path(hass.config.path(".storage", STORAGE_SUBDIR, "thumbs"))
        self.items: dict[str, dict[str, Any]] = {}

    @property
    def folder_path(self) -> str:
        """The folder as shown to the user."""
        return self.backend.folder_display

    # -- persistence ---------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        if data.get("folder_path") == self.folder_path:
            self.items = data.get("items", {})
            self.backend.load_state(data)
        await self.hass.async_add_executor_job(partial(self._thumb_dir.mkdir, parents=True, exist_ok=True))

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"folder_path": self.folder_path, **self.backend.dump_state(), "items": self.items}
        )

    # -- sync ----------------------------------------------------------------

    async def _async_update_data(self) -> MediaData:
        label = self.backend.label
        try:
            self.items = await self.backend.async_sync(self.items)
        except MediaAuthError as err:
            raise ConfigEntryAuthFailed(f"{label}: {err}") from err
        except MediaNotFound as err:
            raise UpdateFailed(f"{label}: folder '{self.folder_path}' not found") from err
        except (MediaError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"{label}: {err}") from err

        await self._async_save()
        recordings = build_recordings(self.items, dt_util.get_default_time_zone())
        return MediaData(
            recordings=recordings, last_sync=datetime.now(UTC).isoformat(), folder=self.folder_path
        )

    # -- thumbnails / files ----------------------------------------------------

    def recording(self, rec_id: str) -> dict[str, Any] | None:
        return self.data.recordings.get(rec_id) if self.data else None

    async def async_thumbnail(self, rec: dict[str, Any]) -> bytes | None:
        """Cover image of a recording, cached on disk."""
        # A cover added after the first request must not be hidden by the
        # cached thumbnail of the proxy, hence the suffix.
        path = self._thumb_dir / f"{rec['id']}{'_cover' if rec.get(ROLE_COVER) else ''}.jpg"
        cached = await self.hass.async_add_executor_job(_read_if_exists, path)
        if cached is not None:
            return cached
        data = await self.backend.async_thumbnail(rec)
        if data:
            await self.hass.async_add_executor_job(_write, path, data)
        return data

    async def async_file(self, ref: dict[str, Any] | None) -> str | Path | None:
        """URL to redirect to, or local path to serve, for one file of a recording."""
        return await self.backend.async_file(ref) if ref else None


def playable_ref(rec: dict[str, Any]) -> dict[str, Any] | None:
    """The file the browser can show: the proxy, else a normal video/photo (not a 360° original)."""
    ref = rec.get(ROLE_PROXY)
    if ref is None and rec.get("kind") != KIND_360:
        ref = rec.get(ROLE_ORIGINAL)
    return ref


def original_ref(rec: dict[str, Any]) -> dict[str, Any] | None:
    """The original file of a recording, or the raw photo if there is nothing else."""
    return rec.get(ROLE_ORIGINAL) or rec.get(ROLE_RAW)


@dataclass
class MediaIndex:
    """Recordings of all media entries, matched to the flights."""

    by_flight: dict[str, list[dict[str, Any]]]
    recordings: dict[str, tuple[MediaCoordinator, dict[str, Any]]]
    unmatched: int


_INDEX_CACHE = f"{DOMAIN}_media_index"


def media_coordinators(hass: HomeAssistant) -> list[MediaCoordinator]:
    return [c for c in hass.data.get(DOMAIN, {}).values() if isinstance(c, MediaCoordinator)]


def media_index(hass: HomeAssistant, flights: dict[str, dict[str, Any]] | None) -> MediaIndex:
    """Match recordings to flights; cached until either side refreshes.

    Coordinators replace their ``data`` object on every refresh, so the
    identity of those objects is a sufficient cache key.
    """
    coordinators = media_coordinators(hass)
    key = (flights, *(c.data for c in coordinators))
    cached = hass.data.get(_INDEX_CACHE)
    if (
        cached is not None
        and len(cached[0]) == len(key)
        and all(a is b for a, b in zip(cached[0], key, strict=True))
    ):
        return cached[1]

    recordings: dict[str, tuple[MediaCoordinator, dict[str, Any]]] = {}
    by_flight: dict[str, list[dict[str, Any]]] = {}
    matched: set[str] = set()
    for coordinator in coordinators:
        if not coordinator.data:
            continue
        recs = coordinator.data.recordings
        for rec_id, rec in recs.items():
            recordings[rec_id] = (coordinator, rec)
        for fid, rec_ids in match_recordings(
            (flights or {}).values(), recs.values(), coordinator.tolerance_s
        ).items():
            by_flight.setdefault(fid, []).extend(recs[r] for r in rec_ids)
            matched.update(rec_ids)
    for recs in by_flight.values():
        recs.sort(key=lambda r: r.get("start") or "")

    index = MediaIndex(by_flight=by_flight, recordings=recordings, unmatched=len(recordings) - len(matched))
    hass.data[_INDEX_CACHE] = (key, index)
    return index


def unmatched_recordings(
    hass: HomeAssistant, coordinator: MediaCoordinator, flights: dict[str, dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Recordings of one account that belong to no flight, oldest first."""
    if not coordinator.data:
        return []
    index = media_index(hass, flights)
    matched = {rec["id"] for recs in index.by_flight.values() for rec in recs}
    left = [rec for rec in coordinator.data.recordings.values() if rec["id"] not in matched]
    return sorted(left, key=lambda rec: rec.get("start") or "")


def media_device_info(entry: ConfigEntry) -> DeviceInfo:
    """One device per media entry (OneDrive account or folder), holding its button and sensors."""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_LOCAL:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_local")},
            name=entry.title,
            model="Local folder",
            entry_type=DeviceEntryType.SERVICE,
        )
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_onedrive")},
        name=entry.title,
        manufacturer="Microsoft",
        model="OneDrive",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://onedrive.live.com",
    )


def media_status(hass: HomeAssistant, index: MediaIndex) -> dict[str, Any]:
    """Connection state per media entry, for the panel."""
    return {
        "connected": bool(media_coordinators(hass)),
        "unmatched": index.unmatched,
        "accounts": [
            {
                "title": c.config_entry.title,
                "source": c.backend.kind,
                "folder": c.folder_path,
                "last_sync": c.data.last_sync if c.data else None,
                "ok": c.last_update_success,
                "recordings": len(c.data.recordings) if c.data else 0,
            }
            for c in media_coordinators(hass)
        ],
    }


def _read_if_exists(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
