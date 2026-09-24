"""Unit tests for the parser wrapper (no HA, no real DJI logs needed)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.dji_flightlog.const import (
    REASON_FC_DAT,
    REASON_SUPPORT_BUNDLE,
    REASON_TOO_LARGE,
    STATUS_HEADER_ONLY,
    STATUS_OK,
)
from custom_components.dji_flightlog.parser import (
    KeychainError,
    _downsample,
    _sd_card,
    classify_log_file,
    parse_flight,
    summarize_frames,
    track_to_geojson,
    track_to_gpx,
    track_to_kml,
)

from .conftest import Frame, make_frames

BASE = {
    "flight_id": "abc123",
    "file": "/x/y.txt",
    "filename": "y.txt",
    "log_version": 14,
    "start_time": "2026-01-01T00:00:00+00:00",
    "end_time": None,
    "duration_s": 0.0,
    "distance_m": 0.0,
    "max_height_m": 0.0,
    "max_h_speed_ms": 0.0,
    "max_v_speed_ms": 0.0,
    "aircraft_name": "Neo",
    "aircraft_sn": "SN1",
    "product_type": "NEO",
    "app_version": "1.0",
    "takeoff_lat": None,
    "takeoff_lon": None,
    "home_lat": None,
    "home_lon": None,
    "city": "",
    "street": "",
    "battery_start_pct": None,
    "battery_end_pct": None,
    "photo_num": 0,
    "video_time_s": 0.0,
    "points": 0,
    "bbox": None,
    "imported_at": "2026-01-01T00:00:00+00:00",
}


def test_summarize_frames(frames):
    summary, track = summarize_frames(frames, dict(BASE), max_track_points=1000)

    assert summary.status == STATUS_OK
    assert summary.duration_s == 99.0
    assert summary.distance_m == 990.0
    assert summary.max_height_m == 60.0
    assert summary.max_h_speed_ms == 10.0
    assert summary.max_v_speed_ms == 1.0
    assert summary.battery_start_pct == 95
    assert summary.battery_end_pct == 95 - 99 // 4
    assert summary.takeoff_lat == pytest.approx(48.1)
    assert summary.takeoff_lon == pytest.approx(11.5)
    assert summary.home_lat == pytest.approx(48.1)
    assert summary.start_time == "2026-09-20T12:00:00+00:00"
    assert summary.end_time == "2026-09-20T12:01:39+00:00"
    assert summary.bbox[1] == pytest.approx(48.1)
    assert summary.bbox[3] == pytest.approx(48.1 + 99 * 0.0001)
    assert summary.points == 100
    assert len(track.points) == 100
    # [lon, lat, alt, height, t, battery]
    assert track.points[0][:2] == [11.5, 48.1]
    assert track.points[-1][4] == 99.0
    assert track.home == [11.5, 48.1]


def test_summarize_skips_bad_gps(frames):
    frames[0].osd.gps_level = 0  # no fix yet
    frames[1].osd.latitude = 0.0
    frames[1].osd.longitude = 0.0
    summary, track = summarize_frames(frames, dict(BASE), max_track_points=1000)
    assert len(track.points) == 98
    assert summary.takeoff_lat == pytest.approx(48.1 + 2 * 0.0001)


def test_summarize_downsamples(frames):
    summary, track = summarize_frames(frames, dict(BASE), max_track_points=10)
    assert summary.points == len(track.points) == 11  # 10 + last point kept
    assert track.points[-1][4] == 99.0


def test_downsample_keeps_ends():
    pts = [[i] for i in range(1000)]
    out = _downsample(pts, 100)
    assert out[0] == [0] and out[-1] == [999]
    assert len(out) == 101
    assert _downsample(pts, 0) is pts
    assert _downsample(pts[:5], 100) == pts[:5]


def test_fly_time_continues_from_previous_flight():
    """Landed and took off again without a battery swap: fly_time starts at 590 s."""
    frames = make_frames(100)
    for f in frames:
        f.osd.fly_time += 590.8
    summary, track = summarize_frames(frames, dict(BASE), max_track_points=1000)
    assert summary.duration_s == 99.0
    assert track.points[0][4] == 0.0
    assert track.points[-1][4] == 99.0


def test_video_time_and_photos_from_frames():
    frames = make_frames(100)
    for i, f in enumerate(frames):
        # two clips: 10 s (frames 10-20) and 30 s (frames 50-80)
        if 10 <= i <= 20 or 50 <= i <= 80:
            f.camera.is_video = True
            f.camera.record_time = i - 10 if i <= 20 else i - 50
        f.camera.remain_photo_num = 500 - (i // 30)
    summary, _ = summarize_frames(frames, dict(BASE), max_track_points=100)
    assert summary.video_time_s == 40.0
    assert summary.photo_num == 3


def test_photo_count_unknown_keeps_header_value():
    # Models like the Avata 360 always report remain_photo_num = 0.
    summary, _ = summarize_frames(make_frames(10), dict(BASE, photo_num=2), max_track_points=100)
    assert summary.photo_num == 2
    assert summary.video_time_s == 0.0


def test_battery_health_from_frames():
    frames = make_frames(100)
    for i, f in enumerate(frames):
        f.recover.battery_sn = "A4SPNBJDA101JD" if i > 2 else ""
        if i < 5:
            continue  # no battery record yet: zeros must not count as readings
        b = f.battery
        b.voltage = 16.8 - i * 0.03
        b.temperature = 30.0 + i * 0.2
        b.design_capacity, b.full_capacity = 2880, 2743
        b.number_of_discharges, b.lifetime_remaining = 1, 99
        b.is_cell_voltage_estimated = i == 99  # estimated values are skipped
        b.cell_voltages = [4.2 - i * 0.01, 4.2 - i * 0.012, 4.2 - i * 0.01, 4.2 - i * 0.01]
        b.cell_voltage_deviation = round(i * 0.002, 3)
    summary, _ = summarize_frames(frames, dict(BASE), max_track_points=100)
    assert summary.battery_sn == "A4SPNBJDA101JD"
    assert summary.battery_cycles == 1
    assert summary.battery_life_pct == 99
    assert (summary.battery_full_mah, summary.battery_design_mah) == (2743, 2880)
    assert summary.battery_temp_start_c == 31.0
    assert summary.battery_temp_max_c == pytest.approx(49.8)
    assert summary.battery_cell_min_v == pytest.approx(4.2 - 98 * 0.012)
    assert summary.battery_cell_dev_max_v == pytest.approx(0.196)


def test_battery_health_absent():
    summary, _ = summarize_frames(make_frames(10), dict(BASE, battery_sn="HDR"), max_track_points=100)
    assert summary.battery_sn == "HDR"  # the header's serial survives
    assert summary.battery_cycles is None
    assert summary.battery_temp_max_c is None
    assert summary.battery_cell_min_v is None


def test_incident_levels():
    summary, _ = summarize_frames(make_frames(10), dict(BASE), max_track_points=100)
    assert (summary.incident, summary.incident_actions) == ("ok", [])

    frames = make_frames(10)
    frames[3].osd.flight_action = "RC_ONEKEY_GO_HOME"  # pilot pressed RTH: no incident
    frames[5].osd.flight_action = "SMART_POWER_GO_HOME"
    frames[6].osd.flight_action = "SMART_POWER_GO_HOME"
    summary, _ = summarize_frames(frames, dict(BASE), max_track_points=100)
    assert (summary.incident, summary.incident_actions) == ("warning", ["SMART_POWER_GO_HOME"])

    frames[8].osd.flight_action = "BATTERY_FORCE_LANDING"
    summary, _ = summarize_frames(frames, dict(BASE), max_track_points=100)
    assert summary.incident == "critical"
    assert summary.incident_actions == ["SMART_POWER_GO_HOME", "BATTERY_FORCE_LANDING"]


def test_incident_motor_blocked_only_in_the_air():
    frames = make_frames(10)
    frames[0].osd.is_motor_blocked = True  # height 0: failed start, not an incident
    summary, _ = summarize_frames(frames, dict(BASE), max_track_points=100)
    assert summary.incident == "ok"
    frames[5].osd.is_motor_blocked = True
    summary, _ = summarize_frames(frames, dict(BASE), max_track_points=100)
    assert (summary.incident, summary.incident_actions) == ("critical", ["MOTOR_BLOCKED"])


class Camera(SimpleNamespace):
    """Stand-in for pydjirecord's Camera record; _sd_card goes by the class name."""


