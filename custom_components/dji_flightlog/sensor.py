"""Sensors: totals for the whole logbook, per-aircraft and per-battery statistics."""

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
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfInformation,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ENTRY_TYPE, DOMAIN, MEDIA_ENTRY_TYPES
from .coordinator import AircraftStats, BatteryStats, FlightData, FlightLogCoordinator
from .media_coordinator import MediaCoordinator, media_device_info, unmatched_recordings
from .parser import INCIDENT_CRITICAL, INCIDENT_OK, INCIDENT_WARNING
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
        "battery_sn": f.get("battery_sn"),
        "incident": f.get("incident"),
        "incident_actions": f.get("incident_actions"),
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
    FlightSensorDescription(
        key="last_flight_incident",
        translation_key="last_flight_incident",
        icon="mdi:alert-circle-outline",
        device_class=SensorDeviceClass.ENUM,
        options=[INCIDENT_OK, INCIDENT_WARNING, INCIDENT_CRITICAL],
        value_fn=lambda s: _last(s, "incident"),
        attrs_fn=lambda s: {"actions": _last(s, "incident_actions") or []},
    ),
)


def _sd_attrs(stats: AircraftStats) -> dict[str, Any]:
    f = stats.sd_flight
    if not f:
        return {}
    return {"total_mb": f.get("sd_total_mb"), "full": f.get("sd_full"), "as_of": f.get("end_time")}


# Only on the aircraft devices: the totals would mix several drones' cards.
AIRCRAFT_SENSORS: tuple[FlightSensorDescription, ...] = (
    FlightSensorDescription(
        key="sd_free",
        translation_key="sd_free",
        icon="mdi:micro-sd",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=1,
        value_fn=lambda s: s.sd_flight.get("sd_free_mb") if s.sd_flight else None,
        attrs_fn=_sd_attrs,
    ),
)


def _bat_last(bat: BatteryStats, key: str) -> Any:
    return bat.last.get(key) if bat.last else None


def _battery_last_flight_attrs(bat: BatteryStats) -> dict[str, Any]:
    if not bat.last:
        return {}
    f = bat.last
    return {
        "flight_id": f["flight_id"],
        "aircraft_name": f.get("aircraft_name"),
        "aircraft_sn": f.get("aircraft_sn"),
        "duration_s": f.get("duration_s"),
        "battery_start_pct": f.get("battery_start_pct"),
        "battery_end_pct": f.get("battery_end_pct"),
    }


@dataclass(frozen=True, kw_only=True)
class BatterySensorDescription(SensorEntityDescription):
    value_fn: Callable[[BatteryStats], Any]
    attrs_fn: Callable[[BatteryStats], dict[str, Any]] | None = None


