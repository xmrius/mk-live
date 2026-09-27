#!/usr/bin/env python3
"""Separate Mario Kart Live video worker.

Receives OpenKart-forwarded LSP/FRAM UDP packets on 127.0.0.1:19001,
reassembles H.264 Annex-B frames, feeds ffmpeg with a selectable SPS/PPS
preamble, and exposes MJPEG/status endpoints on port 9001.
"""
import asyncio
from collections import deque
from fractions import Fraction
import os
import queue
import socket
import threading
import time
from io import BytesIO
from pathlib import Path
from aiohttp import WSMsgType, web

try:
    import av
except ImportError:
    av = None

try:
    from PIL import Image, ImageFilter, ImageStat
except ImportError:
    Image = None
    ImageFilter = None
    ImageStat = None


VIDEO_FORWARD_PORT = int(os.environ.get('KART_VIDEO_FORWARD_PORT', '19001'))
HTTP_PORT = int(os.environ.get('KART_VIDEO_HTTP_PORT', '9001'))
FPS_OUT = os.environ.get('KART_VIDEO_FPS', '12')
# The OV9782 delivers 1280x720, but the stream is coded as 1280x800. Practically
# all remaining decode errors (495/499 over four captures) sit in the padded rows
# below y=720, so the MJPEG output is cropped to the real picture.
# Set KART_VIDEO_CROP=off to see the full coded frame.
CROP = os.environ.get('KART_VIDEO_CROP', '1280:720:0:0').strip()
CROP_ENABLED = CROP.lower() not in ('', '0', 'off', 'none')


# ffmpeg's defaults add a lot of delay on a live pipe: frame threading holds one
# frame per thread and the input probe buffers ~5 MB before decoding starts.
# Measured on the 2026-05-05 capture fed at 30 fps (20-core host):
#   defaults 834 ms, threads=1 136 ms, threads=1 + small probe 69 ms (p90 70 ms).
# (-fflags nobuffer broke the output completely and is not used.)
LOW_LATENCY = os.environ.get('KART_VIDEO_LOW_LATENCY', '1') in ('1', 'true', 'yes')
DECODE_THREADS = os.environ.get('KART_VIDEO_DECODE_THREADS', '1')


def decoder_input_options():
    if not LOW_LATENCY:
        return []
    return ['-threads', DECODE_THREADS, '-probesize', '4096', '-analyzeduration', '0']


def fps_filter_active():
    return FPS_OUT not in ('0', 'off', 'none', '')


def output_filters():
    filters = []
    if CROP_ENABLED:
        filters.append(f'crop={CROP}')
    if fps_filter_active():
        filters.append(f'fps={FPS_OUT}')
    return ['-vf', ','.join(filters)] if filters else []
MJPEG_MAX_FPS = float(os.environ.get('KART_MJPEG_MAX_FPS', '30'))
BATCH_DECODE_INTERVAL = float(os.environ.get('KART_VIDEO_BATCH_DECODE_INTERVAL', '0'))
BATCH_DECODE_FRAMES = int(os.environ.get('KART_VIDEO_BATCH_DECODE_FRAMES', '40'))
BATCH_DECODE_TIMEOUT = float(os.environ.get('KART_VIDEO_BATCH_DECODE_TIMEOUT', '1.5'))
BATCH_PUBLISH_FRAMES = int(os.environ.get('KART_VIDEO_BATCH_PUBLISH_FRAMES', '6'))
BATCH_PUBLISH_FPS = float(os.environ.get('KART_VIDEO_BATCH_PUBLISH_FPS', '12'))
RECENT_FRAME_WINDOW = max(
    BATCH_DECODE_FRAMES,
    int(os.environ.get('KART_VIDEO_RECENT_FRAMES', '40')),
)
STALE_RESTART_SEC = float(os.environ.get('KART_VIDEO_STALE_RESTART_SEC', '0'))
QUALITY_GATE = os.environ.get('KART_VIDEO_QUALITY_GATE', '0') in (
    '1', 'true', 'yes')
QUALITY_MIN_SAT = float(os.environ.get('KART_VIDEO_QUALITY_MIN_SAT', '12'))
QUALITY_MAX_SAT = float(os.environ.get('KART_VIDEO_QUALITY_MAX_SAT', '125'))
QUALITY_MIN_EDGE = float(os.environ.get('KART_VIDEO_QUALITY_MIN_EDGE', '10'))
DUMP_DIR = os.path.expanduser(os.environ.get(
    'KART_VIDEO_DUMP_DIR', '~/openkart_setup/video_dumps'))
# Every IDR dump writes the whole recent-frame context (several MB). With an IDR
# roughly once per second this fills the disk within hours, so cap it.
DUMP_MAX = int(os.environ.get('KART_VIDEO_DUMP_MAX', '50'))
CONTINUATION_SKIP_RAW = os.environ.get('KART_VIDEO_CONT_SKIP', 'auto').lower()
FIXED_CONT_SKIP_ENABLED = os.environ.get(
    'KART_VIDEO_ENABLE_FIXED_CONT_SKIP', '0') in ('1', 'true', 'yes')
CONTINUATION_SKIP = (
    int(CONTINUATION_SKIP_RAW)
    if FIXED_CONT_SKIP_ENABLED and CONTINUATION_SKIP_RAW != 'auto'
    else None
)
FINAL_CONTINUATION_SKIP = int(os.environ.get('KART_VIDEO_FINAL_CONT_SKIP', '1'))
IDR_CONTINUATION_SKIP_RAW = os.environ.get('KART_VIDEO_IDR_CONT_SKIP', '')
IDR_CONTINUATION_SKIP = (
    int(IDR_CONTINUATION_SKIP_RAW)
    if IDR_CONTINUATION_SKIP_RAW.strip() != ''
    else None
)
# Every UDP video packet ends with an 8-byte trailer that is not H.264 (its last
# byte counts up by 2 per packet). It sits on start and continuation packets
# alike and must be removed before the bytes are counted against the FRAM size.
PACKET_TRAILER = int(os.environ.get('KART_VIDEO_PACKET_TRAILER', '8'))
# The FRAM size covers the 32-byte FRAM header plus 2 meta bytes in front of the
# Annex-B data: H.264 length = size - 34.
# Verified on the 2026-05-05 capture (530 of 533 frames end exactly there).
# Set both knobs to 0 to reproduce the pre-2026-09 reassembly.
FRAM_SIZE_OVERHEAD = int(os.environ.get('KART_VIDEO_FRAM_SIZE_OVERHEAD', '34'))
RESTART_ON_IDR = os.environ.get('KART_VIDEO_RESTART_ON_IDR', '0') == '1'
SYNC_ON_FIRST_IDR = os.environ.get('KART_VIDEO_SYNC_ON_FIRST_IDR', '1') in (
    '1', 'true', 'yes')
FAKE_IDR_PATH = os.environ.get(
    'KART_VIDEO_FAKE_IDR_PATH',
    os.path.expanduser('~/openkart_setup/black_1280x800_idr.h264'))
FAKE_IDR_ENABLED = os.environ.get('KART_VIDEO_FAKE_IDR', '0') in (
    '1', 'true', 'yes')
START4 = b'\x00\x00\x00\x01'
START3 = b'\x00\x00\x01'
VALID_ANNEXB_TYPES = (1, 5, 7, 8, 9)
SPLIT_MULTI_AUD = os.environ.get('KART_VIDEO_SPLIT_MULTI_AUD', '1') in (
    '1', 'true', 'yes')
CARRY_OVERRUN = os.environ.get('KART_VIDEO_CARRY_OVERRUN', '0') in (
    '1', 'true', 'yes')

