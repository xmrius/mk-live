#!/usr/bin/env python3
"""Web UI for Mario Kart Live control plus experimental video stream."""
import asyncio
import configparser
import io
import json
import os
import re
import socket
import struct
import time
from pathlib import Path
from aiohttp import ClientSession, ClientTimeout, web

try:
    from . import imu as imu_mod
except ImportError:          # started as a plain script next to imu.py
    import imu as imu_mod

try:
    import qrcode
except ImportError:
    qrcode = None

DRIVE_PORT = 5102
RATE_HZ = 30
VIDEO_WORKER = 'http://127.0.0.1:9001'
OPENKART_API = os.environ.get('OPENKART_API', 'http://127.0.0.1:8181')
KART_IP = os.environ.get('KART_IP', '169.254.98.97')
# Stop the kart if the browser stops sending (tab frozen, Wi-Fi dropped,
# WebSocket half-open). The page re-sends its state every 200 ms.
DRIVE_DEADMAN_SEC = float(os.environ.get('KART_DRIVE_DEADMAN_SEC', '0.6'))
VIDEO_WATCHDOG = os.environ.get('KART_VIDEO_WATCHDOG', '1') in ('1', 'true', 'yes')
VIDEO_UDP_STALE_SEC = float(os.environ.get('KART_VIDEO_UDP_STALE_SEC', '10'))
VIDEO_JPEG_STALE_SEC = float(os.environ.get('KART_VIDEO_JPEG_STALE_SEC', '9999'))
VIDEO_WATCHDOG_COOLDOWN = float(os.environ.get('KART_VIDEO_WATCHDOG_COOLDOWN', '45'))
VIDEO_WORKER_RESET_COOLDOWN = float(os.environ.get(
    'KART_VIDEO_WORKER_RESET_COOLDOWN', '10'))
VIDEO_OPENKART_RESYNC_COOLDOWN = float(os.environ.get(
    'KART_VIDEO_OPENKART_RESYNC_COOLDOWN', str(VIDEO_WATCHDOG_COOLDOWN)))
VIDEO_WATCHDOG_DOWN_SEC = float(os.environ.get('KART_VIDEO_WATCHDOG_DOWN_SEC', '3'))


def clamp(v, lo, hi):
    return max(lo, min(hi, int(v)))


class Drive:
    def __init__(self, ip):
        self.target = (ip, DRIVE_PORT)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target_source = 'env'
        self.throttle = 0
        self.steering = 0
        self.brake = False
        self.counter = 0
        self.sent = 0
        self.last_send_at = 0.0
        self.last_command_at = 0.0
        self.last_ws = None
        self.command_source = None
        self.deadman_stops = 0
        self.last_send_error = None
        self.clients = 0

    def set_target(self, ip, source='openkart'):
        if ip and ip != self.target[0]:
            self.target = (ip, DRIVE_PORT)
            self.target_source = source

    def send_one(self):
        t = clamp(self.throttle, -128, 127)
        s = clamp(self.steering, -128, 127)
        pkt = struct.pack('<bbBxI', t, s, 1 if self.brake else 0, self.counter)
        pkt += b'\x00' * (0x20 - len(pkt))
        self.sock.sendto(pkt, self.target)
        self.counter = (self.counter + 1) & 0xFFFFFFFF
        self.sent += 1
        self.last_send_at = time.time()

    def command(self, t=0, s=0, b=False, source='unknown'):
        self.throttle = clamp(t, -128, 127)
        self.steering = clamp(s, -128, 127)
        self.brake = bool(b)
        self.last_command_at = time.time()
        self.command_source = source
        self.last_ws = {'t': self.throttle, 's': self.steering, 'b': self.brake, 'source': source}

    def check_deadman(self):
        # Only browser input is guarded; scripted sources like motion refresh
        # hold one command for longer than the deadman window on purpose.
        if (self.command_source == 'ws'
                and (self.throttle or self.steering or self.brake)
                and time.time() - self.last_command_at > DRIVE_DEADMAN_SEC):
            self.deadman_stops += 1
            self.command(0, 0, False, source='deadman')


drv = Drive(KART_IP)
watchdog_status = {
    'enabled': VIDEO_WATCHDOG,
    'last_action': None,
    'last_action_at': 0.0,
    'last_worker_reset_at': 0.0,
    'last_openkart_resync_at': 0.0,
    'last_error': None,
    'worker_resets': 0,
    'openkart_resyncs': 0,
}
motion_refresh_status = {
    'active': False,
    'profile': None,
    'last_started_at': 0.0,
    'last_finished_at': 0.0,
    'last_error': None,
    'runs': 0,
}
motion_refresh_lock = asyncio.Lock()


