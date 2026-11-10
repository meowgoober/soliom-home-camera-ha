"""Event platform for Soliom Home."""

from __future__ import annotations

import logging

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import EVENT_TYPE_NAMES
from .coordinator import SoliomDataUpdateCoordinator
from .device import SoliomDevice, SoliomNode
from .entity import SoliomBaseEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SoliomDataUpdateCoordinator = entry.runtime_data.coordinator
    if not coordinator.data:
        return
    async_add_entities(
        SoliomEventEntity(coordinator, entry, device, node)
        for device, node in coordinator.data.camera_nodes()
    )


class SoliomEventEntity(SoliomBaseEntity, EventEntity):
    """Fires a Home Assistant event for every new cloud event."""

    _attr_translation_key = "events"

    def __init__(
        self,
        coordinator: SoliomDataUpdateCoordinator,
        entry: ConfigEntry,
        device: SoliomDevice,
        node: SoliomNode | None = None,
    ) -> None:
        super().__init__(coordinator, entry, device, node)
        self._attr_unique_id = f"{self._sn}_events"

    @property
    def event_types(self) -> list[str]:
        labels = set(EVENT_TYPE_NAMES.values())
        labels.add("unknown")
        if self.coordinator.data:
            labels.update(self.coordinator.data.event_type_labels.values())
        return sorted(labels)

    def _handle_coordinator_update(self) -> None:
        if self.coordinator.data:
            for event in self.coordinator.data.new_events:
                if event.sn != self._sn:
                    continue
                label = self.coordinator.data.label_for(event)
                self._trigger_event(
                    label,
                    {
                        "event_id": event.event_id,
                        "type": event.type_id,
                        "sub_type": event.sub_type,
                        "sn": event.sn,
                        "time": (
                            event.start_time.isoformat() if event.start_time else None
                        ),
                        "thumbnail": event.thumb_url,
                        "video_url": event.video_url,
                        "is_read": event.is_read,
                    },
                )
                _LOGGER.debug(
                    "Fired Soliom event %s (%s) for %s",
                    event.event_id,
                    label,
                    event.sn,
                )
        super()._handle_coordinator_update()
