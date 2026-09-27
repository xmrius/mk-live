# mk-live

Drive **Mario Kart Live: Home Circuit** karts without a Switch. A Raspberry Pi, Steam Deck
or Linux PC with a USB Wi-Fi stick becomes the base station, and the cockpit runs in any
browser: ~60 ms camera latency, gamepad/keyboard/touch controls, battery and signal
status, one-click pairing and live IMU instruments (compass, G-meter).

## Quick start

Hardware: a kart, a USB Wi-Fi stick with AP mode (tested: [Atheros AR9271](https://link.amazon/B01PDLG23)\*) and one of
Raspberry Pi 4/5 (Raspberry Pi OS 64-bit), Steam Deck (SteamOS 3.5+) or Debian 13 / Ubuntu 24.04+.

\* Affiliate link: as an Amazon Associate I earn from qualifying purchases. The price stays the same for you, and it helps keep the project going.

```bash
git clone https://github.com/xmrius/mk-live.git && cd mk-live
sudo bash install/install.sh        # Raspberry Pi / Debian / Ubuntu
./install/steamdeck.sh              # Steam Deck (desktop mode, Konsole)
```

Then open `http://<device>:9000` (on the Deck: *MK Live Cockpit* in the library, game mode)
and pair the kart once via **Pair kart**. Full guide: [docs/INSTALL.md](docs/INSTALL.md).

## How it works

```text
kart ──Wi-Fi──▶ USB stick ──▶ OpenKart (access point, pairing, control, video + telemetry)
                                 │ UDP 19001 video        │ UDP 19002 telemetry
                                 ▼                        ▼
                          video_worker.py ──────▶ drive_web.py ◀──WebSocket──▶ browser cockpit
                          (H.264 reassembly,       (port 9000: UI, driving,
                           WebCodecs/MJPEG, 9001)   status, pairing, IMU)
```

- [OpenKart SDK](https://github.com/OpenKart-SDK/openkart) runs the access point and talks
  to the kart; `patches/` adds video and telemetry forwarding.
- `src/mk_live/video_worker.py` reassembles the kart's H.264 stream and re-encodes it for
  low-latency WebCodecs playback (MJPEG as fallback).
- `src/mk_live/drive_web.py` serves the cockpit, sends drive commands with a deadman switch
  (the kart stops 0.6 s after the browser goes quiet) and decodes the IMU
  ([docs/TELEMETRY.md](docs/TELEMETRY.md)).

## Repository layout

```text
install/        installers: install.sh (Pi/Debian/Ubuntu), steamdeck.sh (SteamOS, podman)
src/mk_live/    video worker, cockpit server, IMU decoder, cockpit UI (static/)
patches/        modified OpenKart SDK files (BSD-3-Clause)
docs/           installation guide, telemetry format
tools/          latency_meter.html – glass-to-glass latency measurement page
```

## Credits

- **[OpenKart SDK](https://github.com/OpenKart-SDK/openkart)** by Sam Edwards (CFSworks) –
  the access point, pairing and kart connection this project builds on, plus the
  [patched hostapd](https://github.com/OpenKart-SDK/hostapd).
- **[SwitchBrew](https://switchbrew.org/wiki/Mario_Kart_Live:_Home_Circuit)** – the
  community documentation of the kart protocol (LP2P, RCD, video, control and telemetry
  channels).
- **[Malu05](https://www.youtube.com/@malu05a)** – the video
  [ESP32 Control of a Mario Kart from Home Circuit](https://www.youtube.com/watch?v=8QY38eNyXG4)
  inspired the cockpit instruments (G-force, compass, telemetry readouts).
- [hostapd](https://w1.fi/hostapd/) by Jouni Malinen and contributors.

## Disclaimer

Not affiliated with or endorsed by Nintendo. Mario Kart and Nintendo Switch are
trademarks of Nintendo. This project is an independent interoperability effort; it
contains no Nintendo code, keys or game data. Use at your own risk.

## License

MIT, see [LICENSE](LICENSE). The files in `patches/openkartd/` are derived from the
OpenKart SDK and remain under its BSD-3-Clause license ([patches/LICENSE](patches/LICENSE)).
