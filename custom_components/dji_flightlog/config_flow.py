"""Config and options flow for DJI Flight Log."""

from __future__ import annotations

import os
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_API_KEY,
    CONF_GEO_LOCATION_LIMIT,
    CONF_LOG_DIR,
    CONF_MAX_TRACK_POINTS,
    CONF_SCAN_INTERVAL,
    DEFAULT_GEO_LOCATION_LIMIT,
    DEFAULT_LOG_DIR,
    DEFAULT_MAX_TRACK_POINTS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)


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


class DjiFlightLogConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance UI setup."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DjiFlightLogOptionsFlow:
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
