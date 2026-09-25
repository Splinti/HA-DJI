"""DJI Flight Log: import DJI Fly flight records into Home Assistant."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import panel_custom
from homeassistant.components.frontend import async_remove_panel
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration

from .const import (
    ATTR_FLIGHT_ID,
    ATTR_FORMAT,
    ATTR_PATH,
    CARD_URL,
    CONF_ENTRY_TYPE,
    CONF_SIDEBAR_PANEL,
    DEFAULT_SIDEBAR_PANEL,
    DOMAIN,
    ENTRY_TYPE_ONEDRIVE,
    EXPORT_FORMATS,
    MEDIA_STORAGE_KEY,
    MEDIA_STORAGE_VERSION,
    PANEL_ELEMENT,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL,
    PANEL_URL_PATH,
    SERVICE_EXPORT_TRACK,
    SERVICE_IMPORT_FILE,
    SERVICE_SCAN,
    STATIC_URL_BASE,
)
from .coordinator import FlightLogCoordinator
from .http import async_register_views, render_export
from .media_coordinator import MediaCoordinator, media_coordinators
from .onedrive import OneDriveClient
from .storage import FlightStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON, Platform.GEO_LOCATION]
ONEDRIVE_PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_DATA_SETUP_DONE = f"{DOMAIN}_global_setup"

IMPORT_FILE_SCHEMA = vol.Schema({vol.Required(ATTR_PATH): cv.string})
EXPORT_TRACK_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_FLIGHT_ID): cv.string,
        vol.Optional(ATTR_FORMAT, default="gpx"): vol.In(EXPORT_FORMATS),
        vol.Optional(ATTR_PATH): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register HTTP views and the static card once per HA run."""
    if hass.data.get(_DATA_SETUP_DONE):
        return True
    hass.data[_DATA_SETUP_DONE] = True

    async_register_views(hass)
    www = Path(__file__).parent / "www"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL_BASE, str(www), cache_headers=False)]
    )
    return True


def _is_onedrive(entry: ConfigEntry) -> bool:
    return entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_ONEDRIVE


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await async_setup(hass, {})
    if _is_onedrive(entry):
        return await _async_setup_onedrive(hass, entry)

    store = FlightStore(hass)
    await store.async_load()
    coordinator = FlightLogCoordinator(hass, entry, store)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    @callback
    def _flights_changed() -> None:
        # Recordings are matched to flights, so the OneDrive sensors follow the flight list.
        for media in media_coordinators(hass):
            media.async_update_listeners()

    entry.async_on_unload(coordinator.async_add_listener(_flights_changed))
    _flights_changed()

    _async_register_services(hass)
    await _async_register_lovelace_resource(hass)
    await _async_setup_panel(hass, entry)
    return True


