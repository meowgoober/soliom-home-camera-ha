"""On-demand live view for homebase child cameras (EXPERIMENTAL).

A background thread keeps one PPCS session to the homebase while at least one
camera is being watched.  Frames are demultiplexed by child serial, fanned out
to subscribers (the HTTP view), and the session shuts itself down after a short
idle period.  Cameras are started with the app's own "live add" command and
stopped with "live remove"/"live close", so battery cameras are only awake
while somebody is looking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import struct
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .ppcs import (
    CMD_LIVE_ADD,
    CMD_LIVE_CLOSE,
    CMD_LIVE_ENABLE,
    CMD_HEART_BEATS,
    CMD_LIVE_KEEPALIVE,
    CMD_LIVE_REMOVE,
    CMD_LOGIN,
    ChannelStream,
    FrameAssembler,
    LiveFrame,
    WanConn,
    aes_ecb_dec,
    build_cmd,
    decode_init_string,
    guess_codec,
    login_body,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import SoliomDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 2.0
STALL_SECONDS = 20.0  # no video from any camera => rebuild the session
CAMERA_RESTART_SECONDS = 30.0  # one camera silent while others flow => re-add it
SESSION_LINGER_SECONDS = 30.0  # keep the session after the last camera stops
CAMERA_LINGER_SECONDS = 20.0  # keep a camera running after its last viewer left
QUEUE_FRAMES = 150
VIDEO_KEEPALIVE = 2  # value the base advertises as videoKeepAlive_2
DEFAULT_STREAM_TYPE = 1  # 1 = 720p H.264 sub-stream, 0 = 4K H.265
LIVE_PATH = "/api/soliom_home/live"


@dataclass(slots=True)
class Creds:
    sn: str
    did: str
    password: str
    service_string: str


class Subscription:
    """A viewer's queue of frames for one camera."""

    def __init__(self, manager: SoliomLiveManager, sn: str) -> None:
        self._manager = manager
        self.sn = sn
        self.queue: asyncio.Queue[LiveFrame] = asyncio.Queue(QUEUE_FRAMES)
        self.need_key = False
        self.closed = False

    def push(self, frame: LiveFrame) -> None:
        if self.closed:
            return
        if self.need_key and not frame.key:
            return
        try:
            self.queue.put_nowait(frame)
            self.need_key = False
        except asyncio.QueueFull:
            # Viewer too slow: drop everything and resume at the next key frame.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.need_key = True

    async def get(self, timeout: float) -> LiveFrame | None:
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except TimeoutError:
            return None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._manager._release(self)


