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

from .const import (
    MAX_LOG_FILE_BYTES,
    REASON_FC_DAT,
    REASON_SUPPORT_BUNDLE,
    REASON_TOO_LARGE,
    STATUS_FAILED,
    STATUS_HEADER_ONLY,
    STATUS_OK,
)

_LOGGER = logging.getLogger(__name__)

# Minimum GPS quality for a frame to count as a valid fix.
_MIN_GPS_LEVEL = 3

# Flight controller actions that mean something went wrong, by severity. Same
# split as pydjirecord's anomaly check, whose other rules are left out: it
# rates a descent faster than 10 m/s as critical, which is an ordinary dive
# for an FPV drone.
INCIDENT_CRITICAL = "critical"
INCIDENT_WARNING = "warning"
INCIDENT_OK = "ok"
_CRITICAL_ACTIONS = frozenset(
    {
        "OUT_OF_CONTROL_GO_HOME",
        "BATTERY_FORCE_LANDING",
        "SERIOUS_LOW_VOLTAGE_LANDING",
        "MOTORBLOCK_LANDING",
        "FAKE_BATTERY_LANDING",
        "RTH_COMING_OBSTACLE_LANDING",
        "IMU_ERROR_RTH",
        "MC_PROTECT_GO_HOME",
    }
)
_WARNING_ACTIONS = frozenset(
    {
        "WARNING_POWER_GO_HOME",
        "WARNING_POWER_LANDING",
        "SMART_POWER_GO_HOME",
        "SMART_POWER_LANDING",
        "LOW_VOLTAGE_LANDING",
        "LOW_VOLTAGE_GO_HOME",
        "AVOID_GROUND_LANDING",
        "AIRPORT_AVOID_LANDING",
        "TOO_CLOSE_GO_HOME_LANDING",
        "TOO_FAR_GO_HOME_LANDING",
        "APP_REQUEST_FORCE_LANDING",
    }
)


class KeychainError(Exception):
    """Fetching the DJI decryption keychain failed (network / API key)."""


# Markers of a DJI support log bundle, as exported by DJI Assistant 2 or the
# app's "export device logs". It packs AES-encrypted blobs (`*.log.enc`,
# `FC_SMP-*.DAT.enc`) that only DJI can decrypt - there is no flight record
# in there for us to read.
_BUNDLE_MARKERS = (b"LOGH", b".log.enc", b".DAT.enc", b"flyctrl_smp")
_HEAD_BYTES = 8192


def classify_log_file(path: Path, size: int) -> str | None:
    """Return a rejection reason for files that cannot hold a flight record.

    ``None`` means "looks like a flight record, try parsing it". Only the
    first few KB are read, so a 60 MB bundle never reaches memory.
    """
    # Reading the first few KB is cheap whatever the file size, and the
    # content gives a far more actionable reason than "too large" would.
    with path.open("rb") as fh:
        head = fh.read(_HEAD_BYTES)
    if any(marker in head for marker in _BUNDLE_MARKERS):
        return REASON_SUPPORT_BUNDLE
    if size > MAX_LOG_FILE_BYTES:
        return REASON_TOO_LARGE
    if path.suffix.lower() == ".dat":
        # Flight controller DAT (aircraft/SD card). Encrypted on every model
        # DJI Fly supports; DatCon only handles Phantom 3/4-era aircraft.
        return REASON_FC_DAT
    return None


# The flight detail charts get one sample per second, and at most this many.
_PROFILE_STEP_S = 1.0
_PROFILE_MAX_SAMPLES = 1800