def _cam(total: int, free: int, state: str = "NORMAL", card: bool = True):
    return SimpleNamespace(
        data=Camera(
            has_sd_card=card,
            sd_card_total_capacity=total,
            sd_card_remain_capacity=free,
            sd_card_state=SimpleNamespace(name=state),
        )
    )


def test_sd_card_from_records():
    records = [
        SimpleNamespace(data=b"unparsed"),
        _cam(0, 0, card=False),
        _cam(42958, 8492),
        _cam(42958, 5, "FULL"),
        _cam(42958, 5, "NORMAL"),
    ]
    assert _sd_card(records) == {"sd_total_mb": 42958, "sd_free_mb": 5, "sd_full": True}
    assert _sd_card([_cam(0, 0, card=False)]) == {}


def test_fallback_duration_from_timestamps():
    frames = make_frames(10)
    for f in frames:
        f.osd.fly_time = 0.0
    summary, _ = summarize_frames(frames, dict(BASE), max_track_points=100)
    assert summary.duration_s == 9.0


class _FakeDetails(SimpleNamespace):
    pass


def _patched(fake):
    """Swap in a stub pydjirecord: ``DJILog.from_bytes`` and a pass-through frame builder."""
    return patch.dict(
        "sys.modules",
        {
            "pydjirecord": SimpleNamespace(DJILog=SimpleNamespace(from_bytes=lambda b: fake())),
            "pydjirecord.frame.builder": SimpleNamespace(records_to_frames=lambda records, details: records),
        },
    )


