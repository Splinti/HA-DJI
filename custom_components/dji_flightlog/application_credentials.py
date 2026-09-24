"""Application credentials (Azure app registration) for the OneDrive connection."""

from __future__ import annotations

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant

from .const import OAUTH2_AUTHORIZE, OAUTH2_TOKEN


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    return AuthorizationServer(authorize_url=OAUTH2_AUTHORIZE, token_url=OAUTH2_TOKEN)


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    return {
        "portal_url": "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
        "redirect_url": "https://my.home-assistant.io/redirect/oauth",
    }
