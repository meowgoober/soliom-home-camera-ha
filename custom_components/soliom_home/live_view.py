"""HTTP endpoint that serves a Soliom camera as a live MPEG-TS stream.

Home Assistant's stream component and go2rtc both run inside the HA process
environment, so the camera entity hands them ``http://127.0.0.1:<port>/...``.
The URL carries a random per-start token because these clients cannot send HA
credentials; the token is only ever given out through ``stream_source``.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import TYPE_CHECKING

from aiohttp import web

try:  # moved between Home Assistant releases
    from homeassistant.helpers.http import HomeAssistantView
except ImportError:  # pragma: no cover
    from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN
from .live import LIVE_PATH
from .mpegts import TSMuxer

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .live import SoliomLiveManager, Subscription

_LOGGER = logging.getLogger(__name__)

FIRST_FRAME_TIMEOUT = 25.0  # camera must produce a key frame within this
FRAME_GAP_TIMEOUT = 30.0  # end the response if the camera goes silent


def find_manager(hass: HomeAssistant, sn: str) -> SoliomLiveManager | None:
    managers = hass.data.get(DOMAIN, {}).get("live_managers", {})
    for manager in managers.values():
        if manager.can_stream(sn):
            return manager
    return None


async def stream_ts(request: web.Request, sub: Subscription, audio=None) -> web.StreamResponse:
    """Pump frames from ``sub`` to the client as MPEG-TS until either side stops."""
    mux: TSMuxer | None = None
    t0 = 0.0
    resp: web.StreamResponse | None = None
    try:
        while True:
            timeout = FRAME_GAP_TIMEOUT if mux else FIRST_FRAME_TIMEOUT
            frame = await sub.get(timeout)
            if frame is None:
                break
            if frame.kind == "audio":
                rel = frame.t - t0
                if mux is not None and rel >= 0:
                    data = mux.mux_audio(frame.data, rel)
                    if data:
                        assert resp is not None
                        await resp.write(data)
                continue
            if mux is None:
                if not frame.key:
                    continue  # always start on a key frame
                mux = TSMuxer(frame.codec, audio() if audio else None)
                t0 = frame.t
                resp = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "video/mp2t",
                        "Cache-Control": "no-cache, no-store",
                    },
                )
                await resp.prepare(request)
            assert resp is not None
            await resp.write(mux.mux(frame.data, max(0.0, frame.t - t0), frame.key))
    except (ConnectionResetError, ConnectionError):
        pass
    finally:
        sub.close()
    if resp is None:
        return web.Response(status=504, text="camera produced no video")
    try:
        await resp.write_eof()
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
        pass
    return resp


class SoliomLiveView(HomeAssistantView):
    """GET /api/soliom_home/live/<token>/<camera serial>.ts"""

    url = LIVE_PATH + "/{token}/{sn}.ts"
    name = "api:soliom_home:live"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, token: str, sn: str) -> web.StreamResponse:
        manager = find_manager(self.hass, sn)
        if manager is None:
            return web.Response(status=404, text="unknown camera")
        if not hmac.compare_digest(token.encode(), manager.token.encode()):
            return web.Response(status=403, text="bad token")
        sub = manager.subscribe(sn)
        if sub is None:
            return web.Response(status=404, text="camera cannot stream")
        _LOGGER.debug("live view opened for %s", sn)
        return await stream_ts(request, sub, lambda: manager.audio_params(sn))
