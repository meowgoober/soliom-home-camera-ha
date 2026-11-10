"""Minimal MPEG-TS muxer for one Annex-B H.264 / H.265 video stream.

Home Assistant's stream component and go2rtc can both read MPEG-TS over HTTP.
Writing the container by hand avoids depending on an encoder being present in
the installed PyAV build: frames are passed through untouched.
"""

from __future__ import annotations

PAT_PID = 0x0000
PMT_PID = 0x1000
VIDEO_PID = 0x0100
STREAM_TYPES = {"h264": 0x1B, "hevc": 0x24}
AUDIO_PID = 0x0101
AAC_RATES = {96000: 0, 88200: 1, 64000: 2, 48000: 3, 44100: 4, 32000: 5, 24000: 6,
             22050: 7, 16000: 8, 12000: 9, 11025: 10, 8000: 11}
PTS_DELAY = 9000  # 100 ms at 90 kHz: PTS stays ahead of the PCR


def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else (crc << 1)
            crc &= 0xFFFFFFFF
    return crc


def _section(body: bytes) -> bytes:
    return body + _crc32_mpeg(body).to_bytes(4, "big")


def _pcr_bytes(base: int) -> bytes:
    base &= (1 << 33) - 1
    return bytes(
        [
            (base >> 25) & 0xFF,
            (base >> 17) & 0xFF,
            (base >> 9) & 0xFF,
            (base >> 1) & 0xFF,
            ((base & 1) << 7) | 0x7E,
            0x00,
        ]
    )


def _pts_bytes(pts: int) -> bytes:
    pts &= (1 << 33) - 1
    return bytes(
        [
            0x21 | (((pts >> 30) & 0x07) << 1),
            (pts >> 22) & 0xFF,
            (((pts >> 15) & 0x7F) << 1) | 1,
            (pts >> 7) & 0xFF,
            ((pts & 0x7F) << 1) | 1,
        ]
    )


class TSMuxer:
    """Turns whole video frames into 188-byte transport stream packets."""

    def __init__(self, codec: str, audio: tuple[int, int] | None = None) -> None:
        """``audio`` = (sample_rate, channels) to add an AAC track, else video only."""
        if codec not in STREAM_TYPES:
            raise ValueError(f"unsupported codec {codec!r}")
        if audio and audio[0] not in AAC_RATES:
            audio = None
        self.stream_type = STREAM_TYPES[codec]
        self.audio = audio
        self._cc: dict[int, int] = {}
        self._frames_since_psi = 1 << 30

    # -- helpers ------------------------------------------------------------
    def _packet(self, pid: int, pusi: bool, af_body: bytes, payload: bytes) -> bytes:
        room = 184 - len(payload)
        cc = self._cc.get(pid, 0)
        self._cc[pid] = (cc + 1) & 0x0F
        if room == 0:
            afc, adaptation = 1, b""
        else:
            afc = 3
            stuffing = room - 1 - len(af_body)
            if room == 1:
                adaptation = b"\x00"
            else:
                body = af_body if af_body else b"\x00"
                stuffing = room - 1 - len(body)
                adaptation = bytes([room - 1]) + body + b"\xff" * stuffing
        header = bytes(
            [0x47, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF, (afc << 4) | cc]
        )
        pkt = header + adaptation + payload
        assert len(pkt) == 188, len(pkt)
        return pkt

    def _psi(self) -> bytes:
        pat = _section(
            bytes([0x00, 0xB0, 13, 0x00, 0x01, 0xC1, 0x00, 0x00, 0x00, 0x01,
                   0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF])
        )
        es = bytes([self.stream_type, 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0x00])
        if self.audio:
            es += bytes([0x0F, 0xE0 | (AUDIO_PID >> 8), AUDIO_PID & 0xFF, 0xF0, 0x00])
        pmt = _section(
            bytes([0x02, 0xB0, 13 + len(es), 0x00, 0x01, 0xC1, 0x00, 0x00,
                   0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0x00]) + es
        )
        out = b""
        for pid, section in ((PAT_PID, pat), (PMT_PID, pmt)):
            payload = b"\x00" + section
            out += self._packet(pid, True, b"", payload + b"\xff" * (184 - len(payload)))
        return out

    # -- public -------------------------------------------------------------
    def mux(self, frame: bytes, t: float, key: bool) -> bytes:
        """Mux one frame at presentation time ``t`` (seconds from stream start)."""
        out = b""
        self._frames_since_psi += 1
        if key or self._frames_since_psi >= 30:
            out += self._psi()
            self._frames_since_psi = 0
        base = max(0, int(t * 90000))
        pes = (
            b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05"
            + _pts_bytes(base + PTS_DELAY)
            + frame
        )
        pos, first = 0, True
        while pos < len(pes):
            af_body = b""
            if first:
                af_body = bytes([(0x40 if key else 0x00) | 0x10]) + _pcr_bytes(base)
            capacity = 184 - (1 + len(af_body) if af_body else 0)
            chunk = pes[pos : pos + capacity]
            out += self._packet(VIDEO_PID, first, af_body, chunk)
            pos += len(chunk)
            first = False
        return out

    def _adts(self, length: int) -> bytes:
        rate, channels = self.audio  # type: ignore[misc]
        total = length + 7
        idx = AAC_RATES[rate]
        return bytes([
            0xFF, 0xF1,
            (1 << 6) | (idx << 2) | (channels >> 2),
            ((channels & 3) << 6) | (total >> 11),
            (total >> 3) & 0xFF,
            ((total & 7) << 5) | 0x1F,
            0xFC,
        ])

    def mux_audio(self, frame: bytes, t: float) -> bytes:
        """Mux one AAC frame (raw or already ADTS) at time ``t`` seconds."""
        if not self.audio or not frame:
            return b""
        if not (len(frame) > 1 and frame[0] == 0xFF and frame[1] & 0xF0 == 0xF0):
            frame = self._adts(len(frame)) + frame
        pts = max(0, int(t * 90000)) + PTS_DELAY
        pes = b"\x00\x00\x01\xc0" + (8 + len(frame)).to_bytes(2, "big") + b"\x80\x80\x05" + _pts_bytes(pts) + frame
        out, pos, first = b"", 0, True
        while pos < len(pes):
            chunk = pes[pos : pos + 184]
            out += self._packet(AUDIO_PID, first, b"", chunk)
            pos += len(chunk)
            first = False
        return out