SPS_PPS_CANDIDATES = {
    # Extracted from the live stream on 2026-04-29 around a real IDR.
    # Parses as High Profile, Level 3.2, 5-bit frame_num, 1280x800.
    'real_1280x800': (
        '0000000167640020ac4d00a00cb2'
        '0000000168ee38b0'
    ),
    # Same structure as the real SPS above (High@3.2, 5-bit frame_num,
    # poc_type=2, max_num_ref_frames=1), but with alternate heights/PPS.
    'realgeom_1280x720_cavlc': (
        '0000000167640020ac4d00a00b720000000168ce3c80'
    ),
    'realgeom_1280x720_cabac': (
        '0000000167640020ac4d00a00b720000000168ee3c80'
    ),
    'realgeom_1280x736_cavlc': (
        '0000000167640020ac4d00a00bb20000000168ce3c80'
    ),
    'realgeom_1280x736_cabac': (
        '0000000167640020ac4d00a00bb20000000168ee3c80'
    ),
    'realgeom_1280x752_cavlc': (
        '0000000167640020ac4d00a00bf20000000168ce3c80'
    ),
    'realgeom_1280x752_cabac': (
        '0000000167640020ac4d00a00bf20000000168ee3c80'
    ),
    'realgeom_1280x768_cavlc': (
        '0000000167640020ac4d00a00c320000000168ce3c80'
    ),
    'realgeom_1280x768_cabac': (
        '0000000167640020ac4d00a00c320000000168ee3c80'
    ),
    'realgeom_1280x784_cavlc': (
        '0000000167640020ac4d00a00c720000000168ce3c80'
    ),
    'realgeom_1280x784_cabac': (
        '0000000167640020ac4d00a00c720000000168ee3c80'
    ),
    'realgeom_1280x800_cavlc': (
        '0000000167640020ac4d00a00cb20000000168ce3c80'
    ),
    'realgeom_1280x800_cabac': (
        '0000000167640020ac4d00a00cb20000000168ee3c80'
    ),
    # Inferred from real slices: frame_num appears to be 5 bits wide
    # (log2_max_frame_num_minus4 = 1). These should be tried before the
    # earlier 4-bit synthetic SPS candidates.
    'baseline5_480x272': (
        '000000016742c01ea6407823900000000168cb83cb20'
    ),
    'baseline5_480x270': (
        '000000016742c01ea6407823fa400000000168cb83cb20'
    ),
    'baseline5_640x360': (
        '000000016742c01ea640280bfe540000000168cb83cb20'
    ),
    'baseline5_1280x720': (
        '000000016742c020a64014016e400000000168cb83cb20'
    ),
    'baseline5_1280x800': (
        '000000016742c020a640140196400000000168cb83cb20'
    ),
    'main5_480x270': (
        '00000001674d401ea6407823fa400000000168cb83cb20'
    ),
    'main5_480x272': (
        '00000001674d401ea6407823900000000168cb83cb20'
    ),
    'main5_640x360': (
        '00000001674d401ea640280bfe540000000168cb83cb20'
    ),
    'main5_1280x720': (
        '00000001674d4020a64014016e400000000168cb83cb20'
    ),
    'main5_1280x800': (
        '00000001674d4020a640140196400000000168cb83cb20'
    ),
    'main5_480x270_cabac': (
        '00000001674d401ea6407823fa400000000168ee3c80'
    ),
    'main5_480x272_cabac': (
        '00000001674d401ea6407823900000000168ee3c80'
    ),
    'main5_640x360_cabac': (
        '00000001674d401ea640280bfe540000000168ee3c80'
    ),
    'main5_1280x720_cabac': (
        '00000001674d4020a64014016e400000000168ee3c80'
    ),
    'main5_1280x800_cabac': (
        '00000001674d4020a640140196400000000168ee3c80'
    ),
    'high_640x360': (
        '000000016764101eacb81405ff2e022000000300200000079080'
        '0000000168ee0f2c8b'
    ),
    'high5_480x270': (
        '000000016764001eac4c80f047f4800000000168cb83cb20'
    ),
    'high5_480x270_cabac': (
        '000000016764001eac4c80f047f4800000000168ee3c80'
    ),
    'high5_480x272': (
        '000000016764001eac4c80f047200000000168cb83cb20'
    ),
    'high5_480x272_cabac': (
        '000000016764001eac4c80f047200000000168ee3c80'
    ),
    'high5_640x360': (
        '000000016764001eac4c805017fca80000000168cb83cb20'
    ),
    'high5_640x360_cabac': (
        '000000016764001eac4c805017fca80000000168ee3c80'
    ),
    'high5_1280x720': (
        '0000000167640020ac4c802802dc800000000168cb83cb20'
    ),
    'high5_1280x720_cabac': (
        '0000000167640020ac4c802802dc800000000168ee3c80'
    ),
    'high5_1280x800': (
        '0000000167640020ac4c8028032c800000000168cb83cb20'
    ),
    'high5_1280x800_cabac': (
        '0000000167640020ac4c8028032c800000000168ee3c80'
    ),
    'baseline_480x272': (
        '000000016742c01ed901e08ec044000003000400000300f03c58b920'
        '0000000168cb83cb20'
    ),
    'baseline_640x360': (
        '000000016742c01ed900a02ff970110000030001000003003c0f162e48'
        '0000000168cb83cb20'
    ),
}

# The real SPS/PPS observed inline in the kart stream; everything else is a
# historical guess kept for comparison.
DEFAULT_SPS = 'real_1280x800'


def pick_preamble():
    custom = os.environ.get('KART_VIDEO_SPS_HEX', '').strip().replace(' ', '')
    if custom:
        return 'custom_hex', bytes.fromhex(custom)
    name = os.environ.get('KART_VIDEO_SPS', DEFAULT_SPS)
    if name not in SPS_PPS_CANDIDATES:
        name = DEFAULT_SPS
    return name, bytes.fromhex(SPS_PPS_CANDIDATES[name])


def preamble_by_name(name):
    if name not in SPS_PPS_CANDIDATES:
        raise KeyError(name)
    return bytes.fromhex(SPS_PPS_CANDIDATES[name])


def find_annexb_start(buf, start=0):
    best = None
    for marker in (START4, START3):
        pos = buf.find(marker, start)
        while pos >= 0:
            header_pos = pos + len(marker)
            if (header_pos < len(buf)
                    and (buf[header_pos] & 31) in VALID_ANNEXB_TYPES):
                if best is None or pos < best:
                    best = pos
                break
            pos = buf.find(marker, pos + 1)
    return best


def l2_checksum_ok(pkt, off=8):
    if off < 0 or off + 14 > len(pkt) or pkt[off:off + 2] != b'L2':
        return False
    value = int.from_bytes(pkt[off + 4:off + 12], 'big')
    folded = ((value >> 48) & 0x7fff) ^ ((value >> 32) & 0xffffffff)
    tail32 = value & 0xffffffff
    folded ^= (tail32 >> 16)
    folded ^= tail32
    folded ^= 0xaaaaaaaa
    checksum = int.from_bytes(pkt[off + 2:off + 4], 'big')
    return checksum == (folded & 0xffff)


def find_fram_start(pkt):
    for fram in (13, 43):
        if fram < 1 or fram + 8 > len(pkt) or pkt[fram:fram + 4] != b'FRAM':
            continue
        if fram == 43 and not l2_checksum_ok(pkt, 8):
            continue
        base = fram - 1
        packet_size = int.from_bytes(pkt[fram + 4:fram + 8], 'big')
        if packet_size <= 0 or packet_size > 2 * 1024 * 1024:
            continue
        raw_start = base + pkt[base] + 4
        if raw_start >= len(pkt) or find_annexb_start(pkt, raw_start) != raw_start:
            fallback = find_annexb_start(pkt, fram + 8)
            if fallback is None:
                continue
            raw_start = fallback
        expected = packet_size - FRAM_SIZE_OVERHEAD
        if expected > 0:
            return expected, raw_start, fram
    return None


GOP_CACHE_MAX = 150
# The kart bitstream is damaged in the bottom rows of ~30 % of frames. ffmpeg conceals
# that well, browser hardware decoders do not (the picture smears until the next IDR).
# So for /video.ws the worker decodes in-process with libavcodec's error concealment and
# re-encodes a clean stream with x264 zerolatency (~3 ms each per frame on a desktop CPU).
WS_TRANSCODE = os.environ.get('KART_VIDEO_WS_TRANSCODE', '1') in ('1', 'true', 'yes')
WS_CRF = os.environ.get('KART_VIDEO_WS_CRF', '23')
WS_GOP = int(os.environ.get('KART_VIDEO_WS_GOP', '30'))