@dataclass
class FlightTrack:
    """Downsampled GPS track of a single flight, plus what the detail view charts.

    ``points`` are ``[lon, lat, altitude_m, height_m, t_offset_s, battery_pct]``
    so the JSON stays compact and directly usable by the map card.
    ``profile`` holds equally long columns (``t``, ``height``, ``speed``,
    ``dist``, ``battery``, ``temp``, ``lat``, ``lon``; ``None`` where a frame
    had no reading), ``modes`` the flight mode segments ``[t_start, t_end,
    mode]``, ``events`` the flight controller actions ``[t, action]`` and
    ``videos`` the camera's recordings ``[t_start, t_end]`` (``t_end`` None
    while still recording when the log ends).
    """

    points: list[list[float]] = field(default_factory=list)
    home: list[float] | None = None  # [lon, lat]
    profile: dict[str, list[Any]] | None = None
    modes: list[list[Any]] = field(default_factory=list)
    events: list[list[Any]] = field(default_factory=list)
    videos: list[list[float | None]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "points": self.points,
            "home": self.home,
            "profile": self.profile,
            "modes": self.modes,
            "events": self.events,
            "videos": self.videos,
        }


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
    video_time_s: float | None  # None: header-only import, the header value is unusable
    points: int
    bbox: list[float] | None  # [min_lon, min_lat, max_lon, max_lat]
    imported_at: str
    error: str | None = None
    # Smart battery; everything but the serial needs the decoded frames.
    battery_sn: str = ""
    battery_cycles: int | None = None
    battery_life_pct: int | None = None  # DJI's "lifetime remaining"
    battery_full_mah: int | None = None
    battery_design_mah: int | None = None
    battery_temp_start_c: float | None = None
    battery_temp_max_c: float | None = None
    battery_cell_min_v: float | None = None
    battery_cell_dev_max_v: float | None = None
    # ok / warning / critical, plus the flight controller actions behind it
    incident: str | None = None
    incident_actions: list[str] = field(default_factory=list)
    # SD card at the end of the flight (MB, as the camera reports it)
    sd_total_mb: int | None = None
    sd_free_mb: int | None = None
    sd_full: bool | None = None
    sd_video_left_s: int | None = None  # the camera's estimate of the recording time left
    sd_problems: list[str] = field(default_factory=list)  # card states other than normal / full
    max_distance_m: float | None = None  # farthest point from home (or the takeoff)
    mode_time_s: dict[str, float] = field(default_factory=dict)  # seconds per flight mode

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


# The DJI app writes these placeholders into the address fields when its
# reverse geocoding has not finished before the record is closed.
_PLACE_PLACEHOLDERS = frozenset({"map loading", "loading", "unknown", "n/a", "--"})


def clean_place(value: str | None) -> str:
    text = (value or "").strip()
    return "" if text.lower() in _PLACE_PLACEHOLDERS else text


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
        "duration_s": round(float(details.total_time or 0.0), 1),
        "distance_m": round(float(details.total_distance or 0.0), 1),
        "max_height_m": round(float(details.max_height or 0.0), 1),
        "max_h_speed_ms": round(float(details.max_horizontal_speed or 0.0), 2),
        "max_v_speed_ms": round(float(details.max_vertical_speed or 0.0), 2),
        "aircraft_name": details.aircraft_name or "",
        "aircraft_sn": details.aircraft_sn or "",
        "product_type": getattr(details.product_type, "name", str(details.product_type)),
        "app_version": details.app_version or "",
        "takeoff_lat": details.latitude if has_header_fix else None,
        "takeoff_lon": details.longitude if has_header_fix else None,
        "home_lat": None,
        "home_lon": None,
        "city": clean_place(details.city),
        "street": clean_place(details.street),
        "battery_start_pct": None,
        "battery_end_pct": None,
        "photo_num": int(details.capture_num or 0),
        # The header's video_time has no consistent unit (65536 for a 4 min
        # clip); only the frames give the real recording time.
        "video_time_s": None,
        "points": 0,
        "bbox": None,
        "imported_at": _iso(now),
        "battery_sn": getattr(details, "battery_sn", "") or "",
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

    from pydjirecord.frame.builder import records_to_frames

    try:
        # Records, not log.frames(): the SD card's capacity is only in the raw
        # camera records, and decrypting the log twice would double the work.
        records = log.records(keychains)
        frames = records_to_frames(records, details)
    except Exception as err:
        _LOGGER.warning("Frame decoding failed for %s: %s", path.name, err)
        return FlightSummary(status=STATUS_FAILED, error=str(err), **base), None

    base.update(_sd_card(records))
    return summarize_frames(frames, base, max_track_points)


