"""Pure helpers of media.py: flight record names and copies across sources."""

from __future__ import annotations

from homeassistant.util import dt as dt_util

from custom_components.dji_flightlog.media import (
    KIND_360,
    KIND_PHOTO,
    KIND_VIDEO,
    ROLE_COVER,
    ROLE_EQUIRECT,
    ROLE_ORIGINAL,
    ROLE_PROXY,
    build_recordings,
    classify_name,
    duplicate_recordings,
    is_flight_record,
    match_recordings,
    model_key,
)
from custom_components.dji_flightlog.media_coordinator import play_projection, playable_ref
from custom_components.dji_flightlog.onedrive import OneDriveClient, OneDriveMedia, normalize_item

from .test_local_media import _mp4


def test_is_flight_record() -> None:
    assert is_flight_record("FlightRecord_2026-09-21_[18-58-21].txt")
    assert is_flight_record("DJIFlightRecord_2026-09-01_0.txt")
    assert is_flight_record("djiflightrecord_2026-09-01_0.TXT")
    assert not is_flight_record("notes.txt")
    assert not is_flight_record("FlightRecord_2026-09-21.dat")
    assert not is_flight_record("FlightRecord_../../evil.txt")


def _rec(rec_id: str, name: str, size: int, *roles: str) -> dict:
    rec = {"id": rec_id, "name": name, "original": {"item_id": rec_id, "name": name, "size": size}}
    for role in roles:
        rec[role] = {"item_id": f"{rec_id}-{role}", "name": f"{name}-{role}", "size": 1}
    return rec


def test_duplicate_recordings() -> None:
    onedrive = {
        "a": _rec("a", "DJI_20260921190306_0001_D.MP4", 1000),
        "b": _rec("b", "DJI_20260921191000_0002_D.MP4", 2000, "proxy"),
    }
    nas = {
        # Same shot, but the NAS copy also has the proxy and a cover: it wins.
        "x": _rec("x", "dji_20260921190306_0001_d.mp4", 1000, "proxy", "cover"),
        # Same shot, fewer files: the OneDrive copy stays.
        "y": _rec("y", "DJI_20260921191000_0002_D.MP4", 2000),
        # Same name, different size: another shot (reused DJI counter).
        "z": _rec("z", "DJI_20260921191000_0002_D.MP4", 3000),
    }
    assert duplicate_recordings([onedrive, nas]) == {"a", "y"}
    # A tie keeps the copy of the earlier source.
    assert duplicate_recordings([{"a": onedrive["a"]}, {"c": _rec("c", onedrive["a"]["name"], 1000)}]) == {
        "c"
    }
    assert duplicate_recordings([onedrive]) == set()


def test_onedrive_keeps_flight_records() -> None:
    """Graph items: flight records are kept (for the import), but never become recordings."""
    raw = {"id": "l1", "name": "FlightRecord_2026-09-21_[18-58-21].txt", "size": 300_000, "file": {}}
    item = normalize_item(raw)
    assert item is not None and item["id"] == "l1"
    assert normalize_item({"id": "n1", "name": "notes.txt", "file": {}}) is None
    assert build_recordings({"l1": item}, dt_util.UTC) == {}


def test_classify_name() -> None:
    stem = "DJI_20260921193308_0005_D"
    expected = {
        f"{stem}.OSV": (ROLE_ORIGINAL, KIND_360),
        f"{stem}.LRF": (ROLE_PROXY, None),
        f"{stem}_proxy.mp4": (ROLE_PROXY, None),
        f"{stem}_360.mp4": (ROLE_EQUIRECT, None),
        f"{stem.lower()}_360.MP4": (ROLE_EQUIRECT, None),
        f"{stem}_cover.jpg": (ROLE_COVER, None),
        f"{stem}.MP4": (ROLE_ORIGINAL, KIND_VIDEO),
        # Only videos are 360° renders.
        f"{stem}_360.jpg": (ROLE_ORIGINAL, KIND_PHOTO),
        # Other names: suffix after the timestamp.
        "flight_20260921_193308_360.mp4": (ROLE_EQUIRECT, None),
    }
    for name, (role, kind) in expected.items():
        info = classify_name(name)
        assert info is not None, name
        assert (info.role, info.kind) == (role, kind), name
        assert info.local_time is not None and info.local_time.minute == 33, name
    assert classify_name(f"{stem}_360.mp4").key == stem
    assert classify_name("flight_20260921_193308_360.mp4").key == "flight_20260921_193308"


