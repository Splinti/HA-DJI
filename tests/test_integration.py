"""End-to-end tests against the HA test harness (parser mocked)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.dji_flightlog.const import (
    CONF_API_KEY,
    CONF_LOG_DIR,
    CONF_SCAN_INTERVAL,
    DOMAIN,
    EVENT_FLIGHT_IMPORTED,
    STATUS_OK,
)
from custom_components.dji_flightlog.parser import summarize_frames

from .conftest import make_frames
from .test_parser import BASE


def _fake_parse(path: Path, *, api_key, max_track_points, now=None):
    """Replacement for parser.parse_flight driven by the file name."""
    idx = int(path.stem.split("_")[-1])
    start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC) + timedelta(days=idx)
    base = dict(BASE)
    base.update(
        flight_id=f"flight{idx:04d}",
        file=str(path),
        filename=path.name,
        aircraft_sn="SN-NEO" if idx % 2 == 0 else "SN-AVATA",
        aircraft_name="Neo" if idx % 2 == 0 else "Avata 2",
        product_type="NEO" if idx % 2 == 0 else "AVATA_2",
        imported_at=datetime.now(UTC).isoformat(),
    )
    return summarize_frames(make_frames(50 + idx, start=start), base, max_track_points)


def _write_logs(log_dir: Path, n: int, *, old: bool = True) -> list[Path]:
    paths = []
    for i in range(n):
        p = log_dir / f"DJIFlightRecord_2026-09-0{i + 1}_{i}.txt"
        p.write_bytes(b"\x01" * (100 + i))
        if old:
            # The coordinator ignores files modified in the last 30 s.
            import os

            past = (datetime.now(UTC) - timedelta(minutes=5)).timestamp()
            os.utime(p, (past, past))
        paths.append(p)
    return paths


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "flightrecords"
    d.mkdir()
    return d


@pytest.fixture
async def setup_entry(hass: HomeAssistant, log_dir: Path, tmp_path: Path):
    # Keep track files out of the harness' shared testing_config directory.
    hass.config.config_dir = str(tmp_path / "config")

    async def _setup(n_files: int = 2, **extra: object) -> MockConfigEntry:
        _write_logs(log_dir, n_files)
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOG_DIR: str(log_dir),
                CONF_API_KEY: "KEY",
                CONF_SCAN_INTERVAL: 60,
                **extra,
            },
            unique_id=DOMAIN,
        )
        entry.add_to_hass(hass)
        with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=_fake_parse):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        return entry

    return _setup


async def test_sensors_and_devices(hass: HomeAssistant, setup_entry):
    await setup_entry(2)

    assert hass.states.get("sensor.dji_flight_log_flights").state == "2"
    # 2 flights: 49 s + 50 s => 99 s, shown in hours (suggested unit)
    ft = hass.states.get("sensor.dji_flight_log_flight_time")
    assert ft is not None and float(ft.state) == pytest.approx(99 / 3600, rel=1e-3)
    assert hass.states.get("sensor.dji_flight_log_pending_files").state == "0"
    assert hass.states.get("sensor.dji_flight_log_aircraft").state == "2"

    last = hass.states.get("sensor.dji_flight_log_last_flight")
    assert last.state == "2026-09-02T10:00:00+00:00"
    assert last.attributes["flight_id"] == "flight0001"
    assert last.attributes["aircraft_name"] == "Avata 2"
    # No "latitude"/"longitude" here: that would put the sensor on HA's map.
    assert last.attributes["takeoff_lat"] == pytest.approx(48.1)
    assert "latitude" not in last.attributes and "longitude" not in last.attributes

    # per-aircraft devices
    assert hass.states.get("sensor.neo_flights").state == "1"
    assert hass.states.get("sensor.avata_2_flights").state == "1"
    # native seconds, displayed in minutes (suggested unit)
    assert float(hass.states.get("sensor.avata_2_last_flight_duration").state) == pytest.approx(50 / 60)
    assert hass.states.get("sensor.neo_last_flight_battery_used").state == str(49 // 4)


async def test_geo_location_entities(hass: HomeAssistant, setup_entry):
    from custom_components.dji_flightlog.const import CONF_GEO_LOCATION_LIMIT

    await setup_entry(2, **{CONF_GEO_LOCATION_LIMIT: 10})
    states = [s for s in hass.states.async_all("geo_location") if s.attributes.get("source") == DOMAIN]
    assert len(states) == 2
    s = next(x for x in states if x.attributes["flight_id"] == "flight0000")
    assert s.attributes["latitude"] == pytest.approx(48.1)
    assert s.attributes["longitude"] == pytest.approx(11.5)
    assert s.attributes["aircraft_name"] == "Neo"
    assert float(s.state) == pytest.approx(490.0)  # distance flown


async def test_geo_location_off_by_default(hass: HomeAssistant, setup_entry):
    """No geo_location entities unless asked for: they would drag the flights
    onto Home Assistant's auto-generated Overview map."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.dji_flightlog.const import CONF_GEO_LOCATION_LIMIT

    entry = await setup_entry(2)
    assert [s for s in hass.states.async_all("geo_location") if s.attributes.get("source") == DOMAIN] == []

    # Turning it on creates them ...
    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=_fake_parse):
        hass.config_entries.async_update_entry(entry, options={**entry.data, CONF_GEO_LOCATION_LIMIT: 10})
        await hass.async_block_till_done()
    assert len(hass.states.async_all("geo_location")) == 2

    # ... and turning it off again leaves nothing behind, registry included.
    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=_fake_parse):
        hass.config_entries.async_update_entry(entry, options={**entry.data, CONF_GEO_LOCATION_LIMIT: 0})
        await hass.async_block_till_done()
    assert hass.states.async_all("geo_location") == []
    registry = er.async_get(hass)
    assert [
        e for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.domain == "geo_location"
    ] == []


