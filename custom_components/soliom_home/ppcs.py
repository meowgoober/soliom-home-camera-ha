"""Soliom/XM live-view protocol over PPCS (EXPERIMENTAL).

The PPPP transport (discovery, hole punching, reliable DRW channels) comes from
the ``pyibaby`` package (MPL-2.0).  Everything XM specific lives here: the
24-byte command header, AES body encryption, login, the 16-byte sub-header that
names the child camera, and reassembly of frames from packets.

This module has no Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

import hashlib
import json
import random
import struct
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pyibaby import protocol as P
from pyibaby.pppp import PPPPConnection, _encode_p2p_addr, _parse_p2p_addr
from pyibaby.servers import decode_init_string

MAGIC = 0x55AA55AA
MAGIC_BYTES = b"\xaa\x55\xaa\x55"
SUBHEAD_MAGIC = 0x66BB66BB

CMD_LOGIN = 1
CMD_LIVE_ENABLE = 390
CMD_LIVE_PAUSE = 391
CMD_LIVE_CLOSE = 394
CMD_LIVE_ADD = 395
CMD_LIVE_REMOVE = 396
CMD_HEART_BEATS = 2001
CMD_LIVE_KEEPALIVE = 2003

__all__ = [
    "ChannelStream",
    "FrameAssembler",
    "LiveFrame",
    "WanConn",
    "aes_ecb_dec",
    "build_cmd",
    "decode_init_string",
    "guess_codec",
    "login_body",
]


@dataclass(slots=True)
class LiveFrame:
    """One whole video frame (Annex-B) from a child camera."""

    t: float  # smoothed presentation time, seconds (set by the manager)
    key: bool
    codec: str  # "h264" or "hevc"
    fps: int
    data: bytes
    kind: str = "video"  # "video" or "audio"


# --------------------------------------------------------------------- crypto
def _cipher(password: str, enc: int) -> Cipher:
    """AES per the app: key = MD5(p2p_password + "xmitech"); 1 = ECB, 4/5 = CBC."""
    key = hashlib.md5((password + "xmitech").encode()).digest()
    if enc == 1:
        return Cipher(algorithms.AES(key), modes.ECB())
    return Cipher(algorithms.AES(key), modes.CBC(key))


def _pad(data: bytes) -> bytes:
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def aes_ecb_dec(password: str, data: bytes) -> bytes | None:
    """AES-ECB decrypt + PKCS7 unpad; None when the length can't be a ciphertext."""
    if not data or len(data) % 16:
        return None
    dec = _cipher(password, 1).decryptor()
    out = dec.update(data) + dec.finalize()
    n = out[-1]
    if 1 <= n <= 16 and out.endswith(bytes([n]) * n):
        return out[:-n]
    return out


def build_cmd(cmd: int, body: dict, password: str | None, enc: int) -> bytes:
    """24-byte little-endian header + (optionally AES encrypted) JSON body."""
    raw = json.dumps(body, separators=(",", ":")).encode()
    if enc:
        encryptor = _cipher(password or "", enc).encryptor()
        raw = encryptor.update(_pad(raw) if enc != 5 else raw) + encryptor.finalize()
    hdr = bytearray(24)
    struct.pack_into("<I", hdr, 0, MAGIC)
    hdr[4] = (enc & 15) | 0x10
    struct.pack_into("<H", hdr, 5, 210)
    struct.pack_into("<H", hdr, 8, 1)
    struct.pack_into("<H", hdr, 10, 1)
    struct.pack_into("<H", hdr, 12, len(raw))
    struct.pack_into("<H", hdr, 14, cmd)
    return bytes(hdr) + raw


def login_body(username: str, password: str, user_id: str = "user_android") -> dict:
    rnd = "".join(str(random.randint(0, 9)) for _ in range(6))
    return {
        "username": username,
        "timestamp": int(time.time() * 1000),
        "random": rnd,
        "user_id": user_id,
        "auth": hashlib.md5(f"{username}:{rnd}:{password}".encode()).hexdigest(),
        "video": 0,
        "mic": 0,
        "res": 1,
    }


# ---------------------------------------------------------------- stream parse
def split_subhead(payload: bytes, password: str) -> tuple[dict | None, bytes]:
    """16-byte sub-head + AES JSON (child sn / seq / liveType) + frame data."""
    if len(payload) < 16 or struct.unpack_from("<I", payload, 0)[0] != SUBHEAD_MAGIC:
        return None, payload
    jsize = struct.unpack_from("<I", payload, 12)[0]
    meta = None
    raw = aes_ecb_dec(password, payload[16 : 16 + jsize])
    if raw:
        try:
            meta = json.loads(raw.decode("utf-8", "replace").rstrip("\x00"))
        except ValueError:
            meta = None
    return meta, payload[16 + jsize :]


def guess_codec(frame: bytes) -> str:
    """'hevc' when parameter sets VPS/SPS/PPS lead the frame, 'h264' for SPS/PPS."""
    if frame[:4] != b"\x00\x00\x00\x01" or len(frame) < 5:
        return "unknown"
    n = frame[4]
    if (n >> 1) & 63 in (32, 33, 34) and n & 0x81 == 0:
        return "hevc"
    if n & 31 in (7, 8):
        return "h264"
    return "unknown"


