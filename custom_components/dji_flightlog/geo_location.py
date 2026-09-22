"""One geo_location entity per flight (take-off point).

These show up on Home Assistant's built-in map card via
``geo_location_sources: [dji_flightlog]`` and can be used in zone
automations, proximity, etc. Only the newest ``geo_location_limit`` flights
are exposed to keep the entity count bounded.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_GEO_LOCATION_LIMIT, DEFAULT_GEO_LOCATION_LIMIT, DOMAIN
from .coordinator import FlightLogCoordinator
from .sensor import aircraft_device_info


def _visible_ids(coordinator: FlightLogCoordinator, limit: int) -> list[str]:
    flights = [
        f
        for f in coordinator.data.flights.values()
        if f.get("takeoff_lat") is not None and f.get("takeoff_lon") is not None
    ]
    flights.sort(key=lambda f: f["start_time"], reverse=True)
    if limit > 0:
        flights = flights[:limit]
    return [f["flight_id"] for f in flights]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FlightLogCoordinator = hass.data[DOMAIN][entry.entry_id]
    limit = int(
        entry.options.get(
            CONF_GEO_LOCATION_LIMIT, entry.data.get(CONF_GEO_LOCATION_LIMIT, DEFAULT_GEO_LOCATION_LIMIT)
        )
    )
    entities: dict[str, FlightLocation] = {}

    @callback
    def _sync() -> None:
        if coordinator.data is None:
            return
        wanted = _visible_ids(coordinator, limit)
        new = [
            FlightLocation(coordinator, entry, flight_id) for flight_id in wanted if flight_id not in entities
        ]
        for ent in new:
            entities[ent.flight_id] = ent
        if new:
            async_add_entities(new)
        # Entities that fell out of the window remove themselves.
        for flight_id in list(entities):
            if flight_id not in wanted:
                ent = entities.pop(flight_id)
                hass.async_create_task(ent.async_remove(force_remove=True))

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class FlightLocation(CoordinatorEntity[FlightLogCoordinator], GeolocationEvent):
    _attr_source = DOMAIN
    _attr_icon = "mdi:quadcopter"
    _attr_unit_of_measurement = UnitOfLength.METERS
    _attr_has_entity_name = True

    def __init__(self, coordinator: FlightLogCoordinator, entry: ConfigEntry, flight_id: str) -> None:
        super().__init__(coordinator)
        self.flight_id = flight_id
        self._attr_unique_id = f"{entry.entry_id}_flight_{flight_id}"
        flight = self._flight
        if flight and (sn := flight.get("aircraft_sn")) and sn in coordinator.data.aircraft:
            self._attr_device_info = aircraft_device_info(entry, coordinator.data.aircraft[sn])

    @property
    def _flight(self) -> dict[str, Any] | None:
        return self.coordinator.data.flights.get(self.flight_id) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        return super().available and self._flight is not None

    @property
    def name(self) -> str:
        f = self._flight or {}
        try:
            start = datetime.fromisoformat(f["start_time"]).astimezone()
            when = start.strftime("%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            when = self.flight_id
        return f"Flight {when}"

    @property
    def latitude(self) -> float | None:
        return (self._flight or {}).get("takeoff_lat")

    @property
    def longitude(self) -> float | None:
        return (self._flight or {}).get("takeoff_lon")

    @property
    def distance(self) -> float | None:
        # geo_location "distance" is the state; use the flown distance.
        return (self._flight or {}).get("distance_m")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        f = self._flight or {}
        return {
            "flight_id": self.flight_id,
            "start_time": f.get("start_time"),
            "duration_s": f.get("duration_s"),
            "max_height_m": f.get("max_height_m"),
            "aircraft_name": f.get("aircraft_name"),
            "aircraft_sn": f.get("aircraft_sn"),
            "city": f.get("city"),
            "status": f.get("status"),
        }