async def test_event_and_rescan(hass: HomeAssistant, setup_entry, log_dir: Path):
    events = []
    hass.bus.async_listen(EVENT_FLIGHT_IMPORTED, lambda e: events.append(e))
    await setup_entry(1)
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["flight_id"] == "flight0000"
    assert events[0].data["status"] == STATUS_OK

    # Drop a new file and let the interval fire.
    p = log_dir / "DJIFlightRecord_2026-09-05_4.txt"
    p.write_bytes(b"\x02" * 500)
    import os

    past = (datetime.now(UTC) - timedelta(minutes=5)).timestamp()
    os.utime(p, (past, past))

    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=_fake_parse):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
        await hass.async_block_till_done(wait_background_tasks=True)

    assert len(events) == 2
    assert hass.states.get("sensor.dji_flight_log_flights").state == "2"

    # A second scan must not re-import or re-fire.
    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=_fake_parse) as m:
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=122))
        await hass.async_block_till_done(wait_background_tasks=True)
        assert m.call_count == 0
    assert len(events) == 2


async def test_unsupported_file_is_reported(hass: HomeAssistant, setup_entry, log_dir: Path):
    """A DJI Assistant support bundle must be named as such, not silently ignored."""
    import os

    from .test_parser import BUNDLE_HEAD

    await setup_entry(1)
    bundle = log_dir / "DJI_Avata_360_2026-09-22_12-34-44.DAT"
    bundle.write_bytes(BUNDLE_HEAD + bytes(10000))
    past = (datetime.now(UTC) - timedelta(minutes=5)).timestamp()
    os.utime(bundle, (past, past))

    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=_fake_parse) as m:
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.dji_flight_log_scan_log_folder"}, blocking=True
        )
        await hass.async_block_till_done()
        m.assert_not_called()  # never read a 60 MB bundle into memory

    state = hass.states.get("sensor.dji_flight_log_unsupported_files")
    assert state.state == "1"
    assert state.attributes["files"] == [{"file": bundle.name, "reason": "support_bundle"}]
    # and it did not become a flight
    assert hass.states.get("sensor.dji_flight_log_flights").state == "1"


