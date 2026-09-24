"""Persistence for parsed flights.

The index (summaries + file bookkeeping + saved spots) lives in Home Assistant's ``Store``
so it is included in backups. Tracks are larger and rarely needed, so each
one is a separate JSON file under ``<config>/.storage/dji_flightlog/tracks``.
"""

from __future__ import annotations

import json
import logging
from functools import partial
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_SUBDIR, STORAGE_VERSION
from .parser import clean_place

_LOGGER = logging.getLogger(__name__)


class FlightStore:
    """Index of flights plus per-flight track files."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._tracks_dir = Path(hass.config.path(".storage", STORAGE_SUBDIR, "tracks"))
        self.flights: dict[str, dict[str, Any]] = {}
        # file path -> {"size", "mtime", "flight_id", "status"}
        self.files: dict[str, dict[str, Any]] = {}
        # spot id -> spot (see spots.py)
        self.spots: dict[str, dict[str, Any]] = {}
        # keys of pre-flight notices marked as done (see coordinator.attention_items)
        self.dismissed: list[str] = []

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.flights = data.get("flights", {})
        self.files = data.get("files", {})
        self.spots = data.get("spots", {})
        self.dismissed = data.get("dismissed", [])
        # Flights imported before placeholders were filtered show "Map Loading".
        for flight in self.flights.values():
            for key in ("city", "street"):
                if key in flight:
                    flight[key] = clean_place(flight[key])
        await self._hass.async_add_executor_job(partial(self._tracks_dir.mkdir, parents=True, exist_ok=True))

    async def async_save(self) -> None:
        await self._store.async_save(
            {"flights": self.flights, "files": self.files, "spots": self.spots, "dismissed": self.dismissed}
        )

    # -- tracks -------------------------------------------------------------

    def track_path(self, flight_id: str) -> Path:
        return self._tracks_dir / f"{flight_id}.json"

    def write_track(self, flight_id: str, track: dict[str, Any]) -> None:
        """Blocking; call from executor."""
        tmp = self.track_path(flight_id).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(track, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.track_path(flight_id))

    def read_track(self, flight_id: str) -> dict[str, Any] | None:
        """Blocking; call from executor."""
        path = self.track_path(flight_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            _LOGGER.warning("Could not read track %s: %s", path, err)
            return None

    async def async_read_track(self, flight_id: str) -> dict[str, Any] | None:
        return await self._hass.async_add_executor_job(self.read_track, flight_id)

    def delete_track(self, flight_id: str) -> None:
        """Blocking; call from executor."""
        try:
            self.track_path(flight_id).unlink(missing_ok=True)
        except OSError as err:
            _LOGGER.warning("Could not delete track %s: %s", flight_id, err)
