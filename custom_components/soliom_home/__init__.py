"""The Soliom Home integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .api import SoliomCloudApi
from .const import (
    APP_KEY,
    APP_SECRET,
    CONF_COUNTRY_CODE,
    CONF_EMAIL,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_REGION,
    DEFAULT_COUNTRY_CODE,
    DEFAULT_REGION,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import SoliomDataUpdateCoordinator
from .live import SoliomLiveManager, new_token
from .live_view import SoliomLiveView
from .local import async_probe_local_hosts, hosts_needing_probe
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.SENSOR,
    Platform.SWITCH,
]


@dataclass(slots=True)
class SoliomRuntimeData:
    """Objects shared by all platforms of one config entry."""

    api: SoliomCloudApi
    coordinator: SoliomDataUpdateCoordinator
    local_ports: dict[str, set[int]] = field(default_factory=dict)
    live: SoliomLiveManager | None = None


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (no YAML configuration)."""
    hass.data.setdefault(DOMAIN, {})
    await async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Soliom Home from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    api = SoliomCloudApi(
        hass,
        email=str(entry.data[CONF_EMAIL]),
        password=str(entry.data[CONF_PASSWORD]),
        region=str(entry.data.get(CONF_REGION, DEFAULT_REGION)),
        country_code=int(entry.data.get(CONF_COUNTRY_CODE, DEFAULT_COUNTRY_CODE)),
        host=entry.data.get(CONF_HOST),
        app_key=APP_KEY,
        app_secret=APP_SECRET,
    )
    coordinator = SoliomDataUpdateCoordinator(hass, entry, api)

    try:
        await coordinator.async_setup()
        await coordinator.async_config_entry_first_refresh()
    except (ConfigEntryNotReady, ConfigEntryAuthFailed):
        raise
    except Exception as err:
        _LOGGER.exception("Failed to initialise Soliom Home")
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = SoliomRuntimeData(api=api, coordinator=coordinator)
    _setup_live_view(hass, entry)
    entry.runtime_data.local_ports = await async_probe_local_hosts(
        hosts_needing_probe(entry, coordinator.data.devices.values())
    )

    _register_homebase_devices(hass, entry, coordinator)
    _remove_legacy_base_entities(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_setup_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


def _register_homebase_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: SoliomDataUpdateCoordinator,
) -> None:
    """Create homebase devices before their child cameras.

    Child cameras link to the homebase with ``via_device_id``, which requires
    the parent device registry entry to exist first.
    """
    if not coordinator.data:
        return
    registry = dr.async_get(hass)
    for device in coordinator.data.devices.values():
        if not device.is_homebase:
            continue
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device.sn)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=device.model,
            serial_number=device.sn,
            sw_version=device.firmware,
        )


_LEGACY_BASE_SUFFIXES: tuple[str, ...] = (
    "camera",
    "events",
    "motion",
    "battery",
    "wifi_signal",
    "storage_used",
    "storage_free",
    "last_event_time",
    "last_event_type",
    "events_24h",
    "unread_events",
    "charging",
    "siren",
    "spotlight",
    "motion_detection",
    "reboot",
)


def _remove_legacy_base_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: SoliomDataUpdateCoordinator,
) -> None:
    """Drop entities that used to represent a homebase as one camera.

    Earlier versions created a single camera plus sensors for the base serial
    number.  Those are replaced by per-child entities; leaving them behind
    would show permanently unavailable entities.
    """
    if not coordinator.data:
        return
    homebases = {
        device.sn: device
        for device in coordinator.data.devices.values()
        if device.is_homebase
    }
    if not homebases:
        return
    legacy_ids = {
        f"{sn}_{suffix}" for sn in homebases for suffix in _LEGACY_BASE_SUFFIXES
    }
    registry = er.async_get(hass)
    removed = 0
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity_entry.unique_id in legacy_ids:
            registry.async_remove(entity_entry.entity_id)
            removed += 1
    if removed:
        _LOGGER.info(
            "Removed %s legacy homebase entities replaced by per-camera entities",
            removed,
        )


def _setup_live_view(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create the on-demand live-view manager and (once) its HTTP endpoint."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    token = domain_data.setdefault("live_token", new_token())
    manager = SoliomLiveManager(hass, entry.runtime_data.coordinator, token)
    entry.runtime_data.live = manager
    domain_data.setdefault("live_managers", {})[entry.entry_id] = manager
    if not domain_data.get("live_view_registered"):
        hass.http.register_view(SoliomLiveView(hass))
        domain_data["live_view_registered"] = True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    live = getattr(entry.runtime_data, "live", None)
    if live is not None:
        await live.async_shutdown()
    hass.data.get(DOMAIN, {}).get("live_managers", {}).pop(entry.entry_id, None)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
