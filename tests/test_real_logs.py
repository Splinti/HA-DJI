"""End-to-end run against real DJI logs in ./flightrecords (skipped if absent).

The folder is gitignored: drop your own DJIFlightRecord/FlightRecord *.txt
there and run `pytest tests/test_real_logs.py -s` to see what HA makes of them.
Set DJI_API_KEY to also decode the GPS tracks.

``test_real_logs_decoded`` checks the parser against the full telemetry. It
needs no API key: it uses the keychains that ``scripts/decode_flightrecords.py``
saved in ``flightrecords/decoded``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dji_flightlog.const import (
    CONF_API_KEY,
    CONF_GEO_LOCATION_LIMIT,
    CONF_LOG_DIR,
    DOMAIN,
    STATUS_OK,
)
from custom_components.dji_flightlog.parser import parse_flight, track_to_gpx

REAL_DIR = Path(__file__).parent.parent / "flightrecords"
pytestmark = pytest.mark.skipif(
    not REAL_DIR.is_dir() or not any(REAL_DIR.glob("*.txt")),
    reason="no real logs in ./flightrecords",
)


async def test_real_logs(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path / "config")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOG_DIR: str(REAL_DIR),
            CONF_API_KEY: os.environ.get("DJI_API_KEY", ""),
            CONF_GEO_LOCATION_LIMIT: 500,  # opt in, so the entities are exercised too
        },
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data
    files = sorted(REAL_DIR.glob("*.txt"))

    print(f"\n--- {len(files)} txt file(s) in {REAL_DIR} ---")
    for f in sorted(data.flights.values(), key=lambda x: x["start_time"]):
        print(
            f"  {f['start_time'][:19]}  {f['duration_s']:7.1f}s  {f['distance_m']:9.1f}m  "
            f"max {f['max_height_m']:5.1f}m  {f['aircraft_name']:15}  status={f['status']:12}"
            f" pts={f['points']:5}  {f['city']}"
        )
    t = data.totals
    print(
        f"  TOTAL: {t.flights} flights, {t.total_time_s / 60:.1f} min, "
        f"{t.total_distance_m / 1000:.2f} km, max {t.max_height_m} m"
    )
    print(f"  unsupported: {data.unsupported}")

    for name in ("flights", "flight_time", "distance", "max_height", "last_flight"):
        state = hass.states.get(f"sensor.dji_flight_log_{name}")
        print(f"  sensor.dji_flight_log_{name} = {state.state}")

    geo = [s for s in hass.states.async_all("geo_location") if s.attributes.get("source") == DOMAIN]
    print(f"  geo_location entities: {len(geo)}")

    # Every flight record must have been imported, none left unsupported/pending.
    assert data.totals.flights == len(files)
    assert data.unsupported == []
    assert data.pending_files == 0
    # Header data is always present, with or without an API key.
    for f in data.flights.values():
        assert f["duration_s"] > 0
        assert f["takeoff_lat"] is not None and f["takeoff_lon"] is not None
        assert f["aircraft_sn"]
    assert len(geo) == len(files)


def _saved_keychains(path: Path):
    from pydjirecord import KeychainFeaturePoint

    saved = REAL_DIR / "decoded" / f"{path.stem}.keychains.json"
    if not saved.is_file():
        return None
    return [[KeychainFeaturePoint(**fp) for fp in group] for group in json.loads(saved.read_text())]


def test_real_logs_decoded() -> None:
    from pydjirecord import DJILog

    files = [p for p in sorted(REAL_DIR.glob("*.txt")) if _saved_keychains(p) is not None]
    if not files:
        pytest.skip("no saved keychains; run scripts/decode_flightrecords.py first")

    for path in files:
        keychains = _saved_keychains(path)
        with patch.object(DJILog, "fetch_keychains", lambda self, key, kc=keychains: kc):
            summary, track = parse_flight(path, api_key="saved", max_track_points=1500)
        header = DJILog.from_bytes(path.read_bytes()).details
        print(
            f"  {path.name}: {summary.duration_s:6.1f}s (header {header.total_time:.0f}s)  "
            f"video {summary.video_time_s}s  photos {summary.photo_num}"
        )
        assert summary.status == STATUS_OK
        # fly_time keeps counting across flights on one battery; the duration must not.
        assert abs(summary.duration_s - header.total_time) < 2
        assert track.points[0][4] < 2
        assert 0 <= summary.video_time_s <= summary.duration_s
        # Exports use the height above takeoff, never the (negative) barometric altitude.
        gpx = track_to_gpx(summary.as_dict(), track.as_dict())
        assert min(float(e.split("<")[0]) for e in gpx.split("<ele>")[1:]) > -5