class WsTranscoder:
    """Decode kart frames (with error concealment) and re-encode them for WebCodecs.

    Runs in its own thread because decoding/encoding is CPU bound. Every input frame is
    decoded so the reference chain stays intact; if the thread falls behind, it skips
    only the encoding of individual frames.
    """

    def __init__(self, loop, publish):
        self.loop = loop
        self.publish = publish
        self.queue = queue.Queue(maxsize=90)
        self.input_dropped = False
        self.decoder = None
        self.encoder = None
        self.pts = 0
        self.stats = {'decoded': 0, 'encoded': 0, 'skipped_encode': 0, 'dropped_input': 0,
                      'decode_errors': 0, 'last_error': None}
        self.decode_ms = deque(maxlen=150)
        self.encode_ms = deque(maxlen=150)
        self.thread = threading.Thread(target=self._run, name='ws-transcoder', daemon=True)
        self.thread.start()

    def submit(self, frame, key, arrived):
        try:
            self.queue.put_nowait((frame, key, arrived))
        except queue.Full:
            self.stats['dropped_input'] += 1
            self.input_dropped = True

    def _new_decoder(self):
        dec = av.CodecContext.create('h264', 'r')
        dec.flags |= av.codec.context.Flags.low_delay
        dec.thread_count = 1
        return dec

    def _new_encoder(self, width, height):
        enc = av.CodecContext.create('libx264', 'w')
        enc.width, enc.height, enc.pix_fmt = width, height, 'yuv420p'
        enc.time_base = Fraction(1, 30)
        enc.framerate = Fraction(30, 1)
        enc.gop_size = WS_GOP
        enc.max_b_frames = 0
        enc.thread_count = 1
        enc.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'crf': WS_CRF,
                       'aud': '1', 'repeat-headers': '1'}
        return enc

    def _run(self):
        need_key = True
        while True:
            frame, key, arrived = self.queue.get()
            if self.input_dropped:
                # A dropped input frame breaks the decoder's reference chain.
                self.input_dropped = False
                need_key = True
            if need_key and not key:
                continue
            need_key = False
            try:
                if self.decoder is None:
                    self.decoder = self._new_decoder()
                t0 = time.perf_counter()
                pictures = self.decoder.decode(av.Packet(frame))
                self.decode_ms.append((time.perf_counter() - t0) * 1000)
            except Exception as e:
                self.stats['decode_errors'] += 1
                self.stats['last_error'] = f'decode: {type(e).__name__}: {e}'
                self.decoder = None
                need_key = True
                continue
            for picture in pictures:
                self.stats['decoded'] += 1
                if self.queue.qsize() > 1:
                    self.stats['skipped_encode'] += 1
                    continue
                try:
                    if (self.encoder is None or self.encoder.width != picture.width
                            or self.encoder.height != picture.height):
                        self.encoder = self._new_encoder(picture.width, picture.height)
                        self.pts = 0
                    picture.pts = self.pts
                    self.pts += 1
                    t1 = time.perf_counter()
                    packets = self.encoder.encode(picture)
                    self.encode_ms.append((time.perf_counter() - t1) * 1000)
                except Exception as e:
                    self.stats['last_error'] = f'encode: {type(e).__name__}: {e}'
                    self.encoder = None
                    continue
                for packet in packets:
                    self.stats['encoded'] += 1
                    self.loop.call_soon_threadsafe(
                        self.publish, bytes(packet), bool(packet.is_keyframe), arrived)

    def status(self):
        def med(values):
            v = sorted(values)
            return round(v[len(v) // 2], 1) if v else None
        return dict(self.stats, queue=self.queue.qsize(), decode_ms=med(self.decode_ms),
                    encode_ms=med(self.encode_ms), crf=WS_CRF, gop=WS_GOP)
CAPTURE_DIR = os.path.expanduser(os.environ.get('KART_VIDEO_CAPTURE_DIR', '~/openkart_setup/captures'))


class PcapWriter:
    """Minimal pcap writer so raw packets can be captured without root/tcpdump.

    Packets are wrapped in synthetic Ethernet/IPv4/UDP headers (dport = forward
    port) so pcap tools read them like a loopback capture.
    """

    def __init__(self, path, dport):
        self.path = path
        self.dport = dport
        self.count = 0
        self.f = open(path, 'wb')
        self.f.write(bytes.fromhex('d4c3b2a1') + (2).to_bytes(2, 'little') + (4).to_bytes(2, 'little')
                     + bytes(8) + (65535).to_bytes(4, 'little') + (1).to_bytes(4, 'little'))

    def write(self, payload, ts):
        udp_len = 8 + len(payload)
        ip = (bytes([0x45, 0]) + (20 + udp_len).to_bytes(2, 'big') + bytes(4) + bytes([64, 17]) + bytes(2)
              + bytes([127, 0, 0, 1]) + bytes([127, 0, 0, 1]))
        udp = (0).to_bytes(2, 'big') + self.dport.to_bytes(2, 'big') + udp_len.to_bytes(2, 'big') + bytes(2)
        frame = bytes(12) + b'\x08\x00' + ip + udp + payload
        sec = int(ts)
        self.f.write(sec.to_bytes(4, 'little') + int((ts - sec) * 1e6).to_bytes(4, 'little')
                     + len(frame).to_bytes(4, 'little') + len(frame).to_bytes(4, 'little') + frame)
        self.count += 1

    def close(self):
        self.f.close()


class _BitReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def bit(self):
        byte = self.data[self.pos >> 3]
        value = (byte >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return value

    def bits(self, n):
        value = 0
        for _ in range(n):
            value = (value << 1) | self.bit()
        return value

    def ue(self):
        zeros = 0
        while self.bit() == 0:
            zeros += 1
        return (1 << zeros) - 1 + self.bits(zeros)


class _BitWriter:
    def __init__(self):
        self.bits_out = []

    def bit(self, value):
        self.bits_out.append(value & 1)

    def bits(self, value, n):
        for i in range(n - 1, -1, -1):
            self.bit(value >> i)

    def ue(self, value):
        value += 1
        n = value.bit_length()
        self.bits(0, n - 1)
        self.bits(value, n)

    def rbsp_trailing(self):
        self.bit(1)
        while len(self.bits_out) % 8:
            self.bit(0)
        return bytes(int(''.join(map(str, self.bits_out[i:i + 8])), 2)
                     for i in range(0, len(self.bits_out), 8))


def _ebsp_to_rbsp(data):
    return data.replace(b'\x00\x00\x03', b'\x00\x00')


def _rbsp_to_ebsp(data):
    out = bytearray()
    zeros = 0
    for b in data:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def low_delay_sps(nal, crop_bottom_px=80):
    """Return the SPS NAL with VUI bitstream_restriction (no reordering, 1-frame DPB)
    and an optional bottom crop added.

    Browser hardware decoders otherwise hold several frames because the kart SPS has
    no VUI (measured ~160 ms extra in Chromium). Only handles the kart's layout
    (no scaling matrix, no existing cropping/VUI); anything else is returned as is.
    """
    try:
        rbsp = _ebsp_to_rbsp(nal[1:])
        r = _BitReader(rbsp)
        profile_idc = r.bits(8)
        r.bits(16)  # constraint flags + level_idc
        r.ue()  # seq_parameter_set_id
        chroma_format_idc = 1
        if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma_format_idc = r.ue()
            if chroma_format_idc == 3:
                r.bit()
            r.ue()
            r.ue()
            r.bit()
            if r.bit():  # seq_scaling_matrix_present_flag
                return nal
        r.ue()  # log2_max_frame_num_minus4
        poc_type = r.ue()
        if poc_type == 0:
            r.ue()
        elif poc_type == 1:
            return nal
        max_num_ref_frames = r.ue()
        r.bit()
        r.ue()  # pic_width_in_mbs_minus1
        r.ue()  # pic_height_in_map_units_minus1
        frame_mbs_only = r.bit()
        if not frame_mbs_only:
            return nal
        r.bit()  # direct_8x8_inference_flag
        crop_pos = r.pos
        if r.bit():  # frame_cropping_flag already set
            return nal
        if r.bit():  # vui_parameters_present_flag already set
            return nal
    except IndexError:
        return nal

    w = _BitWriter()
    reader = _BitReader(rbsp)
    for _ in range(crop_pos):
        w.bit(reader.bit())
    crop_units = crop_bottom_px // 2 if chroma_format_idc == 1 else crop_bottom_px
    if crop_units:
        w.bit(1)
        w.ue(0)
        w.ue(0)
        w.ue(0)
        w.ue(crop_units)
    else:
        w.bit(0)
    w.bit(1)  # vui_parameters_present_flag
    w.bits(0, 8)  # aspect, overscan, video_signal, chroma_loc, timing, nal_hrd, vcl_hrd, pic_struct
    w.bit(1)  # bitstream_restriction_flag
    w.bit(1)  # motion_vectors_over_pic_boundaries_flag
    w.ue(2)  # max_bytes_per_pic_denom
    w.ue(1)  # max_bits_per_mb_denom
    w.ue(15)  # log2_max_mv_length_horizontal
    w.ue(15)  # log2_max_mv_length_vertical
    w.ue(0)  # max_num_reorder_frames
    w.ue(max(1, max_num_ref_frames))  # max_dec_frame_buffering
    return nal[:1] + _rbsp_to_ebsp(w.rbsp_trailing())


_sps_cache = {}


def patch_sps_for_browser(frame):
    """Replace the SPS inside an Annex-B access unit with its low-delay version."""
    start = frame.find(b'\x00\x00\x00\x01\x67')
    if start < 0:
        return frame
    nal_start = start + 4
    end = frame.find(b'\x00\x00\x01', nal_start)
    if end < 0:
        return frame
    if frame[end - 1] == 0:
        end -= 1
    nal = frame[nal_start:end]
    patched = _sps_cache.get(nal)
    if patched is None:
        patched = low_delay_sps(nal, 80 if CROP_ENABLED else 0)
        _sps_cache[nal] = patched
    return frame[:nal_start] + patched + frame[end:]


class WsClient:
    """Per-viewer queue for /video.ws that never accumulates delay.

    If the viewer falls behind, the backlog is dropped and the viewer skips
    ahead to the next keyframe instead of decoding stale frames.
    """

    def __init__(self, maxsize=8):
        self.queue = asyncio.Queue(maxsize=maxsize)
        self.need_key = True
        self.skipped = 0
        self.resyncs = 0

    def offer(self, message, key):
        if self.need_key and not key:
            self.skipped += 1
            return
        if self.queue.full():
            while not self.queue.empty():
                self.queue.get_nowait()
            self.resyncs += 1
            if not key:
                self.need_key = True
                self.skipped += 1
                return
        self.need_key = False
        self.queue.put_nowait(message)


class VideoWorker:
    def __init__(self):
        self.sps_name, self.sps_pps = pick_preamble()
        self.proc = None
        self.current = None
        self.current_raw = 0
        self.current_fragment_count = 0
        self.current_kind_counts = {}
        self.current_fragment_debug = []
        self.current_outer_total = None
        self.current_outer_indices = []
        self.current_is_idr = False
        self.expected = 0
        self.frames_raw = 0
        self.frames_in = 0
        self.frames_dropped = 0
        self.frames_filtered = 0
        self.frames_abandoned = 0
        self.access_unit_splits = 0
        self.udp_packets = 0
        self.jpeg_out = 0
        self.ffmpeg_restarts = 0
        self.dumps_written = 0
        self.last_seq = None
        self.seq_gap_events = 0
        self.seq_lost_packets = 0
        self.current_damaged = False
        self.damaged_frames = 0
        self.last_gap_at = 0.0
        self.chain_broken = False
        self.chain_broken_frames = 0
        self.nal_counts = {}
        self.valid_nal_counts = {}
        self.first_frame_infos = []
        self.filtered_frame_infos = []
        self.idr_frames = []
        self.real_sps_pps_dumps = []
        self.frame_size_min = None
        self.frame_size_max = None
        self.frame_size_sum = 0
        self.frame_size_last = None
        self.frame_size_recent = deque(maxlen=120)
        self.frames_trimmed = 0
        self.bytes_trimmed = 0
        self.cont_skip_counts = {}
        self.cont_kind_counts = {}
        self.cont_kind_head_counts = {}
        self.cont_head_counts = {}
        self.cont_debug = []
        self.frame_assembly_debug = []
        self.idr_assembly_debug = []
        self.fram_debug = []
        self.fragment_total_mismatches = 0
        self.fragment_orphans = 0
        self.fragment_incomplete_transport = 0
        self.fragment_sequence_debug = []
        self.pending_overrun = b''
        self.overrun_carry_candidates = 0
        self.overrun_carry_used = 0
        self.overrun_carry_dropped = 0
        self.overrun_carry_duplicate = 0
        self.overrun_carry_bytes = 0
        self.overrun_carry_debug = []
        self.recent_frames = deque(maxlen=RECENT_FRAME_WINDOW)
        self.latest_jpeg = None
        self.latest_at = 0.0
        self.jpeg_condition = asyncio.Condition()
        self.started_at = time.time()
        self.last_udp_at = 0.0
        self.last_frame_at = 0.0
        self.last_error = None
        self.decoder_synced = False
        self.ready = asyncio.Event()
        self.frame_queue = asyncio.Queue(maxsize=4)
        self.h264_clients = set()
        # WebCodecs viewers (/video.ws) and the current GOP, so a new viewer can
        # start decoding immediately instead of waiting up to a second for the
        # next IDR.
        self.ws_clients = set()
        self.gop = []
        self.transcoder = None
        self.ws_latency = deque(maxlen=150)
        self.jpeg_task = None
        self.batch_task = None
        # asyncio only keeps weak references to tasks; hold them explicitly so
        # the background loops cannot be garbage collected mid-run.
        self.tasks = []
        self.capture = None
        self.capture_until = 0.0
        self.last_capture = None
        # Latency instrumentation: arrival time of each frame written to the
        # current ffmpeg process, matched 1:1 with its JPEGs (valid while no
        # fps filter drops frames).
        self.proc_arrivals = deque(maxlen=512)
        self.proc_written = 0
        self.proc_jpegs = 0
        self.latency_recent = deque(maxlen=150)
        self.batch_decodes = 0
        self.batch_jpeg_out = 0
        self.batch_jpeg_candidates = 0
        self.batch_last_frame = 0
        self.batch_waiting_for_idr = 0
        self.stale_restarts = 0
        self.last_stale_restart_at = 0.0
        self.quality_checked = 0
        self.quality_rejected = 0
        self.last_quality = None
        self.idr_restarts = 0
        self.fake_idr_bytes = b''
        if FAKE_IDR_ENABLED and os.path.exists(FAKE_IDR_PATH):
            with open(FAKE_IDR_PATH, 'rb') as f:
                self.fake_idr_bytes = f.read()

    async def start(self):
        if WS_TRANSCODE and av is not None:
            self.transcoder = WsTranscoder(asyncio.get_running_loop(), self._publish_ws_encoded)
        await self._start_ffmpeg()
        self.tasks.append(asyncio.create_task(self._udp_loop()))
        self.tasks.append(asyncio.create_task(self._writer_loop()))
        if BATCH_DECODE_INTERVAL > 0:
            self.batch_task = asyncio.create_task(self._batch_decode_loop())
            self.tasks.append(self.batch_task)
        if STALE_RESTART_SEC > 0:
            self.tasks.append(asyncio.create_task(self._stale_watchdog_loop()))

    async def set_sps(self, name):
        self.sps_pps = preamble_by_name(name)
        self.sps_name = name
        self.reset_stats(keep_latest=False)
        await self._start_ffmpeg()
        return self.status()

    def reset_stats(self, keep_latest=True):
        self.frames_in = 0
        self.frames_raw = 0
        self.frames_dropped = 0
        self.frames_filtered = 0
        self.frames_abandoned = 0
        self.access_unit_splits = 0
        self.udp_packets = 0
        self.jpeg_out = 0
        self.nal_counts = {}
        self.valid_nal_counts = {}
        self.first_frame_infos = []
        self.filtered_frame_infos = []
        self.idr_frames = []
        self.real_sps_pps_dumps = []
        self.frame_size_min = None
        self.frame_size_max = None
        self.frame_size_sum = 0
        self.frame_size_last = None
        self.frame_size_recent.clear()
        self.frames_trimmed = 0
        self.bytes_trimmed = 0
        self.cont_skip_counts = {}
        self.cont_kind_counts = {}
        self.cont_kind_head_counts = {}
        self.cont_head_counts = {}
        self.cont_debug = []
        self.frame_assembly_debug = []
        self.idr_assembly_debug = []
        self.fram_debug = []
        self.fragment_total_mismatches = 0
        self.fragment_orphans = 0
        self.fragment_incomplete_transport = 0
        self.fragment_sequence_debug = []
        self.pending_overrun = b''
        self.overrun_carry_candidates = 0
        self.overrun_carry_used = 0
        self.overrun_carry_dropped = 0
        self.overrun_carry_duplicate = 0
        self.overrun_carry_bytes = 0
        self.overrun_carry_debug = []
        self.batch_decodes = 0
        self.batch_jpeg_out = 0
        self.batch_jpeg_candidates = 0
        self.batch_last_frame = 0
        self.stale_restarts = 0
        self.last_stale_restart_at = 0.0
        self.quality_checked = 0
        self.quality_rejected = 0
        self.last_quality = None
        self.recent_frames.clear()
        self.started_at = time.time()
        self.last_udp_at = 0.0
        self.last_frame_at = 0.0
        self.last_error = None
        self.idr_restarts = 0
        self.decoder_synced = False
        self.batch_waiting_for_idr = 0
        self.current = None
        self.current_raw = 0
        self.current_fragment_count = 0
        self.current_kind_counts = {}
        self.current_fragment_debug = []
        self.current_outer_total = None
        self.current_outer_indices = []
        self.current_is_idr = False
        self.expected = 0
        while not self.frame_queue.empty():
            try:
                self.frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if not keep_latest:
            self.latest_jpeg = None
            self.latest_at = 0.0
            self.ready = asyncio.Event()

    async def _start_ffmpeg(self):
        old = self.proc
        if self.jpeg_task:
            self.jpeg_task.cancel()
            self.jpeg_task = None
        if old and old.returncode is None:
            try:
                old.kill()
                await old.wait()
            except ProcessLookupError:
                pass
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-err_detect', 'ignore_err',
            '-flags2', '+showall',
            *decoder_input_options(),
            '-f', 'h264', '-i', 'pipe:0',
        ]
        cmd += output_filters()
        cmd += ['-q:v', '5', '-f', 'mjpeg', 'pipe:1']
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.ffmpeg_restarts += 1
        self.proc_arrivals.clear()
        self.proc_written = 0
        self.proc_jpegs = 0
        self.proc.stdin.write(self.sps_pps)
        if self.fake_idr_bytes:
            self.proc.stdin.write(self.fake_idr_bytes)
        await self.proc.stdin.drain()
        self.jpeg_task = asyncio.create_task(self._jpeg_loop(self.proc))

    async def _udp_loop(self):
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        sock.setblocking(False)
        sock.bind(('127.0.0.1', VIDEO_FORWARD_PORT))
        while True:
            pkt = await loop.sock_recv(sock, 4096)
            self.udp_packets += 1
            self.last_udp_at = time.time()
            if self.capture:
                if self.last_udp_at < self.capture_until:
                    self.capture.write(pkt, self.last_udp_at)
                else:
                    self.capture.close()
                    self.last_capture = {'path': self.capture.path, 'packets': self.capture.count}
                    self.capture = None
            if self.udp_packets % 8 == 0:
                await asyncio.sleep(0)
            try:
                self._handle_packet(pkt)
            except Exception as e:
                # A parser bug must not silently kill the receive loop while
                # the process (and thus systemd) keeps looking healthy.
                self.last_error = f'udp packet {self.udp_packets}: {type(e).__name__}: {e}'
                self.current = None
                self.expected = 0

    def _handle_packet(self, pkt):
        for raw_frame in self._feed_lsp(pkt):
            self.frames_raw += 1
            raw_frame, trimmed = self._trim_to_annexb(raw_frame)
            if trimmed:
                self.frames_trimmed += 1
                self.bytes_trimmed += trimmed
            for frame in self._split_access_units(raw_frame):
                units = self._nal_units(frame)
                valid_units = self._valid_nal_units(units)
                nts = [u['type'] for u in units]
                valid_nts = [u['type'] for u in valid_units]
                if not self._accept_frame(frame, valid_nts):
                    self.frames_filtered += 1
                    if len(self.filtered_frame_infos) < 12:
                        self.filtered_frame_infos.append({
                            'raw_n': self.frames_raw,
                            'len': len(frame),
                            'nal_types': nts,
                            'valid_nal_types': valid_nts,
                            'head': frame[:96].hex(),
                        })
                    continue
                self.frames_in += 1
                self.last_frame_at = time.time()
                self._record_frame_size(len(frame))
                self.recent_frames.append((self.frames_in, frame))
                for typ in nts:
                    key = str(typ)
                    self.nal_counts[key] = self.nal_counts.get(key, 0) + 1
                for typ in valid_nts:
                    key = str(typ)
                    self.valid_nal_counts[key] = self.valid_nal_counts.get(key, 0) + 1
                if any(typ in (5, 7, 8) for typ in valid_nts):
                    self._dump_interesting_frame(frame, valid_units)
                if len(self.first_frame_infos) < 12:
                    self.first_frame_infos.append({
                        'n': self.frames_in,
                        'raw_n': self.frames_raw,
                        'len': len(frame),
                        'nal_types': nts,
                        'valid_nal_types': valid_nts,
                        'head': frame[:96].hex(),
                    })
                if self.frame_queue.full():
                    try:
                        self.frame_queue.get_nowait()
                        self.frames_dropped += 1
                    except asyncio.QueueEmpty:
                        pass
                self.frame_queue.put_nowait(
                    (self.frames_in, frame, valid_nts, time.monotonic()))
                self._publish_h264(frame)
                if 5 in valid_nts:
                    self.chain_broken = False
                elif self.chain_broken:
                    self.chain_broken_frames += 1
                key = 5 in valid_nts
                if self.transcoder:
                    # The patched SPS crops to 720 lines, so the decoder outputs 1280x720.
                    self.transcoder.submit(patch_sps_for_browser(frame) if key else frame,
                                           key, time.monotonic())
                else:
                    self._publish_ws(frame, key)

    def _publish_ws_encoded(self, frame, key, arrived):
        self.ws_latency.append(time.monotonic() - arrived)
        self._publish_ws(frame, key, patch=False)

    def _publish_ws(self, frame, key, patch=True):
        if key and patch:
            frame = patch_sps_for_browser(frame)
        message =(b'\x01' if key else b'\x00') + frame
        if key:
            self.gop = [message]
        elif self.gop:
            self.gop.append(message)
            if len(self.gop) > GOP_CACHE_MAX:
                self.gop = []
        for client in tuple(self.ws_clients):
            client.offer(message, key)

    def _publish_h264(self, frame):
        if not self.h264_clients:
            return
        dead = []
        for q in tuple(self.h264_clients):
            try:
                if q.full():
                    q.get_nowait()
                q.put_nowait(frame)
            except Exception:
                dead.append(q)
        for q in dead:
            self.h264_clients.discard(q)

    def _record_frame_size(self, size):
        self.frame_size_last = size
        self.frame_size_sum += size
        self.frame_size_recent.append(size)
        if self.frame_size_min is None or size < self.frame_size_min:
            self.frame_size_min = size
        if self.frame_size_max is None or size > self.frame_size_max:
            self.frame_size_max = size

    def _frame_size_status(self):
        recent = list(self.frame_size_recent)
        return {
            'min': self.frame_size_min,
            'max': self.frame_size_max,
            'avg': (self.frame_size_sum / self.frames_in
                    if self.frames_in else None),
            'last': self.frame_size_last,
            'recent_min': min(recent) if recent else None,
            'recent_max': max(recent) if recent else None,
            'recent_avg': (sum(recent) / len(recent) if recent else None),
            'recent_count': len(recent),
        }

    async def _writer_loop(self):
        while True:
            frame_no, frame, nts, arrived = await self.frame_queue.get()
            try:
                if self.proc.returncode is not None:
                    await self._start_ffmpeg()
                    self.decoder_synced = False
                # With both flags set this used to restart ffmpeg twice on
                # the first IDR; one restart per IDR is enough.
                if 5 in nts and (RESTART_ON_IDR or (
                        SYNC_ON_FIRST_IDR and not self.decoder_synced)):
                    reason = 'restarted' if self.decoder_synced else 'synced'
                    await self._start_ffmpeg()
                    self.decoder_synced = True
                    self.idr_restarts += 1
                    self.last_error = f'{reason} ffmpeg on IDR frame {frame_no}'
                if frame_no % 120 == 1:
                    self.proc.stdin.write(self.sps_pps)
                self.proc.stdin.write(frame)
                self.proc_arrivals.append(arrived)
                self.proc_written += 1
                await asyncio.wait_for(self.proc.stdin.drain(), timeout=1.0)
            except Exception as e:
                self.last_error = f'ffmpeg stdin: {type(e).__name__}: {e}'
                self.decoder_synced = False
                try:
                    await self._start_ffmpeg()
                except Exception as restart_error:
                    # Keep the writer alive; the next frame retries the start.
                    self.last_error = (f'ffmpeg restart: {type(restart_error).__name__}: '
                                       f'{restart_error}')
                    await asyncio.sleep(1.0)

    async def _jpeg_loop(self, proc):
        buf = bytearray()
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                if proc is self.proc:
                    self.last_error = 'ffmpeg stdout closed'
                return
            buf += chunk
            while True:
                start = buf.find(b'\xff\xd8')
                end = buf.find(b'\xff\xd9', start + 2) if start >= 0 else -1
                if start < 0:
                    if len(buf) > 1024 * 1024:
                        del buf[:]
                    break
                if end < 0:
                    if start > 0:
                        del buf[:start]
                    break
                jpg = bytes(buf[start:end + 2])
                del buf[:end + 2]
                if proc is self.proc:
                    self.proc_jpegs += 1
                    if self.proc_arrivals and not fps_filter_active():
                        self.latency_recent.append(
                            time.monotonic() - self.proc_arrivals.popleft())
                if self._publish_jpeg(jpg):
                    async with self.jpeg_condition:
                        self.jpeg_condition.notify_all()

    def _latency_status(self):
        values = sorted(self.latency_recent)
        if not values:
            return None

        def pick(q):
            return round(values[min(len(values) - 1, int(len(values) * q))] * 1000, 1)
        return {'median_ms': pick(0.5), 'p90_ms': pick(0.9),
                'max_ms': round(values[-1] * 1000, 1), 'samples': len(values)}

    def _publish_jpeg(self, jpg):
        if QUALITY_GATE:
            ok, metrics = self._quality_ok(jpg)
            self.last_quality = metrics
            if not ok:
                self.quality_rejected += 1
                return False
        self.latest_jpeg = jpg
        self.latest_at = time.time()
        self.jpeg_out += 1
        self.ready.set()
        return True

    def _quality_ok(self, jpg):
        self.quality_checked += 1
        if Image is None:
            return True, {'ok': True, 'reason': 'pillow_unavailable'}
        try:
            im = Image.open(BytesIO(jpg)).convert('RGB')
            im.thumbnail((320, 180))
            hsv = im.convert('HSV')
            sat = ImageStat.Stat(hsv.split()[1]).mean[0]
            edges = ImageStat.Stat(
                im.convert('L').filter(ImageFilter.FIND_EDGES)).mean[0]
            ok = (QUALITY_MIN_SAT <= sat <= QUALITY_MAX_SAT
                  and edges >= QUALITY_MIN_EDGE)
            return ok, {
                'ok': ok,
                'sat': sat,
                'edges': edges,
                'min_sat': QUALITY_MIN_SAT,
                'max_sat': QUALITY_MAX_SAT,
                'min_edge': QUALITY_MIN_EDGE,
            }
        except Exception as e:
            return True, {'ok': True, 'reason': f'quality_error: {e}'}

    def _jpegs_from_mjpeg(self, data):
        out = []
        pos = 0
        while True:
            start = data.find(b'\xff\xd8', pos)
            if start < 0:
                return out
            end = data.find(b'\xff\xd9', start + 2)
            if end < 0:
                return out
            out.append(data[start:end + 2])
            pos = end + 2

    async def _batch_decode_loop(self):
        while True:
            await asyncio.sleep(BATCH_DECODE_INTERVAL)
            if not self.recent_frames:
                continue
            frame_no = self.recent_frames[-1][0]
            prev_frame_no = self.batch_last_frame
            if frame_no == prev_frame_no:
                continue
            self.batch_last_frame = frame_no
            recent = list(self.recent_frames)[-BATCH_DECODE_FRAMES:]
            idr_index = None
            for i in range(len(recent) - 1, -1, -1):
                units = self._valid_nal_units(self._nal_units(recent[i][1]))
                if any(u['type'] == 5 for u in units):
                    idr_index = i
                    break
            if idr_index is None:
                self.batch_waiting_for_idr += 1
                continue
            frames = [f for _, f in recent[idr_index:]]
            data = self.sps_pps + b''.join(frames)
            try:
                proc = await asyncio.create_subprocess_exec(
                    'ffmpeg', '-hide_banner', '-loglevel', 'error',
                    '-err_detect', 'ignore_err', '-flags2', '+showall',
                    '-f', 'h264', '-i', 'pipe:0',
                    *(['-vf', f'crop={CROP}'] if CROP_ENABLED else []),
                    '-q:v', '5', '-f', 'mjpeg', 'pipe:1',
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(data), timeout=BATCH_DECODE_TIMEOUT)
                self.batch_decodes += 1
                jpegs = self._jpegs_from_mjpeg(stdout)
                self.batch_jpeg_candidates += len(jpegs)
                if not jpegs:
                    continue
                new_frame_count = max(1, frame_no - prev_frame_no)
                publish_count = min(
                    len(jpegs),
                    max(1, BATCH_PUBLISH_FRAMES),
                    new_frame_count,
                )
                delay = 1.0 / max(1.0, BATCH_PUBLISH_FPS)
                for jpg in jpegs[-publish_count:]:
                    self.batch_jpeg_out += 1
                    if self._publish_jpeg(jpg):
                        async with self.jpeg_condition:
                            self.jpeg_condition.notify_all()
                    if publish_count > 1:
                        await asyncio.sleep(delay)
            except Exception as e:
                self.last_error = f'batch decode: {type(e).__name__}: {e}'

    async def _stale_watchdog_loop(self):
        while True:
            await asyncio.sleep(max(1.0, STALE_RESTART_SEC / 2))
            now = time.time()
            if not self.last_frame_at or now - self.last_frame_at > 1.5:
                continue
            if self.last_stale_restart_at and now - self.last_stale_restart_at < STALE_RESTART_SEC:
                continue
            if self.latest_at and now - self.latest_at <= STALE_RESTART_SEC:
                continue
            try:
                await self._start_ffmpeg()
                self.stale_restarts += 1
                self.last_stale_restart_at = now
                self.last_error = f'restarted stale ffmpeg after {STALE_RESTART_SEC:.1f}s without jpeg'
            except Exception as e:
                self.last_error = f'stale restart: {type(e).__name__}: {e}'

    def _feed_lsp(self, pkt):
        if len(pkt) < 20 + PACKET_TRAILER:
            return []
        # Bytes 3..4 are a per-packet sequence counter (mirrored at 9..10).
        # A gap means Wi-Fi lost packets; the frame in progress is then damaged.
        seq = int.from_bytes(pkt[3:5], 'big')
        gap = False
        if self.last_seq is not None:
            step = (seq - self.last_seq) & 0xffff
            if step != 1 and step < 1000:
                gap = True
                self.seq_gap_events += 1
                self.seq_lost_packets += step - 1
                self.last_gap_at = time.time()
                # Until the next IDR every frame references damaged data.
                self.chain_broken = True
        self.last_seq = seq
        if PACKET_TRAILER:
            pkt = pkt[:-PACKET_TRAILER]
        fram_start = find_fram_start(pkt)
        is_start_fragment = fram_start is not None
        if is_start_fragment:
            if self.current is not None:
                # A new FRAM arrived before the previous frame reached its
                # FRAM size; that frame is discarded here.
                self.frames_abandoned += 1
            # A gap right before a start packet hit the previous frame.
            self.current_damaged = False
            self.expected, raw_start, fram = fram_start
            self.current = bytearray()
            self.current_raw = 0
            self.current_fragment_count = 1
            self.current_outer_total = pkt[6] if len(pkt) > 6 else None
            self.current_outer_indices = [pkt[7] if len(pkt) > 7 else None]
            self.current_is_idr = False
            fragment_kind_key = f'0x{pkt[11]:02x}'
            fragment_skip = raw_start
            self.current_kind_counts = {fragment_kind_key: 1}
            self.current_fragment_debug = [{
                'kind': fragment_kind_key,
                'skip': fragment_skip,
                'udp_len': len(pkt),
                'outer12': pkt[:12].hex(),
                'raw_head': pkt[raw_start:raw_start + 16].hex(),
            }]
            if len(self.fram_debug) < 16:
                base = fram - 1
                packet_size = int.from_bytes(pkt[fram + 4:fram + 8], 'big')
                self.fram_debug.append({
                    'udp_len': len(pkt),
                    'outer12': pkt[:12].hex(),
                    'fram': fram,
                    'prefix_byte': pkt[base],
                    'packet_size': packet_size,
                    'expected_h264': self.expected,
                    'raw_start': raw_start,
                    'header_len': raw_start - base,
                    'pre_h264': pkt[base:raw_start].hex(),
                    'post_fram_pre_h264': pkt[fram:raw_start].hex(),
                    'available_from_raw': len(pkt) - raw_start,
                    'head': pkt[raw_start:raw_start + 24].hex(),
                })
            raw_chunk = pkt[raw_start:]
            self.current_is_idr = any(
                unit['type'] in (5, 7, 8)
                for unit in self._nal_units(raw_chunk)
            )
            if self.pending_overrun:
                carry = self.pending_overrun
                self.pending_overrun = b''
                min_cmp = min(16, len(carry), len(raw_chunk))
                duplicate = (
                    min_cmp > 0 and carry[:min_cmp] == raw_chunk[:min_cmp]
                ) or (
                    find_annexb_start(carry) == 0
                    and find_annexb_start(raw_chunk) == 0
                )
                if duplicate:
                    self.overrun_carry_duplicate += 1
                    if len(self.overrun_carry_debug) < 24:
                        self.overrun_carry_debug.append({
                            'event': 'duplicate_drop',
                            'carry_len': len(carry),
                            'carry_head': carry[:32].hex(),
                            'raw_head': raw_chunk[:32].hex(),
                        })
                else:
                    self.current += carry
                    self.current_raw += len(carry)
                    self.current_fragment_count += 1
                    self.current_kind_counts['carry'] = (
                        self.current_kind_counts.get('carry', 0) + 1)
                    self.overrun_carry_used += 1
                    self.overrun_carry_bytes += len(carry)
                    if len(self.current_fragment_debug) < 80:
                        self.current_fragment_debug.append({
                            'kind': 'carry',
                            'skip': 0,
                            'udp_len': 0,
                            'outer12': '',
                            'payload_head': carry[:16].hex(),
                            'raw_head': carry[:16].hex(),
                        })
                    if len(self.overrun_carry_debug) < 24:
                        self.overrun_carry_debug.append({
                            'event': 'used',
                            'carry_len': len(carry),
                            'carry_head': carry[:32].hex(),
                            'next_raw_head': raw_chunk[:32].hex(),
                        })
        else:
            if gap:
                self.current_damaged = True
            if self.current is None or self.expected <= 0:
                self.fragment_orphans += 1
                if len(self.fragment_sequence_debug) < 24:
                    self.fragment_sequence_debug.append({
                        'event': 'orphan_continuation',
                        'udp_len': len(pkt),
                        'outer12': pkt[:12].hex(),
                        'kind': f'0x{pkt[11]:02x}' if len(pkt) > 11 else None,
                        'total': pkt[6] if len(pkt) > 6 else None,
                        'index': pkt[7] if len(pkt) > 7 else None,
                    })
                return []
            payload = pkt[12:]
            skip = self._continuation_skip(pkt, payload)
            if self.current_is_idr and IDR_CONTINUATION_SKIP is not None:
                skip = IDR_CONTINUATION_SKIP
            key = str(skip)
            self.cont_skip_counts[key] = self.cont_skip_counts.get(key, 0) + 1
            kind = pkt[11]
            kind_key = f'0x{kind:02x}'
            self.cont_kind_counts[kind_key] = (
                self.cont_kind_counts.get(kind_key, 0) + 1)
            kind_heads = self.cont_kind_head_counts.setdefault(kind_key, {})
            if payload:
                hkey = payload[:8].hex()
                if hkey in self.cont_head_counts or len(self.cont_head_counts) < 96:
                    self.cont_head_counts[hkey] = (
                        self.cont_head_counts.get(hkey, 0) + 1)
                if hkey in kind_heads or len(kind_heads) < 12:
                    kind_heads[hkey] = kind_heads.get(hkey, 0) + 1
            if len(self.cont_debug) < 48:
                self.cont_debug.append({
                    'udp_len': len(pkt),
                    'outer12': pkt[:12].hex(),
                    'kind': kind_key,
                    'skip': skip,
                    'payload_head': payload[:16].hex(),
                    'raw_head': payload[skip:skip + 16].hex(),
                })
            raw_chunk = payload[skip:]
            fragment_kind_key = kind_key
            fragment_skip = skip
        if self.current is None or self.expected <= 0:
            return []
        if not is_start_fragment:
            self.current_fragment_count += 1
            self.current_outer_indices.append(pkt[7] if len(pkt) > 7 else None)
            self.current_kind_counts[fragment_kind_key] = (
                self.current_kind_counts.get(fragment_kind_key, 0) + 1)
            if len(self.current_fragment_debug) < 80:
                self.current_fragment_debug.append({
                    'kind': fragment_kind_key,
                    'skip': fragment_skip,
                    'udp_len': len(pkt),
                    'outer12': pkt[:12].hex(),
                    'payload_head': pkt[12:28].hex(),
                    'raw_head': raw_chunk[:16].hex(),
                })
        need = self.expected - self.current_raw
        if need > 0:
            raw_part = raw_chunk[:need]
            overrun = raw_chunk[need:]
        else:
            raw_part = b''
            overrun = raw_chunk
        final_overrun = len(overrun)
        self.current_raw += len(raw_part)
        self.current += raw_part
        if self.current_raw >= self.expected:
            frame = bytes(self.current)
            assembly_info = {
                'frame_next': self.frames_in + 1,
                'expected': self.expected,
                'assembled_raw': self.current_raw,
                'frame_len': len(frame),
                'fragment_count': self.current_fragment_count,
                'outer_total': self.current_outer_total,
                'outer_indices': list(self.current_outer_indices),
                'kind_counts': dict(self.current_kind_counts),
                'last_kind': fragment_kind_key,
                'last_skip': fragment_skip,
                'final_overrun': final_overrun,
                'final_overrun_head': overrun[:32].hex(),
                'final_raw_tail': raw_part[-32:].hex(),
                'is_idr_frame': self.current_is_idr,
            }
            last_index = (
                self.current_outer_indices[-1]
                if self.current_outer_indices else None
            )
            if (self.current_outer_total is not None
                    and self.current_outer_total != self.current_fragment_count):
                self.fragment_total_mismatches += 1
                assembly_info['fragment_total_mismatch'] = True
            if (self.current_outer_total is not None and last_index is not None
                    and last_index + 1 < self.current_outer_total):
                self.fragment_incomplete_transport += 1
                assembly_info['fragment_incomplete_transport'] = True
            if (assembly_info.get('fragment_total_mismatch')
                    or assembly_info.get('fragment_incomplete_transport')):
                if len(self.fragment_sequence_debug) < 24:
                    self.fragment_sequence_debug.append({
                        'event': 'assembled_mismatch',
                        'frame_next': self.frames_in + 1,
                        'expected': self.expected,
                        'fragment_count': self.current_fragment_count,
                        'outer_total': self.current_outer_total,
                        'outer_indices': list(self.current_outer_indices),
                        'kind_counts': dict(self.current_kind_counts),
                        'final_overrun': final_overrun,
                        'final_overrun_head': overrun[:24].hex(),
                    })
            if CARRY_OVERRUN and overrun:
                start = find_annexb_start(overrun)
                if start is not None:
                    self.pending_overrun = overrun[start:]
                    self.overrun_carry_candidates += 1
                    if start:
                        self.overrun_carry_dropped += start
                    assembly_info['carry_candidate_offset'] = start
                    assembly_info['carry_candidate_len'] = len(self.pending_overrun)
                    assembly_info['carry_candidate_head'] = self.pending_overrun[:32].hex()
                    if len(self.overrun_carry_debug) < 24:
                        self.overrun_carry_debug.append({
                            'event': 'candidate',
                            'frame_next': self.frames_in + 1,
                            'overrun_len': len(overrun),
                            'annexb_offset': start,
                            'carry_len': len(self.pending_overrun),
                            'carry_head': self.pending_overrun[:32].hex(),
                        })
            if len(self.frame_assembly_debug) < 48:
                self.frame_assembly_debug.append(assembly_info)
            units = self._nal_units(frame)
            if any(u['type'] in (5, 7, 8) for u in units):
                info = dict(assembly_info)
                info['nal_types'] = [u['type'] for u in units[:16]]
                info['fragments'] = list(self.current_fragment_debug)
                self.idr_assembly_debug.append(info)
            self.idr_assembly_debug = self.idr_assembly_debug[-12:]
            self.current = None
            self.current_raw = 0
            self.current_fragment_count = 0
            self.current_kind_counts = {}
            self.current_fragment_debug = []
            self.current_outer_total = None
            self.current_outer_indices = []
            self.current_is_idr = False
            self.expected = 0
            if self.current_damaged:
                self.damaged_frames += 1
            return [frame]
        return []

    def _continuation_skip(self, pkt, payload):
        if CONTINUATION_SKIP is not None:
            return CONTINUATION_SKIP
        if len(pkt) >= 12:
            fragment_kind = pkt[11]
            if fragment_kind == 0x85:
                return 1
            if fragment_kind in (0xb6, 0xba, 0xbe):
                return FINAL_CONTINUATION_SKIP
        if not payload:
            return 0
        # Most continuation packets carry a one-byte LSP counter/header (0x72).
        # Some short/final continuations begin with 0x00/0x01, which can also be
        # valid H.264 payload data. Keep those bytes unless we decode the full
        # variable-length LSP integer format.
        if payload[0] == 0x72:
            return 1
        return 0

    def _nal_units(self, frame):
        out = []
        i = 0
        while i < len(frame) - 3:
            if frame[i:i + 4] == START4:
                j = i + 4
                if j < len(frame):
                    out.append({'offset': i, 'start_len': 4, 'type': frame[j] & 31, 'header': frame[j]})
                i = j
            elif frame[i:i + 3] == START3:
                j = i + 3
                if j < len(frame):
                    out.append({'offset': i, 'start_len': 3, 'type': frame[j] & 31, 'header': frame[j]})
                i = j
            else:
                i += 1
        return out

    def _split_access_units(self, frame):
        if not SPLIT_MULTI_AUD:
            return [frame]
        units = self._nal_units(frame)
        if not units or units[0]['type'] != 9:
            return [frame]
        starts = [u['offset'] for u in units if u['type'] == 9]
        if len(starts) <= 1:
            return [frame]
        parts = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(frame)
            if end > start:
                parts.append(frame[start:end])
        if len(parts) > 1:
            self.access_unit_splits += len(parts) - 1
        return parts or [frame]

    def _accept_frame(self, frame, valid_nts):
        if not frame:
            return False
        return any(typ in (1, 5, 7, 8) for typ in valid_nts)

    def _trim_to_annexb(self, frame):
        start = find_annexb_start(frame)
        if start is None:
            return frame, 0
        if start == 0:
            return frame, 0
        return frame[start:], start

    def _valid_nal_units(self, units):
        out = []
        for u in units:
            header = u['header']
            typ = u['type']
            nal_ref_idc = header & 0x60
            forbidden_zero = header & 0x80
            if forbidden_zero:
                continue
            if typ in (1, 5, 7, 8) and not nal_ref_idc:
                continue
            out.append(u)
        return out

    def _nal_types(self, frame):
        return [u['type'] for u in self._nal_units(frame)]

    def _dump_interesting_frame(self, frame, units):
        if self.dumps_written >= DUMP_MAX:
            return
        self.dumps_written += 1
        os.makedirs(DUMP_DIR, exist_ok=True)
        types = [u['type'] for u in units]
        stamp = time.strftime('%Y%m%d_%H%M%S')
        safe_types = '-'.join(map(str, types[:12])) or 'none'
        context = b''.join(f for _, f in self.recent_frames)
        path = os.path.join(DUMP_DIR, f'frame_{self.frames_in:06d}_{safe_types}_{stamp}.h264')
        with open(path, 'wb') as f:
            f.write(context)
        info = {
            'frame': self.frames_in,
            'types': types,
            'units': units,
            'path': path,
            'context_bytes': len(context),
            'has_sps_in_context': b'\x00\x00\x00\x01\x67' in context or b'\x00\x00\x01\x67' in context,
            'has_pps_in_context': b'\x00\x00\x00\x01\x68' in context or b'\x00\x00\x01\x68' in context,
            'has_idr': 5 in types,
        }
        if 5 in types:
            self.idr_frames.append(info)
        if info['has_sps_in_context'] or info['has_pps_in_context']:
            self.real_sps_pps_dumps.append(info)
        # Keep status bounded.
        self.idr_frames = self.idr_frames[-12:]
        self.real_sps_pps_dumps = self.real_sps_pps_dumps[-12:]

    def status(self):
        now = time.time()
        return {
            'ok': True,
            'uptime': now - self.started_at,
            'udp_port': VIDEO_FORWARD_PORT,
            'http_port': HTTP_PORT,
            'sps_pps': self.sps_name,
            'continuation_skip': CONTINUATION_SKIP,
            'continuation_skip_raw': CONTINUATION_SKIP_RAW,
            'fixed_cont_skip_enabled': FIXED_CONT_SKIP_ENABLED,
            'split_multi_aud': SPLIT_MULTI_AUD,
            'carry_overrun': CARRY_OVERRUN,
            'final_continuation_skip': FINAL_CONTINUATION_SKIP,
            'idr_continuation_skip': IDR_CONTINUATION_SKIP,
            'idr_continuation_skip_raw': IDR_CONTINUATION_SKIP_RAW,
            'crop': CROP if CROP_ENABLED else None,
            'packet_trailer': PACKET_TRAILER,
            'fram_size_overhead': FRAM_SIZE_OVERHEAD,
            'restart_on_idr': RESTART_ON_IDR,
            'sync_on_first_idr': SYNC_ON_FIRST_IDR,
            'decoder_synced': self.decoder_synced,
            'fake_idr_enabled': FAKE_IDR_ENABLED,
            'fake_idr_bytes': len(self.fake_idr_bytes),
            'sps_pps_candidates': sorted(SPS_PPS_CANDIDATES),
            'udp_packets': self.udp_packets,
            'frames_raw': self.frames_raw,
            'frames_in': self.frames_in,
            'frames_dropped': self.frames_dropped,
            'frames_filtered': self.frames_filtered,
            'frames_abandoned': self.frames_abandoned,
            'capture': ({'path': self.capture.path, 'packets': self.capture.count,
                         'remaining_s': round(self.capture_until - now, 1)}
                        if self.capture else self.last_capture),
            'packet_loss': {
                'gap_events': self.seq_gap_events,
                'lost_packets': self.seq_lost_packets,
                'damaged_frames': self.damaged_frames,
                'frames_after_loss_until_idr': self.chain_broken_frames,
                'last_gap_age': None if not self.last_gap_at else round(time.time() - self.last_gap_at, 1),
            },
            'dumps_written': self.dumps_written,
            'dump_max': DUMP_MAX,
            'access_unit_splits': self.access_unit_splits,
            'jpeg_out': self.jpeg_out,
            'ffmpeg_restarts': self.ffmpeg_restarts,
            'low_latency': LOW_LATENCY,
            'decode_threads': DECODE_THREADS if LOW_LATENCY else 'auto',
            'pipeline_latency': self._latency_status(),
            'ffmpeg_backlog_frames': self.proc_written - self.proc_jpegs,
            'idr_restarts': self.idr_restarts,
            'batch_decode_interval': BATCH_DECODE_INTERVAL,
            'batch_decode_frames': BATCH_DECODE_FRAMES,
            'batch_decode_timeout': BATCH_DECODE_TIMEOUT,
            'batch_publish_frames': BATCH_PUBLISH_FRAMES,
            'batch_publish_fps': BATCH_PUBLISH_FPS,
            'recent_frame_window': RECENT_FRAME_WINDOW,
            'batch_decodes': self.batch_decodes,
            'batch_jpeg_out': self.batch_jpeg_out,
            'batch_jpeg_candidates': self.batch_jpeg_candidates,
            'batch_waiting_for_idr': self.batch_waiting_for_idr,
            'stale_restart_sec': STALE_RESTART_SEC,
            'stale_restarts': self.stale_restarts,
            'quality_gate': QUALITY_GATE,
            'quality_checked': self.quality_checked,
            'quality_rejected': self.quality_rejected,
            'last_quality': self.last_quality,
            'frames_trimmed': self.frames_trimmed,
            'bytes_trimmed': self.bytes_trimmed,
            'cont_skip_counts': self.cont_skip_counts,
            'cont_kind_counts': self.cont_kind_counts,
            'cont_kind_head_counts': self.cont_kind_head_counts,
            'cont_head_counts_top': sorted(
                self.cont_head_counts.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:24],
            'cont_debug': self.cont_debug,
            'frame_assembly_debug': self.frame_assembly_debug,
            'idr_assembly_debug': self.idr_assembly_debug,
            'fram_debug': self.fram_debug,
            'fragment_sequence': {
                'total_mismatches': self.fragment_total_mismatches,
                'orphans': self.fragment_orphans,
                'incomplete_transport': self.fragment_incomplete_transport,
                'debug': self.fragment_sequence_debug,
            },
            'overrun_carry': {
                'pending_bytes': len(self.pending_overrun),
                'candidates': self.overrun_carry_candidates,
                'used': self.overrun_carry_used,
                'dropped_prefix_bytes': self.overrun_carry_dropped,
                'duplicate_drops': self.overrun_carry_duplicate,
                'used_bytes': self.overrun_carry_bytes,
                'debug': self.overrun_carry_debug,
            },
            'queue': self.frame_queue.qsize(),
            'h264_clients': len(self.h264_clients),
            'ws_transcode': (dict(self.transcoder.status(), latency_ms=(
                round(sorted(self.ws_latency)[len(self.ws_latency) // 2] * 1000, 1)
                if self.ws_latency else None)) if self.transcoder else
                {'enabled': False, 'reason': 'PyAV missing' if av is None else 'disabled'}),
            'ws_clients': [{'skipped': c.skipped, 'resyncs': c.resyncs, 'queued': c.queue.qsize()}
                           for c in self.ws_clients],
            'latest_age': None if not self.latest_at else now - self.latest_at,
            'last_udp_age': None if not self.last_udp_at else now - self.last_udp_at,
            'last_frame_age': None if not self.last_frame_at else now - self.last_frame_at,
            'nal_counts': self.nal_counts,
            'valid_nal_counts': self.valid_nal_counts,
            'frame_size': self._frame_size_status(),
            'first_frame_infos': self.first_frame_infos,
            'filtered_frame_infos': self.filtered_frame_infos,
            'idr_frames': self.idr_frames,
            'real_sps_pps_dumps': self.real_sps_pps_dumps,
            'last_error': self.last_error,
        }


worker = VideoWorker()


async def index(req):
    html = """<!doctype html><html><head><meta charset=utf-8><title>Kart Video</title>
<style>body{margin:0;background:#111;color:#eee;font:14px system-ui,Segoe UI,sans-serif}main{display:grid;grid-template-columns:minmax(320px,1fr) 360px;gap:16px;padding:16px}.video{background:#000;aspect-ratio:16/9;display:flex;align-items:center;justify-content:center}.video img{width:100%;height:100%;object-fit:cover}.status{white-space:pre-wrap;background:#1b1d22;padding:12px;border:1px solid #333;overflow:auto}@media(max-width:800px){main{grid-template-columns:1fr}}</style>
</head><body><main><div class=video><img src="/video.mjpg"></div><pre class=status id=s>loading...</pre></main>
<script>setInterval(async()=>{try{s.textContent=JSON.stringify(await (await fetch('/status')).json(),null,2)}catch(e){s.textContent=e}},500)</script>
</body></html>"""
    return web.Response(text=html, content_type='text/html')


async def status(req):
    return web.json_response(worker.status())


async def set_sps(req):
    name = req.match_info['name']
    try:
        result = await worker.set_sps(name)
    except KeyError:
        return web.json_response({
            'ok': False,
            'error': f'unknown SPS/PPS candidate: {name}',
            'candidates': sorted(SPS_PPS_CANDIDATES),
        }, status=404)
    return web.json_response(result)


async def reset(req):
    worker.reset_stats()
    await worker._start_ffmpeg()
    return web.json_response(worker.status())


async def snapshot(req):
    if not worker.latest_jpeg:
        return web.json_response({'ok': False, 'error': 'no jpeg yet'}, status=503)
    os.makedirs(DUMP_DIR, exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    path = os.path.join(DUMP_DIR, f'snapshot_{worker.sps_name}_{stamp}_{worker.jpeg_out}.jpg')
    with open(path, 'wb') as f:
        f.write(worker.latest_jpeg)
    return web.json_response({
        'ok': True,
        'path': path,
        'sps_pps': worker.sps_name,
        'jpeg_out': worker.jpeg_out,
        'frames_in': worker.frames_in,
    })


async def latest_jpg(req):
    if not worker.latest_jpeg:
        return web.Response(status=503, text='no jpeg yet')
    return web.Response(body=worker.latest_jpeg, content_type='image/jpeg',
                        headers={'Cache-Control': 'no-store'})


async def video_mjpg(req):
    delay = 1.0 / max(1.0, min(60.0, MJPEG_MAX_FPS))
    resp = web.StreamResponse(headers={
        'Content-Type': 'multipart/x-mixed-replace; boundary=frame',
        'Cache-Control': 'no-store',
    })
    await resp.prepare(req)
    if worker.latest_jpeg is None:
        try:
            await asyncio.wait_for(worker.ready.wait(), timeout=10)
        except asyncio.TimeoutError:
            await resp.write_eof()
            return resp
    last_sent = -1
    try:
        while True:
            async with worker.jpeg_condition:
                try:
                    await asyncio.wait_for(
                        worker.jpeg_condition.wait_for(
                            lambda: worker.jpeg_out != last_sent),
                        timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            jpg = worker.latest_jpeg
            if jpg:
                last_sent = worker.jpeg_out
                sent_at = time.monotonic()
                await resp.write(
                    b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                    + str(len(jpg)).encode() + b'\r\n\r\n' + jpg + b'\r\n'
                )
                # Rate limit only: sleeping a full interval after every frame
                # added up to one frame of delay even when the next was ready.
                remaining = delay - (time.monotonic() - sent_at)
                if remaining > 0:
                    await asyncio.sleep(remaining)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


async def capture(req):
    """POST /capture?seconds=60 - write the raw forwarded UDP packets to a pcap."""
    if worker.capture:
        return web.json_response({'ok': False, 'error': 'capture already running',
                                  'capture': worker.status()['capture']}, status=409)
    seconds = max(1.0, min(600.0, float(req.query.get('seconds', '60'))))
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    path = os.path.join(CAPTURE_DIR, time.strftime('raw_%Y%m%d_%H%M%S.pcap'))
    worker.capture = PcapWriter(path, VIDEO_FORWARD_PORT)
    worker.capture_until = time.time() + seconds
    return web.json_response({'ok': True, 'path': path, 'seconds': seconds})


async def video_ws(req):
    """Raw H.264 access units for browser-side WebCodecs decoding.

    Binary messages: 1 byte flags (1 = keyframe) followed by one Annex-B access
    unit. Keyframes carry SPS/PPS inline.
    """
    ws = web.WebSocketResponse(compress=False, heartbeat=10)
    await ws.prepare(req)
    client = WsClient()
    # Replay the current GOP so the decoder has a reference picture right away.
    for message in worker.gop:
        client.offer(message, message[0] == 1)
    worker.ws_clients.add(client)

    async def drain_incoming():
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break

    reader = asyncio.create_task(drain_incoming())
    try:
        while not ws.closed:
            get = asyncio.create_task(client.queue.get())
            done, _ = await asyncio.wait({get, reader}, return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                get.cancel()
                break
            await ws.send_bytes(get.result())
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        worker.ws_clients.discard(client)
        reader.cancel()
    return ws


PLAYER_JS = Path(__file__).with_name('player.js')


async def player_js(req):
    try:
        body = PLAYER_JS.read_text(encoding='utf-8')
    except FileNotFoundError:
        return web.Response(status=404, text='player.js missing next to video_worker.py')
    return web.Response(text=body, content_type='application/javascript',
                        headers={'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*'})


async def video_mp4(req):
    q = asyncio.Queue(maxsize=8)
    worker.h264_clients.add(q)
    proc = None
    writer_task = None
    resp = None
    try:
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-fflags', 'nobuffer',
            '-f', 'h264', '-i', 'pipe:0',
            '-c:v', 'copy',
            '-an',
            '-f', 'mp4',
            '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
            'pipe:1',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        proc.stdin.write(worker.sps_pps)
        await proc.stdin.drain()

        async def feed():
            try:
                while True:
                    frame = await q.get()
                    proc.stdin.write(frame)
                    await proc.stdin.drain()
            except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                pass

        writer_task = asyncio.create_task(feed())
        resp = web.StreamResponse(headers={
            'Content-Type': 'video/mp4',
            'Cache-Control': 'no-store',
        })
        await resp.prepare(req)
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                break
            await resp.write(chunk)
        return resp
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        worker.h264_clients.discard(q)
        if writer_task:
            writer_task.cancel()
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
    return resp if resp is not None else web.Response(status=499)


async def start():
    await worker.start()
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/status', status)
    app.router.add_post('/sps/{name}', set_sps)
    app.router.add_post('/reset', reset)
    app.router.add_post('/snapshot', snapshot)
    app.router.add_get('/latest.jpg', latest_jpg)
    app.router.add_get('/video.mjpg', video_mjpg)
    app.router.add_get('/video.mp4', video_mp4)
    app.router.add_get('/video.ws', video_ws)
    app.router.add_post('/capture', capture)
    app.router.add_get('/player.js', player_js)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', HTTP_PORT)
    await site.start()
    print(f'Video worker: http://<host>:{HTTP_PORT}/  SPS={worker.sps_name}', flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == '__main__':
    asyncio.run(start())