async def drive_loop():
    period = 1 / RATE_HZ
    while True:
        drv.check_deadman()
        try:
            drv.send_one()
        except OSError as e:
            # e.g. ENETUNREACH while the AP restarts; keep the loop alive.
            drv.last_send_error = f'{type(e).__name__}: {e}'
        await asyncio.sleep(period)


STATIC_DIR = Path(__file__).with_name('static')
TELEMETRY_PORT = int(os.environ.get('KART_TELEMETRY_PORT', '19002'))
TELEMETRY_RECORD_DIR = Path(os.path.expanduser(os.environ.get(
    'KART_TELEMETRY_RECORD_DIR', '~/openkart_setup/telemetry')))


class TelemetryHub(asyncio.DatagramProtocol):
    """Receives the raw kart telemetry mirrored by the patched OpenKart (fuji.py).

    Packet byte 0 is the type (1 status, 2 IMU, 3 motion). IMU packets feed the
    live decoder (imu.py); all types can be recorded as labelled raw runs.
    File format: repeated [f64 unix time][u16 length][packet].
    """

    def __init__(self):
        self.counts = {}
        self.last = {}
        self.last_at = {}
        self.rates = {}
        self._rate_marks = {}
        self.recording = None
        self.last_recording = None
        self.imu = imu_mod.ImuDecoder()

    def datagram_received(self, data, addr):
        if not data:
            return
        now = time.time()
        typ = data[0]
        self.counts[typ] = self.counts.get(typ, 0) + 1
        self.last[typ] = data
        self.last_at[typ] = now
        if typ == 2:
            self.imu.feed(data, now)
        mark = self._rate_marks.get(typ)
        if mark is None or now - mark[0] >= 2.0:
            if mark is not None:
                self.rates[typ] = round((self.counts[typ] - mark[1]) / (now - mark[0]), 1)
            self._rate_marks[typ] = (now, self.counts[typ])
        rec = self.recording
        if rec:
            if now < rec['until']:
                rec['file'].write(struct.pack('<dH', now, len(data)) + data)
                rec['packets'] += 1
            else:
                self.stop_recording()

    def start_recording(self, label, seconds):
        self.stop_recording()
        TELEMETRY_RECORD_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[^A-Za-z0-9_-]+', '_', label)[:40] or 'run'
        path = TELEMETRY_RECORD_DIR / time.strftime(f'%Y%m%d_%H%M%S_{safe}.bin')
        self.recording = {'label': label, 'path': str(path), 'file': open(path, 'wb'),
                          'until': time.time() + seconds, 'packets': 0}
        return str(path)

    def stop_recording(self):
        rec = self.recording
        if rec:
            rec['file'].close()
            self.last_recording = {k: rec[k] for k in ('label', 'path', 'packets')}
            self.recording = None

    def status(self):
        now = time.time()
        types = {}
        for typ, count in sorted(self.counts.items()):
            pkt = self.last.get(typ, b'')
            types[str(typ)] = {'count': count, 'rate_hz': self.rates.get(typ),
                               'age_s': round(now - self.last_at[typ], 2), 'len': len(pkt),
                               'head': pkt[:48].hex()}
        rec = self.recording
        return {'port': TELEMETRY_PORT, 'types': types, 'imu': self.imu.snapshot(now),
                'recording': ({'label': rec['label'], 'packets': rec['packets'],
                               'remaining_s': round(rec['until'] - now, 1)} if rec else None),
                'last_recording': self.last_recording}


telemetry = TelemetryHub()
OPENKART_CONF = os.environ.get('OPENKART_CONF', '/etc/openkart.conf')
CHARACTERS = {1: 'Mario', 2: 'Luigi'}


async def index(req):
    return web.FileResponse(STATIC_DIR / 'index.html', headers={'Cache-Control': 'no-store'})


def wifi_interface():
    """The AP interface from the OpenKart config (the file also holds the secret; only this key is read)."""
    config = configparser.ConfigParser()
    try:
        config.read(OPENKART_CONF)
        return config.get('wireless', 'interface', fallback=None)
    except (OSError, configparser.Error):
        return None


