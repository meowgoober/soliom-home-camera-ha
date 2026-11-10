"""Data models and parsers for the Soliom Home integration."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _dig(data: Any, *path: str, default: Any = None) -> Any:
    """Safely walk nested dictionaries."""
    current = data
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _wifi_percent(value: int | None) -> int | None:
    """Normalise the Wi-Fi value reported by the cloud to 0-100.

    Older firmware reports 0-4 bars, while newer battery cameras report a
    negative RSSI in dBm (for example -63).
    """
    if value is None:
        return None
    if value < 0:
        # Map -100 dBm..-30 dBm to 0..100 %.
        return max(0, min(100, int(2 * (value + 100))))
    if value <= 4:
        return value * 25
    return max(0, min(100, value))


def is_private_address(value: str | None) -> bool:
    """Return True for a usable LAN address (never a public WAN IP)."""
    if not value:
        return False
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        # Hostnames are acceptable; they cannot be checked as private.
        return "." in str(value) and not str(value).replace(".", "").isdigit()
    return address.is_private and not address.is_loopback


@dataclass(slots=True)
class SoliomChannel:
    """One camera channel on a device."""

    sn: str
    name: str
    channel: int
    online: bool
    model: str | None = None
    battery: int | None = None
    charge_state: int = 0
    wifi_signal: int | None = None
    camera_on: bool | None = None
    light_status: int = 0
    product_id: str | None = None
    last_status_time: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def battery_level(self) -> int | None:
        if self.battery is None:
            return None
        return max(0, min(100, self.battery))

    @property
    def wifi_percent(self) -> int | None:
        return _wifi_percent(self.wifi_signal)


@dataclass(slots=True)
class SoliomNode:
    """A camera node: a standalone device or one child channel of a homebase."""

    sn: str
    base_sn: str
    name: str
    model: str | None
    channel: int
    online: bool
    battery: int | None
    charge_state: int
    wifi_signal: int | None
    camera_on: bool | None
    is_child: bool
    product_id: str | None = None
    last_status_time: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def battery_level(self) -> int | None:
        if self.battery is None:
            return None
        return max(0, min(100, self.battery))

    @property
    def wifi_percent(self) -> int | None:
        return _wifi_percent(self.wifi_signal)


@dataclass(slots=True)
class SoliomDevice:
    """A normalized Soliom device record."""

    sn: str
    name: str
    model: str
    did: str
    p2p_password: str
    service_string: str
    p2p_connect: int
    online: bool
    battery: int | None
    charge_state: int
    wifi_signal: int | None
    ip_addr: str | None
    lan_addr: str | None
    ssid: str | None
    storage_total: int
    storage_used: int
    storage_free: int
    storage_state: int
    firmware: str | None
    product_id: str | None
    category: int
    brand: str | None
    share_level: int
    device_type: int
    channels: list[SoliomChannel] = field(default_factory=list)
    last_status_time: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wifi_percent(self) -> int | None:
        """Return a 0-100 Wi-Fi quality value.

        The cloud reports 0-4 bars on most Xiongmai devices.  Some newer
        records already carry a percentage, so both shapes are handled.
        """
        return _wifi_percent(self.wifi_signal)

    @property
    def local_host(self) -> str | None:
        """Return the best candidate address for local LAN access.

        ``device_info.lan`` is the address the camera uses on the local
        network.  ``device_user.ip_addr`` is often the public WAN address of
        the home, which must never be used for RTSP/CGI.
        """
        for candidate in (self.lan_addr, self.ip_addr):
            if candidate and is_private_address(candidate):
                return str(candidate).strip()
        return None

    @property
    def is_homebase(self) -> bool:
        """Return True when this record is a base station with child cameras."""
        return any(channel.sn and channel.sn != self.sn for channel in self.channels)

    def base_node(self) -> SoliomNode:
        """Return a node that represents the device itself."""
        return SoliomNode(
            sn=self.sn,
            base_sn=self.sn,
            name=self.name,
            model=self.model,
            channel=0,
            online=self.online,
            battery=self.battery,
            charge_state=self.charge_state,
            wifi_signal=self.wifi_signal,
            camera_on=None,
            is_child=False,
            product_id=self.product_id,
            last_status_time=self.last_status_time,
            raw=self.raw,
        )

    def nodes(self) -> list[SoliomNode]:
        """Return the camera nodes exposed to Home Assistant.

        A homebase exposes one node per child camera; a standalone camera
        exposes a single node that is the device itself.
        """
        children = [
            channel for channel in self.channels if channel.sn and channel.sn != self.sn
        ]
        if not children:
            return [self.base_node()]
        return [
            SoliomNode(
                sn=channel.sn,
                base_sn=self.sn,
                name=channel.name,
                model=channel.model or self.model,
                channel=channel.channel,
                online=channel.online,
                battery=channel.battery,
                charge_state=channel.charge_state,
                wifi_signal=channel.wifi_signal,
                camera_on=channel.camera_on,
                is_child=True,
                product_id=channel.product_id,
                last_status_time=channel.last_status_time,
                raw=channel.raw,
            )
            for channel in children
        ]

    @property
    def storage_used_percent(self) -> float | None:
        if self.storage_total <= 0:
            return None
        return round(self.storage_used / self.storage_total * 100, 1)

    @property
    def storage_free_percent(self) -> float | None:
        if self.storage_total <= 0:
            return None
        return round(self.storage_free / self.storage_total * 100, 1)

    @property
    def battery_level(self) -> int | None:
        if self.battery is None:
            return None
        return max(0, min(100, self.battery))


def parse_device(record: dict[str, Any]) -> SoliomDevice | None:
    """Parse either the V2 nested record or the legacy flat record."""
    sn = str(
        record.get("device_sn")
        or record.get("sn")
        or _dig(record, "device_extend", "platform", "device_did")
        or record.get("device_did")
        or ""
    )
    if not sn:
        return None

    platform = _dig(record, "device_extend", "platform", default={}) or {}
    info = record.get("device_info") or {}
    user = record.get("device_user") or {}
    child_list = record.get("child_list") or record.get("camera_list") or []

    # ``device_info.CameraChannel`` is the authoritative channel map for a
    # homebase: [{channel: 0, sn: "...", model: "..."}].  The codec channel in
    # each child record is always 0, so it must not be used.
    channel_map: dict[str, int] = {}
    for entry in info.get("CameraChannel") or []:
        if isinstance(entry, dict) and entry.get("sn") is not None:
            channel_map[str(entry["sn"])] = _as_int(entry.get("channel"))

    channels: list[SoliomChannel] = []
    for index, child in enumerate(child_list):
        if not isinstance(child, dict):
            continue
        child_info = child.get("child_info") or {}
        child_sn = str(
            child.get("child_sn") or child.get("camera_sn") or child.get("sn") or sn
        )
        # Store channels 1-based, which is what Xiongmai RTSP/CGI expect.
        raw_channel = channel_map.get(child_sn)
        channel_number = raw_channel + 1 if raw_channel is not None else index + 1
        child_extend = child.get("child_extend") or {}
        channels.append(
            SoliomChannel(
                sn=child_sn,
                name=str(
                    child.get("child_name")
                    or child.get("camera_name")
                    or record.get("device_name")
                    or sn
                ),
                channel=channel_number,
                online=_as_bool(
                    child.get("child_online_status")
                    if child.get("child_online_status") is not None
                    else child.get("camera_online", 1)
                ),
                model=child.get("child_model") or child.get("camera_model"),
                battery=(
                    _as_int(child_info.get("battery"))
                    if child_info.get("battery") is not None
                    else None
                ),
                charge_state=_as_int(child_info.get("charge_state")),
                wifi_signal=(
                    _as_int(child_info.get("wifi_signal"))
                    if child_info.get("wifi_signal") is not None
                    else None
                ),
                camera_on=(
                    _as_bool(child_info.get("camera_on"))
                    if child_info.get("camera_on") is not None
                    else None
                ),
                light_status=_as_int(child_info.get("light_status")),
                product_id=child.get("child_product_id"),
                last_status_time=(
                    _as_int(child_extend.get("last_status_time")) or None
                ),
                raw=child,
            )
        )

    first_channel = channels[0] if channels else None
    has_children = any(channel.sn and channel.sn != sn for channel in channels)

    battery_raw = info.get("battery")
    if battery_raw is None and first_channel is not None and not has_children:
        # A homebase has no battery of its own; its children report theirs.
        battery_raw = first_channel.battery
    if battery_raw is None and not has_children:
        for child in child_list:
            if isinstance(child, dict) and child.get("battery") is not None:
                battery_raw = child.get("battery")
                break

    storage = info.get("storage") or {}
    total = _as_int(storage.get("total") or record.get("card_total"))
    used = _as_int(storage.get("used") or record.get("card_used"))
    free = _as_int(storage.get("free") or record.get("card_free"))
    if total and not free:
        free = max(0, total - used)

    # ``device_info.lan`` is the LAN address.  ``device_user.ip_addr`` is the
    # public WAN address reported by the app and must not be used for RTSP.
    lan_addr = info.get("lan") or record.get("lan")
    ip_addr = (
        info.get("ip_addr")
        or user.get("ip_addr")
        or record.get("ip_addr")
        or (first_channel.raw.get("ip_addr") if first_channel else None)
    )

    online_value = record.get("online_status")
    if online_value is None:
        online_value = record.get("device_online", 0)

    wifi_raw = info.get("wifi_signal")
    if wifi_raw is None:
        wifi_raw = record.get("wifi_signal")
    if wifi_raw is None and first_channel is not None:
        wifi_raw = first_channel.raw.get("wifi_signal")

    firmware = (
        record.get("device_version")
        or record.get("device_sub_ver")
        or record.get("firmware")
        or info.get("firmware")
    )

    return SoliomDevice(
        sn=sn,
        name=str(record.get("device_name") or record.get("name") or sn),
        model=str(
            record.get("device_model")
            or record.get("camera_model")
            or info.get("camera_model")
            or "Soliom camera"
        ),
        did=str(platform.get("device_did") or record.get("device_did") or sn),
        p2p_password=str(
            platform.get("p2p_password") or record.get("p2p_password") or ""
        ),
        service_string=str(
            platform.get("service_string") or record.get("service_string") or ""
        ),
        p2p_connect=_as_int(
            platform.get("p2p_connect") or record.get("p2p_connect") or 0
        ),
        online=_as_bool(online_value),
        battery=_as_int(battery_raw) if battery_raw is not None else None,
        charge_state=_as_int(info.get("charge_state") or record.get("charge_state")),
        wifi_signal=_as_int(wifi_raw) if wifi_raw is not None else None,
        ip_addr=str(ip_addr) if ip_addr else None,
        lan_addr=str(lan_addr) if lan_addr else None,
        ssid=info.get("cur_ssid") or record.get("cur_ssid"),
        storage_total=total,
        storage_used=used,
        storage_free=free,
        storage_state=_as_int(storage.get("card_state") or record.get("card_state")),
        firmware=str(firmware) if firmware else None,
        product_id=record.get("product_id"),
        category=_as_int(record.get("category") or record.get("device_type")),
        brand=record.get("brand"),
        share_level=_as_int(record.get("share_level"), 1),
        device_type=_as_int(record.get("device_type")),
        channels=channels,
        last_status_time=(
            _as_int(_dig(record, "device_extend", "last_status_time")) or None
        ),
        raw=record,
    )


def _ms_to_datetime(value: Any) -> datetime | None:
    if value in (None, 0, ""):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds > 10_000_000_000:  # milliseconds
        seconds /= 1000
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(slots=True)
class SoliomEvent:
    """A normalized cloud event."""

    event_id: str
    sn: str
    type_id: int
    name: str
    start_time: datetime | None
    end_time: datetime | None
    thumb_url: str | None
    video_url: str | None
    is_read: bool
    sub_type: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def event_type(self) -> str:
        return self.name or f"event_{self.type_id}"


def normalise_event_name(name: str) -> str:
    """Turn cloud event names such as ``MotionEvent`` into ``motion``."""
    value = (name or "").strip()
    if not value:
        return ""
    if value.lower().endswith("event"):
        value = value[:-5]
    value = re.sub(r"(?<!^)(?=[A-Z])", "_", value)
    return value.lower().strip("_")


def parse_event(record: dict[str, Any]) -> SoliomEvent | None:
    """Parse an event record from ``GetEventList``.

    Newer cloud responses nest the useful fields under ``payload`` and expose
    the event name as ``event`` instead of ``name``.  Both shapes are
    supported.
    """
    payload = record.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    sn = str(record.get("sn") or record.get("device_sn") or payload.get("sn") or "")
    if not sn:
        return None
    start_time = _ms_to_datetime(
        record.get("start_time")
        or payload.get("start_time")
        or payload.get("timestamp")
        or record.get("timestamp")
        or record.get("time")
    )
    raw_name = (
        record.get("name")
        or record.get("event")
        or payload.get("name")
        or payload.get("event")
        or ""
    )
    event_id = str(
        record.get("event_id")
        or record.get("uuid")
        or record.get("id")
        or f"{sn}:{record.get('start_time')}:{record.get('type')}"
    )
    cloud_info = record.get("cloud_info") or payload.get("cloud_info") or {}
    if not isinstance(cloud_info, dict):
        cloud_info = {}
    labels = payload.get("label") or record.get("label") or []
    sub_type = record.get("sub_type")
    if sub_type is None and isinstance(labels, list) and labels:
        sub_type = labels[-1]
    video_url = (
        cloud_info.get("video_url")
        or record.get("video_url")
        or payload.get("video_url")
        or payload.get("video_path")
        or ""
    )
    return SoliomEvent(
        event_id=event_id,
        sn=sn,
        type_id=_as_int(
            record.get("type")
            if record.get("type") is not None
            else payload.get("type")
        ),
        name=normalise_event_name(str(raw_name)),
        start_time=start_time,
        end_time=_ms_to_datetime(record.get("end_time") or payload.get("end_time")),
        thumb_url=(
            record.get("thumb_path")
            or record.get("thumb_url")
            or payload.get("thumb_path")
            or payload.get("thumb_url")
        ),
        video_url=str(video_url) if video_url else None,
        is_read=_as_bool(record.get("isRead") or record.get("is_read")),
        sub_type=_as_int(sub_type),
        raw=record,
    )


def event_type_label(event_type_id: int, mapping: dict[int, str] | None = None) -> str:
    """Resolve a stable, human-readable event type string."""
    if mapping and event_type_id in mapping:
        return mapping[event_type_id]
    from .const import EVENT_TYPE_NAMES

    return EVENT_TYPE_NAMES.get(event_type_id, f"event_{event_type_id}")
