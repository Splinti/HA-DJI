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
    CONF_IMPORT_LOGS,
    CONF_LOG_DIR,
    CONF_MATCH_TOLERANCE,
    CONF_MAX_TRACK_POINTS,
    CONF_MEDIA_FOLDER,
    CONF_MEDIA_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL,
    CONF_SIDEBAR_PANEL,
    DEFAULT_GEO_LOCATION_LIMIT,
    DEFAULT_IMPORT_LOGS,
    DEFAULT_LOCAL_MEDIA_FOLDER,
    DEFAULT_LOG_DIR,
    DEFAULT_MATCH_TOLERANCE,
    DEFAULT_MAX_TRACK_POINTS,
    DEFAULT_MEDIA_FOLDER,
    DEFAULT_MEDIA_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SIDEBAR_PANEL,
    DOMAIN,
    ENTRY_TYPE_LOCAL,
    ENTRY_TYPE_ONEDRIVE,
    ONEDRIVE_SCOPES,
)
from .onedrive import GraphError, GraphNotFound, OneDriveClient, TokenProvider

_LOGGER = logging.getLogger(__name__)

CONF_CHANGE_FOLDER = "change_folder"
# The picker is a menu; each entry is a step "dir_<choice>", where the choice
# is "use", "up" or the index of a subfolder.
PICK_STEP = "dir_"
PICK_USE = "use"
PICK_UP = "up"


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


def _sync_fields(defaults: Mapping[str, Any]) -> dict[Any, Any]:
    return {
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
        vol.Optional(
            CONF_IMPORT_LOGS, default=defaults.get(CONF_IMPORT_LOGS, DEFAULT_IMPORT_LOGS)
        ): selector.BooleanSelector(),
    }


def _local_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    """Folder path, sync interval and matching tolerance of a local folder."""
    return vol.Schema(
        {
            vol.Required(
                CONF_MEDIA_FOLDER, default=defaults.get(CONF_MEDIA_FOLDER, DEFAULT_LOCAL_MEDIA_FOLDER)
            ): str,
            **_sync_fields(defaults),
        }
    )


def _local_options(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_MEDIA_FOLDER: os.path.normpath(user_input[CONF_MEDIA_FOLDER].strip()),
        CONF_MEDIA_SCAN_INTERVAL: int(user_input.get(CONF_MEDIA_SCAN_INTERVAL, DEFAULT_MEDIA_SCAN_INTERVAL)),
        CONF_MATCH_TOLERANCE: int(user_input.get(CONF_MATCH_TOLERANCE, DEFAULT_MATCH_TOLERANCE)),
        CONF_IMPORT_LOGS: bool(user_input.get(CONF_IMPORT_LOGS, DEFAULT_IMPORT_LOGS)),
    }


async def _async_check_local_folder(hass: HomeAssistant, folder: str) -> str | None:
    """Error key for the folder field, or None."""
    if not os.path.isabs(folder):
        return "dir_not_absolute"
    if not await hass.async_add_executor_job(os.path.isdir, folder):
        return "dir_not_found"
    return None


def _media_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    """Sync interval and matching tolerance; the folder has its own picker step."""
    return vol.Schema(
        {
            **_sync_fields(defaults),
            vol.Optional(CONF_CHANGE_FOLDER, default=False): selector.BooleanSelector(),
        }
    )