async def _async_setup_onedrive(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """A OneDrive account whose folder holds the recordings."""
    try:
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(hass, entry)
    except ValueError as err:
        # Application credentials removed or not loaded yet.
        raise ConfigEntryNotReady(f"OneDrive credentials unavailable: {err}") from err
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

    async def _token() -> str:
        await session.async_ensure_token_valid()
        return session.token["access_token"]

    coordinator = MediaCoordinator(hass, entry, OneDriveClient(async_get_clientsession(hass), _token))
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, ONEDRIVE_PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if _is_onedrive(entry):
        ok = await hass.config_entries.async_unload_platforms(entry, ONEDRIVE_PLATFORMS)
        if ok:
            hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        return ok
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        _async_remove_panel(hass)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not any(isinstance(c, FlightLogCoordinator) for c in hass.data[DOMAIN].values()):
            for service in (SERVICE_SCAN, SERVICE_IMPORT_FILE, SERVICE_EXPORT_TRACK):
                hass.services.async_remove(DOMAIN, service)
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the stored OneDrive listing when an account is removed."""
    if _is_onedrive(entry):
        await Store(hass, MEDIA_STORAGE_VERSION, f"{MEDIA_STORAGE_KEY}.{entry.entry_id}").async_remove()


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


# -- services ------------------------------------------------------------------


def _get_coordinator(hass: HomeAssistant) -> FlightLogCoordinator:
    coordinators = [c for c in hass.data.get(DOMAIN, {}).values() if isinstance(c, FlightLogCoordinator)]
    if not coordinators:
        raise HomeAssistantError("DJI Flight Log is not set up")
    return coordinators[0]


def _resolve_flight(coordinator: FlightLogCoordinator, flight_id: str) -> dict[str, Any]:
    """Accept a flight id or the alias ``last``."""
    if flight_id == "last":
        if coordinator.data is None or coordinator.data.totals.last is None:
            raise ServiceValidationError("No flights imported yet")
        return coordinator.data.totals.last
    summary = coordinator.data.flights.get(flight_id) if coordinator.data else None
    if summary is None:
        raise ServiceValidationError(f"Unknown flight_id {flight_id}")
    return summary


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SCAN):
        return

    async def handle_scan(call: ServiceCall) -> None:
        # OneDrive too, so the ↻ button in the panel picks up new recordings
        # without waiting for the (longer) media sync interval.
        for media in media_coordinators(hass):
            await media.async_refresh()
        await _get_coordinator(hass).async_refresh()

    async def handle_import_file(call: ServiceCall) -> ServiceResponse:
        coordinator = _get_coordinator(hass)
        path = Path(call.data[ATTR_PATH])
        if not await hass.async_add_executor_job(path.is_file):
            raise ServiceValidationError(f"{path} is not a file")
        summary = await coordinator.async_import_file(path)
        return {"flight": summary} if summary else {"flight": None}

    async def handle_export_track(call: ServiceCall) -> ServiceResponse:
        coordinator = _get_coordinator(hass)
        summary = _resolve_flight(coordinator, call.data[ATTR_FLIGHT_ID])
        fmt = call.data[ATTR_FORMAT]
        track = await coordinator.store.async_read_track(summary["flight_id"])
        if track is None:
            raise ServiceValidationError(
                f"Flight {summary['flight_id']} has no track (encrypted log without API key?)"
            )
        body, _ctype = render_export(summary, track, fmt)

        target = call.data.get(ATTR_PATH)
        if target:
            out = Path(target)
            if out.is_dir() or target.endswith(("/", "\\")):
                out = out / f"{summary['start_time'][:10]}_{summary['flight_id']}.{fmt}"
            if not hass.config.is_allowed_path(str(out)):
                raise ServiceValidationError(f"{out} is not in allowlist_external_dirs (configuration.yaml)")

            def _write() -> None:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(body, encoding="utf-8")

            await hass.async_add_executor_job(_write)
            return {"path": str(out), "flight_id": summary["flight_id"], "format": fmt}
        return {"content": body, "flight_id": summary["flight_id"], "format": fmt}

    hass.services.async_register(DOMAIN, SERVICE_SCAN, handle_scan)
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_FILE,
        handle_import_file,
        schema=IMPORT_FILE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_TRACK,
        handle_export_track,
        schema=EXPORT_TRACK_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


# -- sidebar panel -------------------------------------------------------------


async def _async_setup_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Add (or remove) the full-page panel in the sidebar."""
    wanted = entry.options.get(CONF_SIDEBAR_PANEL, entry.data.get(CONF_SIDEBAR_PANEL, DEFAULT_SIDEBAR_PANEL))
    if not wanted:
        _async_remove_panel(hass)
        return
    # The version query busts the browser's ES module cache after an update.
    integration = await async_get_integration(hass, DOMAIN)
    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL_PATH,
            webcomponent_name=PANEL_ELEMENT,
            module_url=f"{PANEL_URL}?v={integration.version}",
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            require_admin=False,
        )
    except ValueError:
        # Already registered (entry reloaded without HA restart).
        _LOGGER.debug("Panel %s already registered", PANEL_URL_PATH)


@callback
def _async_remove_panel(hass: HomeAssistant) -> None:
    async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)


# -- lovelace resource ---------------------------------------------------------


async def _async_register_lovelace_resource(hass: HomeAssistant) -> None:
    """Add the map card as a dashboard resource (storage mode only).

    Best effort: the Lovelace internals differ between HA versions, and in
    YAML mode the user has to list the resource manually anyway.
    """
    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None)
    if resources is None and isinstance(lovelace, dict):
        resources = lovelace.get("resources")
    if resources is None or not hasattr(resources, "async_create_item"):
        _LOGGER.debug("Lovelace resources not available; add %s manually", CARD_URL)
        return
    try:
        if not getattr(resources, "loaded", True):
            await resources.async_load()
        for item in resources.async_items():
            if item.get("url", "").startswith(CARD_URL):
                return
        await resources.async_create_item({"res_type": "module", "url": CARD_URL})
        _LOGGER.info("Registered dashboard resource %s", CARD_URL)
    except Exception as err:
        _LOGGER.warning("Could not register dashboard resource %s: %s", CARD_URL, err)
