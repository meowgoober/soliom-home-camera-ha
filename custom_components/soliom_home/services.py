"""Services for Soliom Home local camera controls."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, PTZ_COMMANDS

_LOGGER = logging.getLogger(__name__)

SERVICE_PTZ = "ptz"
SERVICE_REBOOT = "reboot"
SERVICE_CGI = "cgi"
SERVICE_SET_CONFIG = "set_config"

ATTR_DIRECTION = "direction"
ATTR_SPEED = "speed"
ATTR_PRESET = "preset"
ATTR_DURATION = "duration"
ATTR_PATH = "path"
ATTR_SETTING = "setting"

PTZ_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required(ATTR_DIRECTION): vol.In([*sorted(PTZ_COMMANDS), "stop"]),
        vol.Optional(ATTR_SPEED, default=4): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=8)
        ),
        vol.Optional(ATTR_PRESET, default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=255)
        ),
        vol.Optional(ATTR_DURATION, default=0): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=30)
        ),
    }
)

ENTITY_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids})

CGI_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required(ATTR_PATH): cv.string,
    }
)

SET_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required(ATTR_SETTING): cv.string,
    }
)


def _cameras(hass: HomeAssistant, entity_ids: list[str]) -> list[Any]:
    registered = hass.data.get(DOMAIN, {}).get("cameras", {})
    cameras = []
    for entity_id in entity_ids:
        camera = registered.get(entity_id)
        if camera is None:
            raise ServiceValidationError(
                f"{entity_id} is not a Soliom Home camera entity"
            )
        if camera.local is None:
            raise ServiceValidationError(
                f"{entity_id} has no local host configured; set one in the "
                "integration options"
            )
        cameras.append(camera)
    return cameras


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register integration services once."""
    if hass.services.has_service(DOMAIN, SERVICE_PTZ):
        return

    async def async_ptz(call: ServiceCall) -> None:
        for camera in _cameras(hass, call.data[ATTR_ENTITY_ID]):
            await camera.async_ptz(
                call.data[ATTR_DIRECTION],
                speed=call.data[ATTR_SPEED],
                preset=call.data[ATTR_PRESET],
                duration=call.data[ATTR_DURATION],
            )

    async def async_reboot(call: ServiceCall) -> None:
        for camera in _cameras(hass, call.data[ATTR_ENTITY_ID]):
            await camera.async_reboot()

    async def async_cgi(call: ServiceCall) -> None:
        for camera in _cameras(hass, call.data[ATTR_ENTITY_ID]):
            await camera.async_run_cgi(call.data[ATTR_PATH])

    async def async_set_config(call: ServiceCall) -> None:
        for camera in _cameras(hass, call.data[ATTR_ENTITY_ID]):
            await camera.async_set_config(call.data[ATTR_SETTING])

    hass.services.async_register(DOMAIN, SERVICE_PTZ, async_ptz, schema=PTZ_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_REBOOT, async_reboot, schema=ENTITY_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_CGI, async_cgi, schema=CGI_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_SET_CONFIG, async_set_config, schema=SET_CONFIG_SCHEMA
    )
