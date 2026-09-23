"""Scan the log folder, parse new files and aggregate per-aircraft statistics."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_API_KEY,
    CONF_LOG_DIR,
    CONF_MAX_TRACK_POINTS,
    CONF_SCAN_INTERVAL,
    DEFAULT_MAX_TRACK_POINTS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_FLIGHT_IMPORTED,
    LOG_FILE_SUFFIXES,
    REASON_FC_DAT,
    REASON_SUPPORT_BUNDLE,
    REASON_TOO_LARGE,
    STATUS_FAILED,
    STATUS_HEADER_ONLY,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    UPLOAD_DUPLICATE,
    UPLOAD_IMPORTED,
    UPLOAD_RETRY,
)
from .parser import KeychainError, classify_log_file, parse_flight
from .storage import FlightStore

_LOGGER = logging.getLogger(__name__)

# Never mark a file as "failed" while it is still being written to.
_MIN_FILE_AGE = timedelta(seconds=30)

# Header-only imports get retried until they succeed with the API key.
_RETRY_STATUSES = (STATUS_HEADER_ONLY,)

_REJECT_REASONS = {
    REASON_SUPPORT_BUNDLE: (
        "is a DJI support log bundle (encrypted *.log.enc / FC_SMP-*.DAT.enc), not a flight "
        "record. Those are exported by DJI Assistant 2 and only DJI can decrypt them. Use the "
        "DJI Fly app records instead: Android/data/dji.go.v5/files/FlightRecord/DJIFlightRecord_*.txt"
    ),
    REASON_FC_DAT: (
        "is a flight controller DAT from the aircraft. DJI encrypts these on every current model; "
        "use the DJI Fly app record (DJIFlightRecord_*.txt) instead"
    ),
    REASON_TOO_LARGE: "is too large to be a DJI Fly flight record and was skipped",
}


@dataclass
class AircraftStats:
    """Aggregated numbers for one aircraft (keyed by serial number)."""

    sn: str
    name: str
    product_type: str
    flights: int = 0
    total_time_s: float = 0.0
    total_distance_m: float = 0.0
    max_height_m: float = 0.0
    max_h_speed_ms: float = 0.0
    last: dict[str, Any] | None = None
    first_flight: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class FlightData:
    """Coordinator payload consumed by the entities and the HTTP views."""

    flights: dict[str, dict[str, Any]] = field(default_factory=dict)
    aircraft: dict[str, AircraftStats] = field(default_factory=dict)
    totals: AircraftStats = field(default_factory=lambda: AircraftStats("", "All", ""))
    last_import: str | None = None
    last_scan: str | None = None
    pending_files: int = 0
    unsupported: list[dict[str, Any]] = field(default_factory=list)
    log_dir_ok: bool = False


class FlightLogCoordinator(DataUpdateCoordinator[FlightData]):
    """Polls the log directory on a fixed interval."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, store: FlightStore) -> None:
        interval = entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=int(interval)),
            config_entry=entry,
        )
        self.store = store
        self.log_dir = Path(entry.options.get(CONF_LOG_DIR, entry.data[CONF_LOG_DIR]))
        self.api_key: str | None = entry.options.get(CONF_API_KEY, entry.data.get(CONF_API_KEY)) or None
        self.max_track_points = int(
            entry.options.get(
                CONF_MAX_TRACK_POINTS, entry.data.get(CONF_MAX_TRACK_POINTS, DEFAULT_MAX_TRACK_POINTS)
            )
        )

    # -- scanning -------------------------------------------------------------

    def _list_log_files(self) -> list[Path]:
        """Blocking directory walk (recursive so date subfolders work)."""
        if not self.log_dir.is_dir():
            return []
        return sorted(
            p
            for p in self.log_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in LOG_FILE_SUFFIXES and not p.name.startswith(".")
        )

    def _needs_processing(self, path: Path, stat: os.stat_result) -> bool:
        entry = self.store.files.get(str(path))
        if entry is None:
            return True
        if entry.get("size") != stat.st_size or entry.get("mtime") != stat.st_mtime:
            return True
        return bool(entry.get("status") in _RETRY_STATUSES and self.api_key)

    def _process_file(self, path: Path, *, check_age: bool = True) -> tuple[dict[str, Any] | None, bool]:
        """Blocking: parse one file, persist the track, update the index.

        Returns ``(summary, is_new_flight)``. ``check_age=False`` is for files
        known to be complete (uploads), which would otherwise wait 30 s.
        """
        stat = path.stat()
        if check_age and datetime.now(UTC) - datetime.fromtimestamp(stat.st_mtime, tz=UTC) < _MIN_FILE_AGE:
            _LOGGER.debug("Skipping %s, modified less than %s ago", path.name, _MIN_FILE_AGE)
            return None, False

        record = {"size": stat.st_size, "mtime": stat.st_mtime, "flight_id": None, "status": None}

        reason = classify_log_file(path, stat.st_size)
        if reason is not None:
            _LOGGER.warning("%s %s", path.name, _REJECT_REASONS[reason])
            record["status"] = STATUS_UNSUPPORTED
            record["reason"] = reason
            self.store.files[str(path)] = record
            return None, False

        try:
            summary, track = parse_flight(path, api_key=self.api_key, max_track_points=self.max_track_points)
        except KeychainError as err:
            # Transient (network / DJI API); do not remember the file so it is retried.
            _LOGGER.warning("%s", err)
            return None, False
        except Exception as err:
            _LOGGER.warning("Could not parse %s: %s", path.name, err)
            record["status"] = STATUS_FAILED
            self.store.files[str(path)] = record
            return None, False

        summary_dict = summary.as_dict()
        is_new = summary.flight_id not in self.store.flights or (
            self.store.flights[summary.flight_id].get("status") != STATUS_OK and summary.status == STATUS_OK
        )
        if track is not None:
            self.store.write_track(summary.flight_id, track.as_dict())
        self.store.flights[summary.flight_id] = summary_dict
        record["flight_id"] = summary.flight_id
        record["status"] = summary.status
        self.store.files[str(path)] = record
        return summary_dict, is_new

    def _scan(self) -> tuple[list[dict[str, Any]], int, bool]:
        """Blocking full scan. Returns (new flight summaries, pending count, dir ok)."""
        if not self.log_dir.is_dir():
            return [], 0, False
        new_flights: list[dict[str, Any]] = []
        pending = 0
        present: set[str] = set()
        for path in self._list_log_files():
            present.add(str(path))
            try:
                stat = path.stat()
            except OSError:
                continue
            if not self._needs_processing(path, stat):
                continue
            summary, is_new = self._process_file(path)
            if summary is None and str(path) not in self.store.files:
                pending += 1
            elif summary is not None and is_new:
                new_flights.append(summary)

        # Forget bookkeeping for files that disappeared (flights are kept:
        # deleting a raw log must not erase the logbook).
        for known in list(self.store.files):
            if known not in present:
                self.store.files.pop(known, None)
        return new_flights, pending, True

    async def async_import_file(self, path: Path) -> dict[str, Any] | None:
        """Import a single file on demand (service call)."""
        summary, is_new = await self.hass.async_add_executor_job(self._process_file, path)
        await self.store.async_save()
        if summary is not None and is_new:
            self._fire_imported(summary)
        await self.async_refresh()
        return summary

    def _save_upload(self, filename: str, data: bytes) -> dict[str, Any]:
        """Blocking: write an uploaded log into the log folder and import it.

        ``filename`` must already be sanitised. Re-uploading a file that is
        already there (same name and content) writes nothing; a different file
        with the same name gets a ``_2``, ``_3`` ... suffix.
        """
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / filename
        stem, suffix = path.stem, path.suffix
        already_there = False
        n = 1
        while path.exists():
            if path.stat().st_size == len(data) and path.read_bytes() == data:
                already_there = True
                break
            n += 1
            path = self.log_dir / f"{stem}_{n}{suffix}"
        if not already_there:
            # Dot-prefixed temp name: a concurrent scan skips it until complete.
            tmp = path.with_name(f".{path.name}.part")
            tmp.write_bytes(data)
            os.replace(tmp, path)

        summary, is_new = None, False
        if self._needs_processing(path, path.stat()):
            summary, is_new = self._process_file(path, check_age=False)

        result: dict[str, Any] = {"file": filename, "saved_as": path.name}
        record = self.store.files.get(str(path))
        if record is None:
            # KeychainError: not remembered, so the next scan retries it.
            result["status"] = UPLOAD_RETRY
        elif record["status"] in (STATUS_OK, STATUS_HEADER_ONLY):
            result["status"] = UPLOAD_IMPORTED if is_new else UPLOAD_DUPLICATE
            result["flight"] = summary or self.store.flights.get(record["flight_id"])
        else:
            result["status"] = record["status"]
            if record.get("reason"):
                result["reason"] = record["reason"]
        return result

    async def async_import_upload(self, filename: str, data: bytes) -> dict[str, Any]:
        """Save one uploaded log and import it right away (HTTP upload)."""
        result = await self.hass.async_add_executor_job(self._save_upload, filename, data)
        await self.store.async_save()
        if result["status"] == UPLOAD_IMPORTED:
            self._fire_imported(result["flight"])
        # Publish without a rescan: the panel uploads file by file, and the
        # flight list (served from self.data) should grow with each one.
        data = self._aggregate()
        if self.data is not None:
            data.pending_files = self.data.pending_files
            data.last_scan = self.data.last_scan
        data.log_dir_ok = True
        self.async_set_updated_data(data)
        return result

    async def async_remove_flight(self, flight_id: str) -> bool:
        if flight_id not in self.store.flights:
            return False
        self.store.flights.pop(flight_id)
        for path, rec in list(self.store.files.items()):
            if rec.get("flight_id") == flight_id:
                self.store.files.pop(path)
        await self.hass.async_add_executor_job(self.store.delete_track, flight_id)
        await self.store.async_save()
        await self.async_refresh()
        return True

    def _fire_imported(self, summary: dict[str, Any]) -> None:
        self.hass.bus.async_fire(EVENT_FLIGHT_IMPORTED, summary)

    # -- coordinator API ------------------------------------------------------

    async def _async_update_data(self) -> FlightData:
        try:
            new_flights, pending, dir_ok = await self.hass.async_add_executor_job(self._scan)
        except OSError as err:
            raise UpdateFailed(f"Scanning {self.log_dir} failed: {err}") from err

        if not dir_ok:
            _LOGGER.warning("Log directory %s does not exist (yet)", self.log_dir)

        if new_flights:
            await self.store.async_save()
            for summary in new_flights:
                self._fire_imported(summary)
            _LOGGER.info("Imported %d new flight(s) from %s", len(new_flights), self.log_dir)

        data = self._aggregate()
        data.pending_files = pending
        data.log_dir_ok = dir_ok
        data.last_scan = datetime.now(UTC).isoformat()
        return data

    def _aggregate(self) -> FlightData:
        data = FlightData(flights=dict(self.store.flights))
        ordered = sorted(data.flights.values(), key=lambda f: f["start_time"])
        for f in ordered:
            sn = f.get("aircraft_sn") or "unknown"
            stats = data.aircraft.get(sn)
            if stats is None:
                stats = data.aircraft[sn] = AircraftStats(
                    sn=sn,
                    name=f.get("aircraft_name") or f.get("product_type") or "DJI",
                    product_type=f.get("product_type") or "",
                )
            for target in (stats, data.totals):
                target.flights += 1
                target.total_time_s += float(f.get("duration_s") or 0.0)
                target.total_distance_m += float(f.get("distance_m") or 0.0)
                target.max_height_m = max(target.max_height_m, float(f.get("max_height_m") or 0.0))
                target.max_h_speed_ms = max(target.max_h_speed_ms, float(f.get("max_h_speed_ms") or 0.0))
                target.first_flight = target.first_flight or f["start_time"]
                target.last = f  # ordered by start_time, so the last one wins
            # Prefer the latest known name for the aircraft.
            if f.get("aircraft_name"):
                stats.name = f["aircraft_name"]
        imported = [f["imported_at"] for f in data.flights.values() if f.get("imported_at")]
        data.last_import = max(imported) if imported else None
        data.unsupported = [
            {"file": Path(path).name, "reason": rec.get("reason", "")}
            for path, rec in sorted(self.store.files.items())
            if rec.get("status") == STATUS_UNSUPPORTED
        ]
        return data