async def ap_channel():
    """Channel the AP currently runs on; the pairing QR must carry the same channel."""
    iface = wifi_interface()
    if not iface:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            'iw', 'dev', iface, 'info',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (OSError, asyncio.TimeoutError):
        return None
    m = re.search(rb'channel (\d+)', out)
    return int(m.group(1)) if m else None


def pairing_payload(seed_hex, ssid, channel):
    """Nintendo LP2P pairing QR: seed(16) + SSID(32, NUL padded) + channel(u16 LE) + 12 zero bytes."""
    seed = bytes.fromhex(seed_hex)
    ssid_bytes = ssid.encode('ascii')
    if len(seed) != 16 or len(ssid_bytes) > 32:
        raise ValueError('unexpected pairing data')
    return seed + ssid_bytes.ljust(32, b'\x00') + channel.to_bytes(2, 'little') + bytes(12)


async def _openkart_json(session, path):
    async with session.get(f'{OPENKART_API}{path}') as resp:
        return await resp.json()


async def api_status(req):
    """Everything the HUD needs in one poll: OpenKart state, kart, drive and video health."""
    out = {'time': time.time()}
    async with ClientSession(timeout=ClientTimeout(total=1.5)) as session:
        try:
            state = await _openkart_json(session, '/v1/state')
            out['openkart'] = {'ok': True, 'state': state.get('state'),
                               'pairing': bool(state.get('pairing'))}
        except Exception as e:
            out['openkart'] = {'ok': False, 'error': f'{type(e).__name__}: {e}'}
        kart = None
        try:
            devices = (await _openkart_json(session, '/v1/devices')).get('devices') or []
            if devices:
                dev = devices[0]
                info, status = dev.get('info') or {}, dev.get('status') or {}
                kart = {
                    'serial': info.get('serial'),
                    'address': info.get('address'),
                    'character': CHARACTERS.get(info.get('character'), info.get('character')),
                    'firmware': '.'.join(map(str, (info.get('version') or {}).get('system') or [])),
                    'battery': status.get('battery'),
                    'cable': status.get('cable_connected'),
                    'signal': status.get('signal'),
                }
        except Exception:
            pass
        out['kart'] = kart
        try:
            w = await _get_worker_status(session)
            out['video'] = {
                'ok': True,
                'frames_in': w.get('frames_in'),
                'last_udp_age': w.get('last_udp_age'),
                'pipeline_latency': w.get('pipeline_latency'),
                'ws_transcode': w.get('ws_transcode'),
                'packet_loss': w.get('packet_loss'),
            }
        except Exception as e:
            out['video'] = {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    now = time.time()
    out['drive'] = {
        'clients': drv.clients,
        'target': drv.target[0],
        'sending': bool(drv.last_send_at) and now - drv.last_send_at < 0.5,
        'last_send_error': drv.last_send_error,
        'throttle': drv.throttle,
        'steering': drv.steering,
        'brake': drv.brake,
        'deadman_stops': drv.deadman_stops,
    }
    return web.json_response(out, headers={'Cache-Control': 'no-store'})


async def api_telemetry_status(req):
    return web.json_response(telemetry.status(), headers={'Cache-Control': 'no-store'})


async def api_telemetry_record(req):
    """POST {"label": "still", "seconds": 8} - record raw telemetry for calibration."""
    try:
        body = await req.json()
    except ValueError:
        body = {}
    seconds = max(1.0, min(120.0, float(body.get('seconds', 8))))
    path = telemetry.start_recording(str(body.get('label', 'run')), seconds)
    return web.json_response({'ok': True, 'path': path, 'seconds': seconds})


# Poses and full-turn result collected by the calibration wizard until saved.
imu_calib = {'poses': {}, 'spin': None}


def _imu_error(e):
    """409 with the English text plus a code the cockpit translates."""
    return web.json_response({'ok': False, 'error': str(e), 'code': getattr(e, 'code', None),
                              'params': getattr(e, 'params', {})}, status=409)


async def _json_body(req):
    try:
        return await req.json()
    except ValueError:
        return {}


async def api_imu_pose(req):
    """POST {"pose": "flat"} - average the last second of samples for one still pose."""
    pose = str((await _json_body(req)).get('pose', ''))
    try:
        imu_calib['poses'][pose] = telemetry.imu.capture_pose(pose)
    except ValueError as e:
        return _imu_error(e)
    return web.json_response({'ok': True, 'pose': pose, 'have': sorted(imu_calib['poses'])})


async def api_imu_spin(req):
    """POST {"action": "start"|"stop"} - integrate the gyro over one full turn by hand."""
    action = (await _json_body(req)).get('action')
    try:
        if action == 'start':
            telemetry.imu.spin_start()
            return web.json_response({'ok': True})
        if action == 'stop':
            imu_calib['spin'] = telemetry.imu.spin_stop()
            return web.json_response({'ok': True, 'spin': imu_calib['spin']})
    except ValueError as e:
        return _imu_error(e)
    return web.json_response({'ok': False, 'error': 'action must be start or stop'}, status=400)


async def api_imu_save(req):
    """POST - solve axes and scales from the collected poses + turn and store them."""
    if not imu_calib['spin']:
        return _imu_error(imu_mod.ImuError('spin_missing', 'The turn is still missing.'))
    try:
        cal, warnings = telemetry.imu.save_calibration(imu_calib['poses'], imu_calib['spin'])
    except (ValueError, OSError) as e:
        return _imu_error(e)
    imu_calib.update(poses={}, spin=None)
    return web.json_response({'ok': True, 'calibration': cal, 'warnings': warnings})


async def api_imu_heading_reset(req):
    telemetry.imu.reset_heading()
    return web.json_response({'ok': True})


async def api_openkart_state(req):
    """POST {"state": "RUNNING"|"DOWN"|"PAIRING"} - switch the OpenKart AP."""
    try:
        body = await req.json()
    except ValueError:
        body = {}
    state = str(body.get('state', '')).upper()
    if state not in ('RUNNING', 'DOWN', 'PAIRING'):
        return web.json_response({'ok': False, 'error': 'state must be RUNNING, DOWN or PAIRING'}, status=400)
    mark_openkart_state_change()
    async with ClientSession(timeout=ClientTimeout(total=15.0)) as session:
        try:
            code = await _post_openkart_state(session, state)
        except Exception as e:
            return web.json_response({'ok': False, 'error': f'{type(e).__name__}: {e}'}, status=502)
        if code >= 400:
            return web.json_response({'ok': False, 'error': f'OpenKart answered HTTP {code}'}, status=502)
        pairing = None
        if state == 'PAIRING':
            # The pairing AP comes up asynchronously; wait until seed/SSID are published.
            for _ in range(20):
                current = await _openkart_json(session, '/v1/state')
                pairing = current.get('pairing')
                if pairing:
                    break
                await asyncio.sleep(0.25)
    return web.json_response({'ok': True, 'state': state,
                              'pairing_ready': bool(pairing) if state == 'PAIRING' else None})


async def api_pairing_qr(req):
    """PNG of the pairing QR for the current PAIRING session (channel read from the live AP)."""
    if qrcode is None:
        return web.Response(status=503, text='python3-qrcode is not installed')
    async with ClientSession(timeout=ClientTimeout(total=2.0)) as session:
        try:
            state = await _openkart_json(session, '/v1/state')
        except Exception as e:
            return web.Response(status=502, text=f'OpenKart unreachable: {e}')
    pairing = state.get('pairing')
    if state.get('state') != 'PAIRING' or not pairing:
        return web.Response(status=409, text='OpenKart is not in PAIRING state')
    channel = await ap_channel() or 1
    try:
        payload = pairing_payload(pairing['seed'], pairing['ssid'], channel)
    except (KeyError, ValueError) as e:
        return web.Response(status=502, text=f'bad pairing data: {e}')
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=16, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color='black', back_color='white').save(buf, format='PNG')
    return web.Response(body=buf.getvalue(), content_type='image/png',
                        headers={'Cache-Control': 'no-store', 'X-Pairing-Channel': str(channel),
                                 'X-Pairing-SSID': pairing['ssid']})


