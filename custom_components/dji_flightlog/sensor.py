"""Sensors: totals for the whole logbook and per-aircraft statistics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfLength, UnitOfSpeed, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AircraftStats, FlightData, FlightLogCoordinator
from .spots import maps_url, sorted_spots

TOTALS_ID = "totals"


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _last(stats: AircraftStats, key: str) -> Any:
    return stats.last.get(key) if stats.last else None


def _last_flight_attrs(stats: AircraftStats) -> dict[str, Any]:
    if not stats.last:
        return {}
    f = stats.last
    return {
        "flight_id": f["flight_id"],
        "filename": f.get("filename"),
        "status": f.get("status"),
        "start_time": f.get("start_time"),
        "end_time": f.get("end_time"),
        "duration_s": f.get("duration_s"),
        "distance_m": f.get("distance_m"),
        "max_height_m": f.get("max_height_m"),
        "max_h_speed_ms": f.get("max_h_speed_ms"),
        "aircraft_name": f.get("aircraft_name"),
        "aircraft_sn": f.get("aircraft_sn"),
        # Not "latitude"/"longitude": Home Assistant puts every entity carrying
        # those two attributes onto its auto-generated map — the same reason the
        # geo_location entities are off by default.
        "takeoff_lat": f.get("takeoff_lat"),
        "takeoff_lon": f.get("takeoff_lon"),
        "home_lat": f.get("home_lat"),
        "home_lon": f.get("home_lon"),
        "city": f.get("city"),
        "battery_start_pct": f.get("battery_start_pct"),
        "battery_end_pct": f.get("battery_end_pct"),
        "photo_num": f.get("photo_num"),
        "video_time_s": f.get("video_time_s"),
        "track_points": f.get("points"),
    }


def _battery_used(stats: AircraftStats) -> int | None:
    start, end = _last(stats, "battery_start_pct"), _last(stats, "battery_end_pct")
    if start is None or end is None:
        return None
    return int(start) - int(end)


@dataclass(frozen=True, kw_only=True)
class FlightSensorDescription(SensorEntityDescription):
    value_fn: Callable[[AircraftStats], Any]
    attrs_fn: Callable[[AircraftStats], dict[str, Any]] | None = None


# Shared by the totals device and every aircraft device.
STATS_SENSORS: tuple[FlightSensorDescription, ...] = (
    FlightSensorDescription(
        key="flights",
        translation_key="flights",
        icon="mdi:quadcopter",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda s: s.flights,
    ),
    FlightSensorDescription(
        key="flight_time",
        translation_key="flight_time",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda s: round(s.total_time_s, 1),
    ),
    FlightSensorDescription(
        key="distance",
        translation_key="distance",
        icon="mdi:map-marker-distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda s: round(s.total_distance_m, 1),
    ),
    FlightSensorDescription(
        key="max_height",
        translation_key="max_height",
        icon="mdi:arrow-up-bold",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_display_precision=0,
        value_fn=lambda s: round(s.max_height_m, 1),
    ),
    FlightSensorDescription(
        key="max_speed",
        translation_key="max_speed",
        icon="mdi:speedometer",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        suggested_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_display_precision=1,
        value_fn=lambda s: round(s.max_h_speed_ms, 2),
    ),
    FlightSensorDescription(
        key="first_flight",
        translation_key="first_flight",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=False,
        value_fn=lambda s: _ts(s.first_flight),
    ),
    FlightSensorDescription(
        key="last_flight",
        translation_key="last_flight",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: _ts(_last(s, "start_time")),
        attrs_fn=_last_flight_attrs,
    ),
    FlightSensorDescription(
        key="last_flight_duration",
        translation_key="last_flight_duration",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=1,
        value_fn=lambda s: _last(s, "duration_s"),
    ),
    FlightSensorDescription(
        key="last_flight_distance",
        translation_key="last_flight_distance",
        icon="mdi:map-marker-distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_display_precision=0,
        value_fn=lambda s: _last(s, "distance_m"),
    ),
    FlightSensorDescription(
        key="last_flight_max_height",
        translation_key="last_flight_max_height",
        icon="mdi:arrow-up-bold",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_display_precision=0,
        value_fn=lambda s: _last(s, "max_height_m"),
    ),
    FlightSensorDescription(
        key="last_flight_max_speed",
        translation_key="last_flight_max_speed",
        icon="mdi:speedometer",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        suggested_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_display_precision=1,
        value_fn=lambda s: _last(s, "max_h_speed_ms"),
    ),
    FlightSensorDescription(
        key="last_flight_battery_end",
        translation_key="last_flight_battery_end",
        icon="mdi:battery-50",
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: _last(s, "battery_end_pct"),
    ),
    FlightSensorDescription(
        key="last_flight_battery_used",
        translation_key="last_flight_battery_used",
        icon="mdi:battery-arrow-down",
        native_unit_of_measurement=PERCENTAGE,
        value_fn=_battery_used,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FlightLogCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _sync_entities() -> None:
        data: FlightData | None = coordinator.data
        if data is None:
            return
        new: list[SensorEntity] = []
        if TOTALS_ID not in known:
            known.add(TOTALS_ID)
            new += [TotalsSensor(coordinator, entry, d) for d in STATS_SENSORS]
            new += [
                LastImportSensor(coordinator, entry),
                PendingFilesSensor(coordinator, entry),
                UnsupportedFilesSensor(coordinator, entry),
                AircraftCountSensor(coordinator, entry),
                SavedSpotsSensor(coordinator, entry),
            ]
        for sn in data.aircraft:
            if sn in known:
                continue
            known.add(sn)
            new += [AircraftSensor(coordinator, entry, sn, d) for d in STATS_SENSORS]
        if new:
            async_add_entities(new)

    _sync_entities()
    entry.async_on_unload(coordinator.async_add_listener(_sync_entities))


class _BaseSensor(CoordinatorEntity[FlightLogCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: FlightLogCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry


class TotalsSensor(_BaseSensor):
    entity_description: FlightSensorDescription

    def __init__(
        self, coordinator: FlightLogCoordinator, entry: ConfigEntry, description: FlightSensorDescription
    ) -> None:
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{TOTALS_ID}_{description.key}"
        self._attr_device_info = totals_device_info(entry)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data.totals)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn:
            return self.entity_description.attrs_fn(self.coordinator.data.totals)
        return None


class AircraftSensor(_BaseSensor):
    entity_description: FlightSensorDescription

    def __init__(
        self,
        coordinator: FlightLogCoordinator,
        entry: ConfigEntry,
        sn: str,
        description: FlightSensorDescription,
    ) -> None:
        super().__init__(coordinator, entry)
        self._sn = sn
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{sn}_{description.key}"
        self._attr_device_info = aircraft_device_info(entry, coordinator.data.aircraft[sn])

    @property
    def _stats(self) -> AircraftStats | None:
        return self.coordinator.data.aircraft.get(self._sn)

    @property
    def available(self) -> bool:
        return super().available and self._stats is not None

    @property
    def native_value(self) -> Any:
        stats = self._stats
        return self.entity_description.value_fn(stats) if stats else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        stats = self._stats
        if stats and self.entity_description.attrs_fn:
            return self.entity_description.attrs_fn(stats)
        return None


class LastImportSensor(_BaseSensor):
    """Changes whenever a flight is imported; the map card listens to it."""

    _attr_translation_key = "last_import"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: FlightLogCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_{TOTALS_ID}_last_import"
        self._attr_device_info = totals_device_info(entry)

    @property
    def native_value(self) -> datetime | None:
        return _ts(self.coordinator.data.last_import)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "last_scan": self.coordinator.data.last_scan,
            "log_dir": str(self.coordinator.log_dir),
            "log_dir_ok": self.coordinator.data.log_dir_ok,
            "api_key_configured": bool(self.coordinator.api_key),
        }


class PendingFilesSensor(_BaseSensor):
    """Files seen in the folder that could not be imported yet (e.g. keychain fetch failed)."""

    _attr_translation_key = "pending_files"
    _attr_icon = "mdi:file-clock-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: FlightLogCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_{TOTALS_ID}_pending_files"
        self._attr_device_info = totals_device_info(entry)

    @property
    def native_value(self) -> int:
        return self.coordinator.data.pending_files


class UnsupportedFilesSensor(_BaseSensor):
    """Files in the folder that are not DJI Fly flight records."""

    _attr_translation_key = "unsupported_files"
    _attr_icon = "mdi:file-alert-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: FlightLogCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_{TOTALS_ID}_unsupported_files"
        self._attr_device_info = totals_device_info(entry)

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.unsupported)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"files": self.coordinator.data.unsupported}


class AircraftCountSensor(_BaseSensor):
    _attr_translation_key = "aircraft_count"
    _attr_icon = "mdi:quadcopter"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: FlightLogCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_{TOTALS_ID}_aircraft_count"
        self._attr_device_info = totals_device_info(entry)

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.aircraft)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "aircraft": [
                {"name": a.name, "sn": a.sn, "product_type": a.product_type, "flights": a.flights}
                for a in self.coordinator.data.aircraft.values()
            ]
        }


class SavedSpotsSensor(_BaseSensor):
    """Places saved for a future flight; the list is in the ``spots`` attribute."""

    _attr_translation_key = "saved_spots"
    _attr_icon = "mdi:map-marker-star"
    # The list can grow; keep it out of the recorder database.
    _unrecorded_attributes = frozenset({"spots"})

    def __init__(self, coordinator: FlightLogCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_{TOTALS_ID}_saved_spots"
        self._attr_device_info = totals_device_info(entry)

    @property
    def native_value(self) -> int:
        return len(self.coordinator.store.spots)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "spots": [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "latitude": s["lat"],
                    "longitude": s["lon"],
                    "note": s.get("note", ""),
                    "zones": [z.get("name") or z.get("layer") for z in s.get("zones") or []],
                    "created": s.get("created"),
                    "maps_url": maps_url(s),
                }
                for s in sorted_spots(self.coordinator.store.spots)
            ]
        }


def totals_device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{TOTALS_ID}")},
        name="DJI Flight Log",
        manufacturer="DJI",
        model="Logbook",
    )


def _model(stats: AircraftStats) -> str | None:
    """Readable model, falling back to the name for product types pydjirecord
    does not know yet (they come through as ``UNKNOWN_<n>``)."""
    product = stats.product_type or ""
    if not product or product.startswith("UNKNOWN") or product == "NONE":
        return stats.name or None
    return product


def aircraft_device_info(entry: ConfigEntry, stats: AircraftStats) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{stats.sn}")},
        name=stats.name or stats.product_type or "DJI Aircraft",
        manufacturer="DJI",
        model=_model(stats),
        serial_number=stats.sn if stats.sn != "unknown" else None,
    )