def summarize_frames(
    frames: list[Any], base: dict[str, Any], max_track_points: int
) -> tuple[FlightSummary, FlightTrack]:
    """Build summary fields and a track from decoded frames."""
    raw_points: list[list[float]] = []
    first_time: datetime | None = None
    last_time: datetime | None = None
    # osd.fly_time counts from power-on, not from takeoff: land and take off
    # again without a battery swap and the next log starts where the last one
    # ended. Offsets and duration are relative to the log's first frame.
    fly_time_base = float(getattr(frames[0].osd, "fly_time", 0.0) or 0.0) if frames else 0.0
    max_fly_time = 0.0
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
        fly_time = max(0.0, float(getattr(osd, "fly_time", 0.0) or 0.0) - fly_time_base)
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
    timeline = _timeline(frames, fly_time_base if max_fly_time > 0 else None, first_time, home)
    track.profile, track.modes, track.events = timeline["profile"], timeline["modes"], timeline["events"]
    track.videos = timeline["videos"]
    base["max_distance_m"] = timeline["max_distance_m"]
    base["mode_time_s"] = timeline["mode_time_s"]

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
    base.update(_battery_health(frames))
    base.update(_incident(frames))
    base["video_time_s"] = _video_time(frames)
    photos = _photo_count(frames)
    if photos is not None:
        base["photo_num"] = photos
    base["points"] = len(track.points)

    return FlightSummary(status=STATUS_OK, **base), track


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(a))


def _name(value: Any) -> str | None:
    """Enum member or plain string (test stand-ins) to its name."""
    name = getattr(value, "name", value)
    return name if isinstance(name, str) and name else None


def _timeline(
    frames: list[Any], fly_time_base: float | None, first_time: datetime | None, home: list[float] | None
) -> dict[str, Any]:
    """Per-second profile, flight mode segments, events and distance from home.

    ``fly_time_base`` is None when the frames carry no flight time; the wall
    clock timestamps are used then.
    """
    rows: list[tuple[Any, ...]] = []
    for fr in frames:
        osd = fr.osd
        if fly_time_base is not None:
            t = max(0.0, float(getattr(osd, "fly_time", 0.0) or 0.0) - fly_time_base)
        else:
            ts = getattr(fr.custom, "date_time", None)
            if ts is None or ts.year <= 2000 or first_time is None:
                continue
            t = (ts - first_time).total_seconds()
        fix = osd.gps_level >= _MIN_GPS_LEVEL and _valid_fix(osd.latitude, osd.longitude)
        bat = fr.battery
        has_bat = bool(getattr(bat, "voltage", 0.0))
        camera = getattr(fr, "camera", None)
        rows.append(
            (
                t,
                float(osd.height or 0.0),
                float(osd.h_speed or 0.0),
                osd.latitude if fix else None,
                osd.longitude if fix else None,
                int(bat.charge_level) if bat.charge_level else None,
                float(bat.temperature) if has_bat else None,
                _name(getattr(osd, "flyc_state", None)),
                _name(getattr(osd, "flight_action", None)),
                bool(getattr(camera, "is_video", False)),
                int(getattr(camera, "record_time", 0) or 0),
            )
        )

    ref = (home[1], home[0]) if home else next(((r[3], r[4]) for r in rows if r[3] is not None), None)
    dists = [
        _haversine_m(ref[0], ref[1], r[3], r[4]) if ref is not None and r[3] is not None else None
        for r in rows
    ]

    modes: list[list[Any]] = []
    events: list[list[Any]] = []
    prev_action: str | None = None
    for r in rows:
        t, mode, action = r[0], r[7], r[8]
        if mode is not None:
            if modes and modes[-1][2] == mode:
                modes[-1][1] = t
            else:
                if modes:
                    modes[-1][1] = t  # the previous mode lasts until this one starts
                modes.append([t, t, mode])
        if action and action != "NONE" and action != prev_action:
            events.append([round(t, 1), action])
        prev_action = action
    # Recordings as the camera reports them. The file name's time comes about
    # 2 s early (the file is created before the camera records), so the
    # detail view aligns the recordings to these starts.
    videos: list[list[float | None]] = []
    recording = False
    for r in rows:
        t, is_video, record_time = r[0], r[9], r[10]
        if is_video and not recording:
            # Already recording when the log starts: record_time says since when.
            videos.append([round(t - record_time, 1), None])
        if is_video:
            videos[-1][1] = round(t, 1)
        recording = is_video
    if recording:
        videos[-1][1] = None  # still recording when the log ends: length unknown

    mode_time: dict[str, float] = {}
    for t0, t1, mode in modes:
        mode_time[mode] = mode_time.get(mode, 0.0) + (t1 - t0)

    picked: list[int] = []
    next_t = -math.inf
    for i, r in enumerate(rows):
        if r[0] >= next_t:
            picked.append(i)
            next_t = r[0] + _PROFILE_STEP_S
    if rows and picked[-1] != len(rows) - 1:
        picked.append(len(rows) - 1)
    picked = _downsample(picked, _PROFILE_MAX_SAMPLES)

    def col(values: list[Any], digits: int | None) -> list[Any]:
        return [None if v is None else (round(v, digits) if digits is not None else v) for v in values]

    sel = [rows[i] for i in picked]
    profile = (
        {
            "t": col([r[0] for r in sel], 1),
            "height": col([r[1] for r in sel], 1),
            "speed": col([r[2] for r in sel], 1),
            "dist": col([dists[i] for i in picked], 0),
            "battery": [r[5] for r in sel],
            "temp": col([r[6] for r in sel], 1),
            "lat": col([r[3] for r in sel], 6),
            "lon": col([r[4] for r in sel], 6),
        }
        if sel
        else None
    )
    known = [d for d in dists if d is not None]
    return {
        "profile": profile,
        "modes": [[round(t0, 1), round(t1, 1), m] for t0, t1, m in modes],
        "events": events,
        "videos": videos,
        "max_distance_m": round(max(known), 1) if known else None,
        "mode_time_s": {m: round(s, 1) for m, s in mode_time.items()},
    }


