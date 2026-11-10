"""Switch platform for local Soliom camera controls.

These switches use the Xiongmai ``configManager.cgi`` interface, which is
available on most Soliom/XM firmware but can differ between models.  They are
created only when local access is configured or detected.
"""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CGI_CONFIG_SET
from .coordinator import SoliomDataUpdateCoordinator
from .device import SoliomDevice, SoliomNode
from .entity import SoliomBaseEntity
from .local import LocalCamera, resolve_local

_LOGGER = logging.getLogger(__name__)


SWITCHES: tuple[tuple[str, str, str, str, bool], ...] = (
    # key, translation key, config section, state key, enabled by default
    (
        "motion_detection",
        "motion_detection",
        "MotionDetect",
        "MotionDetect[0].Enable",
        True,
    ),
    ("siren", "siren", "Alarm", "Alarm[0].Enable", False),
    (
        "spotlight",
        "spotlight",
        "Lighting",
        "Lighting[0][0].Enable",
        False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SoliomDataUpdateCoordinator = entry.runtime_data.coordinator
    if not coordinator.data:
        return
    entities: list[SoliomConfigSwitch] = []
    for device, node in coordinator.data.camera_nodes():
        if resolve_local(entry, device, node) is None:
            continue
        for key, translation_key, config_name, state_key, enabled in SWITCHES:
            entities.append(
                SoliomConfigSwitch(
                    coordinator,
                    entry,
                    device,
                    node,
                    key=key,
                    translation_key=translation_key,
                    config_name=config_name,
                    state_key=state_key,
                    enabled_by_default=enabled,
                )
            )
    async_add_entities(entities)


def _parse_config(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


class SoliomConfigSwitch(SoliomBaseEntity, SwitchEntity):
    """A boolean setting exposed through ``configManager.cgi``."""

    def __init__(
        self,
        coordinator: SoliomDataUpdateCoordinator,
        entry: ConfigEntry,
        device: SoliomDevice,
        node: SoliomNode,
        *,
        key: str,
        translation_key: str,
        config_name: str,
        state_key: str,
        enabled_by_default: bool,
    ) -> None:
        super().__init__(coordinator, entry, device, node)
        self._attr_unique_id = f"{node.sn}_{key}"
        self._attr_translation_key = translation_key
        self._attr_entity_registry_enabled_default = enabled_by_default
        self._config_name = config_name
        self._state_key = state_key
        self._attr_is_on: bool | None = None

    @property
    def local(self) -> LocalCamera | None:
        device = self.device
        if device is None:
            return None
        return resolve_local(self.entry, device, self.node)

    @property
    def available(self) -> bool:
        return super().available and self.local is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.async_create_task(self._async_read_state())

    async def _async_read_state(self) -> None:
        local = self.local
        if local is None:
            return
        try:
            text = await local.async_text(
                f"/cgi-bin/configManager.cgi?action=getConfig&name={self._config_name}"
            )
            values = _parse_config(text)
            value = values.get(self._state_key)
            if value is None:
                return
            self._attr_is_on = value.lower() in {"true", "1", "on", "yes"}
            self.async_write_ha_state()
        except Exception as err:  # noqa: BLE001 - firmware varies a lot
            _LOGGER.debug(
                "Could not read %s for %s: %s", self._state_key, self._sn, err
            )

    async def _async_set(self, value: bool) -> None:
        local = self.local
        if local is None:
            return
        setting = f"{self._state_key}={'true' if value else 'false'}"
        await local.async_text(CGI_CONFIG_SET.format(setting=setting))
        self._attr_is_on = value
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)