async def drive_status(req):
    now = time.time()
    return web.json_response({
        'target': f'{drv.target[0]}:{drv.target[1]}',
        'target_source': drv.target_source,
        'clients': drv.clients,
        'throttle': drv.throttle,
        'steering': drv.steering,
        'brake': drv.brake,
        'sent': drv.sent,
        'last_ws': drv.last_ws,
        'last_command_age': None if not drv.last_command_at else now - drv.last_command_at,
        'last_send_age': None if not drv.last_send_at else now - drv.last_send_at,
        'deadman_sec': DRIVE_DEADMAN_SEC,
        'deadman_stops': drv.deadman_stops,
        'last_send_error': drv.last_send_error,
        'motion_refresh': motion_refresh_status,
    })


def _motion_refresh_steps(profile):
    if profile == 'curve':
        return [
            (45, 127, 1.8),
            (0, 0, 0.25),
            (45, -127, 1.8),
            (0, 0, 0.25),
            (-35, 0, 1.2),
        ]
    return [
        (35, 0, 1.4),
        (0, 0, 0.35),
        (-35, 0, 1.4),
        (0, 0, 0.35),
        (35, 0, 1.4),
        (0, 0, 0.35),
        (-35, 0, 1.4),
    ]


async def drive_motion_refresh(req):
    profile = req.query.get('profile', 'fb')
    if profile not in ('fb', 'curve'):
        profile = 'fb'
    if motion_refresh_lock.locked():
        return web.json_response({
            'ok': False,
            'error': 'motion refresh already active',
            'motion_refresh': motion_refresh_status,
        }, status=409)
    async with motion_refresh_lock:
        started = time.time()
        motion_refresh_status.update({
            'active': True,
            'profile': profile,
            'last_started_at': started,
            'last_error': None,
        })
        result = None
        try:
            for throttle, steering, seconds in _motion_refresh_steps(profile):
                drv.command(throttle, steering, False, source=f'motion-refresh:{profile}')
                await asyncio.sleep(seconds)
            motion_refresh_status['runs'] += 1
            result = {
                'ok': True,
                'profile': profile,
                'duration': time.time() - started,
            }
        except Exception as e:
            motion_refresh_status['last_error'] = f'{type(e).__name__}: {e}'
            raise
        finally:
            drv.command(0, 0, False, source=f'motion-refresh:{profile}:stop')
            motion_refresh_status['active'] = False
            motion_refresh_status['last_finished_at'] = time.time()
        result['motion_refresh'] = motion_refresh_status
        return web.json_response(result)


