"""Config flow for Soliom Home."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .api import (
    SoliomApiError,
    SoliomAuthError,
    SoliomCloudApi,
    SoliomConnectionError,
    SoliomError,
)
from .const import (
    APP_KEY,
    APP_SECRET,
    CONF_COUNTRY_CODE,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_HOST,
    CONF_LOCAL_HOST,
    CONF_LOCAL_HTTPS,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_RTSP_CHANNEL,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_PORT,
    CONF_RTSP_STREAM,
    CONF_RTSP_URL,
    CONF_RTSP_USERNAME,
    CONF_SCAN_INTERVAL,
    CONF_SNAPSHOT_URL,
    CONF_VERIFY_SSL,
    COUNTRY_CALLING_CODES,
    DEFAULT_COUNTRY_CODE,
    DEFAULT_REGION,
    DEFAULT_RTSP_CHANNEL,
    DEFAULT_RTSP_PORT,
    DEFAULT_RTSP_STREAM,
    DEFAULT_RTSP_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .local import effective_options

_LOGGER = logging.getLogger(__name__)


def _default_region(hass) -> str:
    country = hass.config.country
    if country and country in COUNTRY_CALLING_CODES:
        return str(country)
    return DEFAULT_REGION


def _default_country_code(hass) -> int:
    country = hass.config.country
    if country:
        return COUNTRY_CALLING_CODES.get(str(country), DEFAULT_COUNTRY_CODE)
    return DEFAULT_COUNTRY_CODE


class SoliomHomeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Soliom Home login flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email = str(user_input[CONF_EMAIL]).strip()
            password = str(user_input[CONF_PASSWORD])
            region = str(user_input[CONF_REGION]).strip().upper()
            country_code = int(user_input[CONF_COUNTRY_CODE])

            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            api = SoliomCloudApi(
                self.hass,
                email=email,
                password=password,
                region=region,
                country_code=country_code,
                app_key=APP_KEY,
                app_secret=APP_SECRET,
            )
            try:
                await api.async_discover_host()
                await api.async_login()
                await api.async_get_devices()
            except SoliomAuthError:
                errors["base"] = "invalid_auth"
            except SoliomConnectionError:
                errors["base"] = "cannot_connect"
            except SoliomApiError as err:
                _LOGGER.debug("Soliom login API error: %s", err)
                errors["base"] = "api_error"
            except SoliomError as err:
                _LOGGER.debug("Soliom login failed: %s", err)
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"Soliom Home ({email})",
                    data={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                        CONF_REGION: region,
                        CONF_COUNTRY_CODE: country_code,
                        CONF_HOST: api.host,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Required(
                    CONF_REGION, default=_default_region(self.hass)
                ): cv.string,
                vol.Required(
                    CONF_COUNTRY_CODE, default=_default_country_code(self.hass)
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=999)),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "app": "Soliom Home",
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            password = str(user_input[CONF_PASSWORD])
            api = SoliomCloudApi(
                self.hass,
                email=str(entry.data[CONF_EMAIL]),
                password=password,
                region=str(entry.data.get(CONF_REGION, DEFAULT_REGION)),
                country_code=int(
                    entry.data.get(CONF_COUNTRY_CODE, DEFAULT_COUNTRY_CODE)
                ),
                host=entry.data.get(CONF_HOST),
                app_key=APP_KEY,
                app_secret=APP_SECRET,
            )
            try:
                await api.async_login()
                await api.async_get_devices()
            except SoliomAuthError:
                errors["base"] = "invalid_auth"
            except SoliomConnectionError:
                errors["base"] = "cannot_connect"
            except SoliomError as err:
                _LOGGER.debug("Soliom reauth failed: %s", err)
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        CONF_PASSWORD: password,
                        CONF_HOST: api.host,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): cv.string}),
            errors=errors,
            description_placeholders={
                CONF_EMAIL: str(entry.data.get(CONF_EMAIL, "")),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SoliomHomeOptionsFlow()


class SoliomHomeOptionsFlow(OptionsFlow):
    """Options for scan interval and per-device local camera settings."""

    _device_sn: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["general", "device"],
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            options = dict(self.config_entry.options)
            options[CONF_SCAN_INTERVAL] = int(user_input[CONF_SCAN_INTERVAL])
            return self.async_create_entry(title="", data=options)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=int(
                        self.config_entry.options.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        )
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                ),
            }
        )
        return self.async_show_form(step_id="general", data_schema=schema)

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        nodes = self._nodes()
        if not nodes:
            return self.async_abort(reason="no_devices")

        if user_input is not None:
            self._device_sn = str(user_input["device"])
            return await self.async_step_device_settings()

        schema = vol.Schema(
            {
                vol.Required("device"): vol.In(
                    {sn: self._label(node) for sn, node in nodes.items()}
                )
            }
        )
        return self.async_show_form(step_id="device", data_schema=schema)

    async def async_step_device_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        nodes = self._nodes()
        sn = self._device_sn
        if sn is None or sn not in nodes:
            return self.async_abort(reason="no_devices")
        node = nodes[sn]
        current = effective_options(self.config_entry, sn, node.base_sn)

        if user_input is not None:
            cleaned: dict[str, Any] = {}
            for key, value in user_input.items():
                if key in (CONF_LOCAL_HTTPS, CONF_VERIFY_SSL):
                    cleaned[key] = bool(value)
                elif value not in (None, ""):
                    cleaned[key] = value
            options = dict(self.config_entry.options)
            stored_devices = dict(options.get(CONF_DEVICES) or {})
            if cleaned.get(CONF_LOCAL_HOST):
                stored_devices[sn] = cleaned
            else:
                stored_devices.pop(sn, None)
            options[CONF_DEVICES] = stored_devices
            return self.async_create_entry(title="", data=options)

        schema = vol.Schema(
            {
                vol.Optional(CONF_LOCAL_HOST): cv.string,
                vol.Optional(CONF_RTSP_PORT, default=DEFAULT_RTSP_PORT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Optional(
                    CONF_RTSP_USERNAME, default=DEFAULT_RTSP_USERNAME
                ): cv.string,
                vol.Optional(CONF_RTSP_PASSWORD): cv.string,
                vol.Optional(CONF_RTSP_CHANNEL, default=DEFAULT_RTSP_CHANNEL): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=64)
                ),
                vol.Optional(CONF_RTSP_STREAM, default=DEFAULT_RTSP_STREAM): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=3)
                ),
                vol.Optional(CONF_RTSP_URL): cv.string,
                vol.Optional(CONF_SNAPSHOT_URL): cv.string,
                vol.Optional(CONF_LOCAL_HTTPS, default=False): bool,
                vol.Optional(CONF_VERIFY_SSL, default=False): bool,
            }
        )
        suggested = dict(current)
        suggested.setdefault(CONF_LOCAL_HOST, self._local_hint(node) or "")
        suggested.setdefault(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)
        suggested.setdefault(CONF_RTSP_USERNAME, DEFAULT_RTSP_USERNAME)
        suggested.setdefault(CONF_RTSP_CHANNEL, node.channel or DEFAULT_RTSP_CHANNEL)
        suggested.setdefault(CONF_RTSP_STREAM, DEFAULT_RTSP_STREAM)
        return self.async_show_form(
            step_id="device_settings",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            description_placeholders={
                "device": self._label(node),
                "ip": self._local_hint(node) or "not reported by cloud",
                "p2p_password_hint": (
                    "The camera password (p2p_password) is used automatically "
                    "when the RTSP password is left empty."
                ),
            },
        )

    def _data(self):
        runtime = getattr(self.config_entry, "runtime_data", None)
        coordinator = getattr(runtime, "coordinator", None)
        if coordinator is None or not coordinator.data:
            return None
        return coordinator.data

    def _nodes(self):
        data = self._data()
        if data is None:
            return {}
        nodes = {}
        for device in data.devices.values():
            base = device.base_node()
            nodes[base.sn] = base
            for node in device.nodes():
                if node.sn != base.sn:
                    nodes[node.sn] = node
        return nodes

    def _label(self, node) -> str:
        data = self._data()
        device = data.device_for(node) if data else None
        if node.is_child and device is not None:
            return f"{device.name} / {node.name}"
        if device is not None and device.is_homebase:
            return f"{device.name} (homebase)"
        return node.name

    def _local_hint(self, node) -> str | None:
        data = self._data()
        device = data.device_for(node) if data else None
        return device.local_host if device else None
