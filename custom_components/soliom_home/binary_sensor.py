"""Binary sensor platform for Soliom Home."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import MOTION_ACTIVE_WINDOW
from .coordinator import SoliomData, SoliomDataUpdateCoordinator
from .device import SoliomDevice, SoliomNode
from .entity import SoliomBaseEntity

ValueFn = Callable[[SoliomData, SoliomNode], bool | None]


@dataclass(frozen=True, kw_only=True)
class SoliomBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a Soliom binary sensor."""

    value_fn: ValueFn
    exists_fn: Callable[[SoliomNode], bool] = lambda node: True


def _motion(data: SoliomData, node: SoliomNode) -> bool:
    event = data.latest_event(node.sn)
    if event is None or event.start_time is None:
        return False
    return datetime.now(UTC) - event.start_time <= MOTION_ACTIVE_WINDOW


def _charging(data: SoliomData, node: SoliomNode) -> bool:
    # Bits 0, 1 and 3 of charge_state are used by the app's battery icons.
    return bool(node.charge_state & 0b1011)


MOTION = SoliomBinarySensorDescription(
    key="motion",
    translation_key="motion",
    device_class=BinarySensorDeviceClass.MOTION,
    value_fn=_motion,
)
ONLINE = SoliomBinarySensorDescription(
    key="online",
    translation_key="online",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    value_fn=lambda data, node: node.online,
)
CHARGING = SoliomBinarySensorDescription(
    key="charging",
    translation_key="charging",
    device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
    entity_registry_enabled_default=False,
    value_fn=_charging,
    exists_fn=lambda node: node.charge_state != 0,
)

BINARY_SENSORS: tuple[SoliomBinarySensorDescription, ...] = (
    MOTION,
    ONLINE,
    CHARGING,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SoliomDataUpdateCoordinator = entry.runtime_data.coordinator
    if not coordinator.data:
        return
    entities: list[SoliomBinarySensor] = []
    for device, node in coordinator.data.camera_nodes():
        for description in BINARY_SENSORS:
            if description.exists_fn(node):
                entities.append(
                    SoliomBinarySensor(coordinator, entry, device, node, description)
                )
    # A homebase has no camera node of its own, but its connectivity is still
    # useful and keeps the parent device in the registry.
    for device in coordinator.data.devices.values():
        if device.is_homebase:
            entities.append(
                SoliomBinarySensor(
                    coordinator, entry, device, device.base_node(), ONLINE
                )
            )
    async_add_entities(entities)


class SoliomBinarySensor(SoliomBaseEntity, BinarySensorEntity):
    """A binary sensor backed by cloud data."""

    entity_description: SoliomBinarySensorDescription

    def __init__(
        self,
        coordinator: SoliomDataUpdateCoordinator,
        entry: ConfigEntry,
        device: SoliomDevice,
        node: SoliomNode,
        description: SoliomBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, entry, device, node)
        self.entity_description = description
        self._attr_unique_id = f"{node.sn}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        node = self.node
        if node is None or self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data, node)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.entity_description.key != "motion":
            return {}
        if not self.coordinator.data:
            return {}
        event = self.coordinator.data.latest_event(self._sn)
        if event is None:
            return {}
        return {
            "event_type": self.coordinator.data.label_for(event),
            "event_id": event.event_id,
            "event_time": event.start_time.isoformat() if event.start_time else None,
            "thumbnail": event.thumb_url,
            "video_url": event.video_url,
        }