BATTERY_SENSORS: tuple[BatterySensorDescription, ...] = (
    BatterySensorDescription(
        key="battery_cycles",
        translation_key="battery_cycles",
        icon="mdi:battery-sync",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda b: b.cycles,
    ),
    BatterySensorDescription(
        key="battery_life",
        translation_key="battery_life",
        icon="mdi:battery-heart-variant",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda b: b.life_pct,
    ),
    BatterySensorDescription(
        key="battery_capacity",
        translation_key="battery_capacity",
        icon="mdi:battery-high",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda b: b.capacity_pct,
        attrs_fn=lambda b: {"full_capacity_mah": b.full_mah, "design_capacity_mah": b.design_mah},
    ),
    BatterySensorDescription(
        key="flights",
        translation_key="flights",
        icon="mdi:quadcopter",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda b: b.flights,
    ),
    BatterySensorDescription(
        key="flight_time",
        translation_key="flight_time",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda b: round(b.total_time_s, 1),
    ),
    BatterySensorDescription(
        key="last_flight",
        translation_key="last_flight",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda b: _ts(_bat_last(b, "start_time")),
        attrs_fn=_battery_last_flight_attrs,
    ),
    BatterySensorDescription(
        key="last_flight_battery_temp",
        translation_key="last_flight_battery_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda b: _bat_last(b, "battery_temp_max_c"),
        attrs_fn=lambda b: {"start_temperature": _bat_last(b, "battery_temp_start_c")},
    ),
    BatterySensorDescription(
        key="last_flight_cell_min",
        translation_key="last_flight_cell_min",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        value_fn=lambda b: _bat_last(b, "battery_cell_min_v"),
    ),
    BatterySensorDescription(
        key="last_flight_cell_deviation",
        translation_key="last_flight_cell_deviation",
        icon="mdi:scale-unbalanced",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
        value_fn=lambda b: _bat_last(b, "battery_cell_dev_max_v"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) in MEDIA_ENTRY_TYPES:
        media: MediaCoordinator = hass.data[DOMAIN][entry.entry_id]
        async_add_entities(
            [
                MediaLastSyncSensor(media, entry),
                MediaRecordingsSensor(media, entry),
                MediaUnmatchedSensor(media, entry),
            ]
        )
        return

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
                AttentionSensor(coordinator, entry),
            ]
        for sn in data.aircraft:
            if sn in known:
                continue
            known.add(sn)
            new += [AircraftSensor(coordinator, entry, sn, d) for d in STATS_SENSORS + AIRCRAFT_SENSORS]
        for sn in data.batteries:
            key = f"battery_{sn}"
            if key in known:
                continue
            known.add(key)
            new += [BatterySensor(coordinator, entry, sn, d) for d in BATTERY_SENSORS]
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


class BatterySensor(_BaseSensor):
    entity_description: BatterySensorDescription

    def __init__(
        self,
        coordinator: FlightLogCoordinator,
        entry: ConfigEntry,
        sn: str,
        description: BatterySensorDescription,
    ) -> None:
        super().__init__(coordinator, entry)
        self._sn = sn
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_battery_{sn}_{description.key}"
        self._attr_device_info = battery_device_info(entry, coordinator.data.batteries[sn])

    @property
    def _stats(self) -> BatteryStats | None:
        return self.coordinator.data.batteries.get(self._sn)

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


class AttentionSensor(_BaseSensor):
    """Pre-flight notices (SD card, battery, incidents) not yet marked as done; list in ``items``."""

    _attr_translation_key = "attention"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"items"})

    def __init__(self, coordinator: FlightLogCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_{TOTALS_ID}_attention"
        self._attr_device_info = totals_device_info(entry)

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.attention)

    @property
    def icon(self) -> str:
        return "mdi:alert-outline" if self.coordinator.data.attention else "mdi:check-circle-outline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        items = self.coordinator.data.attention
        return {
            "items": items,
            "worst": items[0]["level"] if items else None,
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


def battery_device_info(entry: ConfigEntry, bat: BatteryStats) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_battery_{bat.sn}")},
        translation_key="battery",
        translation_placeholders={"aircraft": bat.aircraft_name or "DJI", "sn": bat.sn[-4:]},
        manufacturer="DJI",
        model="Intelligent Flight Battery",
        serial_number=bat.sn,
    )


# -- media source (OneDrive account, local folder) ------------------------------


class _MediaSensor(CoordinatorEntity[MediaCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: MediaCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = f"media_{key}"
        self._attr_unique_id = f"{entry.entry_id}_{coordinator.backend.kind}_{key}"
        self._attr_device_info = media_device_info(entry)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None


class MediaLastSyncSensor(_MediaSensor):
    """Time of the last successful sync; unavailable while syncing fails."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MediaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_sync")

    @property
    def native_value(self) -> datetime | None:
        return _ts(self.coordinator.data.last_sync)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"folder": self.coordinator.folder_path}


class MediaRecordingsSensor(_MediaSensor):
    _attr_icon = "mdi:filmstrip-box-multiple"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: MediaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "recordings")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.recordings)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for rec in self.coordinator.data.recordings.values():
            kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
        return {
            "folder": self.coordinator.folder_path,
            **{f"{k}_count": n for k, n in sorted(kinds.items())},
        }


class MediaUnmatchedSensor(_MediaSensor):
    """Recordings no flight could be found for (clock off, flight log missing, ...)."""

    _attr_icon = "mdi:link-variant-off"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: MediaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "unmatched")

    def _unmatched(self) -> list[dict[str, Any]]:
        flights = next(
            (
                c.data.flights
                for c in self.hass.data.get(DOMAIN, {}).values()
                if isinstance(c, FlightLogCoordinator) and c.data
            ),
            None,
        )
        return unmatched_recordings(self.hass, self.coordinator, flights)

    @property
    def native_value(self) -> int:
        return len(self._unmatched())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # The newest ones; the full list could be long and attributes land in the recorder.
        return {"recordings": [rec["name"] for rec in self._unmatched()[-20:]]}
