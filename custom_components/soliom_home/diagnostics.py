"""Diagnostics support for Soliom Home."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PASSWORD
from .device import SoliomDevice

_LUT = bytes.fromhex(
    "4959433db5bf6da347534f6165e371e9677f02030badb3892b2f35c16b8b959711e5a70deff1050783fb9d3bc5c713171d1f2529d3df"
)


def _p2p_probe(device: SoliomDevice) -> dict[str, Any]:
    """Non-secret P2P facts needed to plan a PPCS client (no password, no raw string)."""
    raw = (device.service_string or "").encode("latin-1", "replace")
    head, sep, suffix = raw.partition(b":")
    out = bytearray()
    run = 0
    for i in range(min(len(head) // 2, 256)):
        v = (((head[2 * i] << 4) + head[2 * i + 1] - 0x451) ^ _LUT[i % len(_LUT)] ^ 0x39 ^ run) & 0xFF
        out.append(v)
        run ^= v
    text = bytes(out).split(b"\x00", 1)[0].decode("latin-1", "replace")
    return {
        "service_string_len": len(raw),
        "has_colon_key_suffix": bool(sep),
        "colon_suffix_len": len(suffix),
        "decoded_supernodes": [h for h in text.split(",") if h],
        "p2p_password_len": len(device.p2p_password or ""),
        "p2p_connect": device.p2p_connect,
    }


def _device_dump(device: SoliomDevice) -> dict[str, Any]:
    return {
        "sn": device.sn,
        "name": device.name,
        "model": device.model,
        "did": device.did,
        "p2p_probe": _p2p_probe(device),
        "online": device.online,
        "battery": device.battery,
        "charge_state": device.charge_state,
        "wifi_signal": device.wifi_signal,
        "ip_addr": device.ip_addr,
        "lan_addr": device.lan_addr,
        "local_host": device.local_host,
        "ssid": device.ssid,
        "storage_total": device.storage_total,
        "storage_used": device.storage_used,
        "storage_free": device.storage_free,
        "storage_state": device.storage_state,
        "firmware": device.firmware,
        "product_id": device.product_id,
        "category": device.category,
        "share_level": device.share_level,
        "channels": [
            {
                "sn": channel.sn,
                "name": channel.name,
                "channel": channel.channel,
                "online": channel.online,
            }
            for channel in device.channels
        ],
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a Soliom Home config entry."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    data = coordinator.data

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": {
                key: ("**REDACTED**" if key == CONF_PASSWORD else value)
                for key, value in entry.data.items()
            },
            "options": {
                "scan_interval": entry.options.get("scan_interval"),
                "local_device_count": len(entry.options.get("devices") or {}),
            },
        },
        "api": {
            "host": runtime.api.host,
            "logged_in": runtime.api.access_token is not None,
        },
        "devices": (
            {sn: _device_dump(device) for sn, device in data.devices.items()}
            if data
            else {}
        ),
        "events": (
            {
                "count": len(data.events),
                "by_device": {
                    sn: len(events) for sn, events in data.events_by_sn.items()
                },
                "latest": [
                    {
                        "event_id": event.event_id,
                        "sn": event.sn,
                        "type": event.type_id,
                        "name": data.label_for(event),
                        "start_time": (
                            event.start_time.isoformat() if event.start_time else None
                        ),
                    }
                    for event in data.events[:20]
                ],
            }
            if data
            else {}
        ),
    }
