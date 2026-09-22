"""Parse DJI Fly flight record files into flight summaries and tracks.

Everything in here is synchronous and CPU/network bound; call it via
``hass.async_add_executor_job``. The module deliberately has no Home
Assistant imports so it can be unit-tested standalone.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .const import STATUS_FAILED, STATUS_HEADER_ONLY, STATUS_OK

_LOGGER = logging.getLogger(__name__)

# Minimum GPS quality for a frame to count as a valid fix.
_MIN_GPS_LEVEL = 3


class KeychainError(Exception):
    """Fetching the DJI decryption keychain failed (network / API key)."""


@dataclass
class FlightTrack:
    """Downsampled GPS track of a single flight.

    ``points`` are ``[lon, lat, altitude_m, height_m, t_offset_s, battery_pct]``
    so the JSON stays compact and directly usable by the map card.
    """

    points: list[list[float]] = field(default_factory=list)
    home: list[float] | None = None  # [lon, lat]

    def as_dict(self) -> dict[str, Any]:
        return {"points": self.points, "home": self.home}


@dataclass
class FlightSummary:
    """Everything the entities and the map need without loading the track."""

    flight_id: str
    file: str
    filename: str
    status: str
    log_version: int
    start_time: str  # ISO 8601, UTC
    end_time: str | None
    duration_s: float
    distance_m: float
    max_height_m: float
    max_h_speed_ms: float
    max_v_speed_ms: float
    aircraft_name: str
    aircraft_sn: str
    product_type: str
    app_version: str
    takeoff_lat: float | None
    takeoff_lon: float | None
    home_lat: float | None
    home_lon: float | None
    city: str
    street: str
    battery_start_pct: int | None
    battery_end_pct: int | None
    photo_num: int
    video_time_s: float
    points: int
    bbox: list[float] | None  # [min_lon, min_lat, max_lon, max_lat]
    imported_at: str
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def file_hash(path: Path) -> str:
    """Return the sha256 of a file; the first 16 hex chars are the flight id."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _valid_fix(lat: float, lon: float) -> bool:
    return (
        lat != 0.0
        and lon != 0.0
        and -90.0 <= lat <= 90.0
        and -180.0 <= lon <= 180.0
        and not math.isnan(lat)
        and not math.isnan(lon)
    )


def _downsample(points: list[list[float]], max_points: int) -> list[list[float]]:
    """Uniform stride downsampling that always keeps first and last point."""
    if max_points <= 0 or len(points) <= max_points:
        return points
    stride = len(points) / max_points
    out = [points[int(i * stride)] for i in range(max_points)]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def parse_flight(
    path: Path,
    *,
    api_key: str | None,
    max_track_points: int,
    now: datetime | None = None,
) -> tuple[FlightSummary, FlightTrack | None]:
    """Parse one DJI Fly ``.txt`` log.

    Returns the summary plus the track. When the log is encrypted (v13+) and
    no API key is configured, only the header is used and the track is
    ``None`` (``status == header_only``). A keychain fetch failure raises
    :class:`KeychainError` so the caller can retry on the next scan.
    """
    from pydjirecord import DJILog  # heavy import, keep it lazy

    now = now or datetime.now(UTC)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    flight_id = digest[:16]

    log = DJILog.from_bytes(data)
    details = log.details
    version = int(log.version)

    has_header_fix = _valid_fix(details.latitude, details.longitude)
    base: dict[str, Any] = {
        "flight_id": flight_id,
        "file": str(path),
        "filename": path.name,
        "log_version": version,
        "start_time": _iso(details.start_time) or _iso(now),
        "end_time": None,
        "duration_s": float(details.total_time or 0.0),
        "distance_m": float(details.total_distance or 0.0),
        "max_height_m": float(details.max_height or 0.0),
        "max_h_speed_ms": float(details.max_horizontal_speed or 0.0),
        "max_v_speed_ms": float(details.max_vertical_speed or 0.0),
        "aircraft_name": details.aircraft_name or "",
        "aircraft_sn": details.aircraft_sn or "",
        "product_type": getattr(details.product_type, "name", str(details.product_type)),
        "app_version": details.app_version or "",
        "takeoff_lat": details.latitude if has_header_fix else None,
        "takeoff_lon": details.longitude if has_header_fix else None,
        "home_lat": None,
        "home_lon": None,
        "city": details.city or "",
        "street": details.street or "",
        "battery_start_pct": None,
        "battery_end_pct": None,
        "photo_num": int(details.capture_num or 0),
        "video_time_s": float(details.video_time or 0.0),
        "points": 0,
        "bbox": None,
        "imported_at": _iso(now),
    }

    keychains = None
    if version >= 13:
        if not api_key:
            _LOGGER.info(
                "%s is an encrypted v%s log and no DJI API key is configured; importing header data only",
                path.name,
                version,
            )
            return FlightSummary(status=STATUS_HEADER_ONLY, **base), None
        try:
            keychains = log.fetch_keychains(api_key)
        except Exception as err:
            raise KeychainError(f"keychain fetch failed for {path.name}: {err}") from err

    try:
        frames = log.frames(keychains)
    except Exception as err:
        _LOGGER.warning("Frame decoding failed for %s: %s", path.name, err)
        return FlightSummary(status=STATUS_FAILED, error=str(err), **base), None

    return summarize_frames(frames, base, max_track_points)


