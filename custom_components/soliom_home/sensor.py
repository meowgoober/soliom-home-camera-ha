"""Sensor platform for Soliom Home."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SoliomData, SoliomDataUpdateCoordinator
from .device import SoliomNode
from .entity import SoliomBaseEntity

ValueFn = Callable[[SoliomData, SoliomNode], Any]
ExistsFn = Callable[[SoliomNode], bool]


@dataclass(frozen=True, kw_only=True)
class SoliomSensorDescription(SensorEntityDescription):
    """Describes a Soliom sensor."""

    value_fn: ValueFn
    exists_fn: ExistsFn = lambda node: True


def _events_for(data: SoliomData, node: SoliomNode):
    return data.events_for(node.sn)


def _events_24h(data: SoliomData, node: SoliomNode) -> int:
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    return sum(
        1
        for event in _events_for(data, node)
        if event.start_time is None or event.start_time >= cutoff
    )


def _unread_events(data: SoliomData, node: SoliomNode) -> int:
    return sum(1 for event in _events_for(data, node) if not event.is_read)


def _last_event_time(data: SoliomData, node: SoliomNode):
    event = data.latest_event(node.sn)
    return event.start_time if event else None


def _last_event_type(data: SoliomData, node: SoliomNode) -> str | None:
    event = data.latest_event(node.sn)
    return data.label_for(event) if event else None


SENSORS: tuple[SoliomSensorDescription, ...] = (
    SoliomSensorDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, node: node.battery_level,
        exists_fn=lambda node: node.battery is not None,
    ),
    SoliomSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        icon="mdi:wifi",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, node: node.wifi_percent,
        exists_fn=lambda node: node.wifi_signal is not None,
    ),
    SoliomSensorDescription(
        key="storage_used",
        translation_key="storage_used",
        icon="mdi:sd",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, node: (
            data.device_for(node).storage_used_percent
            if data.device_for(node)
            else None
        ),
    ),
    SoliomSensorDescription(
        key="storage_free",
        translation_key="storage_free",
        icon="mdi:sd",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda data, node: (
            data.device_for(node).storage_free_percent
            if data.device_for(node)
            else None
        ),
    ),
    SoliomSensorDescription(
        key="last_event_time",
        translation_key="last_event_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:history",
        value_fn=_last_event_time,
    ),
    SoliomSensorDescription(
        key="last_event_type",
        translation_key="last_event_type",
        icon="mdi:alert-circle-outline",
        value_fn=_last_event_type,
    ),
    SoliomSensorDescription(
        key="events_24h",
        translation_key="events_24h",
        icon="mdi:calendar-today",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_events_24h,
    ),
    SoliomSensorDescription(
        key="unread_events",
        translation_key="unread_events",
        icon="mdi:bell-badge-outline",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_unread_events,
    ),
)


def _storage_exists(node: SoliomNode, data: SoliomData) -> bool:
    device = data.device_for(node)
    return bool(not node.is_child and device is not None and device.storage_total > 0)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SoliomDataUpdateCoordinator = entry.runtime_data.coordinator
    if not coordinator.data:
        return
    entities: list[SoliomSensor] = []
    for device, node in coordinator.data.camera_nodes():
        for description in SENSORS:
            if description.key.startswith("storage_"):
                if not _storage_exists(node, coordinator.data):
                    continue
            elif not description.exists_fn(node):
                continue
            entities.append(SoliomSensor(coordinator, entry, device, node, description))
    async_add_entities(entities)


class SoliomSensor(SoliomBaseEntity, SensorEntity):
    """A sensor backed by cloud data for one camera node."""

    entity_description: SoliomSensorDescription

    def __init__(
        self,
        coordinator: SoliomDataUpdateCoordinator,
        entry: ConfigEntry,
        device,
        node: SoliomNode,
        description: SoliomSensorDescription,
    ) -> None:
        super().__init__(coordinator, entry, device, node)
        self.entity_description = description
        self._attr_unique_id = f"{node.sn}_{description.key}"

    @property
    def native_value(self) -> Any:
        node = self.node
        if node is None or self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data, node)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        device = self.device
        node = self.node
        if device is None or node is None:
            return {}
        if self.entity_description.key == "wifi_signal":
            return {"raw_signal": node.wifi_signal}
        if self.entity_description.key.startswith("storage_"):
            return {
                "total": device.storage_total,
                "used": device.storage_used,
                "free": device.storage_free,
                "card_state": device.storage_state,
            }
        return {}