def _fake_log(version: int, frames: list[Frame] | None = None, fail_keychain: bool = False):
    details = _FakeDetails(
        start_time=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
        total_time=120.0,
        total_distance=1234.0,
        max_height=80.0,
        max_horizontal_speed=12.0,
        max_vertical_speed=3.0,
        aircraft_name="Neo",
        aircraft_sn="SN1",
        product_type=SimpleNamespace(name="NEO"),
        app_version="1.2.3",
        latitude=48.2,
        longitude=11.6,
        city="München",
        street="",
        capture_num=3,
        video_time=30.0,
        battery_sn="A4SPNBJDA101JD",
    )

    class Log:
        def __init__(self):
            self.version = version
            self.details = details

        def fetch_keychains(self, api_key):
            if fail_keychain:
                raise RuntimeError("DJI API unreachable")
            assert api_key == "KEY"
            return [["kc"]]

        def records(self, keychains):
            # The stub builder passes records through, so these double as frames.
            if version >= 13:
                assert keychains == [["kc"]]
            return frames or []

    return Log


def _write_log(tmp_path: Path) -> Path:
    p = tmp_path / "DJIFlightRecord_2026-09-20_[12-00-00].txt"
    p.write_bytes(b"\x00" * 64)
    return p


def test_parse_flight_header_only_without_key(tmp_path):
    path = _write_log(tmp_path)
    fake = _fake_log(14)
    with _patched(fake):
        summary, track = parse_flight(path, api_key=None, max_track_points=100)
    assert track is None
    assert summary.status == STATUS_HEADER_ONLY
    assert summary.duration_s == 120.0
    assert summary.distance_m == 1234.0
    assert summary.takeoff_lat == 48.2
    assert summary.city == "München"
    assert summary.photo_num == 3
    assert summary.video_time_s is None  # the header value is not a duration
    assert summary.incident is None  # unknown without the frames
    assert summary.battery_sn == "A4SPNBJDA101JD"  # readable without the key
    assert len(summary.flight_id) == 16


