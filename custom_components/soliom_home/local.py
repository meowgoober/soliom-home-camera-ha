"""Helpers for talking to a camera on the local network.

The Soliom app itself only streams over the proprietary PPCS P2P tunnel, but
many Xiongmai firmwares also expose standard RTSP and CGI endpoints.  These
helpers implement that optional local path for live view, snapshots, PTZ and a
handful of device controls.

Homebase models (for example the SHB01) often expose no local TCP services at
all.  Local entities are therefore only created when the host is explicitly
configured or a port responds during setup.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CGI_SNAPSHOT,
    CONF_DEVICES,
    CONF_LOCAL_HOST,
    CONF_LOCAL_HTTPS,
    CONF_RTSP_CHANNEL,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_PORT,
    CONF_RTSP_STREAM,
    CONF_RTSP_URL,
    CONF_RTSP_USERNAME,
    CONF_SNAPSHOT_URL,
    CONF_VERIFY_SSL,
    DEFAULT_RTSP_CHANNEL,
    DEFAULT_RTSP_PORT,
    DEFAULT_RTSP_STREAM,
    DEFAULT_RTSP_USERNAME,
)
from .device import SoliomDevice, SoliomNode

_LOGGER = logging.getLogger(__name__)

LOCAL_TIMEOUT = aiohttp.ClientTimeout(total=12)

# Ports probed to decide whether a camera is reachable on the LAN.  RTSP is
# 554, the Xiongmai CGI interface usually 80/8000, and some models use
# 8080/34567.  A homebase with no open ports gets no local entities.
PROBE_PORTS: tuple[int, ...] = (80, 443, 554, 8000, 8080, 34567)
PROBE_TIMEOUT = 0.7


def per_device_options(entry: ConfigEntry, sn: str) -> dict[str, Any]:
    """Return the stored local settings for one serial number."""
    devices = entry.options.get(CONF_DEVICES) or {}
    if not isinstance(devices, dict):
        return {}
    value = devices.get(sn) or {}
    return value if isinstance(value, dict) else {}


def effective_options(
    entry: ConfigEntry, sn: str, base_sn: str | None = None
) -> dict[str, Any]:
    """Merge homebase defaults with camera-specific overrides."""
    base = per_device_options(entry, base_sn) if base_sn and base_sn != sn else {}
    own = per_device_options(entry, sn)
    merged = dict(base)
    merged.update(own)
    return merged


def local_host_for(
    device: SoliomDevice,
    options: dict[str, Any],
    node: SoliomNode | None = None,
) -> str | None:
    """Return the configured local host, falling back to the LAN address.

    The cloud-reported WAN address is never used.
    """
    host = options.get(CONF_LOCAL_HOST)
    if host:
        return str(host).strip()
    return device.local_host


def _normalise_host(host: str, options: dict[str, Any]) -> tuple[str, str, int]:
    """Split an optional scheme/port from the configured host."""
    raw = host.strip()
    if "://" not in raw:
        raw = ("https://" if options.get(CONF_LOCAL_HTTPS) else "http://") + raw
    parsed = urlparse(raw)
    scheme = parsed.scheme or "http"
    hostname = parsed.hostname or parsed.path
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, hostname, port


@dataclass(slots=True)
class LocalCamera:
    """A camera reachable over the LAN."""

    host: str
    port: int
    username: str
    password: str
    channel: int
    https: bool
    verify_ssl: bool
    device: SoliomDevice
    options: dict[str, Any]
    camera_sn: str | None = None

    @property
    def sn(self) -> str:
        return self.camera_sn or self.device.sn

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        default_port = 443 if self.https else 80
        if self.port and self.port != default_port:
            return f"{scheme}://{self.host}:{self.port}"
        return f"{scheme}://{self.host}"

    @property
    def snapshot_url(self) -> str:
        template = self.options.get(CONF_SNAPSHOT_URL)
        if template:
            return str(template).format(
                host=self.host,
                port=self.port,
                scheme="https" if self.https else "http",
                username=self.username,
                password=self.password,
                channel=max(0, self.channel - 1),
                sn=self.sn,
                base_url=self.base_url,
            )
        return self.base_url + CGI_SNAPSHOT.format(channel=max(0, self.channel - 1))

    def url_for(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        return self.base_url + path_or_url

    async def async_request(self, path_or_url: str) -> tuple[int, bytes, str]:
        """Fetch a local URL, trying digest and then basic authentication."""
        session = async_get_clientsession()
        url = self.url_for(path_or_url)
        auth_options: list[aiohttp.BasicAuth | aiohttp.DigestAuth] = [
            aiohttp.DigestAuth(self.username, self.password),
            aiohttp.BasicAuth(self.username, self.password),
        ]
        last_error: Exception | None = None
        request_kwargs: dict[str, Any] = {}
        if self.https and not self.verify_ssl:
            request_kwargs["ssl"] = False
        for auth in auth_options:
            try:
                async with session.get(
                    url,
                    auth=auth,
                    timeout=LOCAL_TIMEOUT,
                    **request_kwargs,
                ) as response:
                    body = await response.read()
                    if response.status == 401:
                        last_error = PermissionError("Camera rejected credentials")
                        continue
                    return (
                        response.status,
                        body,
                        response.headers.get("Content-Type", ""),
                    )
            except (TimeoutError, aiohttp.ClientError) as err:
                last_error = err
        raise ConnectionError(
            f"Could not reach camera at {self.host}: {last_error}"
        ) from last_error

    async def async_text(self, path_or_url: str) -> str:
        status, body, _ = await self.async_request(path_or_url)
        if status >= 400:
            raise ConnectionError(f"Camera returned HTTP {status} for {path_or_url}")
        return body.decode("utf-8", "replace")

    async def async_snapshot(self) -> bytes | None:
        status, body, content_type = await self.async_request(self.snapshot_url)
        if status >= 400 or not body:
            return None
        # Some firmware returns a JPEG with an unhelpful content type.
        if (
            content_type
            and not content_type.startswith("image/")
            and not body.startswith(b"\xff\xd8")
        ):
            return None
        return body


def build_local_camera(
    entry: ConfigEntry,
    device: SoliomDevice,
    node: SoliomNode | None = None,
    options: dict[str, Any] | None = None,
) -> LocalCamera | None:
    """Build a :class:`LocalCamera` from entry options and cloud data."""
    if options is None:
        options = effective_options(entry, (node or device.base_node()).sn, device.sn)
    host = local_host_for(device, options, node)
    if not host:
        return None
    scheme, hostname, http_port = _normalise_host(host, options)
    # An explicit local_host that includes a port is an HTTP/CGI port; RTSP
    # has its own port setting, which defaults to 554.
    username = str(options.get(CONF_RTSP_USERNAME) or DEFAULT_RTSP_USERNAME)
    password = str(options.get(CONF_RTSP_PASSWORD) or device.p2p_password or "")
    default_channel = node.channel if node and node.channel else DEFAULT_RTSP_CHANNEL
    return LocalCamera(
        host=hostname,
        port=http_port,
        username=username,
        password=password,
        channel=int(options.get(CONF_RTSP_CHANNEL) or default_channel),
        https=scheme == "https",
        verify_ssl=bool(options.get(CONF_VERIFY_SSL, False)),
        device=device,
        options=options,
        camera_sn=node.sn if node else None,
    )


def build_rtsp_url(
    device: SoliomDevice,
    options: dict[str, Any],
    node: SoliomNode | None = None,
) -> str | None:
    """Build the RTSP URL used by Home Assistant's stream component."""
    host = local_host_for(device, options, node)
    if not host:
        return None

    username = str(options.get(CONF_RTSP_USERNAME) or DEFAULT_RTSP_USERNAME)
    password = str(options.get(CONF_RTSP_PASSWORD) or device.p2p_password or "")
    default_channel = node.channel if node and node.channel else DEFAULT_RTSP_CHANNEL
    channel = int(options.get(CONF_RTSP_CHANNEL) or default_channel)
    stream = int(options.get(CONF_RTSP_STREAM) or DEFAULT_RTSP_STREAM)
    port = int(options.get(CONF_RTSP_PORT) or DEFAULT_RTSP_PORT)
    _, rtsp_host, _ = _normalise_host(host, options)

    template = options.get(CONF_RTSP_URL)
    if template:
        return str(template).format(
            host=rtsp_host,
            port=port,
            username=username,
            password=password,
            user=quote(username, safe=""),
            passwd=quote(password, safe=""),
            channel=channel,
            stream=stream,
            sn=node.sn if node else device.sn,
        )

    # Xiongmai's usual RTSP path.  Credentials are repeated in the path
    # because that is how XM firmware parses them.
    user_q = quote(username, safe="")
    pass_q = quote(password, safe="")
    host_q = rtsp_host
    return (
        f"rtsp://{user_q}:{pass_q}@{host_q}:{port}"
        f"/user={user_q}_password={pass_q}_channel={channel}_stream={stream}.sdp?real_stream"
    )