class ChannelStream:
    """Ordered byte stream over one PPPP DRW channel (16-bit indices, wrap-safe)."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.next = 0
        self.pending: dict[int, bytes] = {}
        self.gap_since: float | None = None
        self.skipped = 0

    def feed(self, idx: int, data: bytes) -> None:
        behind = (self.next - idx) & 0xFFFF
        if 0 < behind < 0x8000:  # retransmit of something already consumed
            return
        self.pending[idx] = data
        while self.next in self.pending:
            self.buf += self.pending.pop(self.next)
            self.next = (self.next + 1) & 0xFFFF
        self.gap_since = (self.gap_since or time.monotonic()) if self.pending else None

    def skip_gap(self, now: float, after: float = 2.0) -> bool:
        """A packet never arrived: jump ahead; the parser resyncs on the next header."""
        if not self.pending or self.gap_since is None or now - self.gap_since < after:
            return False
        nxt = min(self.pending, key=lambda i: (i - self.next) & 0xFFFF)
        self.next = nxt
        self.buf.clear()
        self.gap_since = None
        self.skipped += 1
        self.feed(nxt, self.pending.pop(nxt))
        return True

    def packets(self):
        """Yield (header, payload) for each complete XM packet."""
        while len(self.buf) >= 24:
            if bytes(self.buf[:4]) != MAGIC_BYTES:
                j = self.buf.find(MAGIC_BYTES, 1)
                del self.buf[: j if j > 0 else len(self.buf) - 3]
                continue
            size = struct.unpack_from("<H", self.buf, 12)[0]
            if len(self.buf) < 24 + size:
                return
            hdr, payload = bytes(self.buf[:24]), bytes(self.buf[24 : 24 + size])
            del self.buf[: 24 + size]
            yield hdr, payload


class FrameAssembler:
    """Packets -> whole decrypted frames tagged with the child camera serial."""

    def __init__(self, password: str, model: int = 1) -> None:
        self.password = password
        self.model = model  # 1 = video, 18 = audio
        self.key: int | None = None
        self.parts: list[bytes] = []
        self.sn: str | None = None

    def feed(self, hdr: bytes, payload: bytes) -> tuple[str | None, int, int, bytes] | None:
        """Return (sn, frame_type, fps, frame) when a frame completes, else None."""
        b4, model = hdr[4], hdr[5]
        if model != self.model:
            return None
        frame_num = struct.unpack_from("<H", hdr, 6)[0]
        total, cur = struct.unpack_from("<HH", hdr, 8)
        ftype, fps = hdr[14] >> 4, hdr[15] & 31
        if (b4 >> 4) == 2:
            meta, data = split_subhead(payload, self.password)
        else:
            meta, data = None, payload
        if cur == 1:
            self.key, self.parts, self.sn = frame_num, [], (meta or {}).get("sn")
        elif frame_num != self.key:
            return None  # the start of this frame was lost
        self.parts.append(data)
        if cur != total:
            return None
        blob = b"".join(self.parts)
        self.key, self.parts = None, []
        if b4 & 15 == 1:
            blob = aes_ecb_dec(self.password, blob) or blob
        return self.sn, ftype, fps, blob


class WanConn(PPPPConnection):
    """Rendezvous via the cloud supernodes, punching every candidate they return.

    pyibaby's stock version aborts with a "symmetric NAT" error when the device
    reports several ports for one IP.  This base uses one socket per supernode,
    so that is a false positive: punch every candidate and take whoever answers
    (in practice the base's own LAN address).
    """

    def p2p_rendezvous(self, did, servers, timeout=10.0):
        import socket

        did_b = P.did_encode(did)
        probe = self.probe_nat(servers)
        req = P.pppp(P.MSG_P2P_REQ, did_b + _encode_p2p_addr(*probe.reflexive))
        punch = P.pppp(P.MSG_PUNCH_PKT, did_b)
        targets = [(socket.gethostbyname(h), p) for h, p in servers]
        cands: set = set()
        end, last = time.time() + max(timeout, 12.0), 0.0
        while time.time() < end:
            now = time.time()
            if now - last > 0.4:
                for t in targets:
                    self.sock.sendto(req, t)
                for c in cands:
                    self.sock.sendto(punch, c)
                last = now
            try:
                data, addr = self.sock.recvfrom(2048)
            except TimeoutError:
                continue
            t, _c, _i, body = P.parse(data)
            if t == P.MSG_PUNCH_TO:
                c = _parse_p2p_addr(body)
                if c and c[0] != "0.0.0.0" and c not in cands:
                    cands.add(c)
                    self.sock.sendto(punch, c)
            elif t in (P.MSG_PUNCH_PKT, P.MSG_P2P_RDY) and body[:12] == did_b[:12]:
                return addr
        raise TimeoutError(f"no candidate answered; candidates were {sorted(cands)}")
