"""Authenticated HTTP API used by the map card (and anything else)."""

from __future__ import annotations

import json
from typing import Any

import voluptuous as vol
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from . import spots as spot_utils
from .const import DOMAIN, EXPORT_FORMATS
from .coordinator import FlightLogCoordinator
from .parser import _downsample, track_to_geojson, track_to_gpx, track_to_kml

API_BASE = f"/api/{DOMAIN}"


def _coordinator(hass: HomeAssistant) -> FlightLogCoordinator | None:
    entries = hass.data.get(DOMAIN, {})
    for value in entries.values():
        if isinstance(value, FlightLogCoordinator):
            return value
    return None


def _filter_flights(coordinator: FlightLogCoordinator, query: Any) -> list[dict[str, Any]]:
    flights = list(coordinator.data.flights.values()) if coordinator.data else []
    since = query.get("since")
    until = query.get("until")
    aircraft = query.get("aircraft")
    ids = query.get("ids")
    if since:
        flights = [f for f in flights if f["start_time"] >= since]
    if until:
        flights = [f for f in flights if f["start_time"] <= until]
    if aircraft:
        needle = aircraft.lower()
        flights = [
            f
            for f in flights
            if needle in (f.get("aircraft_name") or "").lower()
            or needle == (f.get("aircraft_sn") or "").lower()
        ]
    if ids:
        wanted = set(ids.split(","))
        flights = [f for f in flights if f["flight_id"] in wanted]
    flights.sort(key=lambda f: f["start_time"], reverse=True)
    limit = query.get("limit")
    if limit and limit.isdigit():
        flights = flights[: int(limit)]
    return flights


class FlightsView(HomeAssistantView):
    """List flights (summaries only)."""

    url = f"{API_BASE}/flights"
    name = f"api:{DOMAIN}:flights"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        coordinator = _coordinator(request.app["hass"])
        if coordinator is None or coordinator.data is None:
            return self.json_message("Integration not ready", status_code=503)
        data = coordinator.data
        return self.json(
            {
                "flights": _filter_flights(coordinator, request.query),
                "aircraft": {sn: a.as_dict() for sn, a in data.aircraft.items()},
                "totals": data.totals.as_dict(),
                "last_import": data.last_import,
                "last_scan": data.last_scan,
            }
        )


class TracksView(HomeAssistantView):
    """Bulk tracks for the overview map, optionally thinned via ``max_points``."""

    url = f"{API_BASE}/tracks"
    name = f"api:{DOMAIN}:tracks"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        coordinator = _coordinator(hass)
        if coordinator is None or coordinator.data is None:
            return self.json_message("Integration not ready", status_code=503)
        flights = _filter_flights(coordinator, request.query)
        max_points = int(request.query.get("max_points", "0") or 0)

        def _load() -> list[dict[str, Any]]:
            out = []
            for f in flights:
                track = coordinator.store.read_track(f["flight_id"])
                if not track or not track.get("points"):
                    continue
                pts = _downsample(track["points"], max_points) if max_points else track["points"]
                out.append({"flight_id": f["flight_id"], "points": pts, "home": track.get("home")})
            return out

        return self.json({"tracks": await hass.async_add_executor_job(_load)})


class TrackView(HomeAssistantView):
    """Full track of one flight."""

    url = f"{API_BASE}/flights/{{flight_id}}/track"
    name = f"api:{DOMAIN}:track"
    requires_auth = True

    async def get(self, request: web.Request, flight_id: str) -> web.Response:
        coordinator = _coordinator(request.app["hass"])
        if coordinator is None or coordinator.data is None:
            return self.json_message("Integration not ready", status_code=503)
        summary = coordinator.data.flights.get(flight_id)
        if summary is None:
            return self.json_message("Unknown flight", status_code=404)
        track = await coordinator.store.async_read_track(flight_id)
        if track is None:
            return self.json_message("No track for this flight", status_code=404)
        return self.json({"flight": summary, **track})