def _explicit_local(options: dict[str, Any]) -> bool:
    return bool(
        options.get(CONF_LOCAL_HOST)
        or options.get(CONF_RTSP_URL)
        or options.get(CONF_SNAPSHOT_URL)
    )


def hosts_needing_probe(
    entry: ConfigEntry, devices: Iterable[SoliomDevice]
) -> list[str]:
    """Return auto-detected hosts that need a reachability probe.

    Devices with an explicit local host or template are trusted as-is, so a
    purely manual setup never causes network traffic during startup.
    """
    hosts: set[str] = set()
    for device in devices:
        for node in device.nodes():
            options = effective_options(entry, node.sn, device.sn)
            if _explicit_local(options):
                continue
            if device.local_host:
                hosts.add(device.local_host)
    return sorted(hosts)


def runtime_local_ports(entry: ConfigEntry) -> dict[str, set[int]]:
    runtime = getattr(entry, "runtime_data", None)
    ports = getattr(runtime, "local_ports", None)
    return ports if isinstance(ports, dict) else {}


def resolve_local(
    entry: ConfigEntry,
    device: SoliomDevice,
    node: SoliomNode | None = None,
) -> LocalCamera | None:
    """Return a usable local camera, or None when local access is not viable.

    An explicit user configuration always wins.  Auto-detected addresses are
    only used when at least one local port responded during setup, which
    avoids creating broken switches/streams for homebases with no local API.
    """
    options = effective_options(entry, (node or device.base_node()).sn, device.sn)
    local = build_local_camera(entry, device, node, options)
    if local is None:
        return None
    if _explicit_local(options):
        return local
    ports = runtime_local_ports(entry).get(local.host)
    if ports:
        return local
    return None


async def _probe_host(host: str, timeout: float = PROBE_TIMEOUT) -> set[int]:
    async def check(port: int) -> int | None:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout
            )
        except Exception:  # noqa: BLE001 - probing must never break setup
            return None
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return port

    results = await asyncio.gather(*(check(port) for port in PROBE_PORTS))
    return {port for port in results if port is not None}


async def async_probe_local_hosts(
    hosts: Iterable[str | None], timeout: float = PROBE_TIMEOUT
) -> dict[str, set[int]]:
    """Probe each unique host once and return the open ports per host."""
    unique = sorted({host for host in hosts if host})
    if not unique:
        return {}
    results = await asyncio.gather(*(_probe_host(host, timeout) for host in unique))
    mapping = dict(zip(unique, results, strict=True))
    for host, ports in mapping.items():
        _LOGGER.debug("Local probe %s: %s", host, sorted(ports) or "no open ports")
    return mapping
