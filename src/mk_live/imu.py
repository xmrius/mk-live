"""Live decoder for the kart IMU telemetry (packet type 2) mirrored by fuji.py.

Packet layout (reverse engineered from labelled recordings, see docs/TELEMETRY.md):

    0       u8      type (2)
    1..2    u16     sample counter, advances by the number of samples
    3..4    ?       flags / unknown
    5..8    u32     timestamp in microseconds (6000 us per sample)
    9..24   4x i32  Q30 orientation quaternion from the kart's own fusion
    25..72  12x i32 firmware integrator state (drifts and resets, unused)
    73..    n x 12  samples, 6x i16 LE each: cols 0..2 accelerometer, 3..5 gyro

Axis mapping and scales are not fixed by the protocol, so they come from a
per-kart calibration (5 still poses + one full turn by hand). Until that has
run, the defaults below are estimates from the first recordings.
"""
import json
import math
import os
import struct
import time
from collections import deque
from pathlib import Path

SAMPLE_OFFSET = 73
SAMPLE_SIZE = 12
SAMPLE_DT = 0.006

CALIBRATION_PATH = Path(os.path.expanduser(os.environ.get(
    'KART_IMU_CALIBRATION', '~/openkart_setup/telemetry/imu_calibration.json')))

# Estimates from the 2026-09-25 recordings: gyro col 5 is the vertical axis
# (positive when turning clockwise); accel col 1 reacts to throttle/brake and
# pitch, col 0 to roll. The accel scales are unverified guesses.
DEFAULT_CALIBRATION = {
    'source': 'default',
    'gyro': {'yaw': {'col': 5, 'lsb_per_dps': 229.0}},
    'accel': {
        'lat': {'col': 0, 'offset': 0.0, 'lsb_per_g': 16384.0},
        'long': {'col': 1, 'offset': 0.0, 'lsb_per_g': -16384.0},
        'vert': {'col': 2, 'offset': 0.0, 'lsb_per_g': -16384.0},
    },
}

POSES = ('flat', 'nose_up', 'nose_down', 'left_down', 'right_down')


class ImuError(ValueError):
    """Calibration error with a stable code, so the cockpit can show it in its own language."""

    def __init__(self, code, text, **params):
        super().__init__(text)
        self.code = code
        self.params = params
STILL_GYRO_SPREAD = 350     # max-min of a gyro axis over the window, LSB
STILL_ACCEL_SPREAD = 600
REST_WINDOW_S = 0.6


def parse_imu(packet):
    """Return (timestamp_us, quaternion, samples) or None if it is not an IMU packet."""
    if len(packet) < SAMPLE_OFFSET or packet[0] != 2 or (len(packet) - SAMPLE_OFFSET) % SAMPLE_SIZE:
        return None
    ts = struct.unpack_from('<I', packet, 5)[0]
    quat = tuple(v / 2**30 for v in struct.unpack_from('<4i', packet, 9))
    n = (len(packet) - SAMPLE_OFFSET) // SAMPLE_SIZE
    samples = [struct.unpack_from('<6h', packet, SAMPLE_OFFSET + SAMPLE_SIZE * k) for k in range(n)]
    return ts, quat, samples


def load_calibration():
    try:
        cal = json.loads(CALIBRATION_PATH.read_text())
        if 'gyro' in cal and 'accel' in cal:
            return cal
    except (OSError, ValueError):
        pass
    return json.loads(json.dumps(DEFAULT_CALIBRATION))


def _mean(rows, col):
    return sum(r[col] for r in rows) / len(rows)


