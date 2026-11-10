"""Shared entity plumbing for Soliom Home."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import SoliomDataUpdateCoordinator
from .device import SoliomDevice, SoliomNode


class SoliomBaseEntity(CoordinatorEntity[SoliomDataUpdateCoordinator]):
    """Base entity attached to one camera node.

    A node is either a standalone camera or one child camera of a homebase.
    The parent :class:`SoliomDevice` still supplies account-level data such as
    firmware and local host.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SoliomDataUpdateCoordinator,
        entry: ConfigEntry,
        device: SoliomDevice,
        node: SoliomNode | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._device_sn = device.sn
        self._node_sn = (node or device.base_node()).sn
        self._sn = self._node_sn

    @property
    def device(self) -> SoliomDevice | None:
        """Return the latest cloud record for the parent device."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.devices.get(self._device_sn)

    @property
    def node(self) -> SoliomNode | None:
        """Return the latest cloud record for this camera node."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.nodes.get(self._node_sn)

    @property
    def available(self) -> bool:
        return super().available and self.node is not None

    @property
    def device_info(self) -> DeviceInfo:
        device = self.device
        node = self.node
        if device is None or node is None:
            return DeviceInfo(identifiers={(DOMAIN, self._node_sn)})
        if node.is_child:
            parent = dr.async_get(self.hass).async_get_device_by_identifier(
                (DOMAIN, device.sn), self.entry.entry_id
            )
            return DeviceInfo(
                identifiers={(DOMAIN, node.sn)},
                name=node.name,
                manufacturer=MANUFACTURER,
                model=node.model or device.model,
                serial_number=node.sn,
                via_device_id=parent.id if parent else None,
            )
        return DeviceInfo(
            identifiers={(DOMAIN, device.sn)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=device.model,
            serial_number=device.sn,
            sw_version=device.firmware,
            hw_version=None,
        )