async def video_status(req):
    status = {}
    worker_status = None
    try:
        async with ClientSession(timeout=ClientTimeout(total=1.0)) as session:
            async with session.get(f'{VIDEO_WORKER}/status') as resp:
                worker_status = await resp.json()
    except Exception as e:
        worker_status = {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    status['worker'] = worker_status
    status['watchdog'] = watchdog_status
    return web.json_response(status)


async def _get_worker_status(session):
    async with session.get(f'{VIDEO_WORKER}/status') as resp:
        return await resp.json()


async def _post_openkart_state(session, state):
    async with session.post(f'{OPENKART_API}/v1/state',
                            json={'state': state}) as resp:
        await resp.read()
        return resp.status


async def _sync_drive_target(session):
    async with session.get(f'{OPENKART_API}/v1/devices') as resp:
        if resp.status != 200:
            await resp.read()
            return False
        data = await resp.json()
    for dev in data.get('devices') or []:
        address = (dev.get('info') or {}).get('address')
        if address:
            drv.set_target(address)
            return True
    return False


async def _reset_worker(session):
    async with session.post(f'{VIDEO_WORKER}/reset') as resp:
        await resp.read()
        return resp.status


# Set whenever the OpenKart state is changed on purpose (pairing, AP restart, resync).
# The watchdog must not "repair" the missing video during such a change: in PAIRING
# the kart is disconnected by design, and resetting to RUNNING kills the pairing AP.
openkart_state_changed_at = 0.0
WATCHDOG_GRACE_SEC = float(os.environ.get('KART_VIDEO_WATCHDOG_GRACE_SEC', '60'))


def mark_openkart_state_change():
    global openkart_state_changed_at
    openkart_state_changed_at = time.time()


async def _watchdog_may_resync(session):
    """Only resync when OpenKart is RUNNING, a kart is connected and nobody changed the state recently."""
    if time.time() - openkart_state_changed_at < WATCHDOG_GRACE_SEC:
        return False
    state = await _openkart_json(session, '/v1/state')
    if state.get('state') != 'RUNNING':
        return False
    devices = (await _openkart_json(session, '/v1/devices')).get('devices') or []
    return bool(devices)


async def video_watchdog_loop():
    if not VIDEO_WATCHDOG:
        return
    timeout = ClientTimeout(total=8.0)
    while True:
        await asyncio.sleep(2.0)
        now = time.time()
        try:
            async with ClientSession(timeout=timeout) as session:
                status = await _get_worker_status(session)
                frames_in = status.get('frames_in') or 0
                last_udp_age = status.get('last_udp_age')
                latest_age = status.get('latest_age')
                if frames_in < 30:
                    continue
                if last_udp_age is not None and last_udp_age > VIDEO_UDP_STALE_SEC:
                    if (now - watchdog_status['last_openkart_resync_at']
                            < VIDEO_OPENKART_RESYNC_COOLDOWN):
                        continue
                    if not await _watchdog_may_resync(session):
                        continue
                    watchdog_status.update({
                        'last_action': 'openkart_resync',
                        'last_action_at': now,
                        'last_openkart_resync_at': now,
                        'last_worker_reset_at': now,
                        'last_error': None,
                    })
                    watchdog_status['openkart_resyncs'] += 1
                    await _reset_worker(session)
                    await _post_openkart_state(session, 'DOWN')
                    await asyncio.sleep(VIDEO_WATCHDOG_DOWN_SEC)
                    await _post_openkart_state(session, 'RUNNING')
                    await _sync_drive_target(session)
                    continue
                if latest_age is not None and latest_age > VIDEO_JPEG_STALE_SEC:
                    if (now - watchdog_status['last_worker_reset_at']
                            < VIDEO_WORKER_RESET_COOLDOWN):
                        continue
                    watchdog_status.update({
                        'last_action': 'worker_reset',
                        'last_action_at': now,
                        'last_worker_reset_at': now,
                        'last_error': None,
                    })
                    watchdog_status['worker_resets'] += 1
                    await _reset_worker(session)
        except Exception as e:
            watchdog_status['last_error'] = f'{type(e).__name__}: {e}'


async def drive_target_loop():
    timeout = ClientTimeout(total=3.0)
    async with ClientSession(timeout=timeout) as session:
        while True:
            try:
                await _sync_drive_target(session)
            except Exception:
                pass
            await asyncio.sleep(2.0)


async def worker_status(req):
    try:
        async with ClientSession(timeout=ClientTimeout(total=1.0)) as session:
            async with session.get(f'{VIDEO_WORKER}/status') as resp:
                body = await resp.read()
                return web.Response(body=body, status=resp.status, content_type='application/json')
    except Exception as e:
        return web.json_response({'ok': False, 'error': f'{type(e).__name__}: {e}'}, status=502)


async def video_resync(req):
    mark_openkart_state_change()
    attempts = int(req.query.get('attempts', '5'))
    attempts = max(1, min(12, attempts))
    wait = float(req.query.get('wait', '8'))
    wait = max(3.0, min(20.0, wait))
    results = []
    async with ClientSession(timeout=ClientTimeout(total=20.0)) as session:
        for attempt in range(1, attempts + 1):
            row = {'attempt': attempt}
            try:
                row['worker_reset'] = await _reset_worker(session)
                row['down'] = await _post_openkart_state(session, 'DOWN')
                await asyncio.sleep(2.0)
                row['running'] = await _post_openkart_state(session, 'RUNNING')
                await asyncio.sleep(wait)
                status = await _get_worker_status(session)
                quality = status.get('last_quality') or {}
                row['status'] = {
                    'frames_in': status.get('frames_in'),
                    'jpeg_out': status.get('jpeg_out'),
                    'latest_age': status.get('latest_age'),
                    'quality_checked': status.get('quality_checked'),
                    'quality_rejected': status.get('quality_rejected'),
                    'last_quality': quality,
                }
                results.append(row)
                latest_age = status.get('latest_age')
                frames_in = status.get('frames_in') or 0
                jpeg_out = status.get('jpeg_out') or 0
                quality_ok = quality.get('ok')
                if (latest_age is not None and latest_age < 2.5
                        and frames_in >= 30 and jpeg_out >= 1
                        and quality_ok is not False):
                    return web.json_response({
                        'ok': True,
                        'attempts': results,
                    })
            except Exception as e:
                row['error'] = f'{type(e).__name__}: {e}'
                results.append(row)
    return web.json_response({
        'ok': False,
        'attempts': results,
    }, status=503)


async def worker_latest_jpg(req):
    try:
        async with ClientSession(timeout=ClientTimeout(total=1.0)) as session:
            async with session.get(f'{VIDEO_WORKER}/latest.jpg') as resp:
                body = await resp.read()
                return web.Response(body=body, status=resp.status, content_type=resp.headers.get('Content-Type', 'image/jpeg'),
                                    headers={'Cache-Control': 'no-store'})
    except Exception as e:
        return web.Response(status=502, text=f'worker image unavailable: {type(e).__name__}: {e}')


async def worker_video_mjpg(req):
    try:
        resp = web.StreamResponse(headers={
            'Content-Type': 'multipart/x-mixed-replace; boundary=frame',
            'Cache-Control': 'no-store',
        })
        await resp.prepare(req)
        async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=1.0)) as session:
            async with session.get(f'{VIDEO_WORKER}/video.mjpg') as upstream:
                async for chunk in upstream.content.iter_chunked(8192):
                    await resp.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception as e:
        return web.Response(status=502, text=f'worker stream unavailable: {type(e).__name__}: {e}')
    return resp


