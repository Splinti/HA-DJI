"""Pure helpers of media.py: flight record names and copies across sources."""

from __future__ import annotations

from homeassistant.util import dt as dt_util

from custom_components.dji_flightlog.media import build_recordings, duplicate_recordings, is_flight_record
from custom_components.dji_flightlog.onedrive import normalize_item


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
