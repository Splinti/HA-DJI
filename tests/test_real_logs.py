"""End-to-end run against real DJI logs in ./flightrecords (skipped if absent).

The folder is gitignored: drop your own DJIFlightRecord/FlightRecord *.txt
there and run `pytest tests/test_real_logs.py -s` to see what HA makes of them.
Set DJI_API_KEY to also decode the GPS tracks.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dji_flightlog.const import (
    CONF_API_KEY,
    CONF_GEO_LOCATION_LIMIT,
    CONF_LOG_DIR,
    DOMAIN,
)

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
