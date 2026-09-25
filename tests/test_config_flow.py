"""Config flow: adding the integration again connects OneDrive (folders: see test_local_media)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant.components.application_credentials import ClientCredential, async_import_client_credential
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dji_flightlog.const import (
    CONF_LOG_DIR,
    CONF_MATCH_TOLERANCE,
    CONF_MEDIA_FOLDER,
    CONF_MEDIA_SCAN_INTERVAL,
    DOMAIN,
    GRAPH_URL,
    OAUTH2_TOKEN,
)


def _flightlog_entry(hass: HomeAssistant, tmp_path) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={CONF_LOG_DIR: str(tmp_path)})
    entry.add_to_hass(hass)
    return entry


async def _start_onedrive(hass: HomeAssistant) -> dict:
    """Add the integration again and pick OneDrive in the menu."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.MENU, result
    assert result["menu_options"] == ["local", "onedrive"]
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "onedrive"})


async def test_second_add_without_credentials(hass: HomeAssistant, tmp_path) -> None:
    _flightlog_entry(hass, tmp_path)
    assert await async_setup_component(hass, "application_credentials", {})
    result = await _start_onedrive(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "missing_credentials"


@pytest.mark.usefixtures("current_request_with_host")
async def test_second_add_starts_oauth(hass: HomeAssistant, tmp_path) -> None:
    _flightlog_entry(hass, tmp_path)
    await hass.config.async_update(external_url="https://example.com")
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(hass, DOMAIN, ClientCredential("client-id", "secret"), "Azure")
    result = await _start_onedrive(hass)
    assert result["type"] is FlowResultType.EXTERNAL_STEP, result
    assert result["url"].startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize")


async def _sign_in(hass: HomeAssistant, tmp_path, hass_client_no_auth, aioclient_mock) -> dict:
    """Run the flow up to the folder picker (Microsoft and Graph mocked)."""
    _flightlog_entry(hass, tmp_path)
    await hass.config.async_update(external_url="https://example.com")
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(hass, DOMAIN, ClientCredential("client-id", "secret"), "Azure")
    result = await _start_onedrive(hass)
    state = parse_qs(urlparse(result["url"]).query)["state"][0]

    client = await hass_client_no_auth()
    resp = await client.get(f"/auth/external/callback?code=abcd&state={state}")
    assert resp.status == 200

    aioclient_mock.post(
        OAUTH2_TOKEN,
        json={"access_token": "at", "refresh_token": "rt", "token_type": "Bearer", "expires_in": 3600},
    )
    aioclient_mock.get(
        f"{GRAPH_URL}/me/drive",
        json={"id": "drive1", "owner": {"user": {"displayName": "Nico", "email": "nico@example.com"}}},
    )
    return await hass.config_entries.flow.async_configure(result["flow_id"])


def _mock_tree(aioclient_mock, *, medien: bool = True) -> None:
    """/Drohne/Medien/2026 plus a file; without ``medien`` the default folder is missing."""
    root = {"id": "root", "name": "root", "folder": {}}
    drohne = {"id": "f-drohne", "name": "Drohne", "folder": {}}
    folder = {"id": "f-medien", "name": "Medien", "folder": {}}
    aioclient_mock.get(f"{GRAPH_URL}/me/drive/root", json=root)
    aioclient_mock.get(
        f"{GRAPH_URL}/me/drive/items/root/children",
        json={"value": [{"name": "Dokumente", "folder": {}}, drohne, {"name": "a.txt", "file": {}}]},
    )
    aioclient_mock.get(f"{GRAPH_URL}/me/drive/root:/Drohne", json=drohne)
    aioclient_mock.get(
        f"{GRAPH_URL}/me/drive/items/f-drohne/children", json={"value": [folder] if medien else []}
    )
    if not medien:
        aioclient_mock.get(
            f"{GRAPH_URL}/me/drive/root:/Drohne/Medien",
            status=404,
            json={"error": {"code": "itemNotFound", "message": "Item does not exist"}},
        )
        return
    aioclient_mock.get(f"{GRAPH_URL}/me/drive/root:/Drohne/Medien", json=folder)
    aioclient_mock.get(
        f"{GRAPH_URL}/me/drive/items/f-medien/children",
        json={"value": [{"name": "2026", "folder": {}}, {"name": "DJI_0001.MP4", "file": {}}]},
    )
    aioclient_mock.get(
        f"{GRAPH_URL}/me/drive/items/f-medien/delta",
        json={
            "value": [
                folder,
                {
                    "id": "v1",
                    "name": "DJI_20260921190306_0001_D.MP4",
                    "size": 1000,
                    "webUrl": "https://onedrive.live.com/v1",
                    "file": {"mimeType": "video/mp4"},
                    "video": {"duration": 84000},
                    "parentReference": {"id": "f-medien"},
                },
            ],
            "@odata.deltaLink": f"{GRAPH_URL}/me/drive/items/f-medien/delta?token=t1",
        },
    )


def _labels(result) -> list[str]:
    assert result["type"] is FlowResultType.MENU, result
    return list(result["menu_options"].values())


def _open(result, name: str) -> str:
    """Step id of the menu entry that opens subfolder ``name``."""
    return next(step for step, label in result["menu_options"].items() if label == f"📁 {name}")


@pytest.mark.usefixtures("current_request_with_host")
async def test_onedrive_flow_picks_folder(
    hass: HomeAssistant, tmp_path, hass_client, hass_client_no_auth, aioclient_mock
) -> None:
    """Sign-in, walking the folder tree and the first sync of the new entry."""
    _mock_tree(aioclient_mock)
    result = await _sign_in(hass, tmp_path, hass_client_no_auth, aioclient_mock)
    # Starts in the default folder, which exists here.
    assert result["step_id"] == "onedrive_folder"
    assert result["description_placeholders"]["path"] == "/Drohne/Medien"
    assert _labels(result) == ['✓ Use "/Drohne/Medien"', "⬆ Up one level", "📁 2026"]
    assert result["description_placeholders"]["notice"] == ""

    configure = hass.config_entries.flow.async_configure
    result = await configure(result["flow_id"], {"next_step_id": "dir_up"})
    assert result["description_placeholders"]["path"] == "/Drohne"
    result = await configure(result["flow_id"], {"next_step_id": "dir_up"})
    assert result["description_placeholders"]["path"] == "/"
    assert _labels(result) == [
        '✓ Use "/"',
        "📁 Dokumente",
        "📁 Drohne",
    ]  # no "up" at the root, files left out
    result = await configure(result["flow_id"], {"next_step_id": _open(result, "Drohne")})
    result = await configure(result["flow_id"], {"next_step_id": _open(result, "Medien")})
    assert result["description_placeholders"]["path"] == "/Drohne/Medien"
    result = await configure(result["flow_id"], {"next_step_id": "dir_use"})

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["title"] == "OneDrive (nico@example.com)"
    entry = result["result"]
    assert entry.unique_id == "onedrive_drive1"
    assert entry.options[CONF_MEDIA_FOLDER] == "Drohne/Medien"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert len(coordinator.data.recordings) == 1

    # Sensors on the account's device. The flight log (loaded along with the
    # integration) has no flights, so nothing is matched.
    registry = er.async_get(hass)

    def state(key: str) -> State:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_onedrive_{key}")
        assert entity_id is not None, key
        return hass.states.get(entity_id)

    assert state("recordings").state == "1"
    assert state("recordings").attributes["folder"] == "/Drohne/Medien"
    assert state("unmatched").state == "1"
    assert state("unmatched").attributes["recordings"] == ["DJI_20260921190306_0001_D.MP4"]
    assert state("last_sync").state not in ("unknown", "unavailable")

    flightlog = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, DOMAIN)
    assert flightlog.state is ConfigEntryState.LOADED

    # A flight during the recording shows up (as after an import): now it is matched.
    rec_start = coordinator.data.recordings["v1"]["start"]
    flights = hass.data[DOMAIN][flightlog.entry_id]
    flights.data.flights = {
        **flights.data.flights,
        "f1": {"flight_id": "f1", "start_time": rec_start, "duration_s": 300},
    }
    flights.async_update_listeners()
    await hass.async_block_till_done()
    assert state("unmatched").state == "0"

    # The account's device carries a button that syncs right away.
    button_id = registry.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_onedrive_sync")
    assert button_id is not None

    def delta_calls() -> int:
        return sum(1 for call in aioclient_mock.mock_calls if "/delta" in str(call[1]))

    before = delta_calls()
    assert before == 1  # the first sync after setup
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    assert delta_calls() == before + 1

    # Playback redirects to the short-lived URL Graph hands out for /content.
    aioclient_mock.get(
        f"{GRAPH_URL}/me/drive/items/v1/content",
        status=302,
        headers={"Location": "https://my.microsoftpersonalcontent.com/v1"},
    )
    http = await hass_client()
    resp = await http.get("/api/dji_flightlog/media/v1/play", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "https://my.microsoftpersonalcontent.com/v1"

    # Options: keep the folder, then change it with the same picker.
    options = hass.config_entries.options
    result = await options.async_init(entry.entry_id)
    assert result["step_id"] == "media"
    assert result["description_placeholders"]["path"] == "/Drohne/Medien"
    result = await options.async_configure(
        result["flow_id"], {CONF_MEDIA_SCAN_INTERVAL: 600, CONF_MATCH_TOLERANCE: 60, "change_folder": True}
    )
    assert result["step_id"] == "folder"
    assert result["type"] is FlowResultType.MENU
    result = await options.async_configure(result["flow_id"], {"next_step_id": "dir_up"})
    result = await options.async_configure(result["flow_id"], {"next_step_id": "dir_use"})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert entry.options == {
        CONF_MEDIA_FOLDER: "Drohne",
        CONF_MEDIA_SCAN_INTERVAL: 600,
        CONF_MATCH_TOLERANCE: 60,
    }


@pytest.mark.usefixtures("current_request_with_host")
async def test_onedrive_picker_starts_at_root_without_default_folder(
    hass: HomeAssistant, tmp_path, hass_client_no_auth, aioclient_mock
) -> None:
    _mock_tree(aioclient_mock, medien=False)
    result = await _sign_in(hass, tmp_path, hass_client_no_auth, aioclient_mock)
    assert result["step_id"] == "onedrive_folder"
    assert result["description_placeholders"]["path"] == "/"
    assert result["description_placeholders"]["notice"] == ""  # not the user's fault, so no notice at first
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": _open(result, "Drohne")}
    )
    assert _labels(result) == ['✓ Use "/Drohne"', "⬆ Up one level"]
