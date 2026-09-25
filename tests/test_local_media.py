"""Recordings from a local folder (or network storage mounted by Home Assistant)."""

from __future__ import annotations

import os
import shutil
import struct
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.dji_flightlog import local_media
from custom_components.dji_flightlog.const import (
    CONF_API_KEY,
    CONF_ENTRY_TYPE,
    CONF_IMPORT_LOGS,
    CONF_LOG_DIR,
    CONF_MATCH_TOLERANCE,
    CONF_MEDIA_FOLDER,
    CONF_MEDIA_SCAN_INTERVAL,
    DOMAIN,
    ENTRY_TYPE_LOCAL,
    EVENT_FLIGHT_IMPORTED,
)
from custom_components.dji_flightlog.local_media import item_id, mp4_duration_ms, scan_folder
from custom_components.dji_flightlog.media_backend import MediaError, MediaNotFound

from .test_integration import _fake_parse, _write_logs


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _mp4(duration_s: float, *, version: int = 0, large_mdat: bool = False) -> bytes:
    """ftyp, mdat, then moov at the end (no faststart), like the DJI files."""
    timescale = 90000
    duration = round(duration_s * timescale)
    if version == 1:
        mvhd = bytes([1, 0, 0, 0]) + struct.pack(">QQIQ", 0, 0, timescale, duration)
    else:
        mvhd = bytes([0, 0, 0, 0]) + struct.pack(">IIII", 0, 0, timescale, duration)
    mvhd += b"\0" * 80
    data = b"\x42" * 200
    mdat = struct.pack(">I4sQ", 1, b"mdat", 16 + len(data)) + data if large_mdat else _box(b"mdat", data)
    return _box(b"ftyp", b"isom\0\0\0\0isom") + mdat + _box(b"moov", _box(b"mvhd", mvhd))


def test_mp4_duration(tmp_path: Path) -> None:
    for i, (version, large) in enumerate([(0, False), (1, False), (0, True)]):
        path = tmp_path / f"v{i}.mp4"
        path.write_bytes(_mp4(84.5, version=version, large_mdat=large))
        assert mp4_duration_ms(path) == 84500
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\0\0\0\x08ftyp" + b"\xff" * 20)
    assert mp4_duration_ms(broken) is None
    assert mp4_duration_ms(tmp_path / "missing.mp4") is None


def _media_tree(root: Path) -> dict[str, Path]:
    day = root / "2026" / "2026-09-21"
    day.mkdir(parents=True)
    files = {
        "video": day / "DJI_20260921190306_0001_D.MP4",
        "proxy": day / "DJI_20260921190306_0001_D.LRF",
        "photo": day / "DJI_20260921191000_0002_D.JPG",
    }
    files["video"].write_bytes(_mp4(84))
    files["proxy"].write_bytes(_mp4(84) + b"proxy")
    files["photo"].write_bytes(b"\xff\xd8\xff\xe0photo")
    # NAS housekeeping and non-media files are ignored.
    (root / "@eaDir").mkdir()
    (root / "@eaDir" / "SYNOFILE_THUMB_M.jpg").write_bytes(b"x")
    (day / "notes.txt").write_text("x")
    return files


def test_scan_folder(tmp_path: Path) -> None:
    root = tmp_path / "media"
    files = _media_tree(root)
    items = scan_folder(root, {})
    assert sorted(i["name"] for i in items.values()) == sorted(p.name for p in files.values())
    video = items[item_id(root, "2026/2026-09-21/DJI_20260921190306_0001_D.MP4")]
    assert video["duration_ms"] == 84000
    assert video["folder"] == "2026/2026-09-21"
    assert video["path"] == "2026/2026-09-21/DJI_20260921190306_0001_D.MP4"

    # Unchanged files keep what was read before (the header is not read again).
    video["duration_ms"] = 1
    assert scan_folder(root, items)[video["id"]]["duration_ms"] == 1
    # A changed file is read again.
    files["video"].write_bytes(_mp4(90))
    assert scan_folder(root, items)[video["id"]]["duration_ms"] == 90000

    with pytest.raises(MediaNotFound):
        scan_folder(tmp_path / "missing", {})
    empty = tmp_path / "unmounted"
    empty.mkdir()
    assert scan_folder(empty, {}) == {}
    with pytest.raises(MediaError, match="mounted"):
        scan_folder(empty, items)


