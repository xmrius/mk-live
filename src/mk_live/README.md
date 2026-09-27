# mk_live

Runtime modules. The installers copy them to `/opt/mk-live/app` (Raspberry Pi,
Debian, Ubuntu) or into the container image (Steam Deck).

| File | Role |
|---|---|
| `video_worker.py` | Receives the kart's UDP video, reassembles H.264, serves MJPEG and the WebCodecs stream (port 9001) |
| `player.js` | Browser WebCodecs player, served by the worker at `/player.js` |
| `drive_web.py` | Cockpit web server (port 9000): driving, status, pairing QR, telemetry |
| `imu.py` | Live IMU decoder and calibration, see `docs/TELEMETRY.md` |
| `static/` | Cockpit UI (HTML/CSS/JS, texts in `i18n.js`) |