def _items(*names: str) -> dict[str, dict]:
    return {
        name: {"id": name, "name": name, "size": 100 + i, "folder": "f", "duration_ms": 137_000}
        for i, name in enumerate(names)
    }


def test_360_recording_prefers_equirect() -> None:
    stem = "DJI_20260921193308_0005_D"
    recs = build_recordings(
        _items(f"{stem}.OSV", f"{stem}_proxy.mp4", f"{stem}_360.mp4", f"{stem}_cover.jpg"), dt_util.UTC
    )
    # One recording, named after the original.
    (rec,) = recs.values()
    assert rec["id"] == f"{stem}.OSV"
    assert rec["kind"] == KIND_360
    assert rec["duration_s"] == 137.0
    assert rec[ROLE_EQUIRECT]["name"] == f"{stem}_360.mp4"
    assert playable_ref(rec)["name"] == f"{stem}_360.mp4"
    assert play_projection(rec) == "equirect"

    # Without the render the dual fisheye proxy plays.
    (rec,) = build_recordings(_items(f"{stem}.OSV", f"{stem}.LRF"), dt_util.UTC).values()
    assert playable_ref(rec)["name"] == f"{stem}.LRF"
    assert play_projection(rec) == "dfisheye"
    # The 360° original alone does not play.
    (rec,) = build_recordings(_items(f"{stem}.OSV"), dt_util.UTC).values()
    assert playable_ref(rec) is None
    assert play_projection(rec) is None

    # Without the original the render still makes it a 360° recording.
    (rec,) = build_recordings(_items(f"{stem}.LRF", f"{stem}_360.mp4"), dt_util.UTC).values()
    assert rec["kind"] == KIND_360
    assert rec["name"] == f"{stem}.LRF"
    assert play_projection(rec) == "equirect"
    (rec,) = build_recordings(_items(f"{stem}_360.mp4"), dt_util.UTC).values()
    assert rec["kind"] == KIND_360
    assert playable_ref(rec)["name"] == f"{stem}_360.mp4"


def test_flat_recording_projection() -> None:
    stem = "DJI_20260921190306_0001_D"
    (rec,) = build_recordings(_items(f"{stem}.MP4", f"{stem}.LRF"), dt_util.UTC).values()
    assert rec["kind"] == KIND_VIDEO
    assert playable_ref(rec)["name"] == f"{stem}.LRF"
    assert play_projection(rec) is None
    (rec,) = build_recordings(_items(f"{stem}.JPG"), dt_util.UTC).values()
    assert playable_ref(rec)["name"] == f"{stem}.JPG"
    assert play_projection(rec) is None


def test_dji_files_group_across_folders() -> None:
    """A source that reports the proxy in another folder still yields one recording."""
    stem = "DJI_20260925181819_0015_D"
    items = _items(f"{stem}.MP4", f"{stem}.LRF", "clip_1.MP4", "clip_1.LRF")
    items[f"{stem}.LRF"]["folder"] = "g"
    items["clip_1.LRF"]["folder"] = "g"
    recs = build_recordings(items, dt_util.UTC)
    (rec,) = (r for r in recs.values() if r["name"].startswith("DJI_"))
    assert rec["name"] == f"{stem}.MP4"
    assert playable_ref(rec)["name"] == f"{stem}.LRF"
    # Names without a timestamp stay apart per folder.
    clips = sorted(r["name"] for r in recs.values() if r["name"].startswith("clip"))
    assert clips == ["clip_1.LRF", "clip_1.MP4"]


def test_duplicate_prefers_copy_with_render() -> None:
    name = "DJI_20260921193308_0005_D.OSV"
    onedrive = {"a": _rec("a", name, 5000, "proxy")}
    nas = {"x": _rec("x", name, 5000, "proxy", "equirect")}
    assert duplicate_recordings([onedrive, nas]) == {"a"}