def solve_accel(poses):
    """Per-axis column, offset and signed scale from the five still poses.

    The accelerometer measures +1 g along the body axis that points up, so
    nose_up/nose_down give +-1 g forward, left_down/right_down +-1 g to the
    right and flat +1 g up. Returns (accel_calibration, warnings)."""
    missing = [p for p in POSES if p not in poses]
    if missing:
        raise ImuError('poses_missing', 'Poses missing: ' + ', '.join(missing), poses=', '.join(missing))
    warnings = []
    d_long = [poses['nose_up'][i] - poses['nose_down'][i] for i in range(3)]
    d_lat = [poses['left_down'][i] - poses['right_down'][i] for i in range(3)]
    c_long = max(range(3), key=lambda i: abs(d_long[i]))
    c_lat = max(range(3), key=lambda i: abs(d_lat[i]))
    if c_long == c_lat:
        raise ImuError('axes_collide', 'Longitudinal and lateral axis map to the same sensor axis - check the poses.')
    c_vert = 3 - c_long - c_lat
    side = [poses[p][c_vert] for p in ('nose_up', 'nose_down', 'left_down', 'right_down')]
    vert_offset = sum(side) / 4
    accel = {
        'long': {'col': c_long, 'offset': (poses['nose_up'][c_long] + poses['nose_down'][c_long]) / 2,
                 'lsb_per_g': d_long[c_long] / 2},
        'lat': {'col': c_lat, 'offset': (poses['left_down'][c_lat] + poses['right_down'][c_lat]) / 2,
                'lsb_per_g': d_lat[c_lat] / 2},
        'vert': {'col': c_vert, 'offset': vert_offset, 'lsb_per_g': poses['flat'][c_vert] - vert_offset},
    }
    scales = {k: abs(v['lsb_per_g']) for k, v in accel.items()}
    lo, hi = min(scales.values()), max(scales.values())
    if lo < 200:
        raise ImuError('axis_flat', 'One axis barely moves - was every pose really taken?')
    if hi / lo > 1.4:
        text = ', '.join(f'{k} {v:.0f}' for k, v in scales.items())
        warnings.append({'code': 'scales_differ', 'params': {'scales': text},
                         'text': f'The axes have different scales ({text}). Calibration saved anyway.'})
    return accel, warnings


