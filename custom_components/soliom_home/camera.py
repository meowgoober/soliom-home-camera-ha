"""Camera platform for Soliom Home."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CGI_CONFIG_GET,
    CGI_CONFIG_SET,
    CGI_PTZ,
    CGI_REBOOT,
    CONF_RTSP_URL,
    DOMAIN,
    PTZ_COMMANDS,
    SNAPSHOT_CACHE_SECONDS,
)
from .coordinator import SoliomDataUpdateCoordinator
from .device import SoliomDevice, SoliomNode
from .entity import SoliomBaseEntity
from .local import (
    LocalCamera,
    build_rtsp_url,
    effective_options,
    resolve_local,
)

_LOGGER = logging.getLogger(__name__)


def _webp_to_jpeg(data: bytes) -> bytes | None:
    """Convert a WebP thumbnail to JPEG for Home Assistant's camera API."""
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with Home Assistant
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            rgb = image.convert("RGB")
            output = BytesIO()
            rgb.save(output, format="JPEG", quality=85)
            return output.getvalue()
    except Exception:  # noqa: BLE001 - corrupt thumbnails are non-fatal
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one camera entity per camera node."""
    coordinator: SoliomDataUpdateCoordinator = entry.runtime_data.coordinator
    if not coordinator.data:
        return
    async_add_entities(
        SoliomCamera(coordinator, entry, device, node)
        for device, node in coordinator.data.camera_nodes()
    )


class SoliomCamera(SoliomBaseEntity, Camera):
    """A Soliom camera with local RTSP live view where available."""

    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_brand = "Soliom"

    def __init__(
        self,
        coordinator: SoliomDataUpdateCoordinator,
        entry: ConfigEntry,
        device: SoliomDevice,
        node: SoliomNode | None = None,
    ) -> None:
        super().__init__(coordinator, entry, device, node)
        # CoordinatorEntity does not chain to Camera.__init__, so initialise the
        # camera base explicitly. Home Assistant 2026 expects attributes such as
        # _webrtc_provider to exist when the entity is added.
        Camera.__init__(self)
        self._attr_unique_id = f"{self._sn}_camera"
        self._local: LocalCamera | None = resolve_local(entry, device, node)
        self._options = effective_options(entry, self._sn, device.sn)
        self._last_image: bytes | None = None
        self._last_image_time: float = 0.0
        self._last_thumb_url: str | None = None
        self._last_ptz_code: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        cameras = self.hass.data.setdefault(DOMAIN, {}).setdefault("cameras", {})
        cameras[self.entity_id] = self

    async def async_will_remove_from_hass(self) -> None:
        cameras = self.hass.data.get(DOMAIN, {}).get("cameras", {})
        cameras.pop(self.entity_id, None)
        await super().async_will_remove_from_hass()

    # ------------------------------------------------------------------
    # Camera platform
    # ------------------------------------------------------------------
    def _live_url(self) -> str | None:
        """Built-in PPCS live view for homebase child cameras (None if n/a)."""
        if self._options.get(CONF_RTSP_URL):
            return None  # an explicit RTSP URL template always wins
        live = getattr(self.entry.runtime_data, "live", None)
        return live.stream_url(self._sn) if live else None

    @property
    def supported_features(self) -> CameraEntityFeature:
        if self._live_url():
            return CameraEntityFeature.STREAM
        device = self.device
        if device is None or self._local is None:
            return CameraEntityFeature(0)
        node = self.node
        if not build_rtsp_url(device, self._options, node):
            return CameraEntityFeature(0)
        return CameraEntityFeature.STREAM

    async def stream_source(self) -> str | None:
        """Return the URL consumed by HA's stream component."""
        live_url = self._live_url()
        if live_url:
            return live_url
        device = self.device
        if device is None or self._local is None:
            return None
        return build_rtsp_url(device, self._options, self.node)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a local snapshot, falling back to the latest cloud event thumbnail."""
        now = time.monotonic()
        if (
            self._last_image is not None
            and now - self._last_image_time < SNAPSHOT_CACHE_SECONDS
        ):
            return self._last_image

        if self._local is not None:
            try:
                image = await self._local.async_snapshot()
                if image:
                    self._last_image = image
                    self._last_image_time = now
                    return image
            except Exception as err:  # noqa: BLE001 - local devices fail in many ways
                _LOGGER.debug("Local snapshot failed for %s: %s", self._sn, err)

        image = await self._async_cloud_thumbnail()
        if image:
            self._last_image = image
            self._last_image_time = now
        return image

    async def _async_cloud_thumbnail(self) -> bytes | None:
        """Fetch the thumbnail of the most recent cloud event."""
        if not self.coordinator.data:
            return None
        event = self.coordinator.data.latest_event(self._sn)
        if event is None or not event.thumb_url:
            return None
        if event.thumb_url == self._last_thumb_url and self._last_image:
            return self._last_image
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(
                event.thumb_url, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status >= 400:
                    return None
                data = await response.read()
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug(
                "Could not download cloud thumbnail for %s: %s", self._sn, err
            )
            return None
        if data.startswith((b"\xff\xd8", b"\x89PNG")):
            self._last_thumb_url = event.thumb_url
            return data
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            jpeg = await self.hass.async_add_executor_job(_webp_to_jpeg, data)
            if jpeg:
                self._last_thumb_url = event.thumb_url
                return jpeg
        return None

    # ------------------------------------------------------------------
    # Extra state
    # ------------------------------------------------------------------
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        device = self.device
        node = self.node
        if device is None or node is None:
            return {}
        latest = (
            self.coordinator.data.latest_event(self._sn)
            if self.coordinator.data
            else None
        )
        return {
            "online": node.online,
            "camera_sn": node.sn,
            "parent_sn": device.sn,
            "channel": node.channel,
            "device_model": node.model or device.model,
            "firmware": device.firmware,
            "battery": node.battery,
            "wifi_signal": node.wifi_signal,
            "wifi_percent": node.wifi_percent,
            "ip_address": device.ip_addr,
            "lan_address": device.lan_addr,
            "wifi_ssid": device.ssid,
            "storage_total": device.storage_total,
            "storage_used": device.storage_used,
            "storage_free": device.storage_free,
            "local_control": self._local is not None,
            "local_host": self._local.host if self._local else None,
            "last_event": (self.coordinator.data.label_for(latest) if latest else None),
            "last_event_time": (
                latest.start_time.isoformat() if latest and latest.start_time else None
            ),
            "last_event_thumbnail": latest.thumb_url if latest else None,
        }

    # ------------------------------------------------------------------
    # Local control helpers (used by the services)
    # ------------------------------------------------------------------
    @property
    def local(self) -> LocalCamera | None:
        return self._local

    async def async_ptz(
        self,
        direction: str,
        *,
        speed: int = 4,
        preset: int = 0,
        duration: float = 0.0,
    ) -> None:
        """Move the camera using the local Xiongmai PTZ CGI."""
        if self._local is None:
            raise HomeAssistantError("No local host is configured for this camera")
        if direction == "stop":
            await self.async_ptz_stop()
            return
        if direction not in PTZ_COMMANDS:
            raise HomeAssistantError(f"Unsupported PTZ direction: {direction}")
        code = PTZ_COMMANDS[direction]
        path = CGI_PTZ.format(
            action="start",
            channel=max(0, self._local.channel - 1),
            code=code,
            speed=max(1, min(8, int(speed))),
            preset=int(preset),
        )
        await self._local.async_text(path)
        self._last_ptz_code = code
        if duration > 0:
            await asyncio.sleep(min(duration, 30))
            await self.async_ptz_stop()

    async def async_ptz_stop(self) -> None:
        if self._local is None:
            raise HomeAssistantError("No local host is configured for this camera")
        code = self._last_ptz_code or "Up"
        path = CGI_PTZ.format(
            action="stop",
            channel=max(0, self._local.channel - 1),
            code=code,
            speed=0,
            preset=0,
        )
        await self._local.async_text(path)
        self._last_ptz_code = None

    async def async_reboot(self) -> None:
        if self._local is None:
            raise HomeAssistantError("No local host is configured for this camera")
        await self._local.async_text(CGI_REBOOT)

    async def async_run_cgi(self, path: str) -> str:
        if self._local is None:
            raise HomeAssistantError("No local host is configured for this camera")
        return await self._local.async_text(path)

    async def async_get_config(self, name: str) -> str:
        if self._local is None:
            raise HomeAssistantError("No local host is configured for this camera")
        return await self._local.async_text(CGI_CONFIG_GET.format(name=name))

    async def async_set_config(self, setting: str) -> str:
        if self._local is None:
            raise HomeAssistantError("No local host is configured for this camera")
        return await self._local.async_text(CGI_CONFIG_SET.format(setting=setting))