async def test_local_folder_flow_and_playback(
    hass: HomeAssistant, tmp_path: Path, hass_client, monkeypatch
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={CONF_LOG_DIR: str(logs)}).add_to_hass(hass)
    media = tmp_path / "media"
    files = _media_tree(media)

    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.MENU
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "local"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "local"

    result = await flow.async_configure(result["flow_id"], {CONF_MEDIA_FOLDER: "media"})
    assert result["errors"] == {CONF_MEDIA_FOLDER: "dir_not_absolute"}
    result = await flow.async_configure(result["flow_id"], {CONF_MEDIA_FOLDER: str(tmp_path / "nope")})
    assert result["errors"] == {CONF_MEDIA_FOLDER: "dir_not_found"}
    result = await flow.async_configure(result["flow_id"], {CONF_MEDIA_FOLDER: f"{media}{os.sep}"})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    entry = result["result"]
    assert entry.title == str(media)
    assert entry.options[CONF_MEDIA_FOLDER] == str(media)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    coordinator = hass.data[DOMAIN][entry.entry_id]
    recs = coordinator.data.recordings
    assert len(recs) == 2
    video = next(r for r in recs.values() if r["kind"] == "video")
    photo = next(r for r in recs.values() if r["kind"] == "photo")
    assert video["duration_s"] == 84.0
    assert video["proxy"]["name"].endswith(".LRF")

    registry = er.async_get(hass)
    sensor_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_local_recordings")
    assert hass.states.get(sensor_id).state == "2"
    assert hass.states.get(sensor_id).attributes["folder"] == str(media)
    assert registry.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_local_sync")

    # The same folder cannot be added twice.
    result = await flow.async_init(DOMAIN, context={"source": "user"})
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "local"})
    result = await flow.async_configure(result["flow_id"], {CONF_MEDIA_FOLDER: str(media)})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    # Playback streams the proxy through HA, with Range for seeking.
    http = await hass_client()
    base = f"/api/{DOMAIN}/media/{video['id']}"
    resp = await http.get(f"{base}/play")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "video/mp4"
    assert await resp.read() == files["proxy"].read_bytes()
    resp = await http.get(f"{base}/play", headers={"Range": "bytes=0-9"})
    assert resp.status == 206
    assert len(await resp.read()) == 10
    resp = await http.get(f"{base}/original")
    assert resp.status == 200
    assert resp.headers["Content-Disposition"] == 'attachment; filename="DJI_20260921190306_0001_D.MP4"'
    assert await resp.read() == files["video"].read_bytes()
    resp = await http.get(f"/api/{DOMAIN}/media/{photo['id']}/play")
    assert resp.headers["Content-Type"] == "image/jpeg"

    # Thumbnails come from ffmpeg (the proxy here) and are cached.
    calls: list[tuple[Path, float | None]] = []

    async def fake_ffmpeg(binary: str, path: Path, *, seek_s: float | None) -> bytes:
        calls.append((path, seek_s))
        return b"JPEG"

    monkeypatch.setattr(local_media, "ffmpeg_thumbnail", fake_ffmpeg)
    for _ in range(2):
        resp = await http.get(f"{base}/thumb")
        assert resp.status == 200
        assert await resp.read() == b"JPEG"
    assert calls == [(files["proxy"], 1.0)]

    # The flight list links the recording and offers the original as download.
    flightlog = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, DOMAIN)
    flights = hass.data[DOMAIN][flightlog.entry_id]
    flights.data.flights = {"f1": {"flight_id": "f1", "start_time": video["start"], "duration_s": 300}}
    flights.async_update_listeners()
    body = await (await http.get(f"/api/{DOMAIN}/flights")).json()
    (media_json,) = [m for m in body["flights"][0]["media"] if m["id"] == video["id"]]
    assert media_json["web_url"] is None
    assert media_json["play"].startswith(f"{base}/play?authSig=")
    assert media_json["projection"] is None
    assert media_json["download"].startswith(f"{base}/original?authSig=")
    assert body["media"]["accounts"][0]["source"] == "local"

    # An unmounted share shows up as an empty folder: the sync fails instead
    # of dropping every recording.
    for path in files.values():
        path.unlink()
    for path in sorted(media.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert len(coordinator.data.recordings) == 2

    # Options: another folder.
    other = tmp_path / "other"
    other.mkdir()
    options = hass.config_entries.options
    result = await options.async_init(entry.entry_id)
    assert result["step_id"] == "local"
    result = await options.async_configure(
        result["flow_id"],
        {CONF_MEDIA_FOLDER: str(other), CONF_MEDIA_SCAN_INTERVAL: 600, CONF_MATCH_TOLERANCE: 60},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert entry.options == {
        CONF_MEDIA_FOLDER: str(other),
        CONF_MEDIA_SCAN_INTERVAL: 600,
        CONF_MATCH_TOLERANCE: 60,
        CONF_IMPORT_LOGS: False,
    }
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][entry.entry_id].data.recordings == {}


async def test_flight_records_are_copied_into_the_log_folder(hass: HomeAssistant, tmp_path: Path) -> None:
    """A second pilot's logs in the media folder end up in the (authoritative) log folder."""
    hass.config.config_dir = str(tmp_path / "config")
    logs = tmp_path / "logs"
    logs.mkdir()
    (mine,) = _write_logs(logs, 1)  # DJIFlightRecord_2026-09-01_0.txt
    media = tmp_path / "media"
    (media / "papa").mkdir(parents=True)
    shutil.copy2(mine, media / mine.name)  # the same file again: no second copy
    theirs = logs.parent / "staging"
    theirs.mkdir()
    new_old, new_fresh = _write_logs(theirs, 3)[1:]  # _1 (old) and _2 (made old below, then fresh)
    shutil.copy2(new_old, media / "papa" / new_old.name)
    fresh = media / "papa" / new_fresh.name
    shutil.copy2(new_fresh, fresh)
    now = time.time()
    os.utime(fresh, (now, now))  # still being copied as far as the import can tell
    (media / "papa" / "notes.txt").write_text("not a flight record")

    events = async_capture_events(hass, EVENT_FLIGHT_IMPORTED)
    flightlog = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={CONF_LOG_DIR: str(logs), CONF_API_KEY: "KEY"}
    )
    flightlog.add_to_hass(hass)
    source = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"local_{media}",
        title=str(media),
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_LOCAL},
        options={CONF_MEDIA_FOLDER: str(media), CONF_IMPORT_LOGS: True},
    )
    source.add_to_hass(hass)
    with patch("custom_components.dji_flightlog.coordinator.parse_flight", side_effect=_fake_parse):
        # Sets up both entries of the domain.
        assert await hass.config_entries.async_setup(flightlog.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

        assert sorted(p.name for p in logs.iterdir()) == [mine.name, new_old.name]
        flights = hass.data[DOMAIN][flightlog.entry_id]
        assert set(flights.data.flights) == {"flight0000", "flight0001"}
        # The flight log's own first scan, then the copied record (the duplicate adds nothing).
        assert sorted(e.data["flight_id"] for e in events) == ["flight0000", "flight0001"]
        media_coordinator = hass.data[DOMAIN][source.entry_id]
        assert len(media_coordinator.imported_logs) == 2
        # Flight records are not recordings.
        assert media_coordinator.data.recordings == {}

        # Once the fresh file has settled, the next sync takes it.
        past = now - 300
        os.utime(fresh, (past, past))
        await media_coordinator.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert new_fresh.name in {p.name for p in logs.iterdir()}
        assert "flight0002" in flights.data.flights

        # Nothing is copied twice, and the source is left alone.
        before = sorted(p.name for p in logs.iterdir())
        await media_coordinator.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert sorted(p.name for p in logs.iterdir()) == before
        assert len(events) == 3
    assert sorted(p.name for p in (media / "papa").iterdir()) == sorted(
        [new_old.name, new_fresh.name, "notes.txt"]
    )

    registry = er.async_get(hass)
    sensor_id = registry.async_get_entity_id("sensor", DOMAIN, f"{source.entry_id}_local_last_sync")
    assert hass.states.get(sensor_id).attributes["flight_records_imported"] == 3