class _Session(threading.Thread):
    """Blocking PPCS session for one homebase; owns a socket and an ffmpeg-free loop."""

    def __init__(self, manager: SoliomLiveManager, base_sn: str) -> None:
        super().__init__(name=f"soliom-live-{base_sn}", daemon=True)
        self.manager = manager
        self.base_sn = base_sn
        self._wlock = threading.Lock()
        self._wanted: dict[str, int] = {}
        self._pending: list[tuple[str, str, int]] = []
        self._stop_evt = threading.Event()
        self._closing = False
        self._finished = False

    # -- called from the event loop ---------------------------------------
    @property
    def finished(self) -> bool:
        return self._finished

    def want(self, sn: str, channel: int) -> bool:
        """Ask for a camera. False when the thread is already shutting down."""
        with self._wlock:
            if self._closing:
                return False
            if sn not in self._wanted:
                self._wanted[sn] = channel
                self._pending.append(("add", sn, channel))
            return True

    def unwant(self, sn: str) -> None:
        with self._wlock:
            if self._wanted.pop(sn, None) is not None:
                self._pending.append(("remove", sn, 0))

    def stop(self) -> None:
        self._stop_evt.set()

    # -- thread body --------------------------------------------------------
    def run(self) -> None:
        backoff = 3.0
        try:
            while not self._stop_evt.is_set():
                started = time.monotonic()
                try:
                    clean = self._run_once()
                except Exception as err:  # noqa: BLE001 - keep retrying while wanted
                    _LOGGER.warning(
                        "Soliom live session for %s failed: %s: %s",
                        self.base_sn,
                        type(err).__name__,
                        err,
                    )
                    clean = False
                if clean:
                    with self._wlock:
                        if not self._wanted or self._stop_evt.is_set():
                            self._closing = True
                            break
                    continue  # someone asked for a camera while we were idling out
                ran = time.monotonic() - started
                backoff = 3.0 if ran > 60 else 1.0 if ran > 5 else min(backoff * 2, 30.0)
                if self._stop_evt.wait(backoff):
                    break
                with self._wlock:
                    if not self._wanted:
                        self._closing = True
                        break
        finally:
            with self._wlock:
                self._closing = True
            self._finished = True
            _LOGGER.debug("Soliom live session thread for %s ended", self.base_sn)

    def _send(self, conn: WanConn, cmd: int, body: dict, creds: Creds, enc: int = 1) -> None:
        conn.send(0, build_cmd(cmd, body, creds.password, enc))

    def _live_body(
        self, sns: list[tuple[str, int]], stream_type: int | None, keepalive: int | None = None
    ) -> dict:
        first_sn, first_ch = sns[0]
        items = []
        for sn, _ch in sns:
            item = {"sn": sn}
            if stream_type is not None:
                item["streamType"] = stream_type
            items.append(item)
        body = {"camera_sn": first_sn, "channel": first_ch, "list": items}
        if keepalive is not None:
            body["videoKeepAlive_2"] = keepalive  # puts the base into keepalive mode
        return body

    def _run_once(self) -> bool:
        """One connected session. Returns True when it ended because it went idle."""
        from pyibaby import protocol as P

        mgr = self.manager
        creds = mgr.creds(self.base_sn)
        stream_type = mgr.stream_type

        conn = WanConn(timeout=8.0)
        try:
            _LOGGER.debug("Soliom live: connecting to %s", creds.did)
            conn.connect_wan(
                creds.did, decode_init_string(creds.service_string), timeout=20.0,
                allow_relay=False,
            )
            _LOGGER.info("Soliom live session up with %s (%s)", creds.did, conn.peer)
            for enc in (0, 1):
                conn.send(
                    0, build_cmd(CMD_LOGIN, login_body(creds.sn, creds.password), creds.password, enc)
                )
                conn.pump(1.0)

            chans = {0: ChannelStream(), 1: ChannelStream(), 2: ChannelStream()}
            asm = FrameAssembler(creds.password)
            asm_a = FrameAssembler(creds.password, model=18)
            session_start = time.monotonic()
            ctl_logged = 0
            seen_types: set = set()
            last_ack = last_retx = last_hb = last_check = last_alive = 0.0
            now = time.monotonic()
            last_video = now
            cam_seen: dict[str, float] = {}
            idle_since: float | None = None

            with self._wlock:
                self._pending.clear()
                initial = list(self._wanted.items())
            if initial:
                self._send(conn, CMD_LIVE_ADD, self._live_body(initial, stream_type), creds)
                self._send(
                    conn, CMD_LIVE_ENABLE,
                    self._live_body(initial, stream_type, VIDEO_KEEPALIVE), creds,
                )
                cam_seen = {sn: now for sn, _ in initial}

            for ch, stream in chans.items():  # data received while pumping during setup
                for idx, chunk in sorted(conn.rx.get(ch, {}).items()):
                    stream.feed(idx, chunk)
            conn.rx.clear()

            conn._unacked.clear()  # setup commands were answered; stop re-sending them
            wanted_now: dict[str, int] = dict(initial)
            while not self._stop_evt.is_set():
                try:
                    data, _ = conn.sock.recvfrom(4096)
                except TimeoutError:
                    data = None
                now = time.monotonic()
                if data:
                    t, ch, idx, payload = P.parse(data)
                    if t == P.MSG_DRW:
                        conn.rx_seen.setdefault(ch, set()).add(idx)  # ack everything
                        if ch in chans:
                            chans[ch].feed(idx, payload)
                    elif t == P.MSG_ALIVE:
                        conn.sock.sendto(P.pppp(P.MSG_ALIVE_ACK), conn.peer)
                    elif t == P.MSG_DRW_ACK and len(payload) >= 6:
                        conn._handle_drw_ack(payload[1], payload)  # parse() gives ch=None here
                    elif t not in (P.MSG_CLOSE,) and t not in seen_types:
                        seen_types.add(t)
                        _LOGGER.warning("Soliom live: other PPPP packet type=%s %s", t, data[:32].hex())
                    if t == P.MSG_CLOSE:
                        raise ConnectionError(
                            f"base closed the session after {now - session_start:.0f}s "
                            f"(last video {now - last_video:.0f}s ago, cameras {sorted(wanted_now)})"
                        )
                for c in chans.values():
                    c.skip_gap(now)
                for hdr, payload in chans[1].packets():
                    got = asm.feed(hdr, payload)
                    if got:
                        sn, ftype, fps, blob = got
                        if sn:
                            last_video = now
                            cam_seen[sn] = now
                            codec = guess_codec(blob)
                            key = codec != "unknown"
                            mgr.loop.call_soon_threadsafe(
                                mgr._on_frame, self.base_sn, sn,
                                LiveFrame(0.0, key, codec if key else "", fps or 15, blob),
                            )
                for hdr, payload in chans[2].packets():
                    got = asm_a.feed(hdr, payload)
                    if got and got[0]:
                        mgr.loop.call_soon_threadsafe(
                            mgr._on_frame, self.base_sn, got[0],
                            LiveFrame(0.0, False, "aac", 0, got[3], "audio"),
                        )
                for hdr, payload in chans[0].packets():
                    ctl_logged += 1
                    level = logging.WARNING if ctl_logged <= 25 else logging.DEBUG  # TEMP: visible at default log level
                    if _LOGGER.isEnabledFor(level):
                        body = aes_ecb_dec(creds.password, payload) if hdr[4] & 15 == 1 else payload
                        _LOGGER.log(
                            level, "Soliom live reply cmd=%d %s",
                            struct.unpack_from("<H", hdr, 14)[0], (body or payload)[:200],
                        )
                if now - last_alive > 2.0:  # PPPP-level keepalive from our side too
                    conn.sock.sendto(P.pppp(P.MSG_ALIVE), conn.peer)
                    last_alive = now
                if now - last_ack > 0.1:
                    conn._flush_acks()
                    last_ack = now
                if now - last_retx > 0.4 and conn._unacked:
                    conn._retransmit()
                    last_retx = now

                with self._wlock:
                    cmds, self._pending = self._pending, []
                    wanted = dict(self._wanted)
                wanted_now = wanted
                for action, sn, ch in cmds:
                    if action == "add":
                        _LOGGER.debug("Soliom live: add %s", sn)
                        self._send(conn, CMD_LIVE_ADD, self._live_body([(sn, ch)], stream_type), creds)
                        self._send(
                            conn, CMD_LIVE_ENABLE,
                            self._live_body([(sn, ch)], stream_type, VIDEO_KEEPALIVE), creds,
                        )
                        cam_seen[sn] = now
                    else:
                        _LOGGER.debug("Soliom live: remove %s", sn)
                        self._send(conn, CMD_LIVE_REMOVE, self._live_body([(sn, 0)], None), creds)
                        cam_seen.pop(sn, None)

                if now - last_hb > HEARTBEAT_SECONDS:
                    conn.sock.sendto(P.pppp(P.MSG_ALIVE), conn.peer)  # transport-level ALIVE from us
                    if wanted:
                        # The app's homebase heartbeat is {gateway_sn, channel} but the channel
                        # (and whether camera_sn is included) is unknown: send every plausible shape.
                        bodies = [{"gateway_sn": creds.sn, "channel": 0}]
                        for sn, ch in wanted.items():
                            bodies.append({"gateway_sn": creds.sn, "channel": ch})
                            bodies.append({"gateway_sn": creds.sn, "camera_sn": sn, "channel": ch})
                        bodies += [{**b, "videoKeepAlive_2": VIDEO_KEEPALIVE} for b in list(bodies)]
                        sent: set[str] = set()
                        for body in bodies:
                            key = json.dumps(body, sort_keys=True)
                            if key not in sent:
                                sent.add(key)
                                self._send(conn, CMD_LIVE_KEEPALIVE, body, creds)
                        self._send(conn, CMD_LIVE_KEEPALIVE, bodies[0], creds, enc=0)
                        # generic session heartbeat (HEART_BEATS), body shape unknown
                        self._send(conn, CMD_HEART_BEATS, {"channel": 0}, creds)
                        self._send(conn, CMD_HEART_BEATS, {"gateway_sn": creds.sn, "channel": 0}, creds)
                    last_hb = now

                if now - last_check > 5.0:
                    last_check = now
                    for sn, ch in wanted.items():
                        if now - cam_seen.get(sn, 0) > CAMERA_RESTART_SECONDS:
                            _LOGGER.debug("Soliom live: %s silent, re-adding", sn)
                            self._send(conn, CMD_LIVE_ADD, self._live_body([(sn, ch)], stream_type), creds)
                            cam_seen[sn] = now
                    if wanted and now - last_video > STALL_SECONDS:
                        raise TimeoutError(f"no video for {STALL_SECONDS:.0f}s")

                if wanted:
                    idle_since = None
                else:
                    idle_since = idle_since or now
                    if now - idle_since > SESSION_LINGER_SECONDS:
                        try:
                            self._send(conn, CMD_LIVE_CLOSE, {"channel": 0, "list": []}, creds)
                            conn.pump(0.3)
                        except OSError:
                            pass
                        return True
            return True
        finally:
            try:
                conn.close()
            except OSError:
                pass