class ImuDecoder:
    """Turns the raw 166 Hz samples into HUD values: heading, yaw rate, G and tilt."""

    def __init__(self):
        self.cal = load_calibration()
        self.packets = 0
        self.bad_packets = 0
        self.last_at = 0.0
        self.last_ts = None
        self.quat = None
        self.window = deque(maxlen=int(2.0 / SAMPLE_DT))   # recent raw samples
        self.gyro_bias = [0.0, 0.0, 0.0]
        self.bias_ready = False
        self.accel_rest = None          # accel at the last stop, zero point of the G meter
        self.still = False
        self.heading = 0.0
        self.yaw_rate = 0.0
        self.g_lat = 0.0
        self.g_long = 0.0
        self.tilt = None                # low-passed accel vector for pitch/roll
        self.spin = None                # running full-turn calibration

    # ---------------------------------------------------------------- input
    def feed(self, packet, now=None):
        parsed = parse_imu(packet)
        if parsed is None:
            self.bad_packets += 1
            return
        now = now or time.time()
        ts, quat, samples = parsed
        self.packets += 1
        self.last_at = now
        self.quat = quat
        # The timestamp belongs to the packet; spread it over its samples.
        dt = SAMPLE_DT
        if self.last_ts is not None:
            span = ((ts - self.last_ts) & 0xFFFFFFFF) / 1e6
            if 0 < span < 0.5:
                dt = span / len(samples)
        self.last_ts = ts
        for s in samples:
            self.window.append(s)
            self._integrate(s, dt)
        self._update_rest()

    def _integrate(self, s, dt):
        yaw = self.cal['gyro']['yaw']
        rate = (s[yaw['col']] - self.gyro_bias[yaw['col'] - 3]) / yaw['lsb_per_dps']
        self.yaw_rate += (rate - self.yaw_rate) * 0.15
        if self.bias_ready and not self.still:
            self.heading = (self.heading + rate * dt) % 360.0
        if self.spin is not None:
            for i in range(3):
                self.spin['sum'][i] += (s[3 + i] - self.gyro_bias[i]) * dt
        acc = self.cal['accel']
        if self.accel_rest is not None:
            lat, lon = acc['lat'], acc['long']
            g_lat = (s[lat['col']] - self.accel_rest[lat['col']]) / lat['lsb_per_g']
            g_long = (s[lon['col']] - self.accel_rest[lon['col']]) / lon['lsb_per_g']
            k = 1 - math.exp(-dt / 0.08)      # ~80 ms smoothing against motor vibration
            self.g_lat += (g_lat - self.g_lat) * k
            self.g_long += (g_long - self.g_long) * k
        k = 1 - math.exp(-dt / 0.4)
        if self.tilt is None:
            self.tilt = list(s[:3])
        else:
            for i in range(3):
                self.tilt[i] += (s[i] - self.tilt[i]) * k

    def _recent(self, seconds):
        n = min(len(self.window), int(seconds / SAMPLE_DT))
        return list(self.window)[-n:] if n else []

    def _update_rest(self):
        rows = self._recent(REST_WINDOW_S)
        if len(rows) < int(REST_WINDOW_S / SAMPLE_DT) - 2:
            return
        spread = lambda c: max(r[c] for r in rows) - min(r[c] for r in rows)
        self.still = (all(spread(c) < STILL_GYRO_SPREAD for c in (3, 4, 5))
                      and all(spread(c) < STILL_ACCEL_SPREAD for c in (0, 1, 2)))
        if not self.still:
            return
        mean = [_mean(rows, c) for c in range(6)]
        if not self.bias_ready:
            self.gyro_bias = mean[3:]
            self.bias_ready = True
        else:
            self.gyro_bias = [b + (m - b) * 0.05 for b, m in zip(self.gyro_bias, mean[3:])]
        self.accel_rest = mean[:3]

    # ---------------------------------------------------------------- output
    def attitude(self):
        """(pitch, roll) in degrees from the low-passed accelerometer, or None if uncalibrated."""
        if self.cal.get('source') != 'wizard' or self.tilt is None:
            return None
        acc = self.cal['accel']
        g = {k: (self.tilt[v['col']] - v['offset']) / v['lsb_per_g'] for k, v in acc.items()}
        pitch = math.degrees(math.atan2(g['long'], math.hypot(g['lat'], g['vert'])))
        roll = math.degrees(math.atan2(-g['lat'], g['vert']))
        return round(pitch, 1), round(roll, 1)

    def snapshot(self, now=None):
        now = now or time.time()
        age = now - self.last_at if self.last_at else None
        att = self.attitude()
        return {
            'ok': age is not None and age < 1.0,
            'age_s': None if age is None else round(age, 2),
            'calibrated': self.cal.get('source') == 'wizard',
            'still': self.still,
            'heading': round(self.heading, 1),
            'yaw_rate': round(self.yaw_rate, 1),
            'g_lat': round(self.g_lat, 3),
            'g_long': round(self.g_long, 3),
            'pitch': att[0] if att else None,
            'roll': att[1] if att else None,
        }

    def reset_heading(self):
        self.heading = 0.0

    # ---------------------------------------------------------------- calibration
    def capture_pose(self, pose):
        """Average of the last second of samples; the kart must be still."""
        if pose not in POSES:
            raise ImuError('unknown_pose', f'Unknown pose {pose!r}', pose=pose)
        if not self.last_at or time.time() - self.last_at > 1.0:
            raise ImuError('no_imu', 'No IMU data from the kart.')
        rows = self._recent(1.0)
        spread = max(max(r[c] for r in rows) - min(r[c] for r in rows) for c in (3, 4, 5))
        if spread > STILL_GYRO_SPREAD * 2:
            raise ImuError('kart_moving', 'The kart is still moving - hold it still and try again.')
        return [_mean(rows, c) for c in range(6)]

    def spin_start(self):
        if not self.bias_ready:
            raise ImuError('gyro_zero', 'Put the kart down flat and still for a moment first (gyro zero point).')
        self.spin = {'sum': [0.0, 0.0, 0.0], 'started': time.time()}

    def spin_stop(self):
        spin, self.spin = self.spin, None
        if spin is None:
            raise ImuError('spin_not_started', 'The turn was not started.')
        axis = max(range(3), key=lambda i: abs(spin['sum'][i]))
        total = spin['sum'][axis]
        # Plausibility against the current scale; generous because it may be a guess.
        if abs(total) < 0.3 * 360 * abs(self.cal['gyro']['yaw']['lsb_per_dps']):
            raise ImuError('spin_too_small', 'Turn too small - rotate the kart exactly one full turn.')
        # A clockwise turn (seen from above) counts positive on the compass.
        return {'col': 3 + axis, 'lsb_per_dps': total / 360.0, 'integral': [round(v) for v in spin['sum']]}

    def save_calibration(self, poses, spin):
        accel, warnings = solve_accel(poses)
        if abs(spin['lsb_per_dps']) < 5:
            raise ImuError('spin_too_small', 'Turn too small - rotate the kart exactly one full turn.')
        cal = {'source': 'wizard', 'created': time.strftime('%Y-%m-%d %H:%M:%S'),
               'gyro': {'yaw': {'col': spin['col'], 'lsb_per_dps': spin['lsb_per_dps']}},
               'accel': accel, 'poses': poses}
        CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CALIBRATION_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(cal, indent=2))
        tmp.replace(CALIBRATION_PATH)
        self.cal = cal
        self.heading = 0.0
        return cal, warnings
