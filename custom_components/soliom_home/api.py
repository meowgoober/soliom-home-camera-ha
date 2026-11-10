"""Client for the Soliom / Xiongmai XM cloud HTTP API.

The Soliom Home app talks to a Xiongmai-hosted REST API.  Every call is a
POST to the regional API host with the operation selected by the ``Action``
header.  Requests are signed with an MD5 over ``timestamp + app_key +
app_secret`` and authenticated with a bearer token obtained from
``UserMultiLoginV2``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ACTION_DEVICE_LIST,
    ACTION_EVENT_LIST,
    ACTION_LOGIN,
    ACTION_MQTT_CONFIG,
    ACTION_PRODUCT_EVENT_TYPES,
    ACTION_REFRESH,
    API_LANG,
    API_VERSION,
    APP_KEY,
    APP_SECRET,
    DEFAULT_EVENT_HOURS,
    DEFAULT_EVENT_LIMIT,
    DEFAULT_HOST,
    HOST_DISCOVERY_URL,
    USER_AGENT,
)
from .crypto import api_signature, encrypt_passphrase

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=25)


class SoliomError(Exception):
    """Base error for the Soliom cloud API."""


class SoliomConnectionError(SoliomError):
    """Raised when the cloud cannot be reached."""


class SoliomAuthError(SoliomError):
    """Raised when credentials or tokens are rejected."""


class SoliomApiError(SoliomError):
    """Raised when the API returns a non-success result."""

    def __init__(self, code: int | None, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{message} (code {code})" if code else message)


class SoliomCloudApi:
    """Small async wrapper around the Soliom cloud API."""

    def __init__(
        self,
        hass,
        *,
        email: str,
        password: str,
        region: str,
        country_code: int,
        host: str | None = None,
        app_key: str = APP_KEY,
        app_secret: str = APP_SECRET,
    ) -> None:
        self._session = async_get_clientsession(hass, verify_ssl=True)
        self.email = email
        self.password = password
        self.region = region
        self.country_code = country_code
        self.host = (host or "").rstrip("/") or None
        self.app_key = app_key
        self.app_secret = app_secret

        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.token_expires_at: float = 0.0
        self._auth_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Low level transport
    # ------------------------------------------------------------------
    def _headers(self, action: str | None, token: str | None) -> dict[str, str]:
        timestamp = int(time.time() * 1000)
        headers = {
            "User-Agent": USER_AGENT,
            "Timestamp": str(timestamp),
            "Version": API_VERSION,
            "SecretId": self.app_key,
            "Signature": api_signature(timestamp, self.app_key, self.app_secret),
            "RequestId": str(uuid.uuid4()),
            "Content-Type": "application/json; charset=utf-8",
            "Accept-Charset": "utf-8",
            "Lang": API_LANG,
        }
        if action:
            headers["Action"] = action
        if token:
            headers["Authorization"] = token
        return headers

    async def _post(
        self,
        url: str,
        *,
        action: str | None,
        body: dict[str, Any],
        token: str | None = None,
    ) -> dict[str, Any]:
        try:
            async with self._session.post(
                url,
                json=body,
                headers=self._headers(action, token),
                timeout=REQUEST_TIMEOUT,
            ) as response:
                text = await response.text()
                status = response.status
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SoliomConnectionError(f"Error talking to {url}: {err}") from err

        try:
            data = json.loads(text)
        except (ValueError, TypeError) as err:
            raise SoliomError(
                f"Unexpected response from {url} (HTTP {status}): {text[:200]}"
            ) from err

        if not isinstance(data, dict):
            raise SoliomError(f"Unexpected response from {url}: {data!r}")

        if status >= 500:
            raise SoliomConnectionError(
                f"Cloud server error {status}: {data.get('msg', text[:200])}"
            )
        return data

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    async def async_discover_host(self) -> str:
        """Resolve the regional API host for this account's country code."""
        timestamp = int(time.time() * 1000)
        body = {
            "code": int(self.country_code),
            "env_id": "",
            "app_key": self.app_key,
            "sign": api_signature(timestamp, self.app_key, self.app_secret),
            "sign_ts": timestamp,
        }
        data = await self._post(HOST_DISCOVERY_URL, action=None, body=body)
        payload = data.get("payload") or {}
        host = payload.get("host") if isinstance(payload, dict) else None
        if not data.get("result") or not host:
            # Fall back to the known US host so a discovery hiccup does not
            # make the whole integration unusable.
            _LOGGER.debug("Host discovery failed: %s; using %s", data, DEFAULT_HOST)
            host = DEFAULT_HOST
        self.host = str(host).rstrip("/")
        return self.host

    async def async_login(self) -> None:
        """Perform a full e-mail/password login."""
        if not self.host:
            await self.async_discover_host()
        assert self.host

        body = {
            "user_id": self.email,
            "passphrase": encrypt_passphrase(self.password, self.app_secret),
            "region": self.region,
            "client_type": "android",
        }
        data = await self._post(self.host, action=ACTION_LOGIN, body=body, token=None)
        if not data.get("result"):
            raise SoliomAuthError(
                str(data.get("msg") or "Login rejected by Soliom cloud")
            )
        self._apply_login_payload(data.get("payload") or {})
        _LOGGER.debug("Logged in to Soliom cloud at %s", self.host)

    async def async_refresh_token(self) -> None:
        """Refresh the access token, falling back to a full login."""
        if not self.refresh_token:
            await self.async_login()
            return
        if not self.host:
            await self.async_discover_host()
        assert self.host
        data = await self._post(
            self.host,
            action=ACTION_REFRESH,
            body={"refresh_token": self.refresh_token},
            token=self.access_token,
        )
        if not data.get("result"):
            _LOGGER.debug("Token refresh rejected (%s); logging in again", data)
            await self.async_login()
            return
        self._apply_login_payload(data.get("payload") or {})

    def _apply_login_payload(self, payload: dict[str, Any]) -> None:
        access_token = payload.get("access_token")
        if not access_token:
            raise SoliomAuthError("Login response did not contain an access token")
        self.access_token = str(access_token)
        if payload.get("refresh_token"):
            self.refresh_token = str(payload["refresh_token"])
        expires_in = int(payload.get("expires_in") or 3600)
        # Renew a little early so long-running requests do not race expiry.
        self.token_expires_at = time.monotonic() + max(60, expires_in - 120)

    def invalidate_session(self) -> None:
        """Forget every token so the next request performs a full login."""
        self.access_token = None
        self.refresh_token = None
        self.token_expires_at = 0.0

    async def async_ensure_token(self) -> None:
        """Make sure a valid access token is available."""
        async with self._auth_lock:
            if self.access_token and time.monotonic() < self.token_expires_at:
                return
            if self.refresh_token:
                try:
                    await self.async_refresh_token()
                    return
                except SoliomAuthError:
                    raise
                except SoliomError as err:
                    _LOGGER.debug("Refresh failed (%s), performing full login", err)
            await self.async_login()

    # ------------------------------------------------------------------
    # API operations
    # ------------------------------------------------------------------
    async def async_request(
        self,
        action: str,
        body: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
        retry: bool = True,
    ) -> dict[str, Any]:
        """Execute an authenticated API action."""
        if authenticated:
            await self.async_ensure_token()
        if not self.host:
            await self.async_discover_host()

        data = await self._post(
            self.host or DEFAULT_HOST,
            action=action,
            body=body or {},
            token=self.access_token if authenticated else None,
        )
        if data.get("result"):
            return data

        code = data.get("code")
        message = str(data.get("msg") or "Unknown Soliom cloud error")
        if code == 100401:
            if retry and authenticated:
                async with self._auth_lock:
                    await self.async_refresh_token()
                return await self.async_request(
                    action, body, authenticated=authenticated, retry=False
                )
            raise SoliomAuthError(message)
        raise SoliomApiError(code if isinstance(code, int) else None, message)

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Return the raw device records for this account."""
        data = await self.async_request(ACTION_DEVICE_LIST)
        payload = data.get("payload")
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            devices = payload.get("device_list")
            if isinstance(devices, list):
                return [item for item in devices if isinstance(item, dict)]
        return []

    async def async_get_events(
        self,
        device_sns: list[str],
        *,
        hours: int = DEFAULT_EVENT_HOURS,
        limit: int = DEFAULT_EVENT_LIMIT,
    ) -> list[dict[str, Any]]:
        """Return recent events for the supplied device serial numbers."""
        now_ms = int(time.time() * 1000)
        body: dict[str, Any] = {
            "size": int(limit),
            "start_time": now_ms - hours * 60 * 60 * 1000,
            "end_time": now_ms,
            "sort": 1,
            "filters": {"event": [], "label": []},
        }
        if device_sns:
            body["devices"] = device_sns
        data = await self.async_request(ACTION_EVENT_LIST, body)
        payload = data.get("payload")
        if isinstance(payload, dict):
            history = payload.get("history_list")
            if isinstance(history, list):
                return [item for item in history if isinstance(item, dict)]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    async def async_get_product_event_types(
        self, product_ids: list[str]
    ) -> dict[int, str]:
        """Return event type id -> label mappings for the given products."""
        if not product_ids:
            return {}
        data = await self.async_request(
            ACTION_PRODUCT_EVENT_TYPES, {"product": product_ids}
        )
        payload = data.get("payload")
        mapping: dict[int, str] = {}
        if not isinstance(payload, dict):
            return mapping
        for entry in payload.get("events") or []:
            if isinstance(entry, dict) and entry.get("type") is not None:
                mapping[int(entry["type"])] = str(
                    entry.get("event") or entry.get("name") or entry["type"]
                )
        for entry in payload.get("detections") or []:
            if isinstance(entry, dict) and entry.get("type") is not None:
                mapping.setdefault(
                    int(entry["type"]),
                    str(entry.get("label") or entry.get("event") or entry["type"]),
                )
        return mapping

    async def async_get_mqtt_config(self) -> dict[str, Any]:
        """Return the MQTT broker settings the app would use.

        This is exposed for diagnostics and future push support; the
        integration itself polls the REST API.
        """
        data = await self.async_request(ACTION_MQTT_CONFIG)
        payload = data.get("payload")
        return payload if isinstance(payload, dict) else {}
