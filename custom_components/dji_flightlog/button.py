"""Buttons to scan the log folder / sync a media source (OneDrive, folder) immediately."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ENTRY_TYPE, DOMAIN, MEDIA_ENTRY_TYPES
from .coordinator import FlightLogCoordinator
from .media_coordinator import MediaCoordinator, media_device_info
from .sensor import TOTALS_ID, totals_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if entry.data.get(CONF_ENTRY_TYPE) in MEDIA_ENTRY_TYPES:
        async_add_entities([MediaSyncButton(coordinator, entry)])
    else:
        async_add_entities([ScanButton(coordinator, entry)])


class ScanButton(CoordinatorEntity[FlightLogCoordinator], ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "scan"
    _attr_icon = "mdi:folder-refresh"

    def __init__(self, coordinator: FlightLogCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{TOTALS_ID}_scan"
        self._attr_device_info = totals_device_info(entry)

    async def async_press(self) -> None:
        await self.coordinator.async_refresh()


class MediaSyncButton(CoordinatorEntity[MediaCoordinator], ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: MediaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        kind = coordinator.backend.kind
        self._attr_translation_key = f"{kind}_sync"
        self._attr_icon = "mdi:cloud-sync" if kind == "onedrive" else "mdi:folder-sync"
        self._attr_unique_id = f"{entry.entry_id}_{kind}_sync"
        self._attr_device_info = media_device_info(entry)

    @property
    def available(self) -> bool:
        # Stays pressable after a failed sync; pressing it is how one retries.
        return True

    async def async_press(self) -> None:
        await self.coordinator.async_refresh()