class FolderPicker:
    """Walks the OneDrive folder tree, one level per menu.

    Config flows have no tree widget, so each menu lists the subfolders of the
    current folder plus "use this folder" and "up one level"; clicking a
    subfolder shows the next level.
    """

    def __init__(self, hass: HomeAssistant, token: TokenProvider, start: str) -> None:
        self._client = OneDriveClient(async_get_clientsession(hass), token)
        self._german = (hass.config.language or "").startswith("de")
        self._first = True
        self._names: list[str] = []
        self.path = start.strip().strip("/")

    @property
    def display(self) -> str:
        return f"/{self.path}"

    def choose(self, choice: str) -> str | None:
        """Apply a menu choice; returns the folder once it was accepted."""
        if choice == PICK_USE:
            return self.path
        if choice == PICK_UP:
            self.path = self.path.rpartition("/")[0]
        elif choice.isdigit() and int(choice) < len(self._names):
            self.path = f"{self.path}/{self._names[int(choice)]}".strip("/")
        return None

    async def async_menu(self) -> tuple[dict[str, str], str]:
        """Menu options (step id -> label) for the current folder, plus a notice for the description."""
        first, self._first = self._first, False
        notice = ""
        try:
            try:
                self._names = await self._client.async_list_subfolders(self.path)
            except GraphNotFound:
                if not self.path:
                    raise
                # The start folder does not exist (yet), or was removed meanwhile.
                if not first:
                    notice = "folder_not_found"
                self.path = ""
                self._names = await self._client.async_list_subfolders(self.path)
        except (GraphError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Listing OneDrive folder %s failed: %s", self.display, err)
            notice = "cannot_connect"
            self._names = []

        if self._german:
            use, up = "✓ „{}“ verwenden", "⬆ Eine Ebene höher"
            notices = {
                "folder_not_found": "Ordner in OneDrive nicht gefunden",
                "cannot_connect": "OneDrive nicht erreichbar",
            }
        else:
            use, up = '✓ Use "{}"', "⬆ Up one level"
            notices = {
                "folder_not_found": "Folder not found in OneDrive",
                "cannot_connect": "Could not reach OneDrive",
            }
        options = {f"{PICK_STEP}{PICK_USE}": use.format(self.display)}
        if self.path:
            options[f"{PICK_STEP}{PICK_UP}"] = up
        options.update({f"{PICK_STEP}{i}": f"📁 {name}" for i, name in enumerate(self._names)})
        return options, f"\n\n⚠️ {notices[notice]}" if notice else ""


class _FolderMenu:
    """Routes the picker's menu steps (``async_step_dir_*``) to ``_async_picked``."""

    async def _async_picked(self, choice: str) -> ConfigFlowResult:
        raise NotImplementedError

    def __getattr__(self, name: str) -> Any:
        prefix = f"async_step_{PICK_STEP}"
        if not name.startswith(prefix):
            raise AttributeError(name)
        choice = name.removeprefix(prefix)

        async def _step(user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
            return await self._async_picked(choice)

        return _step


def _static_token(token: str) -> TokenProvider:
    async def _token() -> str:
        return token

    return _token


class DjiFlightLogConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, _FolderMenu, domain=DOMAIN):
    """Flight log (single instance) and media sources for the recordings.

    The first "Add integration" sets up the flight log; once that exists,
    adding the integration again connects a OneDrive account or a folder.
    """

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._oauth_data: dict[str, Any] | None = None
        self._account = ""
        self._picker: FolderPicker | None = None

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        return {"scope": " ".join(ONEDRIVE_SCOPES), "prompt": "select_account"}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None and self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, DOMAIN):
            return await self.async_step_media_source()
        return await self.async_step_flightlog(user_input)

    async def async_step_media_source(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Where the recordings are: a folder (incl. network storage) or OneDrive."""
        return self.async_show_menu(step_id="media_source", menu_options=["local", "onedrive"])

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

    # -- local folder (incl. network storage mounted by Home Assistant) ----------

    async def async_step_local(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            options = _local_options(user_input)
            folder = options[CONF_MEDIA_FOLDER]
            if error := await _async_check_local_folder(self.hass, folder):
                errors[CONF_MEDIA_FOLDER] = error
            else:
                await self.async_set_unique_id(f"local_{folder}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=folder, data={CONF_ENTRY_TYPE: ENTRY_TYPE_LOCAL}, options=options
                )
        return self.async_show_form(
            step_id="local", data_schema=_local_schema(user_input or {}), errors=errors
        )

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
        if self._picker is None:
            token = _static_token(self._oauth_data["token"]["access_token"])
            self._picker = FolderPicker(self.hass, token, DEFAULT_MEDIA_FOLDER)
        options, notice = await self._picker.async_menu()
        return self.async_show_menu(
            step_id="onedrive_folder",
            menu_options=options,
            description_placeholders={
                "account": self._account,
                "path": self._picker.display,
                "notice": notice,
            },
        )

    async def _async_picked(self, choice: str) -> ConfigFlowResult:
        assert self._picker is not None and self._oauth_data is not None
        folder = self._picker.choose(choice)
        if folder is None:
            return await self.async_step_onedrive_folder()
        return self.async_create_entry(
            title=f"OneDrive ({self._account})",
            data=self._oauth_data,
            options={
                CONF_MEDIA_FOLDER: folder,
                CONF_MEDIA_SCAN_INTERVAL: DEFAULT_MEDIA_SCAN_INTERVAL,
                CONF_MATCH_TOLERANCE: DEFAULT_MATCH_TOLERANCE,
                CONF_IMPORT_LOGS: DEFAULT_IMPORT_LOGS,
            },
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
        entry_type = config_entry.data.get(CONF_ENTRY_TYPE)
        if entry_type == ENTRY_TYPE_ONEDRIVE:
            return OneDriveOptionsFlow()
        if entry_type == ENTRY_TYPE_LOCAL:
            return LocalFolderOptionsFlow()
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


class OneDriveOptionsFlow(OptionsFlow, _FolderMenu):
    """Sync interval and matching tolerance of a OneDrive entry; the folder via the picker."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._picker: FolderPicker | None = None

    @property
    def _folder(self) -> str:
        return str(self.config_entry.options.get(CONF_MEDIA_FOLDER, DEFAULT_MEDIA_FOLDER)).strip("/")

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Own step id: the translations for "init" are the flight log's options.
        return await self.async_step_media()

    async def async_step_media(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._options = {
                **self.config_entry.options,
                CONF_MEDIA_FOLDER: self._folder,
                CONF_MEDIA_SCAN_INTERVAL: int(user_input[CONF_MEDIA_SCAN_INTERVAL]),
                CONF_MATCH_TOLERANCE: int(user_input[CONF_MATCH_TOLERANCE]),
                CONF_IMPORT_LOGS: bool(user_input.get(CONF_IMPORT_LOGS, DEFAULT_IMPORT_LOGS)),
            }
            if user_input.get(CONF_CHANGE_FOLDER):
                return await self.async_step_folder()
            return self.async_create_entry(title="", data=self._options)
        return self.async_show_form(
            step_id="media",
            data_schema=_media_schema(self.config_entry.options),
            description_placeholders={"path": f"/{self._folder}"},
        )

    async def async_step_folder(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._picker is None:
            implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
                self.hass, self.config_entry
            )
            session = config_entry_oauth2_flow.OAuth2Session(self.hass, self.config_entry, implementation)

            async def _token() -> str:
                await session.async_ensure_token_valid()
                return session.token["access_token"]

            self._picker = FolderPicker(self.hass, _token, self._folder)
        options, notice = await self._picker.async_menu()
        return self.async_show_menu(
            step_id="folder",
            menu_options=options,
            description_placeholders={"path": self._picker.display, "notice": notice},
        )

    async def _async_picked(self, choice: str) -> ConfigFlowResult:
        assert self._picker is not None
        folder = self._picker.choose(choice)
        if folder is None:
            return await self.async_step_folder()
        return self.async_create_entry(title="", data={**self._options, CONF_MEDIA_FOLDER: folder})


class LocalFolderOptionsFlow(OptionsFlow):
    """Folder, sync interval and matching tolerance of a local folder."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Own step id: the translations for "init" are the flight log's options.
        return await self.async_step_local()

    async def async_step_local(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            options = _local_options(user_input)
            if error := await _async_check_local_folder(self.hass, options[CONF_MEDIA_FOLDER]):
                errors[CONF_MEDIA_FOLDER] = error
            else:
                return self.async_create_entry(title="", data=options)
        return self.async_show_form(
            step_id="local",
            data_schema=_local_schema(user_input or self.config_entry.options),
            errors=errors,
        )