def summarize_frames(
    frames: list[Any], base: dict[str, Any], max_track_points: int
) -> tuple[FlightSummary, FlightTrack]:
    """Build summary fields and a track from decoded frames."""
    raw_points: list[list[float]] = []
    first_time: datetime | None = None
    last_time: datetime | None = None
    max_fly_time = 0.0  # osd.fly_time is elapsed flight time, so its max is the duration
    home: list[float] | None = None
    battery_start: int | None = None
    battery_end: int | None = None
    max_height = 0.0
    max_h_speed = 0.0
    max_v_speed = 0.0
    cumulative = 0.0
    min_lon = min_lat = math.inf
    max_lon = max_lat = -math.inf

    for fr in frames:
        osd = fr.osd
        ts = getattr(fr.custom, "date_time", None)
        if ts is not None and ts.year > 2000:
            first_time = first_time or ts
            last_time = ts
        fly_time = float(getattr(osd, "fly_time", 0.0) or 0.0)
        max_fly_time = max(max_fly_time, fly_time)

        level = fr.battery.charge_level
        if level:
            battery_start = level if battery_start is None else battery_start
            battery_end = level

        max_height = max(max_height, float(osd.height or 0.0))
        max_h_speed = max(max_h_speed, float(osd.h_speed or 0.0))
        max_v_speed = max(max_v_speed, abs(float(osd.z_speed or 0.0)))
        cumulative = max(cumulative, float(osd.cumulative_distance or 0.0))

        if home is None and _valid_fix(fr.home.latitude, fr.home.longitude):
            home = [round(fr.home.longitude, 7), round(fr.home.latitude, 7)]

        if osd.gps_level < _MIN_GPS_LEVEL or not _valid_fix(osd.latitude, osd.longitude):
            continue
        lon, lat = osd.longitude, osd.latitude
        min_lon, max_lon = min(min_lon, lon), max(max_lon, lon)
        min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
        raw_points.append(
            [
                round(lon, 7),
                round(lat, 7),
                round(float(osd.altitude or 0.0), 1),
                round(float(osd.height or 0.0), 1),
                round(fly_time, 1),
                int(level or 0),
            ]
        )

    track = FlightTrack(points=_downsample(raw_points, max_track_points), home=home)

    if raw_points:
        base["takeoff_lon"], base["takeoff_lat"] = raw_points[0][0], raw_points[0][1]
        base["bbox"] = [min_lon, min_lat, max_lon, max_lat]
    if home is not None:
        base["home_lon"], base["home_lat"] = home
    if first_time is not None:
        base["start_time"] = _iso(first_time)
        base["end_time"] = _iso(last_time)
    if max_fly_time > 0:
        base["duration_s"] = round(max_fly_time, 1)
    elif first_time and last_time:
        base["duration_s"] = round((last_time - first_time).total_seconds(), 1)
    if cumulative > 0:
        base["distance_m"] = round(cumulative, 1)
    if max_height > 0:
        base["max_height_m"] = round(max_height, 1)
    if max_h_speed > 0:
        base["max_h_speed_ms"] = round(max_h_speed, 2)
    if max_v_speed > 0:
        base["max_v_speed_ms"] = round(max_v_speed, 2)
    base["battery_start_pct"] = battery_start
    base["battery_end_pct"] = battery_end
    base["points"] = len(track.points)

    return FlightSummary(status=STATUS_OK, **base), track


# ---------------------------------------------------------------------------
# Export helpers (pure functions, used by the service and the HTTP view)
# ---------------------------------------------------------------------------


def track_to_geojson(summary: dict[str, Any], track: dict[str, Any]) -> dict[str, Any]:
    """GeoJSON FeatureCollection: LineString track + optional Home point."""
    coords = [[p[0], p[1], p[2]] for p in track.get("points", [])]
    props = {k: v for k, v in summary.items() if k != "file"}
    features: list[dict[str, Any]] = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": props,
        }
    ]
    if track.get("home"):
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": track["home"]},
                "properties": {"name": "Home", "flight_id": summary["flight_id"]},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _flight_name(summary: dict[str, Any]) -> tuple[str, datetime]:
    start = datetime.fromisoformat(summary["start_time"])
    return f"{summary.get('aircraft_name') or 'DJI'} {start:%Y-%m-%d %H:%M}", start


def track_to_gpx(summary: dict[str, Any], track: dict[str, Any]) -> str:
    """GPX 1.1 track with timestamps derived from the flight start."""
    name, start = _flight_name(summary)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="dji_flightlog (Home Assistant)" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        f"  <metadata><name>{_xml(name)}</name><time>{start.isoformat()}</time></metadata>",
        f"  <trk><name>{_xml(name)}</name><trkseg>",
    ]
    for lon, lat, alt, _height, t_off, _bat in track.get("points", []):
        ts = datetime.fromtimestamp(start.timestamp() + t_off, tz=UTC)
        lines.append(
            f'    <trkpt lat="{lat}" lon="{lon}"><ele>{alt}</ele><time>{ts.isoformat()}</time></trkpt>'
        )
    lines += ["  </trkseg></trk>", "</gpx>", ""]
    return "\n".join(lines)


def track_to_kml(summary: dict[str, Any], track: dict[str, Any]) -> str:
    """KML LineString with absolute altitude."""
    name, _start = _flight_name(summary)
    coords = " ".join(f"{lon},{lat},{alt}" for lon, lat, alt, *_ in track.get("points", []))
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
            f"  <name>{_xml(name)}</name>",
            '  <Style id="track"><LineStyle><color>ff0000ff</color><width>3</width></LineStyle></Style>',
            f"  <Placemark><name>{_xml(name)}</name><styleUrl>#track</styleUrl>",
            "    <LineString><altitudeMode>absolute</altitudeMode><coordinates>",
            f"      {coords}",
            "    </coordinates></LineString></Placemark>",
            "</Document></kml>",
            "",
        ]
    )


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