async def worker_video_mp4(req):
    resp = None
    try:
        async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=1.0)) as session:
            async with session.get(f'{VIDEO_WORKER}/video.mp4') as upstream:
                resp = web.StreamResponse(
                    status=upstream.status,
                    headers={
                        'Content-Type': upstream.headers.get('Content-Type', 'video/mp4'),
                        'Cache-Control': 'no-store',
                    })
                await resp.prepare(req)
                async for chunk in upstream.content.iter_chunked(8192):
                    await resp.write(chunk)
                return resp
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception as e:
        return web.Response(status=502, text=f'worker mp4 unavailable: {type(e).__name__}: {e}')
    return resp if resp is not None else web.Response(status=499)


async def _push_telemetry(ws):
    """Stream the decoded IMU values to one cockpit at 20 Hz."""
    try:
        while not ws.closed:
            snap = telemetry.imu.snapshot()
            if snap['ok']:
                await ws.send_str(json.dumps({'telem': snap}))
            await asyncio.sleep(0.05)
    except (ConnectionResetError, RuntimeError):
        pass     # socket closed while sending; ws_handler cleans up


async def ws_handler(req):
    ws = web.WebSocketResponse()
    await ws.prepare(req)
    drv.clients += 1
    pusher = asyncio.create_task(_push_telemetry(ws))
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                d = json.loads(msg.data)
                t, s, b = d.get('t', 0), d.get('s', 0), d.get('b', False)
                # The page heartbeats its (neutral) state; don't let that cancel
                # a running motion refresh. Real input still takes over.
                if motion_refresh_lock.locked() and not (t or s or b):
                    continue
                drv.command(t, s, b, source='ws')
    finally:
        pusher.cancel()
        drv.clients = max(0, drv.clients - 1)
        drv.command(0, 0, False, source='ws-close')
    return ws


