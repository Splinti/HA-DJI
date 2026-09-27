"""Weather at flight time from Open-Meteo."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.dji_flightlog.const import CONF_WEATHER, DOMAIN
from custom_components.dji_flightlog.weather import (
    ARCHIVE_URL,
    HOURLY,
    flight_moment,
    flight_position,
    pick_hour,
)

from .test_integration import _fake_parse, log_dir, setup_entry  # noqa: F401


def _hourly(day: str, **overrides: list) -> dict:
    hours = [f"{day}T{h:02d}:00" for h in range(24)]
    data = {"time": hours}
    for i, var in enumerate(HOURLY):
        data[var] = overrides.get(var, [float(i * 100 + h) for h in range(24)])
    return data


def test_flight_position_and_moment():
    f = {"takeoff_lat": 48.12345, "takeoff_lon": 11.56789, "start_time": "2026-09-20T13:50:00+00:00"}
    assert flight_position(f) == (48.12, 11.57)
    # No fix (0/0) at takeoff: the home point.
    no_fix = {"takeoff_lat": 0.0, "takeoff_lon": 0.0, "home_lat": 1.234, "home_lon": 2.345}
    assert flight_position(no_fix) == (1.23, 2.35)
    assert flight_position({"takeoff_lat": None, "takeoff_lon": None}) is None
    assert flight_moment({**f, "duration_s": 1200}) == datetime(2026, 9, 20, 14, 0, tzinfo=UTC)


def test_pick_hour():
    hourly = _hourly("2026-09-20")
    rec = pick_hour(hourly, datetime(2026, 9, 20, 14, 20, tzinfo=UTC))
    assert rec["time"] == "2026-09-20T14:00:00+00:00"
    assert rec["wind_ms"] == 14.0  # first variable, hour 14
    assert rec["code"] == (len(HOURLY) - 1) * 100 + 14
    assert pick_hour(hourly, datetime(2026, 9, 20, 14, 40, tzinfo=UTC))["time"].startswith("2026-09-20T15")
    # Hours without values (not in the archive yet) count as missing.
    empty = _hourly("2026-09-20", wind_speed_10m=[None] * 24, temperature_2m=[None] * 24)
    assert pick_hour(empty, datetime(2026, 9, 20, 14, tzinfo=UTC)) is None
    assert pick_hour({"time": []}, datetime(2026, 9, 20, 14, tzinfo=UTC)) is None


async def test_weather_is_opt_in(hass: HomeAssistant, setup_entry, aioclient_mock):  # noqa: F811
    entry = await setup_entry(2)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert aioclient_mock.call_count == 0
    assert all(f["weather"] is None for f in coordinator.data.flights.values())


async def test_weather_lookup(hass: HomeAssistant, setup_entry, aioclient_mock, log_dir: Path):  # noqa: F811
    # _fake_parse: flight n starts 2026-09-(n+1) 10:00 UTC at 48.1/11.5.
    aioclient_mock.get(ARCHIVE_URL, json={"hourly": _hourly("2026-09-02")})
    entry = await setup_entry(2, **{CONF_WEATHER: True})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # One request per place and day; rounded position, UTC, m/s.
    assert aioclient_mock.call_count == 2
    params = [dict(call[1].query) for call in aioclient_mock.mock_calls]
    assert {p["start_date"] for p in params} == {"2026-09-01", "2026-09-02"}
    assert {(p["latitude"], p["longitude"], p["wind_speed_unit"], p["timezone"]) for p in params} == {
        ("48.10", "11.50", "ms", "GMT")
    }

    # The mock answers every day with 2026-09-02: only flight0001 finds its hour.
    w = coordinator.data.flights["flight0001"]["weather"]
    assert w["time"] == "2026-09-02T10:00:00+00:00"
    assert w["wind_ms"] == 10.0
    assert w["source"] == "open-meteo"
    assert coordinator.data.flights["flight0000"]["weather"] is None
    assert "flight0000" in coordinator._weather_retry

    # Stored apart from the summary: a re-parse keeps it, the next scan does not ask again.
    path = next(p for p in log_dir.iterdir() if p.stem.endswith("_1"))
    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=_fake_parse):
        await coordinator.async_import_file(path)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.data.flights["flight0001"]["weather"]["wind_ms"] == 10.0
    assert aioclient_mock.call_count == 2

    await coordinator.async_remove_flight("flight0001")
    assert coordinator.store.flight_weather == {}


async def test_weather_lookup_failure_is_retried(
    hass: HomeAssistant,
    setup_entry,  # noqa: F811
    aioclient_mock,
):
    aioclient_mock.get(ARCHIVE_URL, status=502)
    entry = await setup_entry(2, **{CONF_WEATHER: True})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    # The first failure stops the run; nothing is marked, so the next scan tries again.
    assert aioclient_mock.call_count == 1
    assert coordinator._weather_retry == {}

    aioclient_mock.clear_requests()
    aioclient_mock.get(ARCHIVE_URL, json={"hourly": _hourly("2026-09-02")})
    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=_fake_parse):
        await coordinator.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.data.flights["flight0001"]["weather"] is not None
