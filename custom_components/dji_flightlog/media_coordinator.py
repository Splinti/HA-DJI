"""Keeps the list of recordings in one OneDrive folder up to date."""

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
    CONF_MATCH_TOLERANCE,
    CONF_MEDIA_FOLDER,
    CONF_MEDIA_SCAN_INTERVAL,
    DEFAULT_MATCH_TOLERANCE,
    DEFAULT_MEDIA_FOLDER,
    DEFAULT_MEDIA_SCAN_INTERVAL,
    DOMAIN,
    MEDIA_STORAGE_KEY,
    MEDIA_STORAGE_VERSION,
    STORAGE_SUBDIR,
)
from .media import ROLE_COVER, ROLE_ORIGINAL, ROLE_PROXY, build_recordings, match_recordings
from .onedrive import GraphAuthError, GraphError, GraphNotFound, OneDriveClient, normalize_item

_LOGGER = logging.getLogger(__name__)


@dataclass
class MediaData:
    recordings: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_sync: str | None = None
    folder: str = ""


class MediaCoordinator(DataUpdateCoordinator[MediaData]):
    """Polls one OneDrive folder (delta query where available)."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: OneDriveClient) -> None:
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
        self.client = client
        self.folder_path: str = str(opts.get(CONF_MEDIA_FOLDER, DEFAULT_MEDIA_FOLDER)).strip().strip("/")
        self.tolerance_s = int(opts.get(CONF_MATCH_TOLERANCE, DEFAULT_MATCH_TOLERANCE))
        self._store: Store[dict[str, Any]] = Store(
            hass, MEDIA_STORAGE_VERSION, f"{MEDIA_STORAGE_KEY}.{entry.entry_id}"
        )
        self._thumb_dir = Path(hass.config.path(".storage", STORAGE_SUBDIR, "thumbs"))
        self.items: dict[str, dict[str, Any]] = {}
        self._folder_id: str | None = None
        self._delta_link: str | None = None
        self._use_delta = True

    # -- persistence ---------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        if data.get("folder_path") == self.folder_path:
            self.items = data.get("items", {})
            self._folder_id = data.get("folder_id")
            self._delta_link = data.get("delta_link")
            self._use_delta = data.get("use_delta", True)
        await self.hass.async_add_executor_job(partial(self._thumb_dir.mkdir, parents=True, exist_ok=True))

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "folder_path": self.folder_path,
                "folder_id": self._folder_id,
                "delta_link": self._delta_link,
                "use_delta": self._use_delta,
                "items": self.items,
            }
        )

    # -- sync ----------------------------------------------------------------

    def _apply_delta(self, raw_items: list[dict[str, Any]], full: bool) -> None:
        if full:
            self.items = {}
        for raw in raw_items:
            item_id = raw.get("id")
            if not item_id or item_id == self._folder_id:
                continue
            if "deleted" in raw:
                self.items.pop(item_id, None)
                # A deleted folder is not always followed by its children.
                for key in [k for k, v in self.items.items() if v.get("folder") == item_id]:
                    self.items.pop(key, None)
                continue
            item = normalize_item(raw)
            if item is None:
                self.items.pop(item_id, None)
            else:
                self.items[item_id] = item

    async def _async_sync(self) -> None:
        if self._folder_id is None:
            folder = await self.client.async_get_folder(self.folder_path)
            self._folder_id = folder["id"]
            self._delta_link = None

        if self._use_delta:
            try:
                raw, self._delta_link, full = await self.client.async_delta(self._folder_id, self._delta_link)
            except GraphAuthError:
                raise
            except GraphNotFound:
                # The folder was deleted or moved; resolve the path again next time.
                self._folder_id = None
                raise
            except GraphError as err:
                _LOGGER.info("OneDrive delta not available (%s), falling back to full listings", err)
                self._use_delta = False
                self._delta_link = None
            else:
                self._apply_delta(raw, full)
                return

        raw = await self.client.async_list_recursive(self._folder_id)
        self._apply_delta(raw, full=True)

    async def _async_update_data(self) -> MediaData:
        try:
            await self._async_sync()
        except GraphAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except aiohttp.ClientResponseError as err:
            # Raised by the token refresh when the grant was revoked.
            if err.status in (400, 401):
                raise ConfigEntryAuthFailed(f"OneDrive token refresh failed: {err}") from err
            raise UpdateFailed(f"OneDrive: {err}") from err
        except GraphNotFound as err:
            raise UpdateFailed(f"OneDrive folder '{self.folder_path}' not found") from err
        except (GraphError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"OneDrive: {err}") from err

        await self._async_save()
        recordings = build_recordings(self.items, dt_util.get_default_time_zone())
        return MediaData(
            recordings=recordings, last_sync=datetime.now(UTC).isoformat(), folder=self.folder_path
        )

    # -- thumbnails / playback -------------------------------------------------

    def recording(self, rec_id: str) -> dict[str, Any] | None:
        return self.data.recordings.get(rec_id) if self.data else None

    async def async_thumbnail(self, rec: dict[str, Any]) -> bytes | None:
        """Cover image of a recording, cached on disk."""
        # A cover uploaded after the first request must not be hidden by the
        # cached OneDrive thumbnail of the proxy, hence the suffix.
        path = self._thumb_dir / f"{rec['id']}{'_cover' if rec.get(ROLE_COVER) else ''}.jpg"
        cached = await self.hass.async_add_executor_job(_read_if_exists, path)
        if cached is not None:
            return cached

        data: bytes | None = None
        if cover := rec.get(ROLE_COVER):
            # Extracted by the sync script from the .OSV (OneDrive cannot
            # thumbnail 360° originals). Let OneDrive scale it down.
            data = await self.client.async_thumbnail(cover["item_id"]) or await self.client.async_content(
                cover["item_id"]
            )
        for role in (ROLE_PROXY, ROLE_ORIGINAL):
            if data is None and (ref := rec.get(role)):
                data = await self.client.async_thumbnail(ref["item_id"])
        if data:
            await self.hass.async_add_executor_job(_write, path, data)
        return data

    async def async_play_url(self, rec: dict[str, Any]) -> str | None:
        """Direct URL of the browser-friendly file: the proxy, else a normal video/photo."""
        ref = rec.get(ROLE_PROXY)
        if ref is None and rec.get("kind") != "360":
            ref = rec.get(ROLE_ORIGINAL)
        if ref is None:
            return None
        return await self.client.async_download_url(ref["item_id"])


@dataclass
class MediaIndex:
    """Recordings of all OneDrive entries, matched to the flights."""

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


def onedrive_device_info(entry: ConfigEntry) -> DeviceInfo:
    """One device per OneDrive account, holding its button and sensors."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_onedrive")},
        name=entry.title,
        manufacturer="Microsoft",
        model="OneDrive",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://onedrive.live.com",
    )


def media_status(hass: HomeAssistant, index: MediaIndex) -> dict[str, Any]:
    """Connection state per OneDrive entry, for the panel."""
    return {
        "connected": bool(media_coordinators(hass)),
        "unmatched": index.unmatched,
        "accounts": [
            {
                "title": c.config_entry.title,
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