async def start():
    # asyncio only keeps weak references to tasks; hold them for the process
    # lifetime so the drive loop cannot be garbage collected.
    loop = asyncio.get_running_loop()
    # Keep a reference to the transport for the process lifetime.
    telemetry_transport, _ = await loop.create_datagram_endpoint(
        lambda: telemetry, local_addr=('127.0.0.1', TELEMETRY_PORT))
    tasks = [
        asyncio.create_task(drive_loop()),
        asyncio.create_task(drive_target_loop()),
        asyncio.create_task(video_watchdog_loop()),
    ]
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/api/status', api_status)
    app.router.add_post('/api/openkart/state', api_openkart_state)
    app.router.add_get('/api/pairing/qr.png', api_pairing_qr)
    app.router.add_get('/api/telemetry/status', api_telemetry_status)
    app.router.add_post('/api/telemetry/record', api_telemetry_record)
    app.router.add_post('/api/imu/pose', api_imu_pose)
    app.router.add_post('/api/imu/spin', api_imu_spin)
    app.router.add_post('/api/imu/save', api_imu_save)
    app.router.add_post('/api/imu/heading/reset', api_imu_heading_reset)
    app.router.add_static('/static/', STATIC_DIR)
    app.router.add_get('/ws', ws_handler)
    app.router.add_get('/drive/status', drive_status)
    app.router.add_post('/drive/motion-refresh', drive_motion_refresh)
    app.router.add_get('/video/status', video_status)
    app.router.add_post('/video/resync', video_resync)
    app.router.add_get('/worker/status', worker_status)
    app.router.add_get('/worker/latest.jpg', worker_latest_jpg)
    app.router.add_get('/worker/video.mjpg', worker_video_mjpg)
    app.router.add_get('/worker/video.mp4', worker_video_mp4)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 9000)
    await site.start()
    print(f'Browser: http://<host>:9000  (driving {drv.target[0]})', flush=True)
    await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(start())
