"""Authenticated HTTP API used by the map card (and anything else)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiohttp
import voluptuous as vol
from aiohttp import BodyPartReader, web
from homeassistant.components.http import HomeAssistantView, require_admin
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant

from . import spots as spot_utils
from .const import (
    DOMAIN,
    EXPORT_FORMATS,
    MAX_LOG_FILE_BYTES,
    MEDIA_URL_TTL_S,
    REASON_NOT_TXT,
    REASON_TOO_LARGE,
    UPLOAD_REJECTED,
)
from .coordinator import FlightLogCoordinator
from .local_media import content_type
from .media import ROLE_ORIGINAL, ROLE_RAW
from .media_backend import MediaError
from .media_coordinator import (
    MediaCoordinator,
    media_coordinators,
    media_index,
    media_status,
    original_ref,
    play_projection,
    playable_ref,
)
from .parser import _downsample, track_to_geojson, track_to_gpx, track_to_kml

_LOGGER = logging.getLogger(__name__)

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
        hass: HomeAssistant = request.app["hass"]
        data = coordinator.data
        flights = _filter_flights(coordinator, request.query)
        body: dict[str, Any] = {
            "aircraft": {sn: a.as_dict() for sn, a in data.aircraft.items()},
            "totals": data.totals.as_dict(),
            "attention": data.attention,
            "last_import": data.last_import,
            "last_scan": data.last_scan,
        }
        if media_coordinators(hass):
            index = media_index(hass, data.flights)
            flights = [
                {**f, "media": [_public_recording(hass, r) for r in index.by_flight.get(f["flight_id"], [])]}
                for f in flights
            ]
            body["media"] = media_status(hass, index)
        body["flights"] = flights
        return self.json(body)


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


class AttentionDismissView(HomeAssistantView):
    """Mark pre-flight notices as done: ``{"keys": [...]}``."""

    url = f"{API_BASE}/attention/dismiss"
    name = f"api:{DOMAIN}:attention_dismiss"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        coordinator = _coordinator(request.app["hass"])
        if coordinator is None or coordinator.data is None:
            return self.json_message("Integration not ready", status_code=503)
        try:
            body = await request.json()
            keys = [str(k) for k in body["keys"]]
        except (ValueError, KeyError, TypeError):
            return self.json_message('Expected {"keys": [...]}', status_code=400)
        await coordinator.async_dismiss(keys)
        return self.json({"attention": coordinator.data.attention})


_UNSAFE_CHARS = re.compile(r"[^\w.()\[\] -]")


def safe_log_filename(name: str) -> str | None:
    """Reduce an uploaded file name to a plain ``*.txt`` name, or ``None``."""
    base = re.split(r"[\\/]", name)[-1].strip()
    base = _UNSAFE_CHARS.sub("_", base).lstrip(".")[-120:]
    if not base.lower().endswith(".txt") or len(base) <= len(".txt"):
        return None
    return base


class UploadView(HomeAssistantView):
    """Upload DJI Fly flight records from the browser (multipart, field ``file``).

    Each file is saved into the log folder and imported right away. The panel
    sends one file per request, so progress can be shown and HA's request
    size limit never matters; several files per request work as well.
    """

    url = f"{API_BASE}/upload"
    name = f"api:{DOMAIN}:upload"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        coordinator = _coordinator(request.app["hass"])
        if coordinator is None:
            return self.json_message("Integration not ready", status_code=503)
        try:
            reader = await request.multipart()
        except (AssertionError, ValueError):
            return self.json_message("Expected multipart/form-data", status_code=400)

        results: list[dict[str, Any]] = []
        while (part := await reader.next()) is not None:
            if not isinstance(part, BodyPartReader) or part.name != "file" or not part.filename:
                continue
            filename = safe_log_filename(part.filename)
            if filename is None:
                results.append({"file": part.filename, "status": UPLOAD_REJECTED, "reason": REASON_NOT_TXT})
                continue
            data = bytearray()
            while chunk := await part.read_chunk():
                data += chunk
                if len(data) > MAX_LOG_FILE_BYTES:
                    break
            if len(data) > MAX_LOG_FILE_BYTES:
                results.append({"file": part.filename, "status": UPLOAD_REJECTED, "reason": REASON_TOO_LARGE})
                continue
            results.append(await coordinator.async_import_upload(filename, bytes(data)))

        if not results:
            return self.json_message("No file in the request (form field 'file')", status_code=400)
        return self.json({"results": results})


async def _spots_changed(coordinator: FlightLogCoordinator) -> None:
    await coordinator.store.async_save()
    # Pushes the new list to the "saved spots" sensor, which the cards watch.
    coordinator.async_update_listeners()


def _public_recording(hass: HomeAssistant, rec: dict[str, Any]) -> dict[str, Any]:
    """What the frontend gets per recording, with signed thumb/play URLs.

    The URLs are signed because <img>/<video> cannot send the auth header.
    OneDrive item ids only contain [A-Za-z0-9!] and local ids are hex, so the
    path needs no quoting (and must not be quoted: the signature is checked
    against the decoded path).

    ``web_url`` opens the file at the source (OneDrive). Sources without a web
    view (local folder) get ``download`` for the original instead.
    """
    ttl = timedelta(seconds=MEDIA_URL_TTL_S)
    base = f"{API_BASE}/media/{rec['id']}"
    original = rec.get(ROLE_ORIGINAL) or {}
    download = not rec.get("web_url") and original_ref(rec) is not None
    return {
        "id": rec["id"],
        "kind": rec["kind"],
        "name": rec["name"],
        "start": rec.get("start"),
        "duration_s": rec.get("duration_s"),
        "size": original.get("size"),
        "web_url": rec.get("web_url"),
        "has_original": bool(original),
        "has_raw": bool(rec.get(ROLE_RAW)),
        "thumb": async_sign_path(hass, f"{base}/thumb", ttl),
        "play": async_sign_path(hass, f"{base}/play", ttl) if playable_ref(rec) else None,
        # "equirect" / "dfisheye": play as a 360° video; None: flat.
        "projection": play_projection(rec),
        "download": async_sign_path(hass, f"{base}/original", ttl) if download else None,
    }


def _find_recording(hass: HomeAssistant, rec_id: str) -> tuple[MediaCoordinator, dict[str, Any]] | None:
    for coordinator in media_coordinators(hass):
        rec = coordinator.recording(rec_id)
        if rec is not None:
            return coordinator, rec
    return None


class MediaThumbView(HomeAssistantView):
    """Cover image of a recording (cached on disk)."""

    url = f"{API_BASE}/media/{{rec_id}}/thumb"
    name = f"api:{DOMAIN}:media_thumb"
    requires_auth = True

    async def get(self, request: web.Request, rec_id: str) -> web.Response:
        found = _find_recording(request.app["hass"], rec_id)
        if found is None:
            return self.json_message("Unknown recording", status_code=404)
        coordinator, rec = found
        try:
            data = await coordinator.async_thumbnail(rec)
        except (MediaError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Thumbnail for %s failed: %s", rec["name"], err)
            return self.json_message(f"{coordinator.backend.label} not reachable", status_code=502)
        if not data:
            return self.json_message("No thumbnail", status_code=404)
        return web.Response(
            body=data, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"}
        )


async def _serve_file(
    view: HomeAssistantView,
    request: web.Request,
    rec_id: str,
    pick: Callable[[dict[str, Any]], dict[str, Any] | None],
    *,
    attachment: bool,
) -> web.StreamResponse:
    """Redirect to the file at the source (OneDrive) or stream it from disk (local folder)."""
    found = _find_recording(request.app["hass"], rec_id)
    if found is None:
        return view.json_message("Unknown recording", status_code=404)
    coordinator, rec = found
    ref = pick(rec)
    try:
        target = await coordinator.async_file(ref)
    except (MediaError, aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("File of %s not available: %s", rec["name"], err)
        return view.json_message(f"{coordinator.backend.label} not reachable", status_code=502)
    if not target:
        return view.json_message("No such file for this recording", status_code=404)
    if isinstance(target, str):
        raise web.HTTPFound(target)
    # FileResponse answers Range requests, so the player can seek.
    name = ref["name"].replace('"', "")
    disposition = "attachment" if attachment else "inline"
    return web.FileResponse(
        Path(target),
        headers={
            "Content-Type": content_type(ref["name"]),
            "Content-Disposition": f'{disposition}; filename="{name}"',
            "Cache-Control": "private, max-age=3600",
        },
    )


class MediaPlayView(HomeAssistantView):
    """The playable file: the proxy, else a normal video or photo."""

    url = f"{API_BASE}/media/{{rec_id}}/play"
    name = f"api:{DOMAIN}:media_play"
    requires_auth = True

    async def get(self, request: web.Request, rec_id: str) -> web.StreamResponse:
        return await _serve_file(self, request, rec_id, playable_ref, attachment=False)


class MediaOriginalView(HomeAssistantView):
    """Download the original (sources without a web view, i.e. a local folder)."""

    url = f"{API_BASE}/media/{{rec_id}}/original"
    name = f"api:{DOMAIN}:media_original"
    requires_auth = True

    async def get(self, request: web.Request, rec_id: str) -> web.StreamResponse:
        return await _serve_file(self, request, rec_id, original_ref, attachment=True)


def render_export(summary: dict[str, Any], track: dict[str, Any], fmt: str) -> tuple[str, str]:
    if fmt == "gpx":
        return track_to_gpx(summary, track), "application/gpx+xml"
    if fmt == "kml":
        return track_to_kml(summary, track), "application/vnd.google-earth.kml+xml"
    return json.dumps(track_to_geojson(summary, track)), "application/geo+json"


def async_register_views(hass: HomeAssistant) -> None:
    for view in (
        FlightsView,
        TracksView,
        TrackView,
        ExportView,
        SpotsView,
        SpotView,
        UploadView,
        AttentionDismissView,
        MediaThumbView,
        MediaPlayView,
        MediaOriginalView,
    ):
        hass.http.register_view(view())
