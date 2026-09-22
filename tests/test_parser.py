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


def test_fallback_duration_from_timestamps():
    frames = make_frames(10)
    for f in frames:
        f.osd.fly_time = 0.0
    summary, _ = summarize_frames(frames, dict(BASE), max_track_points=100)
    assert summary.duration_s == 9.0


class _FakeDetails(SimpleNamespace):
    pass


def _patched(fake):
    """Swap in a stub pydjirecord module exposing only ``DJILog.from_bytes``."""
    return patch.dict(
        "sys.modules", {"pydjirecord": SimpleNamespace(DJILog=SimpleNamespace(from_bytes=lambda b: fake()))}
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

        def frames(self, keychains):
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

    kml = track_to_kml(s, t)
    assert "<coordinates>" in kml and "11.5,48.1,500.0" in kml


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
