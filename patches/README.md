# OpenKart patches

Modified files of the [OpenKart SDK](https://github.com/OpenKart-SDK/openkart)
(`openkartd`, upstream commit `463cfda`). The installers copy them over the
upstream package automatically; they stay under OpenKart's BSD-3-Clause license
(see `LICENSE` in this folder).

| File | Change |
|---|---|
| `openkartd/fuji.py` | Forwards the kart's video to the video worker (`127.0.0.1:19001`), keeps the video-control channel alive, mirrors raw telemetry to `127.0.0.1:19002` (`OPENKART_TELEMETRY_FORWARD_PORT`, 0 disables); further experimental probes are all off by default |
| `openkartd/__init__.py` | Opens a third UDP port (`ports.udp3`) that the patched `fuji.py` needs |
| `openkartd/lp2p.py` | Logs hostapd output and stops waiting for `AP-ENABLED` after 12 s so the HTTP API still starts |
| `openkartd/api/v1.py` | Debug endpoints `POST /v1/devices/{serial}/debug/fuji_state` and `.../debug/video_control_write` |

The debug endpoints are unauthenticated; the installers bind the OpenKart API to
`127.0.0.1:8181` so they are not reachable from the network.