class SoliomLiveManager:
    """Event-loop side: reference counting, frame fan-out and session lifecycle."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: SoliomDataUpdateCoordinator,
        token: str,
        stream_type: int = DEFAULT_STREAM_TYPE,
    ) -> None:
        self.hass = hass
        self.loop = hass.loop
        self.coordinator = coordinator
        self.token = token
        self.stream_type = stream_type
        self._sessions: dict[str, _Session] = {}
        self._subs: dict[str, set[Subscription]] = {}
        self._gop: dict[str, list[LiveFrame]] = {}
        self._clock: dict[str, tuple[float, float]] = {}  # sn -> (start, last stream time)
        self._base_of: dict[str, str] = {}
        self._audio_seen: dict[str, float] = {}
        self._camera_timers: dict[str, asyncio.TimerHandle] = {}
        self._closed = False

    # -- lookups ---------------------------------------------------------
    def _node_device(self, sn: str):
        data = self.coordinator.data
        if not data:
            return None, None
        node = data.nodes.get(sn)
        if node is None or not node.is_child:
            return None, None
        device = data.devices.get(node.base_sn)
        if device is None or not (device.did and device.p2p_password and device.service_string):
            return None, None
        return node, device

    def can_stream(self, sn: str) -> bool:
        return self._node_device(sn)[0] is not None

    def stream_url(self, sn: str) -> str | None:
        if not self.can_stream(sn):
            return None
        port = getattr(self.hass.http, "server_port", 8123)
        return f"http://127.0.0.1:{port}{LIVE_PATH}/{self.token}/{sn}.ts"

    def audio_params(self, sn: str) -> tuple[int, int] | None:
        """(sample_rate, channels) when this camera is sending AAC audio, else None."""
        seen = self._audio_seen.get(sn)
        if not seen or time.monotonic() - seen > 5.0:
            return None
        _node, device = self._node_device(sn)
        raw = getattr(device, "raw", None) or {}
        codec = (raw.get("device_extend") or {}).get("codec") or {}
        if not str(codec.get("audio_codec") or "AAC").upper().startswith("AAC"):
            return None
        try:
            return int(codec.get("sample_rate") or 16000), 1
        except (TypeError, ValueError):
            return 16000, 1

    def creds(self, base_sn: str) -> Creds:
        """Thread-safe snapshot of the base's P2P credentials."""
        data = self.coordinator.data
        device = data.devices.get(base_sn) if data else None
        if device is None:
            raise RuntimeError(f"no cloud record for base {base_sn}")
        return Creds(device.sn, device.did, device.p2p_password, device.service_string)

    # -- viewers ----------------------------------------------------------
    def subscribe(self, sn: str) -> Subscription | None:
        node, device = self._node_device(sn)
        if node is None or self._closed:
            return None
        sub = Subscription(self, sn)
        self._subs.setdefault(sn, set()).add(sub)
        self._base_of[sn] = device.sn
        timer = self._camera_timers.pop(sn, None)
        if timer:
            timer.cancel()
        session = self._sessions.get(device.sn)
        if session is None or session.finished or not session.want(sn, node.channel):
            session = _Session(self, device.sn)
            self._sessions[device.sn] = session
            session.want(sn, node.channel)
            session.start()
            _LOGGER.info("Soliom live: starting session for %s", device.sn)
        for frame in self._gop.get(sn, []):  # start instantly from the last key frame
            sub.push(frame)
        return sub

    def _release(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.sn)
        if subs is not None:
            subs.discard(sub)
            if not subs and not self._closed:
                self._camera_timers[sub.sn] = self.loop.call_later(
                    CAMERA_LINGER_SECONDS, self._stop_camera, sub.sn
                )

    def _stop_camera(self, sn: str) -> None:
        self._camera_timers.pop(sn, None)
        if self._subs.get(sn):
            return
        base = self._base_of.get(sn)
        session = self._sessions.get(base) if base else None
        if session:
            session.unwant(sn)
        self._gop.pop(sn, None)
        self._clock.pop(sn, None)
        self._audio_seen.pop(sn, None)

    # -- frames (event loop) ------------------------------------------------
    def _on_frame(self, base_sn: str, sn: str, frame: LiveFrame) -> None:
        now = time.monotonic()
        prev = self._clock.get(sn)
        if frame.kind == "audio":
            if prev is None:
                return  # audio before the first video frame: no clock yet
            frame.t = now - prev[0]
            self._audio_seen[sn] = now
            for sub in list(self._subs.get(sn, ())):
                sub.push(frame)
            return
        t0 = prev[0] if prev else now
        t = now - t0
        if prev and t <= prev[1]:
            t = prev[1] + 0.001  # strictly increasing
        self._clock[sn] = (t0, t)
        frame.t = t
        if frame.key:
            self._gop[sn] = [frame]
        elif sn in self._gop:
            gop = self._gop[sn]
            gop.append(frame)
            if len(gop) > 600:  # no key frame for ages: stop buffering
                self._gop.pop(sn, None)
        for sub in list(self._subs.get(sn, ())):
            sub.push(frame)

    # -- shutdown -----------------------------------------------------------
    async def async_shutdown(self) -> None:
        self._closed = True
        for timer in self._camera_timers.values():
            timer.cancel()
        self._camera_timers.clear()
        for subs in self._subs.values():
            for sub in list(subs):
                sub.closed = True
        for session in self._sessions.values():
            session.stop()
        self._sessions.clear()


def new_token() -> str:
    return secrets.token_urlsafe(24)