def test_parse_flight_with_key(tmp_path):
    path = _write_log(tmp_path)
    fake = _fake_log(14, make_frames(50))
    with _patched(fake):
        summary, track = parse_flight(path, api_key="KEY", max_track_points=100)
    assert summary.status == STATUS_OK
    assert track is not None and len(track.points) == 50
    assert summary.duration_s == 49.0  # frames win over header


def test_parse_flight_keychain_error(tmp_path):
    path = _write_log(tmp_path)
    fake = _fake_log(14, fail_keychain=True)
    with _patched(fake), pytest.raises(KeychainError):
        parse_flight(path, api_key="KEY", max_track_points=100)


def test_parse_flight_old_version_needs_no_key(tmp_path):
    path = _write_log(tmp_path)
    fake = _fake_log(12, make_frames(5))
    with _patched(fake):
        summary, track = parse_flight(path, api_key=None, max_track_points=100)
    assert summary.status == STATUS_OK
    assert len(track.points) == 5


def test_exports(frames):
    summary, track = summarize_frames(frames, dict(BASE), max_track_points=1000)
    s, t = summary.as_dict(), track.as_dict()

    gj = track_to_geojson(s, t)
    assert gj["type"] == "FeatureCollection"
    assert gj["features"][0]["geometry"]["type"] == "LineString"
    assert len(gj["features"][0]["geometry"]["coordinates"]) == 100
    assert gj["features"][1]["geometry"]["coordinates"] == [11.5, 48.1]
    assert "file" not in gj["features"][0]["properties"]
    json.dumps(gj)

    gpx = track_to_gpx(s, t)
    assert gpx.count("<trkpt") == 100
    assert "<time>2026-09-20T12:00:00+00:00</time>" in gpx
    assert "<time>2026-09-20T12:01:39+00:00</time>" in gpx
    # Height above takeoff, not the log's (barometric) altitude of 500 m + height.
    assert "<ele>0.0</ele>" in gpx and "<ele>60.0</ele>" in gpx and "<ele>560.0</ele>" not in gpx
    assert gj["features"][0]["geometry"]["coordinates"][-1][2] == 60.0

    kml = track_to_kml(s, t)
    assert "<coordinates>" in kml and "11.5,48.1,0.0" in kml
    assert "<altitudeMode>relativeToGround</altitudeMode>" in kml


# --- file classification ----------------------------------------------------

# Header of a real DJI support log bundle, as exported by DJI Assistant 2
# (DJI_<model>_<date>.DAT): a record header naming an encrypted blob, then LOGH.
BUNDLE_HEAD = (
    bytes.fromhex("a4401110004eb455")
    + b"-E4/hms/system/hms/hms_07.log.enc"
    + bytes(213)
    + b"LOGH"
    + bytes.fromhex("02000000a0000000")
    + b"eagle4_wa530"
)


def test_classify_support_bundle(tmp_path):
    p = tmp_path / "DJI_Avata_360_2026-09-22_12-34-44.DAT"
    p.write_bytes(BUNDLE_HEAD + bytes(5000))
    assert classify_log_file(p, p.stat().st_size) == REASON_SUPPORT_BUNDLE


def test_classify_support_bundle_named_txt(tmp_path):
    # The suffix must not decide on its own; the content does.
    p = tmp_path / "renamed.txt"
    p.write_bytes(BUNDLE_HEAD)
    assert classify_log_file(p, p.stat().st_size) == REASON_SUPPORT_BUNDLE


def test_classify_fc_dat(tmp_path):
    p = tmp_path / "FLY042.DAT"
    p.write_bytes(bytes.fromhex("551234") + bytes(500))
    assert classify_log_file(p, p.stat().st_size) == REASON_FC_DAT


def test_classify_too_large(tmp_path):
    p = tmp_path / "huge.txt"
    p.write_bytes(bytes(16))
    assert classify_log_file(p, 64 * 1024 * 1024) == REASON_TOO_LARGE


def test_classify_accepts_flight_record(tmp_path):
    p = tmp_path / "DJIFlightRecord_2026-09-20_[12-00-00].txt"
    p.write_bytes(bytes.fromhex("0e0050") + b"x" * 4000)
    assert classify_log_file(p, p.stat().st_size) is None