async def test_scan_button(hass: HomeAssistant, setup_entry, log_dir: Path):
    await setup_entry(1)
    assert hass.states.get("sensor.dji_flight_log_flights").state == "1"
    _write_logs(log_dir, 2)  # adds file index 1
    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=_fake_parse):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.dji_flight_log_scan_log_folder"}, blocking=True
        )
        await hass.async_block_till_done()
    assert hass.states.get("sensor.dji_flight_log_flights").state == "2"


async def test_services(hass: HomeAssistant, setup_entry, tmp_path: Path):
    await setup_entry(1)
    hass.config.allowlist_external_dirs = {str(tmp_path)}

    out_dir = tmp_path / "exports"
    res = await hass.services.async_call(
        DOMAIN,
        "export_track",
        {"flight_id": "last", "format": "gpx", "path": str(out_dir) + "/"},
        blocking=True,
        return_response=True,
    )
    written = Path(res["path"])
    assert written.parent == out_dir
    assert written.suffix == ".gpx"
    assert written.read_text(encoding="utf-8").count("<trkpt") == 50

    res = await hass.services.async_call(
        DOMAIN,
        "export_track",
        {"flight_id": "flight0000", "format": "geojson"},
        blocking=True,
        return_response=True,
    )
    gj = json.loads(res["content"])
    assert gj["features"][0]["geometry"]["type"] == "LineString"

    with pytest.raises(Exception, match="allowlist_external_dirs"):
        await hass.services.async_call(
            DOMAIN,
            "export_track",
            {"flight_id": "last", "path": "/etc/nope.gpx"},
            blocking=True,
            return_response=True,
        )


async def test_http_api(hass: HomeAssistant, setup_entry, hass_client):
    await setup_entry(3)
    client = await hass_client()

    resp = await client.get(f"/api/{DOMAIN}/flights")
    assert resp.status == 200
    body = await resp.json()
    assert [f["flight_id"] for f in body["flights"]] == ["flight0002", "flight0001", "flight0000"]
    assert body["totals"]["flights"] == 3
    assert set(body["aircraft"]) == {"SN-NEO", "SN-AVATA"}

    resp = await client.get(f"/api/{DOMAIN}/flights?aircraft=neo")
    body = await resp.json()
    assert [f["flight_id"] for f in body["flights"]] == ["flight0002", "flight0000"]

    resp = await client.get(f"/api/{DOMAIN}/flights?since=2026-09-03T00:00:00&limit=1")
    body = await resp.json()
    assert [f["flight_id"] for f in body["flights"]] == ["flight0002"]

    resp = await client.get(f"/api/{DOMAIN}/tracks?max_points=10")
    body = await resp.json()
    assert len(body["tracks"]) == 3
    assert all(len(t["points"]) == 11 for t in body["tracks"])

    resp = await client.get(f"/api/{DOMAIN}/flights/flight0001/track")
    body = await resp.json()
    assert body["flight"]["aircraft_name"] == "Avata 2"
    assert len(body["points"]) == 51

    resp = await client.get(f"/api/{DOMAIN}/flights/flight0001/export/kml")
    assert resp.status == 200
    assert "attachment" in resp.headers["Content-Disposition"]
    assert "<kml" in await resp.text()

    resp = await client.get(f"/api/{DOMAIN}/flights/nope/track")
    assert resp.status == 404

    resp = await client.get(f"/{DOMAIN}_static/dji-flight-map-card.js")
    assert resp.status == 200
    assert "dji-flight-map-card" in await resp.text()


async def test_http_requires_auth(hass: HomeAssistant, setup_entry, hass_client_no_auth):
    await setup_entry(1)
    client = await hass_client_no_auth()
    resp = await client.get(f"/api/{DOMAIN}/flights")
    assert resp.status == 401


async def test_missing_dir_does_not_break_setup(hass: HomeAssistant, tmp_path: Path):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOG_DIR: str(tmp_path / "missing")}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dji_flight_log_flights").state == "0"
    li = hass.states.get("sensor.dji_flight_log_last_import")
    assert li.attributes["log_dir_ok"] is False


