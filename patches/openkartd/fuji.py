# Copyright 2021, Sam Edwards <CFSworks@gmail.com>
# SPDX-License-Identifier: BSD-3-Clause

import asyncio
import struct
import time
import logging
import socket
import os
import json

from dataclasses import dataclass

from .util import pad_to, retry_connect
from .rcd import RcdClient, RcdDevice, RcdError, RcdMsg
from .lp2p import GroupInfo

l = logging.getLogger(__name__)

DRIVE_PORT   = 5102
CONTROL_PORT = 5103
PAIRING_PORT = 5106
EVENT_PORT   = 5107

MAC_FORMAT = ':'.join('%02x' for x in range(6))

# Raw telemetry packets are mirrored to the drive UI (mk-live drive_web.py), like
# the video packets go to the video worker on 19001. Port 0 disables forwarding.
TELEMETRY_FORWARD_PORT = int(os.environ.get('OPENKART_TELEMETRY_FORWARD_PORT', '19002'))

@dataclass
class FujiSystemInfo:
    boot_version: tuple
    system_version: tuple
    sha1: str

    @classmethod
    def decode(cls, data: bytes):
        return cls(
            boot_version=(data[0], data[1]),
            system_version=(data[2], data[3]),
            sha1=data[4:].split(b'\0', 1)[0].decode('latin1'),
        )


@dataclass
class FujiProductCode:
    unk1: int
    character: int
    unk2: int
    serial: str

    @classmethod
    def decode(cls, data: bytes):
        (unk1, character, unk2), serial = struct.unpack('<HHB', data[:5]), data[5:]

        serial = serial.split(b'\0', 1)[0].decode('latin1')

        return cls(
            unk1=unk1,
            character=character,
            unk2=unk2,
            serial=serial,
        )


class FujiControlClient(RcdClient):
    SERVICE_ID = 0x100

    async def get_system_info(self):
        resp = await self.invoke(self.SERVICE_ID, 1)

        return FujiSystemInfo.decode(resp)

    async def set_param(self, param: str, value: bytes):
        param = param.encode()
        assert len(param) < 0x80

        request  = pad_to(param, 0x80)
        request += pad_to(len(value).to_bytes(2, 'big'), 0x10)
        request += value

        await self.invoke(self.SERVICE_ID, 2, request)

    async def set_connection_info(self, *, telemetry_port, video_control_port,
                                  video_stream_port, unk=1, timestamp=None):
        if timestamp is None:
            timestamp = int(time.time())

        await self.set_param('connection_info',
                             struct.pack('<HHHHQ', unk, telemetry_port,
                                         video_control_port, video_stream_port,
                                         timestamp))

    async def get_param(self, param: str):
        param = param.encode()
        assert len(param) < 0x80

        request = pad_to(param, 0x80)

        resp = await self.invoke(self.SERVICE_ID, 3, request)

        x = int.from_bytes(resp[:2], 'big')
        return resp[0x10:][:x]

    # Special case of above
    async def get_product_code(self):
        resp = await self.get_param('product_code')

        return FujiProductCode.decode(resp)

    async def get_application_data(self, application_id: str,
                                   max_size: int = 0x800):
        app_id = application_id.encode('ascii')
        assert len(app_id) <= 0x10
        assert 0 <= max_size <= 0xffffffff

        request = pad_to(app_id, 0x10)
        request += max_size.to_bytes(4, 'big')
        request = pad_to(request, 0x20)

        return await self.invoke(self.SERVICE_ID, 0x12, request)

    async def set_state(self, state: int):
        request = pad_to(bytes([state]), 0x10)

        await self.invoke(self.SERVICE_ID, 4, request)

    async def shutdown(self):
        await self.invoke(self.SERVICE_ID, 9)


class FujiPairingClient(RcdClient):
    SERVICE_ID = 0x102

    async def set_group_info(self, group_info: GroupInfo):
        assert len(group_info.psk) == 0x20

        ssid = group_info.ssid.encode()

        request  = pad_to(ssid, 0x20)
        request += group_info.psk

        await self.invoke(self.SERVICE_ID, 1, request)


