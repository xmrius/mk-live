# Kart telemetry

The kart sends telemetry over UDP to OpenKart. The patch in `patches/openkartd/fuji.py`
mirrors every raw packet to `127.0.0.1:19002`. `drive_web.py` (TelemetryHub) receives
them, decodes IMU packets live (`src/mk_live/imu.py`) and pushes the values at 20 Hz over
the drive WebSocket `/ws` as `{"telem": {...}}` to the cockpit.

Byte 0 of a packet is its type:

| Type | Rate  | Length      | Content |
|------|-------|-------------|---------|
| 1    | 10 Hz | short       | status, battery in byte 4 |
| 2    | 30 Hz | 73 + n·12   | IMU, see below |
| 3    | 30 Hz | 88          | two 32-byte records (current + previous), meaning open |

## Type 2 – IMU

| Offset  | Type    | Meaning |
|---------|---------|---------|
| 0       | u8      | type (2) |
| 1..2    | u16     | sample counter, advances by the number of samples in the packet |
| 3..4    | ?       | flags, unknown |
| 5..8    | u32     | timestamp in microseconds (6000 µs per sample, ≈ 166 Hz) |
| 9..24   | 4× i32  | Q30 quaternion from the kart's own sensor fusion (convention open) |
| 25..72  | 12× i32 | firmware integrator state, drifts and resets, unused |
| 73..    | n × 12  | samples of 6× i16 LE: columns 0..2 accelerometer, 3..5 gyro |

Established from labelled recordings:

- **Gyro:** column 5 is the vertical axis (positive clockwise), 4 the longitudinal axis
  (roll), 3 the lateral axis (pitch). Scale: 229.8 LSB per °/s – one full turn by hand
  integrated to 82 712. Rest offsets are around (70, −158, −8).
- **Accelerometer:** column 1 reacts to throttle/brake and pitch, column 0 to roll. Offsets
  and scales do not fit a simple model from the recorded poses, so they are measured per
  kart with the calibration wizard.
- **Speed:** no packet type contains a field that rises when driving straight and is zero
  at rest, so the cockpit does not show km/h.

## Values in the cockpit

- **Heading (compass tape):** integrated vertical gyro. It only integrates while the kart
  moves; the zero point follows automatically at rest. The heading is relative – click
  the tape to set it to 0°. It drifts slowly over time; there is no magnetometer.
- **G-force:** lateral and longitudinal acceleration, auto-zeroed at every stop (so slopes
  do not count as G) and smoothed over 80 ms.
- **Attitude (pitch/roll):** from the low-passed accelerometer, only after calibration.

## Calibration

Settings → **Calibrate IMU**. Five still poses – flat, nose up, nose down, left side
down, right side down – plus one full turn by hand. The server derives each axis' sensor
column, offset and signed scale and stores them in
`~/openkart_setup/telemetry/imu_calibration.json` (override with `KART_IMU_CALIBRATION`).
If the axis scales differ by more than 40 % it still saves, with a warning.

API:

- `POST /api/imu/pose {"pose": "flat"|"nose_up"|"nose_down"|"left_down"|"right_down"}`
- `POST /api/imu/spin {"action": "start"|"stop"}`
- `POST /api/imu/save`
- `POST /api/imu/heading/reset`
- `POST /api/telemetry/record {"label", "seconds"}` – record raw data for further analysis

Errors come back as `{"error": <English text>, "code": <stable code>, "params": {...}}`;
the cockpit translates the code.