def _incident(frames: list[Any]) -> dict[str, Any]:
    """Worst flight controller action of the flight (RTH on low battery, forced landing, ...)."""
    actions: list[str] = []
    motor_blocked = False
    for fr in frames:
        osd = fr.osd
        action = getattr(osd, "flight_action", None)
        name = getattr(action, "name", action)
        if name and (name in _CRITICAL_ACTIONS or name in _WARNING_ACTIONS) and name not in actions:
            actions.append(name)
        # In the air only; a blocked motor on the ground is a failed start.
        if getattr(osd, "is_motor_blocked", False) and float(osd.height or 0.0) > 1.0:
            motor_blocked = True
    if motor_blocked and "MOTOR_BLOCKED" not in actions:
        actions.append("MOTOR_BLOCKED")
    if motor_blocked or any(a in _CRITICAL_ACTIONS for a in actions):
        level = INCIDENT_CRITICAL
    elif actions:
        level = INCIDENT_WARNING
    else:
        level = INCIDENT_OK
    return {"incident": level, "incident_actions": actions}


# Card states that need no attention: fine, full (reported on its own) or
# passing while the camera starts up or formats.
_SD_OK_STATES = frozenset({"NORMAL", "FULL", "INITIALIZE", "FORMATTING"})


def _sd_card(records: list[Any]) -> dict[str, Any]:
    """SD card capacity, fill state and faults from the camera records."""
    out: dict[str, Any] = {}
    problems: list[str] = []
    cameras = 0
    card_seen = False
    for rec in records:
        cam = getattr(rec, "data", None)
        if type(cam).__name__ != "Camera":
            continue
        cameras += 1
        if not cam.has_sd_card:
            continue
        card_seen = True
        state = getattr(cam.sd_card_state, "name", "") or ""
        if state and state not in _SD_OK_STATES and state not in problems:
            problems.append(state)
        if not cam.sd_card_total_capacity:
            continue
        out = {
            "sd_total_mb": int(cam.sd_card_total_capacity),
            "sd_free_mb": int(cam.sd_card_remain_capacity),
            "sd_video_left_s": int(getattr(cam, "remain_video_timer", 0) or 0),
            # Once full, stay full for this flight even if a later record says otherwise.
            "sd_full": out.get("sd_full", False) or state == "FULL",
        }
    if cameras and not card_seen:
        problems.append("NO_CARD")
    if problems:
        out["sd_problems"] = problems
    return out


# A LiPo cell reading outside this range is not a reading: pydjirecord sizes the
# cell list by the model's usual cell count and leaves missing cells at 0 V
# (seen on a DJI Neo 2), which made its own deviation "3.66 V".
_CELL_PLAUSIBLE_V = (2.0, 4.6)