async def test_config_flow(hass: HomeAssistant, log_dir: Path):
    assert await async_setup_component(hass, DOMAIN, {})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOG_DIR: str(log_dir / "nope"), CONF_API_KEY: ""}
    )
    assert result["errors"] == {CONF_LOG_DIR: "dir_not_found"}

    with patch("custom_components.dji_flightlog.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOG_DIR: str(log_dir), CONF_API_KEY: " abc "}
        )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_LOG_DIR] == str(log_dir)
    assert result["data"][CONF_API_KEY] == "abc"


async def test_lovelace_resource_registered(hass: HomeAssistant, setup_entry):
    from homeassistant.components.lovelace.const import LOVELACE_DATA

    assert await async_setup_component(hass, "lovelace", {})
    await setup_entry(1)
    resources = hass.data[LOVELACE_DATA].resources
    urls = [r["url"] for r in resources.async_items()]
    assert any(u.startswith("/dji_flightlog_static/dji-flight-map-card.js") for u in urls)

    # Reloading the entry must not add a duplicate.
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=_fake_parse):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    urls = [r["url"] for r in resources.async_items()]
    assert sum(u.startswith("/dji_flightlog_static/") for u in urls) == 1


async def test_api_key_added_later_backfills_tracks(hass: HomeAssistant, log_dir: Path, tmp_path: Path):
    """Without a key only headers are imported; adding one must fill in the tracks."""
    from custom_components.dji_flightlog.const import STATUS_HEADER_ONLY

    hass.config.config_dir = str(tmp_path / "config")

    def parse(path, *, api_key, max_track_points, now=None):
        summary, track = _fake_parse(path, api_key=api_key, max_track_points=max_track_points)
        if not api_key:
            summary.status = STATUS_HEADER_ONLY
            summary.points = 0
            return summary, None
        return summary, track

    _write_logs(log_dir, 1)
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_LOG_DIR: str(log_dir), CONF_API_KEY: ""}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=parse):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.data.flights["flight0000"]["status"] == STATUS_HEADER_ONLY
    assert coordinator.data.flights["flight0000"]["points"] == 0
    assert await coordinator.store.async_read_track("flight0000") is None

    # Entering the key in the options flow reloads the entry and re-reads the file.
    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=parse):
        hass.config_entries.async_update_entry(
            entry, options={CONF_LOG_DIR: str(log_dir), CONF_API_KEY: "KEY"}
        )
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    flight = coordinator.data.flights["flight0000"]
    assert flight["status"] == "ok"
    assert flight["points"] == 50
    track = await coordinator.store.async_read_track("flight0000")
    assert track is not None and len(track["points"]) == 50


async def test_sidebar_panel(hass: HomeAssistant, setup_entry, hass_client):
    """The panel is registered, served, and removed again when switched off."""
    from homeassistant.components.frontend import DATA_PANELS

    from custom_components.dji_flightlog.const import (
        CONF_SIDEBAR_PANEL,
        PANEL_ELEMENT,
        PANEL_TITLE,
        PANEL_URL_PATH,
    )

    entry = await setup_entry(1)
    panel = hass.data[DATA_PANELS][PANEL_URL_PATH]
    assert panel.component_name == "custom"
    assert panel.sidebar_title == PANEL_TITLE
    custom = panel.config["_panel_custom"]
    assert custom["name"] == PANEL_ELEMENT
    assert custom["module_url"].startswith("/dji_flightlog_static/dji-flightlog-panel.js?v=")
    assert custom["embed_iframe"] is False

    # The module it points at is actually served.
    client = await hass_client()
    resp = await client.get(custom["module_url"])
    assert resp.status == 200
    body = await resp.text()
    assert "dji-flightlog-panel" in body

    # Reloading must not raise "panel already registered".
    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=_fake_parse):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert PANEL_URL_PATH in hass.data[DATA_PANELS]

    # Turning the option off removes the sidebar entry.
    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=_fake_parse):
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.data, CONF_SIDEBAR_PANEL: False},
        )
        await hass.async_block_till_done()
    assert PANEL_URL_PATH not in hass.data[DATA_PANELS]
