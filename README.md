<img width="240" height="240" alt="image" src="https://github.com/user-attachments/assets/a7b6a659-680c-4ba0-be88-de9c8db20d2b" />

# soliom-home-camera-ha

Reverse engineered plugin of the Soliom Home app to add Soliom Home cameras to Home Assistant with its base station.

![License](https://img.shields.io/badge/license-MIT-blue)

## installation for the normal users

**HACS (recommended):** HACS > Integrations > ⋮ > Custom repositories, add `https://github.com/meowgoober/soliom-home-camera-ha` as type *Integration*, install **Soliom Home**, restart Home Assistant.

**Manual:** copy `custom_components/soliom_home` into your HA `config/custom_components/` folder and restart.

Then go to Settings > Devices & services > Add integration > Soliom Home and log in with your Soliom account.

## Usage

- Entities for the homebase, its child cameras, motion events, and thumbnails.
- Live view for child cameras is served by Home Assistant on demand: a session to the base starts when a camera is opened and stops about 30 seconds after the last viewer leaves.
- Uses the homebase's sub-stream (720p H.264) by default. Change `DEFAULT_STREAM_TYPE` in `live.py` to `0` for 4K H.265.
- Audio (AAC) is included when the camera sends it.
- Requires `pyibaby` and `cryptography`, installed automatically.

This is an unofficial, reverse engineered integration. It is not affiliated with Soliom. Live view is experimental.

## Technical Recipe for the nerds

How this plugin gets live video out of a Soliom homebase, and how it was figured out. Everything below was reverse engineered from the Soliom Home Android app (decompiled with jadx) and checked against one real SHB01 base with SH801 cameras. It is unofficial and experimental.

### 1. The problem

The base has no RTSP, ONVIF or HTTP video port. The app talks to it over a **PPCS/PPPP** peer-to-peer tunnel (the CS2 Network P2P stack used by many cheap cameras), and the child cameras are just channels multiplexed behind the base. So "add the camera by RTSP URL" is impossible, and the plugin has to speak the tunnel itself.

### 2. Architecture

```
HA camera entity ──stream_source──> http://127.0.0.1:<port>/api/soliom_home/live/<token>/<camera sn>.ts
                                              │  (live_view.py, hand-written MPEG-TS)
                                              ▼
                                   live.py  SoliomLiveManager (event loop)
                                              │  refcounts viewers, fans frames out
                                              ▼
                                   _Session thread (blocking PPCS session to the base)
                                              │  ppcs.py: XM framing, AES, frame assembly
                                              ▼
                              pyibaby PPPP transport ── UDP ──> Soliom homebase ──> child cameras
```

| File | Job |
|---|---|
| `api.py`, `crypto.py`, `coordinator.py` | Cloud login, device list, events, polling, self-healing re-login |
| `ppcs.py` | XM command framing, AES, login body, sub-header parsing, frame reassembly, supernode rendezvous |
| `live.py` | On-demand session thread and viewer manager |
| `mpegts.py` | Minimal MPEG-TS muxer (H.264/H.265 + AAC) |
| `live_view.py` | HTTP endpoint that streams a camera to HA |

### 3. Finding the base

The cloud device record gives three things for the base: a device ID (`did`), a `p2p_password`, and a `service_string`. The service string is an obfuscated list of PPCS "supernode" servers. It decodes (same algorithm as the open-source `pyibaby`) to three public servers on UDP port 32100.

Connection steps:

1. Send `P2P_REQ` for the base's DID to all three supernodes.
2. Each supernode answers with candidate addresses for the base: its WAN address and its **LAN address**, on a different port per supernode (the base keeps one socket per supernode).
3. Punch every candidate. On a home network the base answers from its LAN IP, so video never leaves your network.

`pyibaby` aborts here with a "symmetric NAT" error because it sees several ports for one IP. For this base that is a false positive, so `WanConn` overrides the rendezvous to punch all candidates and take whoever replies.

### 4. The reliable transport

PPPP gives reliable ordered "DRW" channels over UDP: indexed packets, ACKs, retransmits. The plugin uses `pyibaby` for this with a few fixes:

- Channel indices are 16-bit and wrap, so `ChannelStream` orders and de-duplicates modulo 65536 and skips a lost packet after 2 s, then resyncs on the next frame header.
- `pyibaby.protocol.parse()` returns `channel=None` for ACK packets, so the base's ACKs were never processed. The ACK channel is read from `payload[1]` instead.
- Data that arrives while the plugin is still logging in is kept, not thrown away.

Channels: **0** = commands and replies, **1** = video, **2** = audio.

### 5. The XM command layer

Every command is a 24-byte little-endian header plus a JSON body, sent on channel 0:

| Offset | Field |
|---|---|
| 0-3 | magic `0x55AA55AA` |
| 4 | `(encryption type & 15) \| 0x10` |
| 8-9 / 10-11 | total packets / current packet (always 1) |
| 12-13 | body length |
| 14-15 | command id |

Bodies are AES encrypted with a key of `MD5(p2p_password + "xmitech")` (ECB, PKCS7). Commands used:

| Id | Command | Notes |
|---|---|---|
| 1 | login | `auth = md5(username:random:password)`, username is the base serial |
| 395 | live add | `{"list":[{"sn":…,"streamType":0\|1}]}` |
| 390 | live enable | same list plus `"videoKeepAlive_2": 2` |
| 396 / 394 | live remove / close | stop one camera / end the session |
| 2003 / 2001 | keepalive / heartbeat | see "Known issues" |

### 6. Video and audio

Each media packet has a 24-byte header (magic, version/encryption, model, frame number, packet count, current packet, size, frame type, fps, timestamp). For video `model=1`, for audio `model=18`. Inside the payload:

1. A **16-byte sub-header** (`0x66BB66BB`, version, command, payload size).
2. An AES-encrypted JSON blob that names the **child camera serial** (`{"sn": …}`). This is how several cameras share one session.
3. Up to 4072 bytes of frame data.

Packets are collected by their counters into a whole frame, then the whole frame is AES-ECB decrypted. Result: Annex-B video.

- `streamType 0` is the **main stream, 3840x2160 H.265**.
- `streamType 1` is the **sub-stream, 1280x720 H.264**. This is the default, because browsers and HA play H.264 everywhere.
- The codec is detected from the first NAL units (H.265 VPS/SPS/PPS versus H.264 SPS/PPS). The header does not say.
- Audio is **AAC**. If the frames are not ADTS, a 7-byte ADTS header is added from the sample rate.

### 7. Getting it into Home Assistant

HA's stream component and go2rtc read a URL, so the plugin serves one itself:

- `mpegts.py` is a small **hand-written MPEG-TS muxer** (PAT, PMT, PES with PTS, PCR, continuity counters). It was written instead of using PyAV because HA's PyAV build may not include an H.264 encoder, and pass-through needs none.
- `live_view.py` registers `GET /api/soliom_home/live/<token>/<sn>.ts`. HA's stream clients cannot send credentials, so the URL carries a random per-start token that is only handed out through `stream_source`.
- A new viewer starts from the last key frame (a small GOP cache) so playback starts at once.

### 8. On-demand, battery friendly

`SoliomLiveManager` counts viewers per camera. The first viewer starts a session thread, which sends live-add and live-enable. When the last viewer leaves, the camera is removed after about 20 s and the session closed about 30 s later. Cameras are only awake while someone watches.

### 9. Cloud reliability

The coordinator re-logins on **any** failed poll (not just one error code), within a 45 s timeout, polls every 30 s while down, treats an unexpectedly empty device list as a failure, and only asks for reauth after repeated rejected logins over 10 minutes.

### 10. How it was tested

- Decompiled the app to find commands, headers and the encryption.
- Probed a real base from a PC with a script that dumped raw channels, then decoded the frames with ffprobe/ffplay to confirm H.265 4K and H.264 720p.
- Built a **fake base** that speaks the real packet formats (including out-of-order and duplicate packets, index wraparound, real H.264 and AAC from an encoder) and ran the real session thread, manager and HTTP view against it: frames arrive byte-identical, add/remove/close commands go out, sessions start and idle out, and a client decodes the MPEG-TS stream.

### 11. Known issues and guesses

- **The base can close the session about 12-14 s after login while video is flowing.** This was unresolved when this was written. The base answers "ok" to every heartbeat variant tried, so the real cause may be a transport-level ALIVE or a different heartbeat shape. The plugin currently sends several heartbeat variants and its own PPPP ALIVE, and logs the base's replies.
- Guesses not confirmed on hardware: the heartbeat channel and body, and the exact remove/close bodies.
- LAN only. If HA runs in a bridged Docker network, the UDP punching may not work; host networking is safest.
- Tested only on an SHB01 base with SH801 cameras.
- Credit: the PPPP transport comes from [`pyibaby`](https://pypi.org/project/pyibaby/) (MPL-2.0).

## License

MIT. See [LICENSE](LICENSE).