def _battery_health(frames: list[Any]) -> dict[str, Any]:
    """Smart battery figures for the flight; empty if the log carries none."""
    out: dict[str, Any] = {}
    temps: list[float] = []
    cell_min = math.inf
    dev_max = 0.0
    sn = ""
    for fr in frames:
        sn = max(sn, getattr(getattr(fr, "recover", None), "battery_sn", "") or "", key=len)
        bat = fr.battery
        # Frames before the first battery record carry zeros, not readings.
        if not getattr(bat, "voltage", 0.0):
            continue
        temps.append(float(bat.temperature))
        if bat.design_capacity:
            out["battery_cycles"] = int(bat.number_of_discharges)
            out["battery_life_pct"] = int(bat.lifetime_remaining) or None
            out["battery_full_mah"] = int(bat.full_capacity) or None
            out["battery_design_mah"] = int(bat.design_capacity)
        if not bat.is_cell_voltage_estimated:
            lo, hi = _CELL_PLAUSIBLE_V
            cells = [v for v in bat.cell_voltages if lo <= v <= hi]
            if cells:
                cell_min = min(cell_min, *cells)
                dev_max = max(dev_max, max(cells) - min(cells))
    if sn:
        out["battery_sn"] = sn
    if temps:
        out["battery_temp_start_c"] = round(temps[0], 1)
        out["battery_temp_max_c"] = round(max(temps), 1)
    if cell_min < math.inf:
        out["battery_cell_min_v"] = round(cell_min, 3)
        out["battery_cell_dev_max_v"] = round(dev_max, 3)
    return out


def _video_time(frames: list[Any]) -> float:
    """Seconds of video recorded: the sum of each recording's longest record_time."""
    total = 0
    segment = 0
    for fr in frames:
        camera = getattr(fr, "camera", None)
        if camera is not None and camera.is_video:
            segment = max(segment, int(camera.record_time or 0))
        elif segment:
            total += segment
            segment = 0
    return float(total + segment)


def _photo_count(frames: list[Any]) -> int | None:
    """Photos taken, from the drop in remaining shots; None if the model never reports it."""
    remaining = [
        fr.camera.remain_photo_num
        for fr in frames
        if getattr(fr, "camera", None) is not None and fr.camera.remain_photo_num > 0
    ]
    return max(0, remaining[0] - remaining[-1]) if remaining else None


# ---------------------------------------------------------------------------
# Export helpers (pure functions, used by the service and the HTTP view)
#
# Elevations are the height above the takeoff point, not the log's altitude:
# DJI adds a barometric home altitude to it that drifts from day to day and
# sits below 0 m even at sea level, which buried exported tracks underground.
# ---------------------------------------------------------------------------


def track_to_geojson(summary: dict[str, Any], track: dict[str, Any]) -> dict[str, Any]:
    """GeoJSON FeatureCollection: LineString track + optional Home point."""
    coords = [[p[0], p[1], p[3]] for p in track.get("points", [])]
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
    """GPX 1.1 track with timestamps derived from the flight start, ele = height above takeoff."""
    name, start = _flight_name(summary)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="dji_flightlog (Home Assistant)" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        f"  <metadata><name>{_xml(name)}</name><time>{start.isoformat()}</time></metadata>",
        f"  <trk><name>{_xml(name)}</name><trkseg>",
    ]
    for lon, lat, _alt, height, t_off, _bat in track.get("points", []):
        ts = datetime.fromtimestamp(start.timestamp() + t_off, tz=UTC)
        lines.append(
            f'    <trkpt lat="{lat}" lon="{lon}"><ele>{height}</ele><time>{ts.isoformat()}</time></trkpt>'
        )
    lines += ["  </trkseg></trk>", "</gpx>", ""]
    return "\n".join(lines)


def track_to_kml(summary: dict[str, Any], track: dict[str, Any]) -> str:
    """KML LineString, height relative to the ground."""
    name, _start = _flight_name(summary)
    coords = " ".join(f"{lon},{lat},{height}" for lon, lat, _alt, height, *_ in track.get("points", []))
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
            f"  <name>{_xml(name)}</name>",
            '  <Style id="track"><LineStyle><color>ff0000ff</color><width>3</width></LineStyle></Style>',
            f"  <Placemark><name>{_xml(name)}</name><styleUrl>#track</styleUrl>",
            "    <LineString><altitudeMode>relativeToGround</altitudeMode><coordinates>",
            f"      {coords}",
            "    </coordinates></LineString></Placemark>",
            "</Document></kml>",
            "",
        ]
    )


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
