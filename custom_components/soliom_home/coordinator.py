"""Coordinator for Soliom Home cloud data."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import (
    SoliomAuthError,
    SoliomCloudApi,
    SoliomConnectionError,
    SoliomError,
)
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_EVENT_HOURS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .device import (
    SoliomDevice,
    SoliomEvent,
    SoliomNode,
    event_type_label,
    parse_device,
    parse_event,
)

_LOGGER = logging.getLogger(__name__)

# Self-healing: when a poll fails we force a brand new login, retry within a
# bounded time, and poll faster than usual until the cloud answers again.
RECOVERY_TIMEOUT = 45  # seconds allowed for one "re-login + refetch" attempt
RECOVERY_RETRY_DELAYS = (0, 5, 12)  # pauses before each login attempt inside it
FAST_RETRY_SECONDS = 30  # poll interval while unavailable
REAUTH_AFTER_FAILURES = 5  # rejected logins in a row ...
REAUTH_AFTER_SECONDS = 600  # ... spanning at least this long => ask for reauth


@dataclass(slots=True)
class SoliomData:
    """Everything the entities need from one cloud poll."""

    devices: dict[str, SoliomDevice] = field(default_factory=dict)
    nodes: dict[str, SoliomNode] = field(default_factory=dict)
    events: list[SoliomEvent] = field(default_factory=list)
    events_by_sn: dict[str, list[SoliomEvent]] = field(default_factory=dict)
    event_type_labels: dict[int, str] = field(default_factory=dict)
    new_events: list[SoliomEvent] = field(default_factory=list)

    def events_for(self, sn: str) -> list[SoliomEvent]:
        return self.events_by_sn.get(sn, [])

    def latest_event(self, sn: str) -> SoliomEvent | None:
        events = self.events_for(sn)
        return events[0] if events else None

    def label_for(self, event: SoliomEvent) -> str:
        return event.name or event_type_label(event.type_id, self.event_type_labels)

    def node(self, sn: str) -> SoliomNode | None:
        return self.nodes.get(sn)

    def device_for(self, node_or_sn: SoliomNode | str) -> SoliomDevice | None:
        sn = node_or_sn.base_sn if isinstance(node_or_sn, SoliomNode) else node_or_sn
        return self.devices.get(sn)

    def camera_nodes(self) -> list[tuple[SoliomDevice, SoliomNode]]:
        """Return every camera node paired with its owning cloud device."""
        result: list[tuple[SoliomDevice, SoliomNode]] = []
        for device in self.devices.values():
            result.extend((device, node) for node in device.nodes())
        return result


class SoliomDataUpdateCoordinator(DataUpdateCoordinator[SoliomData]):
    """Poll the Soliom cloud for device status and events."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: SoliomCloudApi
    ) -> None:
        scan_interval = int(
            entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=max(30, scan_interval)),
            config_entry=entry,
        )
        self.api = api
        self._seen_event_ids: set[str] = set()
        self._event_type_labels: dict[int, str] = {}
        self._event_types_loaded_for: set[str] = set()
        self._normal_interval = self.update_interval
        self._login_rejections = 0
        self._first_rejection_at: float | None = None

    async def async_setup(self) -> None:
        """Log in before the first refresh so errors surface cleanly."""
        try:
            await self.api.async_ensure_token()
        except SoliomAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SoliomConnectionError as err:
            raise ConfigEntryNotReady(str(err)) from err

    async def _async_update_data(self) -> SoliomData:
        try:
            async with asyncio.timeout(RECOVERY_TIMEOUT):
                data = await self._fetch()
        except (SoliomError, TimeoutError) as err:
            _LOGGER.warning(
                "Soliom update failed (%s: %s); forcing a fresh login",
                type(err).__name__,
                err,
            )
            data = await self._async_recover(err)
        except Exception as err:
            _LOGGER.exception("Unexpected error while polling Soliom cloud")
            raise UpdateFailed(str(err)) from err
        self._mark_healthy()
        return data

    async def _async_recover(self, first_error: Exception) -> SoliomData:
        """Throw away the session, log in again and refetch, within a timeout."""
        last_error: Exception = first_error
        rejected = False
        try:
            async with asyncio.timeout(RECOVERY_TIMEOUT):
                for delay in RECOVERY_RETRY_DELAYS:
                    if delay:
                        await asyncio.sleep(delay)
                    self.api.invalidate_session()
                    try:
                        await self.api.async_login()
                        data = await self._fetch(allow_empty=True)
                    except SoliomAuthError as err:
                        rejected, last_error = True, err
                    except SoliomError as err:
                        rejected, last_error = False, err
                    else:
                        _LOGGER.info("Soliom cloud connection restored")
                        return data
        except TimeoutError:
            last_error = TimeoutError(
                f"re-login did not finish within {RECOVERY_TIMEOUT}s"
            )
            rejected = False

        if rejected:
            now = time.monotonic()
            self._login_rejections += 1
            self._first_rejection_at = self._first_rejection_at or now
            if (
                self._login_rejections >= REAUTH_AFTER_FAILURES
                and now - self._first_rejection_at >= REAUTH_AFTER_SECONDS
            ):
                raise ConfigEntryAuthFailed(str(last_error)) from last_error
        else:
            self._login_rejections = 0
            self._first_rejection_at = None

        self._poll_faster()
        raise UpdateFailed(f"Soliom cloud unavailable, will retry: {last_error}") from last_error

    def _poll_faster(self) -> None:
        if self._normal_interval and (
            not self.update_interval
            or self.update_interval > timedelta(seconds=FAST_RETRY_SECONDS)
        ):
            self.update_interval = timedelta(seconds=FAST_RETRY_SECONDS)

    def _mark_healthy(self) -> None:
        self._login_rejections = 0
        self._first_rejection_at = None
        if self.update_interval != self._normal_interval:
            self.update_interval = self._normal_interval

    async def _fetch(self, allow_empty: bool = False) -> SoliomData:
        raw_devices = await self.api.async_get_devices()
        if not raw_devices and not allow_empty and self.data and self.data.devices:
            # An expired session can look like "no devices"; treat as a failure
            # so we re-login instead of marking everything unavailable.
            raise SoliomError("cloud returned no devices")
        devices: dict[str, SoliomDevice] = {}
        nodes: dict[str, SoliomNode] = {}
        for raw in raw_devices:
            parsed = parse_device(raw)
            if parsed is not None:
                devices[parsed.sn] = parsed
                # Include the base node as well; a homebase's children are
                # the camera entities, but the base still has connectivity.
                base = parsed.base_node()
                nodes[base.sn] = base
                for node in parsed.nodes():
                    nodes[node.sn] = node

        product_ids = sorted(
            {device.product_id for device in devices.values() if device.product_id}
        )
        missing_products = [
            product_id
            for product_id in product_ids
            if product_id not in self._event_types_loaded_for
        ]
        if missing_products:
            try:
                labels = await self.api.async_get_product_event_types(missing_products)
                self._event_type_labels.update(labels)
                self._event_types_loaded_for.update(missing_products)
            except SoliomError as err:
                _LOGGER.debug("Could not load event type labels: %s", err)

        events: list[SoliomEvent] = []
        if nodes:
            # Homebase child cameras have their own serial numbers; querying
            # only the base SN returns no events.
            raw_events = await self.api.async_get_events(
                list(nodes), hours=DEFAULT_EVENT_HOURS
            )
            for raw in raw_events:
                parsed_event = parse_event(raw)
                if parsed_event is not None and parsed_event.sn in nodes:
                    events.append(parsed_event)

        events.sort(
            key=lambda event: event.start_time or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

        current_ids = {event.event_id for event in events}
        new_events = [
            event for event in events if event.event_id not in self._seen_event_ids
        ]
        if self._seen_event_ids:
            # Keep memory bounded; only events in the current window matter.
            self._seen_event_ids &= current_ids
        self._seen_event_ids.update(current_ids)

        events_by_sn: dict[str, list[SoliomEvent]] = {sn: [] for sn in nodes}
        for event in events:
            events_by_sn.setdefault(event.sn, []).append(event)

        return SoliomData(
            devices=devices,
            nodes=nodes,
            events=events,
            events_by_sn=events_by_sn,
            event_type_labels=dict(self._event_type_labels),
            new_events=new_events,
        )
