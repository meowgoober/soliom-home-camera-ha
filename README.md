# soliom-home-camera-ha

Reverse engineered plugin of the Soliom Home app to add Soliom Home cameras to Home Assistant with its base station.

![License](https://img.shields.io/badge/license-MIT-blue)

## Installation

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

## License

MIT. See [LICENSE](LICENSE).