def _flight(fid: str, start: str, end: str, aircraft: str) -> dict:
    return {"flight_id": fid, "start_time": start, "end_time": end, "aircraft_name": aircraft}


def test_model_key() -> None:
    # Camera metadata vs. flight record.
    assert model_key("DJI NEO2") == model_key("DJI Neo 2")
    assert model_key("DJI Avata360") == model_key("DJI Avata 360")
    assert model_key(None) == ""


def test_match_recordings_by_aircraft() -> None:
    """Two drones in the air at once: each shot goes to the flight of its own aircraft."""
    flights = [
        _flight("neo", "2026-09-26T14:14:00+00:00", "2026-09-26T15:25:00+00:00", "DJI Neo 2"),
        _flight("avata", "2026-09-26T14:26:00+00:00", "2026-09-26T14:40:00+00:00", "DJI Avata 360"),
    ]

    def rec(rid: str, model: str | None) -> dict:
        # Fully inside both flights: by time alone a tie, which the earlier (Neo) flight wins.
        return {"id": rid, "start": "2026-09-26T14:30:00+00:00", "duration_s": 60, "model": model}

    assert match_recordings(flights, [rec("osv", "DJI Avata360"), rec("mp4", "DJI NEO2")]) == {
        "avata": ["osv"],
        "neo": ["mp4"],
    }
    # No model (photo, old item) or one no flight carries (renamed in DJI Fly): time alone decides.
    assert match_recordings(flights, [rec("a", None), rec("b", "DJI Mini 4 Pro")]) == {"neo": ["a", "b"]}
    # Known model, but its flight is not in the air then: the shot stays unassigned.
    late = {"id": "late", "start": "2026-09-26T15:00:00+00:00", "duration_s": 60, "model": "DJI Avata360"}
    assert match_recordings(flights, [late]) == {}


def test_build_recordings_model() -> None:
    items = {
        "o": {"id": "o", "name": "DJI_20260926162655_0025_D.OSV", "model": None},
        "p": {"id": "p", "name": "DJI_20260926162655_0025_D.LRF", "model": "DJI Avata360"},
        "j": {"id": "j", "name": "DJI_20260926162700_0026_D.JPG"},
    }
    recs = build_recordings(items, dt_util.UTC)
    assert recs["o"]["model"] == "DJI Avata360"
    assert recs["j"]["model"] is None


class _RangeClient(OneDriveClient):
    """Serves a byte string through the range reader, counting requests."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.requests = 0

    async def async_download_url(self, item_id: str) -> str | None:
        return "https://download.example/x"

    async def async_range(self, url: str, start: int, length: int) -> bytes:
        self.requests += 1
        return self.data[start : start + length]


async def test_onedrive_mp4_model() -> None:
    for iso_meta in (True, False):
        # 300 KiB of video data before the moov at the end.
        data = _mp4(10, model="DJI Avata360", iso_meta=iso_meta, mdat_bytes=300 * 1024)
        client = _RangeClient(data)
        assert await client.async_mp4_model("id", len(data)) == "DJI Avata360"
        assert client.requests <= 3  # header block, then the block(s) around moov
    data = _mp4(10)
    assert await _RangeClient(data).async_mp4_model("id", len(data)) is None


async def test_onedrive_reads_models_once() -> None:
    data = _mp4(10, model="DJI NEO2")
    client = _RangeClient(data)
    media = OneDriveMedia(client, "Drohne")
    media._folder_id = "root"
    raw = [
        {"id": "v", "name": "DJI_20260926161442_0063_D.MP4", "size": len(data), "file": {}},
        {"id": "s", "name": "DJI_20260926161442_0063_D.SRT", "size": 10, "file": {}},
    ]
    items = await media._async_read_models(media._apply_delta({}, raw, {}))
    assert items["v"]["model"] == "DJI NEO2"
    assert "model" not in items["s"]
    # An unchanged file keeps the model through a delta (and a full resync).
    requests = client.requests
    items = await media._async_read_models(media._apply_delta({}, raw, items))
    assert items["v"]["model"] == "DJI NEO2"
    assert client.requests == requests
