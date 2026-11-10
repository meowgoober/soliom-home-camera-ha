"""Button platform for Soliom Home."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CGI_REBOOT
from .coordinator import SoliomDataUpdateCoordinator
from .device import SoliomDevice, SoliomNode
from .entity import SoliomBaseEntity
from .local import resolve_local

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SoliomDataUpdateCoordinator = entry.runtime_data.coordinator
    if not coordinator.data:
        return
    entities = [
        SoliomRebootButton(coordinator, entry, device, node)
        for device, node in coordinator.data.camera_nodes()
        if resolve_local(entry, device, node) is not None
    ]
    async_add_entities(entities)


class SoliomRebootButton(SoliomBaseEntity, ButtonEntity):
    """Reboot a camera through its local CGI interface."""

    _attr_translation_key = "reboot"
    _attr_icon = "mdi:restart"

    def __init__(
        self,
        coordinator: SoliomDataUpdateCoordinator,
        entry: ConfigEntry,
        device: SoliomDevice,
        node: SoliomNode,
    ) -> None:
        super().__init__(coordinator, entry, device, node)
        self._attr_unique_id = f"{node.sn}_reboot"

    async def async_press(self) -> None:
        device = self.device
        if device is None:
            return
        local = resolve_local(self.entry, device, self.node)
        if local is None:
            return
        await local.async_text(CGI_REBOOT)
