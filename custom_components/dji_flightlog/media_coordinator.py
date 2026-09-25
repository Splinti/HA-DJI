"""Keeps the list of recordings of one media source (OneDrive, local folder) up to date.

Optionally it also copies the DJI flight records found there into the log
folder of the flight log (see ``_async_import_logs``). The log folder stays
the source of truth: nothing is ever written to or deleted at the source.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTRY_TYPE,
    CONF_IMPORT_LOGS,
    CONF_MATCH_TOLERANCE,
    CONF_MEDIA_SCAN_INTERVAL,
    DEFAULT_IMPORT_LOGS,
    DEFAULT_MATCH_TOLERANCE,
    DEFAULT_MEDIA_SCAN_INTERVAL,
    DOMAIN,
    ENTRY_TYPE_LOCAL,
    MAX_LOG_FILE_BYTES,
    MEDIA_STORAGE_KEY,
    MEDIA_STORAGE_VERSION,
    STORAGE_SUBDIR,
)
from .coordinator import FlightLogCoordinator
from .media import (
    KIND_360,
    PROJECTION_DFISHEYE,
    PROJECTION_EQUIRECT,
    ROLE_COVER,
    ROLE_EQUIRECT,
    ROLE_ORIGINAL,
    ROLE_PROXY,
    ROLE_RAW,
    build_recordings,
    duplicate_recordings,
    is_flight_record,
    match_recordings,
)
from .media_backend import MediaAuthError, MediaBackend, MediaError, MediaNotFound

_LOGGER = logging.getLogger(__name__)

# A flight record changed less than this long ago may still be being copied
# into a local folder; it is picked up by the next sync.
_LOG_MIN_AGE_S = 60


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
        self.import_logs = bool(opts.get(CONF_IMPORT_LOGS, DEFAULT_IMPORT_LOGS))
        # Flight records already copied: item id -> size (a changed file is copied again).
        self.imported_logs: dict[str, int | None] = {}
        self._import_task: asyncio.Task[None] | None = None
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
        # Kept across folder changes: the ids stay the same for OneDrive, and
        # the log folder recognises files it already has anyway.
        self.imported_logs = data.get("imported_logs", {})
        await self.hass.async_add_executor_job(partial(self._thumb_dir.mkdir, parents=True, exist_ok=True))

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "folder_path": self.folder_path,
            **self.backend.dump_state(),
            "items": self.items,
            "imported_logs": self.imported_logs,
        }

    async def _async_save(self) -> None:
        await self._store.async_save(self._data_to_save())

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
        self.async_schedule_log_import()
        recordings = build_recordings(self.items, dt_util.get_default_time_zone())
        return MediaData(
            recordings=recordings, last_sync=datetime.now(UTC).isoformat(), folder=self.folder_path
        )

    # -- flight records ----------------------------------------------------------

    @callback
    def async_schedule_log_import(self) -> None:
        """Copy new flight records, unless off, already running or the flight log is not loaded yet.

        Runs in the background: the first sync of an account with a long
        history may copy hundreds of logs, each with a DJI keychain fetch.
        The flight log calls this too once it is set up, since config entries
        load in no particular order.
        """
        if not self.import_logs or (self._import_task is not None and not self._import_task.done()):
            return
        if _flightlog(self.hass) is None:
            return
        self._import_task = self.config_entry.async_create_background_task(
            self.hass, self._async_import_logs(), f"{DOMAIN} import flight records"
        )

    def pending_logs(self) -> list[dict[str, Any]]:
        """Flight records at the source that were not copied yet, oldest name first."""
        now_ns = time.time_ns()
        return sorted(
            (
                item
                for item in self.items.values()
                if is_flight_record(item["name"])
                and self.imported_logs.get(item["id"], -1) != item.get("size")
                and not (item.get("mtime") and now_ns - item["mtime"] < _LOG_MIN_AGE_S * 1_000_000_000)
            ),
            key=lambda item: item["name"],
        )

    async def _async_import_logs(self) -> None:
        """Copy new flight records into the log folder and import them.

        Goes through the flight log's upload path: a file it already has
        (same name and content) is not saved twice, a different file with the
        same name gets a suffix, and a failed keychain fetch is retried by its
        next scan because the file is already in the log folder then.
        """
        flightlog = _flightlog(self.hass)
        if flightlog is None:
            return
        counts: dict[str, int] = {}
        for item in self.pending_logs():
            size = item.get("size")
            data: bytes | None = None
            if size is None or size <= MAX_LOG_FILE_BYTES:
                try:
                    data = await self.backend.async_read(item, MAX_LOG_FILE_BYTES)
                except (MediaError, aiohttp.ClientError, TimeoutError) as err:
                    _LOGGER.warning(
                        "%s: could not read %s, retrying next sync: %s", self.backend.label, item["name"], err
                    )
                    break
            if data is None:
                _LOGGER.warning(
                    "%s: %s is larger than a flight record can be, skipped", self.backend.label, item["name"]
                )
                status = "too_large"
            else:
                status = (await flightlog.async_import_upload(item["name"], data))["status"]
            self.imported_logs[item["id"]] = size
            counts[status] = counts.get(status, 0) + 1
            self._store.async_delay_save(self._data_to_save, 10)
        if counts:
            _LOGGER.info("%s: flight records copied to the log folder: %s", self.backend.label, counts)
            await self._async_save()
            self.async_update_listeners()

    # -- thumbnails / files ----------------------------------------------------

    def recording(self, rec_id: str) -> dict[str, Any] | None:
        return self.data.recordings.get(rec_id) if self.data else None

    async def async_thumbnail(self, rec: dict[str, Any]) -> bytes | None:
        """Cover image of a recording, cached on disk."""
        # A cover or 360° render added after the first request must not be
        # hidden by the cached thumbnail of the proxy, hence the suffix.
        suffix = "_cover" if rec.get(ROLE_COVER) else "_360" if rec.get(ROLE_EQUIRECT) else ""
        path = self._thumb_dir / f"{rec['id']}{suffix}.jpg"
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


def _flightlog(hass: HomeAssistant) -> FlightLogCoordinator | None:
    return next((c for c in hass.data.get(DOMAIN, {}).values() if isinstance(c, FlightLogCoordinator)), None)


def playable_ref(rec: dict[str, Any]) -> dict[str, Any] | None:
    """The file the browser can show.

    The stitched 360° render (H.264, plays everywhere), else the proxy, else a
    normal video/photo (not a 360° original).
    """
    ref = rec.get(ROLE_EQUIRECT) or rec.get(ROLE_PROXY)
    if ref is None and rec.get("kind") != KIND_360:
        ref = rec.get(ROLE_ORIGINAL)
    return ref


def play_projection(rec: dict[str, Any]) -> str | None:
    """How the frontend projects the file of :func:`playable_ref`; None for a flat video/photo."""
    if rec.get(ROLE_EQUIRECT):
        return PROJECTION_EQUIRECT
    if rec.get("kind") == KIND_360 and rec.get(ROLE_PROXY):
        return PROJECTION_DFISHEYE
    return None


def original_ref(rec: dict[str, Any]) -> dict[str, Any] | None:
    """The original file of a recording, or the raw photo if there is nothing else."""
    return rec.get(ROLE_ORIGINAL) or rec.get(ROLE_RAW)


@dataclass
class MediaIndex:
    """Recordings of all media entries, matched to the flights."""

    by_flight: dict[str, list[dict[str, Any]]]
    recordings: dict[str, tuple[MediaCoordinator, dict[str, Any]]]
    unmatched: int
    # Copies of a recording another source holds as well (left out above).
    duplicates: set[str] = field(default_factory=set)


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
    duplicates = duplicate_recordings([c.data.recordings for c in coordinators if c.data])
    for coordinator in coordinators:
        if not coordinator.data:
            continue
        recs = {k: v for k, v in coordinator.data.recordings.items() if k not in duplicates}
        for rec_id, rec in recs.items():
            recordings[rec_id] = (coordinator, rec)
        for fid, rec_ids in match_recordings(
            (flights or {}).values(), recs.values(), coordinator.tolerance_s
        ).items():
            by_flight.setdefault(fid, []).extend(recs[r] for r in rec_ids)
            matched.update(rec_ids)
    for recs in by_flight.values():
        recs.sort(key=lambda r: r.get("start") or "")

    index = MediaIndex(
        by_flight=by_flight,
        recordings=recordings,
        unmatched=len(recordings) - len(matched),
        duplicates=duplicates,
    )
    hass.data[_INDEX_CACHE] = (key, index)
    return index


def unmatched_recordings(
    hass: HomeAssistant, coordinator: MediaCoordinator, flights: dict[str, dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Recordings of one source that belong to no flight, oldest first (copies left out)."""
    if not coordinator.data:
        return []
    index = media_index(hass, flights)
    matched = {rec["id"] for recs in index.by_flight.values() for rec in recs}
    left = [
        rec
        for rec in coordinator.data.recordings.values()
        if rec["id"] not in matched and rec["id"] not in index.duplicates
    ]
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
                "flight_records": len(c.imported_logs) if c.import_logs else None,
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