class ExportView(HomeAssistantView):
    """Download a flight as GPX / KML / GeoJSON."""

    url = f"{API_BASE}/flights/{{flight_id}}/export/{{fmt}}"
    name = f"api:{DOMAIN}:export"
    requires_auth = True

    async def get(self, request: web.Request, flight_id: str, fmt: str) -> web.Response:
        coordinator = _coordinator(request.app["hass"])
        if coordinator is None or coordinator.data is None:
            return self.json_message("Integration not ready", status_code=503)
        if fmt not in EXPORT_FORMATS:
            return self.json_message(f"format must be one of {EXPORT_FORMATS}", status_code=400)
        summary = coordinator.data.flights.get(flight_id)
        if summary is None:
            return self.json_message("Unknown flight", status_code=404)
        track = await coordinator.store.async_read_track(flight_id)
        if track is None:
            return self.json_message("No track for this flight", status_code=404)
        body, content_type = render_export(summary, track, fmt)
        filename = f"{summary['start_time'][:10]}_{flight_id}.{fmt}"
        return web.Response(
            text=body,
            content_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


class SpotsView(HomeAssistantView):
    """List saved spots or add one."""

    url = f"{API_BASE}/spots"
    name = f"api:{DOMAIN}:spots"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        coordinator = _coordinator(request.app["hass"])
        if coordinator is None:
            return self.json_message("Integration not ready", status_code=503)
        spots = spot_utils.sorted_spots(coordinator.store.spots)
        return self.json({"spots": [spot_utils.public(s) for s in spots]})

    async def post(self, request: web.Request) -> web.Response:
        coordinator = _coordinator(request.app["hass"])
        if coordinator is None:
            return self.json_message("Integration not ready", status_code=503)
        try:
            data = spot_utils.CREATE_SCHEMA(await request.json())
        except (ValueError, vol.Invalid) as err:
            return self.json_message(f"Invalid spot: {err}", status_code=400)
        spot = spot_utils.new_spot(data)
        coordinator.store.spots[spot["id"]] = spot
        await _spots_changed(coordinator)
        return self.json({"spot": spot_utils.public(spot)}, status_code=201)


class SpotView(HomeAssistantView):
    """Edit or delete one saved spot."""

    url = f"{API_BASE}/spots/{{spot_id}}"
    name = f"api:{DOMAIN}:spot"
    requires_auth = True

    async def patch(self, request: web.Request, spot_id: str) -> web.Response:
        coordinator = _coordinator(request.app["hass"])
        if coordinator is None:
            return self.json_message("Integration not ready", status_code=503)
        spot = coordinator.store.spots.get(spot_id)
        if spot is None:
            return self.json_message("Unknown spot", status_code=404)
        try:
            data = spot_utils.UPDATE_SCHEMA(await request.json())
        except (ValueError, vol.Invalid) as err:
            return self.json_message(f"Invalid spot: {err}", status_code=400)
        spot.update(data)
        await _spots_changed(coordinator)
        return self.json({"spot": spot_utils.public(spot)})

    async def delete(self, request: web.Request, spot_id: str) -> web.Response:
        coordinator = _coordinator(request.app["hass"])
        if coordinator is None:
            return self.json_message("Integration not ready", status_code=503)
        if coordinator.store.spots.pop(spot_id, None) is None:
            return self.json_message("Unknown spot", status_code=404)
        await _spots_changed(coordinator)
        return self.json({"deleted": spot_id})


async def _spots_changed(coordinator: FlightLogCoordinator) -> None:
    await coordinator.store.async_save()
    # Pushes the new list to the "saved spots" sensor, which the cards watch.
    coordinator.async_update_listeners()


def render_export(summary: dict[str, Any], track: dict[str, Any], fmt: str) -> tuple[str, str]:
    if fmt == "gpx":
        return track_to_gpx(summary, track), "application/gpx+xml"
    if fmt == "kml":
        return track_to_kml(summary, track), "application/vnd.google-earth.kml+xml"
    return json.dumps(track_to_geojson(summary, track)), "application/geo+json"


def async_register_views(hass: HomeAssistant) -> None:
    for view in (FlightsView, TracksView, TrackView, ExportView, SpotsView, SpotView):
        hass.http.register_view(view())
