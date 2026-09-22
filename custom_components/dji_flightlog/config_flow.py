"""Config and options flow for DJI Flight Log."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_entry_oauth2_flow, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_KEY,
    CONF_ENTRY_TYPE,
    CONF_GEO_LOCATION_LIMIT,
    CONF_LOG_DIR,
    CONF_MATCH_TOLERANCE,
    CONF_MAX_TRACK_POINTS,
    CONF_MEDIA_FOLDER,
    CONF_MEDIA_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL,
    CONF_SIDEBAR_PANEL,
    DEFAULT_GEO_LOCATION_LIMIT,
    DEFAULT_LOG_DIR,
    DEFAULT_MATCH_TOLERANCE,
    DEFAULT_MAX_TRACK_POINTS,
    DEFAULT_MEDIA_FOLDER,
    DEFAULT_MEDIA_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SIDEBAR_PANEL,
    DOMAIN,
    ENTRY_TYPE_ONEDRIVE,
    ONEDRIVE_SCOPES,
)
from .onedrive import GraphError, GraphNotFound, OneDriveClient

_LOGGER = logging.getLogger(__name__)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_LOG_DIR, default=defaults.get(CONF_LOG_DIR, DEFAULT_LOG_DIR)): str,
            vol.Optional(CONF_API_KEY, default=defaults.get(CONF_API_KEY, "")): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(
                CONF_SCAN_INTERVAL, default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=30, max=86400, step=1, unit_of_measurement="s")
            ),
            vol.Optional(
                CONF_MAX_TRACK_POINTS,
                default=defaults.get(CONF_MAX_TRACK_POINTS, DEFAULT_MAX_TRACK_POINTS),
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=100, max=20000, step=100)),
            vol.Optional(
                CONF_GEO_LOCATION_LIMIT,
                default=defaults.get(CONF_GEO_LOCATION_LIMIT, DEFAULT_GEO_LOCATION_LIMIT),
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=2000, step=10)),
            vol.Optional(
                CONF_SIDEBAR_PANEL,
                default=defaults.get(CONF_SIDEBAR_PANEL, DEFAULT_SIDEBAR_PANEL),
            ): selector.BooleanSelector(),
        }
    )


def _normalize(user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    out[CONF_LOG_DIR] = out[CONF_LOG_DIR].strip()
    out[CONF_API_KEY] = (out.get(CONF_API_KEY) or "").strip()
    for key in (CONF_SCAN_INTERVAL, CONF_MAX_TRACK_POINTS, CONF_GEO_LOCATION_LIMIT):
        if key in out:
            out[key] = int(out[key])
    return out


def _media_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_MEDIA_FOLDER, default=defaults.get(CONF_MEDIA_FOLDER, DEFAULT_MEDIA_FOLDER)
            ): str,
            vol.Optional(
                CONF_MEDIA_SCAN_INTERVAL,
                default=defaults.get(CONF_MEDIA_SCAN_INTERVAL, DEFAULT_MEDIA_SCAN_INTERVAL),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=60, max=86400, step=60, unit_of_measurement="s")
            ),
            vol.Optional(
                CONF_MATCH_TOLERANCE,
                default=defaults.get(CONF_MATCH_TOLERANCE, DEFAULT_MATCH_TOLERANCE),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=3600, step=10, unit_of_measurement="s")
            ),
        }
    )


def _normalize_media(user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    out[CONF_MEDIA_FOLDER] = out[CONF_MEDIA_FOLDER].strip().strip("/")
    for key in (CONF_MEDIA_SCAN_INTERVAL, CONF_MATCH_TOLERANCE):
        if key in out:
            out[key] = int(out[key])
    return out


async def _async_check_folder(hass: HomeAssistant, token: str, folder: str) -> str | None:
    """Return an error key, or None if the folder exists."""

    async def _token() -> str:
        return token

    client = OneDriveClient(async_get_clientsession(hass), _token)
    try:
        await client.async_get_folder(folder)
    except GraphNotFound:
        return "folder_not_found"
    except (GraphError, aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("OneDrive folder check failed: %s", err)
        return "cannot_connect"
    return None


class DjiFlightLogConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Flight log (single instance) and OneDrive accounts for the recordings.

    The first "Add integration" sets up the flight log; once that exists,
    adding the integration again connects a OneDrive account.
    """

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._oauth_data: dict[str, Any] | None = None
        self._account = ""

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        return {"scope": " ".join(ONEDRIVE_SCOPES), "prompt": "select_account"}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None and self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, DOMAIN):
            return await self.async_step_onedrive()
        return await self.async_step_flightlog(user_input)

    async def async_step_flightlog(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize(user_input)
            if not await self.hass.async_add_executor_job(os.path.isdir, data[CONF_LOG_DIR]):
                errors[CONF_LOG_DIR] = "dir_not_found"
            else:
                return self.async_create_entry(title="DJI Flight Log", data=data)

        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}), errors=errors)

    # -- OneDrive -------------------------------------------------------------

    async def async_step_onedrive(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_pick_implementation()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        token = data["token"]["access_token"]

        async def _token() -> str:
            return token

        client = OneDriveClient(async_get_clientsession(self.hass), _token)
        try:
            drive = await client.async_get_drive()
        except (GraphError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("Could not read the OneDrive drive: %s", err)
            return self.async_abort(reason="cannot_connect")

        await self.async_set_unique_id(f"onedrive_{drive['id']}")
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates={**data, CONF_ENTRY_TYPE: ENTRY_TYPE_ONEDRIVE}
            )
        self._abort_if_unique_id_configured()

        owner = (drive.get("owner") or {}).get("user") or {}
        self._account = owner.get("email") or owner.get("displayName") or drive["id"]
        self._oauth_data = {**data, CONF_ENTRY_TYPE: ENTRY_TYPE_ONEDRIVE}
        return await self.async_step_onedrive_folder()

    async def async_step_onedrive_folder(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._oauth_data is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            options = _normalize_media(user_input)
            error = await _async_check_folder(
                self.hass, self._oauth_data["token"]["access_token"], options[CONF_MEDIA_FOLDER]
            )
            if error:
                errors[CONF_MEDIA_FOLDER] = error
            else:
                return self.async_create_entry(
                    title=f"OneDrive ({self._account})", data=self._oauth_data, options=options
                )
        return self.async_show_form(
            step_id="onedrive_folder",
            data_schema=_media_schema(user_input or {}),
            errors=errors,
            description_placeholders={"account": self._account},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_pick_implementation()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        if config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_ONEDRIVE:
            return OneDriveOptionsFlow()
        return DjiFlightLogOptionsFlow()


class DjiFlightLogOptionsFlow(OptionsFlow):
    """Same fields as setup, stored in ``entry.options``."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize(user_input)
            if not await self.hass.async_add_executor_job(os.path.isdir, data[CONF_LOG_DIR]):
                errors[CONF_LOG_DIR] = "dir_not_found"
            else:
                return self.async_create_entry(title="", data=data)

        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(defaults), errors=errors)


class OneDriveOptionsFlow(OptionsFlow):
    """Folder, sync interval and matching tolerance of a OneDrive entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            options = _normalize_media(user_input)
            implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
                self.hass, self.config_entry
            )
            session = config_entry_oauth2_flow.OAuth2Session(self.hass, self.config_entry, implementation)
            try:
                await session.async_ensure_token_valid()
            except aiohttp.ClientError:
                errors["base"] = "cannot_connect"
            else:
                error = await _async_check_folder(
                    self.hass, session.token["access_token"], options[CONF_MEDIA_FOLDER]
                )
                if error:
                    errors[CONF_MEDIA_FOLDER] = error
                else:
                    return self.async_create_entry(title="", data=options)

        defaults = {**self.config_entry.options, **(user_input or {})}
        return self.async_show_form(step_id="init", data_schema=_media_schema(defaults), errors=errors)