class Fuji:
    """Client class for representing a 'Fuji' (Mario Kart Live kart) device."""

    @classmethod
    async def connect(cls, device: RcdDevice):
        assert device.name == 'Fuji'

        host = device.address
        try:
            (_, event), (_, control) = await asyncio.gather(*(
                retry_connect(proto, host, port) for proto, port in [
                    (RcdClient, EVENT_PORT),
                    (FujiControlClient, CONTROL_PORT)
                ]))

        except (ConnectionError, asyncio.CancelledError):
            device.close()
            raise

        else:
            return cls(device, event, control)

    @classmethod
    async def pair(cls, device: RcdDevice, group_info: GroupInfo):
        assert device.name == 'Fuji'

        client = None

        try:
            _, client = await retry_connect(FujiPairingClient,
                                            device.address, PAIRING_PORT)
            await client.set_group_info(group_info)

        except (ConnectionError, RcdError) as e:
            l.warning('Pairing with %s failed: %s', device.address, e)
            return False

        else:
            l.info('Pairing with %s succeeded', device.address)
            return True

        finally:
            if client:
                client.close()
            device.close()

    def __init__(self, device, event, control):
        self.device = device
        self.event = event
        self.control = control

        self.address = device.address
        self.mac_address = MAC_FORMAT % tuple(device.ident[-6:])
        self.system_info = None
        self.product_code = None

        self.battery_state = 0
        self.cable_connected = False
        self.signal = None

        self.telemetry_socket = None
        self.video_socket = None
        self.video_control_rendezvous = None
        self.video_control_socket = None
        self._video_control_transport = None
        self._video_forward_socket = None
        self._telemetry_forward_socket = None
        self._video_probe_count = 0
        self._video_control_probe_count = 0
        self._video_pi_count = 0
        self._video_lvni_count = 0
        self._video_lvni_prev_id = 0
        self._video_lvni_current_id = 0
        self._video_lvni_flags = 0
        self._video_lvni_frame_count = 0
        self._video_lvni_last_frame_at = 0.0
        self._video_fram_probe_count = 0
        self._video_last_tail_be = None
        self._video_control_trace_path = os.environ.get(
            'OPENKART_VIDEO_CONTROL_TRACE_PATH',
            '/var/lib/mk-live/video_control_trace.jsonl')
        self._car_pipe_net_socket = None
        self._car_pipe_net_task = None
        self._car_pipe_net_recv_task = None
        self._car_pipe_net_keyframe_counter = 0
        self._car_pipe_net_ping_count = 0
        self._car_pipe_net_keyframe_count = 0
        self._car_pipe_net_probe_packet_count = 0
        self._car_pipe_net_recv_count = 0
        self._status_probe_count = 0
        self._imu_probe_count = 0
        self._telemetry_type3_probe_count = 0
        self._switch_prc_status_probe_count = 0
        self._switch_prc_type1 = None
        self._switch_prc_type3 = None

        self.__telemetry_event = asyncio.Event()
        self.__poll_ap_task = None
        self.__event_probe_task = None
        self.__video_control_task = None
        self.__video_control_keepalive_task = None

    def _trace_video_control(self, event: str, **fields):
        enabled = os.environ.get('OPENKART_VIDEO_CONTROL_TRACE', '1').lower()
        if enabled not in ('1', 'true', 'yes'):
            return
        row = {
            't': time.time(),
            'event': event,
            **fields,
        }
        try:
            os.makedirs(os.path.dirname(self._video_control_trace_path),
                        exist_ok=True)
            with open(self._video_control_trace_path, 'a',
                      encoding='utf-8') as f:
                f.write(json.dumps(row, sort_keys=True) + '\n')
        except Exception as e:
            l.info('video-control trace write failed: %r', e)

    def _video_control_init_payload(self) -> bytes:
        hex_data = ''.join(os.environ.get(
            'OPENKART_VIDEO_CONTROL_INIT_HEX', '').split())
        if hex_data:
            try:
                return bytes.fromhex(hex_data)
            except ValueError as e:
                l.warning('video-control init hex invalid: %s', e)
                return b''
        try:
            with open(os.environ.get('OPENKART_VIDEO_INIT_PATH',
                                     '/var/lib/mk-live/video_init.bin'), 'rb') as f:
                return f.read()
        except FileNotFoundError:
            return b''

    def close(self):
        self.device.close()
        self.event.close()
        self.control.close()

        if self.telemetry_socket:
            try:
                self.telemetry_socket.unregister(self.device.address)
            except KeyError:
                pass
            self.telemetry_socket = None

        if self.video_socket:
            try:
                self.video_socket.unregister(self.device.address)
            except KeyError:
                pass
            self.video_socket = None

        if self._video_forward_socket:
            self._video_forward_socket.close()
            self._video_forward_socket = None

        if self._telemetry_forward_socket:
            self._telemetry_forward_socket.close()
            self._telemetry_forward_socket = None

        if self._video_control_transport:
            self._video_control_transport.close()
            self._video_control_transport = None

        if self.video_control_socket:
            try:
                self.video_control_socket.unregister(self.device.address)
            except KeyError:
                pass
            self.video_control_socket = None

        if self._car_pipe_net_task:
            self._car_pipe_net_task.cancel()
            self._car_pipe_net_task = None

        if self._car_pipe_net_recv_task:
            self._car_pipe_net_recv_task.cancel()
            self._car_pipe_net_recv_task = None

        if self._car_pipe_net_socket:
            self._car_pipe_net_socket.close()
            self._car_pipe_net_socket = None

        if self.__poll_ap_task:
            self.__poll_ap_task.cancel()

        if self.__event_probe_task:
            self.__event_probe_task.cancel()

        if self.__video_control_task:
            self.__video_control_task.cancel()

        if self.__video_control_keepalive_task:
            self.__video_control_keepalive_task.cancel()

    async def setup(self, ports, *, ap=None):
        self.system_info = await self.control.get_system_info()
        self.product_code = await self.control.get_product_code()
        app_id = os.environ.get('OPENKART_APP_DATA_ID',
                                'YVCOQ00000000XFB')
        app_data_probe = os.environ.get('OPENKART_APP_DATA_PROBE',
                                        '1').lower()
        if app_data_probe not in ('0', 'false', 'no', 'off'):
            try:
                app_data = await self.control.get_application_data(app_id)
                l.info('app-data probe id=%s len=%d head=%s',
                       app_id, len(app_data), app_data[:64].hex())
            except (EOFError, RcdError, ValueError) as e:
                l.warning('app-data probe id=%s failed: %s', app_id, e)

        async def _event_probe():
            while True:
                try:
                    msg = await self.event.receive()
                except EOFError:
                    break

                l.info('event-probe service=0x%04x command=%d status=0x%x '
                       'response=%s len=%d head=%s',
                       msg.service, msg.command, msg.status, msg.is_response,
                       len(msg.data), msg.data[:64].hex())
                if msg.data:
                    words_le = [
                        int.from_bytes(msg.data[i:i + 4], 'little')
                        for i in range(0, len(msg.data) - 3, 4)
                    ]
                    words_be = [
                        int.from_bytes(msg.data[i:i + 4], 'big')
                        for i in range(0, len(msg.data) - 3, 4)
                    ]
                    l.info('event-probe words_le=%s words_be=%s',
                           words_le, words_be)

                if not msg.is_response:
                    response_data = self._make_event_response(msg)
                    self.event.send(RcdMsg(
                        service=msg.service,
                        command=msg.command,
                        status=0,
                        is_response=True,
                        data=response_data,
                    ))
                    l.info('event-probe response service=0x%04x command=%d '
                           'mode=%s len=%d head=%s',
                           msg.service, msg.command,
                           os.environ.get('OPENKART_EVENT_RESPONSE_MODE',
                                          'empty'),
                           len(response_data), response_data[:64].hex())

        self.__event_probe_task = asyncio.create_task(_event_probe())

        self.telemetry_socket = ports.udp1
        self.video_socket = ports.udp2
        self.video_control_rendezvous = ports.tcp1
        self.video_control_socket = ports.udp3
        self._video_probe_count = 0
        self._video_control_probe_count = 0
        self._video_fram_probe_count = 0
        self._video_last_tail_be = None

        try:
            self.telemetry_socket.unregister(self.device.address)
        except KeyError:
            pass
        try:
            self.video_socket.unregister(self.device.address)
        except KeyError:
            pass
        try:
            self.video_control_socket.unregister(self.device.address)
        except KeyError:
            pass
        self.telemetry_socket.register(self.device.address, self.handle_telemetry)
        self.video_socket.register(self.device.address, self.handle_video)

        use_udp_video_control = os.environ.get(
            'OPENKART_VIDEO_CONTROL_UDP', '0').lower() in ('1', 'true', 'yes')
        if use_udp_video_control:
            self.video_control_socket.register(self.device.address,
                                               self.handle_video_control)

        await self.control.set_connection_info(
            telemetry_port=self.telemetry_socket.port,
            video_control_port=(self.video_control_socket.port
                                if use_udp_video_control
                                else self.video_control_rendezvous.port),
            video_stream_port=self.video_socket.port,
        )

        await self.__telemetry_event.wait()

        class _VideoControlProbe(asyncio.Protocol):
            def connection_made(probe_self, transport):
                probe_self.transport = transport
                self._video_control_transport = transport
                peer = transport.get_extra_info('peername')
                l.info('video-control TCP connected from %s', peer)
                self._trace_video_control('connect', peer=repr(peer))
                data = self._video_control_init_payload()
                if data:
                    transport.write(data)
                    l.info('video-control sent init len=%d head=%s',
                            len(data), data[:64].hex())
                    self._trace_video_control('send_init', length=len(data),
                                              hex=data.hex())
                else:
                    l.info('video-control no initial payload configured')
                    self._trace_video_control('send_init_empty')

            def data_received(probe_self, data):
                l.info('video-control recv len=%d head=%s',
                        len(data), data[:64].hex())
                self._trace_video_control('recv', length=len(data),
                                          hex=data.hex())

            def connection_lost(probe_self, exc):
                l.info('video-control TCP closed: %s', exc)
                self._trace_video_control('close', error=repr(exc))
                if self._video_control_transport is probe_self.transport:
                    self._video_control_transport = None

        if not use_udp_video_control:
            async def _video_control_accept_loop():
                while True:
                    try:
                        await self.video_control_rendezvous.expect(
                            _VideoControlProbe, self.device.address)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        l.info('video-control rendezvous accept failed: %r', e)
                    await asyncio.sleep(0.1)

            self.__video_control_task = asyncio.create_task(
                _video_control_accept_loop())
            self.__video_control_keepalive_task = asyncio.create_task(
                self.__video_control_keepalive())

        try:
            await asyncio.wait_for(self.control.set_state(1), timeout=1.5)
            l.info('set_state(1) for video returned OK')
        except (asyncio.TimeoutError, EOFError, RcdError) as e:
            l.info('set_state(1) for video returned/closed: %r', e)

        self._start_car_pipe_net_probe()

        if ap:
            self.__poll_ap_task = asyncio.create_task(self.__poll_ap(ap))

    async def __poll_ap(self, ap):
        while True:
            await asyncio.sleep(0.1)
            mib = await ap.get_mib(self.mac_address)
            signal = mib.get('signal')
            try:
                self.signal = int(signal)
            except (TypeError, ValueError):
                pass

    async def __video_control_keepalive(self):
        enabled = os.environ.get('OPENKART_VIDEO_LVNI_KEEPALIVE', '1').lower()
        if enabled not in ('1', 'true', 'yes'):
            return

        interval = float(os.environ.get(
            'OPENKART_VIDEO_LVNI_INTERVAL', '1.0'))
        while True:
            await asyncio.sleep(interval)
            self._send_video_lvni('keepalive')

    async def shutdown(self):
        await self.control.shutdown()
        self.close()

    def _make_event_response(self, msg: RcdMsg):
        mode = os.environ.get('OPENKART_EVENT_RESPONSE_MODE', 'empty').lower()
        target = os.environ.get('OPENKART_EVENT_RESPONSE_SERVICE', '0x0103')
        try:
            target_service = int(target, 0)
        except ValueError:
            target_service = 0x0103

        if msg.service != target_service:
            return b''

        custom_hex = os.environ.get('OPENKART_EVENT_RESPONSE_HEX', '').strip()
        if custom_hex:
            return bytes.fromhex(custom_hex.replace(' ', ''))
        if mode == 'echo':
            return msg.data
        if mode == 'first_word':
            return msg.data[:4].ljust(4, b'\0')
        if mode == 'word_0301':
            return (0x301).to_bytes(4, 'little') + b'\0' * 12
        if mode == 'word_0100':
            return (0x100).to_bytes(4, 'little') + b'\0' * 12
        return b''

    def send_video_control(self, data: bytes):
        transport = self._video_control_transport
        if transport is None or transport.is_closing():
            raise RuntimeError('video-control TCP is not connected')
        transport.write(data)
        l.info('video-control debug write len=%d head=%s',
                len(data), data[:64].hex())
        self._trace_video_control('send_debug', length=len(data),
                                  hex=data.hex())

    def _start_car_pipe_net_probe(self):
        enabled = os.environ.get('OPENKART_CAR_PIPE_NET_PROBE', '0').lower()
        if enabled not in ('1', 'true', 'yes'):
            return
        if self._car_pipe_net_task:
            return

        self._car_pipe_net_socket = socket.socket(socket.AF_INET,
                                                  socket.SOCK_DGRAM)
        self._car_pipe_net_socket.setblocking(False)
        bind_port = int(os.environ.get(
            'OPENKART_CAR_PIPE_NET_BIND_PORT', '0'))
        if bind_port:
            self._car_pipe_net_socket.bind(('', bind_port))

        self._car_pipe_net_task = asyncio.create_task(
            self._car_pipe_net_probe_loop())
        self._car_pipe_net_recv_task = asyncio.create_task(
            self._car_pipe_net_recv_loop())

    async def _car_pipe_net_probe_loop(self):
        ping_interval = float(os.environ.get(
            'OPENKART_CAR_PIPE_NET_PING_INTERVAL', '1.0'))
        keyframe_interval = float(os.environ.get(
            'OPENKART_CAR_PIPE_NET_KEYFRAME_INTERVAL', '2.0'))
        port = int(os.environ.get('OPENKART_CAR_PIPE_NET_PORT', '3334'))
        sequence = os.environ.get('OPENKART_CAR_PIPE_NET_SEQUENCE', '').strip()
        next_keyframe_at = 0.0

        while True:
            now = time.monotonic()
            if ping_interval >= 0:
                self._send_car_pipe_net_ping(port)
            if keyframe_interval >= 0 and now >= next_keyframe_at:
                if sequence:
                    self._send_car_pipe_net_sequence(port, sequence)
                else:
                    self._send_car_pipe_net_keyframe(port)
                next_keyframe_at = now + keyframe_interval
            await asyncio.sleep(max(0.05, ping_interval))

    def _send_car_pipe_net(self, packet: bytes, port: int, label: str,
                           count: int):
        if not self._car_pipe_net_socket:
            return
        self._car_pipe_net_socket.sendto(packet, (self.device.address, port))
        if count <= 8 or count % 30 == 0:
            l.info('car-pipe-net sent %s #%d to %s:%d len=%d head=%s',
                   label, count, self.device.address, port, len(packet),
                   packet.hex())

    def _send_car_pipe_net_ping(self, port: int):
        # Ping: VV type 5 followed by an unaligned u64 timestamp.
        timestamp = int(time.monotonic() * 1000) & ((1 << 64) - 1)
        packet = b'VV' + bytes([5]) + timestamp.to_bytes(8, 'little')
        self._car_pipe_net_ping_count += 1
        self._send_car_pipe_net(packet, port, 'ping', self._car_pipe_net_ping_count)

    def _send_car_pipe_net_keyframe(self, port: int):
        # Keyframe request: 8 bytes, "VV" at bytes 0..1, type 3 at byte 2, rest zero.
        packet = b'VV' + bytes([3, 0, 0, 0, 0, 0])
        self._car_pipe_net_keyframe_counter = (
            self._car_pipe_net_keyframe_counter + 1) & 0xffffffff
        self._car_pipe_net_keyframe_count += 1
        self._send_car_pipe_net(packet, port, 'keyframe',
                                self._car_pipe_net_keyframe_count)

    def _send_car_pipe_net_probe_packet(self, packet: bytes, port: int,
                                        label: str):
        self._car_pipe_net_probe_packet_count += 1
        self._send_car_pipe_net(packet, port, label,
                                self._car_pipe_net_probe_packet_count)

    def _car_pipe_net_arg_bytes(self, env_name: str, count: int):
        raw = os.environ.get(env_name, '')
        if not raw:
            return [0] * count
        out = []
        for part in raw.replace(';', ',').split(','):
            part = part.strip()
            if part:
                out.append(int(part, 0) & 0xff)
        return (out + [0] * count)[:count]

    def _send_car_pipe_net_type1(self, port: int):
        # VV type 1: one rolling byte, then two command bytes.
        args = self._car_pipe_net_arg_bytes(
            'OPENKART_CAR_PIPE_NET_TYPE1_ARGS', 2)
        seq = self._car_pipe_net_keyframe_counter & 0xff
        packet = b'VV' + bytes([1, seq, args[0], args[1]])
        self._send_car_pipe_net_probe_packet(packet, port, 'type1')

    def _send_car_pipe_net_type2(self, port: int):
        # VV type 2: args are stored as [type=2, w2, w1].
        args = self._car_pipe_net_arg_bytes(
            'OPENKART_CAR_PIPE_NET_TYPE2_ARGS', 2)
        packet = b'VV' + bytes([2, args[1], args[0]])
        self._send_car_pipe_net_probe_packet(packet, port, 'type2')

    def _send_car_pipe_net_type8(self, port: int):
        # VV type 8: followed by 21 bytes of state/config.
        raw = os.environ.get('OPENKART_CAR_PIPE_NET_TYPE8_HEX', '')
        payload = bytes.fromhex(raw.replace(' ', '')) if raw else (b'\0' * 21)
        packet = b'VV' + bytes([8]) + payload[:21].ljust(21, b'\0')
        self._send_car_pipe_net_probe_packet(packet, port, 'type8')

    def _send_car_pipe_net_sequence(self, port: int, sequence: str):
        for token in sequence.replace(';', ',').split(','):
            token = token.strip().lower()
            if not token:
                continue
            if token in ('1', 'type1'):
                self._send_car_pipe_net_type1(port)
            elif token in ('2', 'type2'):
                self._send_car_pipe_net_type2(port)
            elif token in ('8', 'type8'):
                self._send_car_pipe_net_type8(port)
            elif token in ('3', 'keyframe', 'idr'):
                self._send_car_pipe_net_keyframe(port)
            elif token in ('5', 'ping'):
                self._send_car_pipe_net_ping(port)
            else:
                l.warning('unknown car-pipe-net sequence token %r', token)

    async def _car_pipe_net_recv_loop(self):
        loop = asyncio.get_running_loop()
        while self._car_pipe_net_socket:
            data, addr = await loop.sock_recvfrom(
                self._car_pipe_net_socket, 2048)
            self._car_pipe_net_recv_count += 1
            if self._car_pipe_net_recv_count <= 16 or (
                    self._car_pipe_net_recv_count % 30 == 0):
                l.info('car-pipe-net recv #%d from %s len=%d head=%s',
                       self._car_pipe_net_recv_count, addr, len(data),
                       data[:64].hex())

    def handle_telemetry(self, data: bytes):
        if not data:
            l.warning('Received empty telemetry packet from %s',
                      self.device.address)
            return

        telemetry_type, telemetry_data = data[0], data[1:]
        self._forward_telemetry(data)
        self._update_switch_prc_status(telemetry_type, data)

        if telemetry_type == 1:
            self.handle_status(telemetry_data)

        elif telemetry_type == 2:
            self.handle_imu(telemetry_data)

        elif telemetry_type == 3:
            self.handle_type3_telemetry(telemetry_data)

        else:
            l.warning('%s sent unknown telemetry type 0x%02x',
                      self.device.address, telemetry_type)

    def _forward_telemetry(self, data: bytes):
        if not TELEMETRY_FORWARD_PORT:
            return
        if self._telemetry_forward_socket is None:
            self._telemetry_forward_socket = socket.socket(socket.AF_INET,
                                                           socket.SOCK_DGRAM)
        try:
            self._telemetry_forward_socket.sendto(
                data, ('127.0.0.1', TELEMETRY_FORWARD_PORT))
        except OSError as e:
            l.debug('telemetry forward failed: %r', e)

    def _update_switch_prc_status(self, telemetry_type: int, packet: bytes):
        if telemetry_type == 1 and len(packet) == 0x20:
            self._switch_prc_type1 = packet
        elif telemetry_type == 3 and len(packet) >= 0x58:
            self._switch_prc_type3 = packet[:0x58]
        else:
            return

        if not self._switch_prc_type1 or not self._switch_prc_type3:
            return

        status = bytearray(0x60)
        type1 = self._switch_prc_type1
        type3 = self._switch_prc_type3

        # Builds a 0x60-byte PRC status snapshot from the stored type-1 and
        # type-3 telemetry packets.
        status[0:4] = type1[4:8]
        status[4] = type1[1]
        status[8:12] = type1[8:12]
        status[0x10:0x18] = type3[72:80]
        status[0x18] = type3[80]
        status[0x19] = type3[81]
        status[0x1c:0x20] = type3[84:88]
        status[0x20:0x30] = type3[8:24]
        status[0x30:0x40] = type3[24:40]
        status[0x40:0x50] = type3[40:56]
        status[0x50:0x60] = type3[56:72]

        self._switch_prc_status_probe_count += 1
        if (self._switch_prc_status_probe_count <= 12
                or self._switch_prc_status_probe_count % 300 == 0):
            words_le = [
                int.from_bytes(status[i:i + 4], 'little')
                for i in range(0, len(status), 4)
            ]
            l.info('switch-prc-status #%d source=type%d clean_bit=%d '
                   'head=%s words_le=%s',
                   self._switch_prc_status_probe_count, telemetry_type,
                   status[4] & 1, status.hex(), words_le)

    def handle_video(self, data: bytes):
        self._video_probe_count += 1
        if self._video_probe_count <= 5:
            l.info('video-probe #%d from %s len=%d head=%s',
                   self._video_probe_count, self.device.address, len(data),
                   data[:64].hex())

        if self._video_forward_socket is None:
            self._video_forward_socket = socket.socket(socket.AF_INET,
                                                       socket.SOCK_DGRAM)

        self._update_video_lvni(data)
        self._probe_video_fram(data)
        self._maybe_send_frame_lvni()
        self._maybe_send_video_pi(data)
        self._video_forward_socket.sendto(data, ('127.0.0.1', 19001))

    def handle_video_control(self, data: bytes):
        self._video_control_probe_count += 1
        if self._video_control_probe_count <= 12:
            l.info('video-control UDP #%d from %s len=%d head=%s',
                   self._video_control_probe_count, self.device.address,
                   len(data), data[:96].hex())

    def _maybe_send_video_pi(self, data: bytes):
        if os.environ.get('OPENKART_VIDEO_PI_ACK', '0') not in ('1', 'true', 'yes'):
            return
        transport = self._video_control_transport
        if transport is None or transport.is_closing():
            return
        if len(data) < 48:
            return

        payload = data[12:]
        if len(payload) < 9 or payload[1:5] != b'FRAM':
            return

        header_len = payload[0] + 4
        if header_len > len(payload):
            return

        mode = os.environ.get('OPENKART_VIDEO_PI_MODE', 'meta').lower()
        if mode == 'switch_like':
            # Real PI packets are not a raw metadata echo: "PI\0\0\0\0\0\0"
            # followed by five big-endian fields. Kept gated until the source
            # fields are identified from FRAM/tail logs.
            return
        if mode == 'fram':
            fields = payload[1:header_len]
        else:
            fields = payload[9:header_len]

        packet = b'PI' + b'\0' * 6 + fields[:40].ljust(40, b'\0')
        transport.write(packet)
        self._trace_video_control('send_pi', mode=mode, length=len(packet),
                                  hex=packet.hex())
        self._video_pi_count += 1
        if self._video_pi_count <= 8 or self._video_pi_count % 300 == 0:
            l.info('video-control sent PI ack #%d mode=%s len=%d head=%s',
                   self._video_pi_count, mode, len(packet), packet[:64].hex())

    def _probe_video_fram(self, data: bytes):
        enabled = os.environ.get('OPENKART_VIDEO_FRAM_PROBE', '1').lower()
        if enabled not in ('1', 'true', 'yes'):
            return
        if len(data) < 24:
            return

        payload = data[12:]
        if len(payload) < 9 or payload[1:5] != b'FRAM':
            return

        header_len = payload[0] + 4
        if header_len > len(payload):
            return

        self._video_fram_probe_count += 1
        probe_every = int(os.environ.get('OPENKART_VIDEO_FRAM_PROBE_EVERY',
                                         '300'))
        should_log = (
            self._video_fram_probe_count <= 12
            or (probe_every > 0
                and self._video_fram_probe_count % probe_every == 0)
        )
        if not should_log:
            return

        meta = payload[9:header_len]
        tail = data[-8:]
        tail_be = int.from_bytes(tail, 'big')
        tail_le = int.from_bytes(tail, 'little')
        tail_delta = None
        if self._video_last_tail_be is not None:
            tail_delta = (tail_be - self._video_last_tail_be) & ((1 << 64) - 1)
        self._video_last_tail_be = tail_be

        prefix_le = [
            int.from_bytes(data[i:i + 4], 'little')
            for i in range(0, min(12, len(data) - 3), 4)
        ]
        prefix_be = [
            int.from_bytes(data[i:i + 4], 'big')
            for i in range(0, min(12, len(data) - 3), 4)
        ]
        meta_words_le = [
            int.from_bytes(meta[i:i + 4], 'little')
            for i in range(0, len(meta) - 3, 4)
        ]
        meta_words_be = [
            int.from_bytes(meta[i:i + 4], 'big')
            for i in range(0, len(meta) - 3, 4)
        ]
        meta_tail_le = (
            int.from_bytes(meta[-8:], 'little') if len(meta) >= 8 else 0
        )
        meta_tail_be = (
            int.from_bytes(meta[-8:], 'big') if len(meta) >= 8 else 0
        )
        h264_head = payload[header_len:header_len + 32]

        l.info('video-fram #%d len=%d payload_len=%d header_len=%d '
               'prefix=%s prefix_le=%s prefix_be=%s tail=%s '
               'tail_be=0x%x tail_le=0x%x tail_delta=%s',
               self._video_fram_probe_count, len(data), len(payload),
               header_len, data[:12].hex(), prefix_le, prefix_be, tail.hex(),
               tail_be, tail_le,
               ('0x%x' % tail_delta) if tail_delta is not None else None)
        l.info('video-fram #%d meta_len=%d meta=%s meta_words_le=%s '
               'meta_words_be=%s meta_tail_le=0x%x meta_tail_be=0x%x '
               'h264_head=%s',
               self._video_fram_probe_count, len(meta), meta.hex(),
               meta_words_le, meta_words_be, meta_tail_le, meta_tail_be,
               h264_head.hex())

    def _update_video_lvni(self, data: bytes):
        mode = os.environ.get('OPENKART_VIDEO_LVNI_MODE', 'meta_tail').lower()
        if mode in ('0', 'off', 'zero'):
            return

        payload = data[12:]
        if len(payload) < 9 or payload[1:5] != b'FRAM':
            return

        header_len = payload[0] + 4
        if header_len > len(payload):
            return

        meta = payload[9:header_len]
        current = 0
        flags = 0
        if mode == 'prefix_le':
            current = int.from_bytes(data[:8], 'little') >> 8
            flags = int.from_bytes(data[8:12], 'little')
        elif mode == 'prefix_be':
            current = int.from_bytes(data[:8], 'big') >> 8
            flags = int.from_bytes(data[8:12], 'big')
        elif mode == 'meta_frame_be' and len(meta) >= 16:
            current = int.from_bytes(meta[12:16], 'big')
            if len(meta) >= 20:
                flags = int.from_bytes(meta[16:20], 'big')
        elif len(meta) >= 8:
            current = int.from_bytes(meta[-8:], 'little') >> 8
            if len(meta) >= 16:
                flags = int.from_bytes(meta[12:16], 'big')

        if current:
            if self._video_lvni_current_id:
                self._video_lvni_prev_id = self._video_lvni_current_id
            self._video_lvni_current_id = current
            self._video_lvni_flags = flags

    def _make_video_lvni_packet(self):
        return b'LVNI' + struct.pack(
            '<QQI',
            self._video_lvni_prev_id,
            self._video_lvni_current_id,
            self._video_lvni_flags)

    def _send_video_lvni(self, source: str):
        transport = self._video_control_transport
        if transport is None or transport.is_closing():
            return False
        packet = self._make_video_lvni_packet()
        transport.write(packet)
        self._trace_video_control('send_lvni', source=source,
                                  prev=self._video_lvni_prev_id,
                                  current=self._video_lvni_current_id,
                                  flags=self._video_lvni_flags,
                                  length=len(packet), hex=packet.hex())
        self._video_lvni_count += 1
        if self._video_lvni_count <= 8 or self._video_lvni_count % 300 == 0:
            l.info('video-control sent LVNI %s #%d prev=0x%x current=0x%x '
                   'flags=0x%x len=%d head=%s',
                   source, self._video_lvni_count, self._video_lvni_prev_id,
                   self._video_lvni_current_id, self._video_lvni_flags,
                   len(packet), packet.hex())
        return True

    def _maybe_send_frame_lvni(self):
        enabled = os.environ.get('OPENKART_VIDEO_LVNI_ON_FRAME', '0').lower()
        if enabled not in ('1', 'true', 'yes'):
            return
        if not self._video_lvni_current_id:
            return
        min_interval = float(os.environ.get(
            'OPENKART_VIDEO_LVNI_FRAME_MIN_INTERVAL', '0.03'))
        now = time.monotonic()
        if now - self._video_lvni_last_frame_at < min_interval:
            return
        if self._send_video_lvni('frame'):
            self._video_lvni_frame_count += 1
            self._video_lvni_last_frame_at = now

    def handle_status(self, data: bytes):
        if len(data) != 0x1f:
            l.warning('%s sent status telemetry of size %d',
                      self.device.address, len(data))
            return

        self.cable_connected = bool(data[0]&1)
        self.battery_state = data[3]
        self._status_probe_count += 1
        if (self._status_probe_count <= 12
                or self._status_probe_count % 300 == 0):
            words_le = [
                int.from_bytes(data[i:i + 4], 'little')
                for i in range(0, len(data) - 3, 4)
            ]
            l.info('status-telemetry #%d len=%d cable=%s battery=%d '
                   'switch_c41_bit=%d head=%s words_le=%s',
                   self._status_probe_count, len(data),
                   self.cable_connected, self.battery_state,
                   data[0] & 1, data[:31].hex(), words_le)

        self.__telemetry_event.set()

    def handle_imu(self, data: bytes):
        self._imu_probe_count += 1
        if self._imu_probe_count <= 12 or self._imu_probe_count % 300 == 0:
            l.info('imu-telemetry #%d len=%d head=%s',
                   self._imu_probe_count, len(data), data[:96].hex())

    def handle_type3_telemetry(self, data: bytes):
        self._telemetry_type3_probe_count += 1
        if (self._telemetry_type3_probe_count <= 12
                or self._telemetry_type3_probe_count % 300 == 0):
            l.info('type3-telemetry #%d len=%d head=%s',
                   self._telemetry_type3_probe_count, len(data),
                   data[:96].hex())
