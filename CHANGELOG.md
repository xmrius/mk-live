# Changelog

## v0.1.1 – 2026-09-27

- Cockpit, installer output and install guide are now in **English** by default; German
  can be selected in the cockpit under ⚙ → Language.

## v0.1.0 – 2026-09-27

First version that drives a Mario Kart Live kart without a Switch.

- **Base station:** Raspberry Pi 4/5, Debian/Ubuntu or Steam Deck with a USB Wi-Fi stick
  (tested: AR9271). Installers `install/install.sh` and `install/steamdeck.sh`, guide in
  `docs/INSTALL.md`.
- **Video:** correct reassembly of the kart stream (packet trailer, FRAM overhead),
  WebCodecs path with ~60 ms glass-to-glass latency, MJPEG fallback.
- **Browser cockpit:** gamepad (incl. Steam Deck), keyboard, touch; battery, signal and
  cable status; pairing via QR-code wizard.
- **Telemetry:** live IMU decoding – compass (gyro), G-force, attitude (after calibration).
- **Steam Deck:** runs in a container and survives SteamOS updates; launcher in the Steam
  library and on the desktop; iwd and firewall are set up automatically.

Known limits: no speed readout (the kart does not send one); G-force is only estimated
without IMU calibration; WebCodecs only via localhost/HTTPS.
