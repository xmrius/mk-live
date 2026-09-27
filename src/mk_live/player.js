// Low-latency kart video player: raw H.264 over WebSocket, decoded with WebCodecs.
// Served by video_worker.py at /player.js; used by the drive UI and tools/latency_meter.html.
//
// Protocol (/video.ws): binary messages, byte 0 = 1 for keyframes, rest = one Annex-B
// access unit. Keyframes carry SPS/PPS inline, so the decoder is configured without a
// `description` (Annex-B mode).
(function () {
  'use strict';

  // The OV9782 delivers 1280x720; the stream is coded as 1280x800 and the padded rows
  // below y=720 are garbage (see docs/CURRENT_STATE.md).
  const CROP = { w: 1280, h: 720 };
  const FALLBACK_CODEC = 'avc1.640020'; // High, level 3.2 as in the kart SPS

  function codecFromSps(au) {
    // Find the SPS NAL (type 7) and build avc1.PPCCLL from profile/constraints/level.
    for (let i = 0; i + 7 < au.length; i++) {
      if (au[i] === 0 && au[i + 1] === 0 && au[i + 2] === 1 && (au[i + 3] & 31) === 7) {
        const hex = b => b.toString(16).padStart(2, '0');
        return 'avc1.' + hex(au[i + 4]) + hex(au[i + 5]) + hex(au[i + 6]);
      }
    }
    return FALLBACK_CODEC;
  }

  async function supported() {
    if (typeof VideoDecoder === 'undefined') return false;
    try {
      const r = await VideoDecoder.isConfigSupported({ codec: FALLBACK_CODEC, optimizeForLatency: true });
      return !!r.supported;
    } catch (e) {
      return false;
    }
  }

  function start(container, opts) {
    opts = opts || {};
    const wsUrl = opts.wsUrl || ('ws://' + location.hostname + ':9001/video.ws');
    const canvas = document.createElement('canvas');
    canvas.width = CROP.w;
    canvas.height = CROP.h;
    canvas.style.width = '100%';
    canvas.style.height = '100%';
    canvas.style.objectFit = 'contain';
    canvas.style.display = 'block';
    container.appendChild(canvas);
    const ctx = canvas.getContext('2d');

    const stats = { decoded: 0, fps: 0, skipped: 0, resets: 0, codec: null, hw: null, error: null, connected: false,
                    decodeMs: null, decodeMaxMs: 0, maxQueue: 0, recvGapMaxMs: 0 };
    const submitted = new Map(); // chunk timestamp -> performance.now() at decode()
    const failures = [];
    let lastRecv = 0;
    const hwPref = opts.hardwareAcceleration || 'prefer-hardware';
    let decoder = null;
    let configured = false;
    let needKey = true;
    let timestamp = 0;
    let ws = null;
    let stopped = false;
    let fpsCount = 0;
    const fpsTimer = setInterval(() => { stats.fps = fpsCount; fpsCount = 0; }, 1000);

    function newDecoder() {
      if (decoder && decoder.state !== 'closed') {
        try { decoder.close(); } catch (e) { /* already closed */ }
      }
      configured = false;
      needKey = true;
      decoder = new VideoDecoder({
        output: frame => {
          const t0 = submitted.get(frame.timestamp);
          if (t0 !== undefined) {
            submitted.delete(frame.timestamp);
            const ms = performance.now() - t0;
            stats.decodeMs = stats.decodeMs === null ? ms : stats.decodeMs * 0.9 + ms * 0.1;
            stats.decodeMaxMs = Math.max(stats.decodeMaxMs, ms);
          }
          // Draw immediately; the canvas presents on the next vsync.
          ctx.drawImage(frame, 0, 0, CROP.w, CROP.h, 0, 0, CROP.w, CROP.h);
          frame.close();
          stats.decoded++;
          fpsCount++;
        },
        error: e => {
          stats.error = String(e);
          stats.resets++;
          // The software decoder rejects the kart's damaged padding rows; if the
          // decoder keeps failing, let the page fall back (e.g. to MJPEG).
          const t = performance.now();
          failures.push(t);
          while (failures.length && t - failures[0] > 10000) failures.shift();
          if (failures.length >= 3 && opts.onFail && !stopped) {
            opts.onFail(stats.error);
            return;
          }
          if (!stopped) setTimeout(newDecoder, 0);
        },
      });
    }

    function configure(au) {
      const codec = codecFromSps(au);
      const config = { codec, optimizeForLatency: true, hardwareAcceleration: hwPref };
      decoder.configure(config);
      stats.codec = codec;
      stats.hw = config.hardwareAcceleration;
      configured = true;
    }

    function onMessage(ev) {
      const now = performance.now();
      if (lastRecv) stats.recvGapMaxMs = Math.max(stats.recvGapMaxMs, now - lastRecv);
      lastRecv = now;
      stats.maxQueue = Math.max(stats.maxQueue, decoder.decodeQueueSize);
      const buf = new Uint8Array(ev.data);
      const key = buf[0] === 1;
      const au = buf.subarray(1);
      if (needKey && !key) { stats.skipped++; return; }
      // Never build up delay: if the decoder falls behind, skip to the next keyframe.
      if (decoder.decodeQueueSize > 2 && !key) { needKey = true; stats.skipped++; return; }
      if (!configured) configure(au);
      needKey = false;
      timestamp += 33333;
      submitted.set(timestamp, performance.now());
      if (submitted.size > 300) submitted.clear();
      decoder.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp, data: au }));
    }

    function connect() {
      if (stopped) return;
      ws = new WebSocket(wsUrl);
      ws.binaryType = 'arraybuffer';
      ws.onopen = () => { stats.connected = true; };
      ws.onmessage = onMessage;
      ws.onclose = () => {
        stats.connected = false;
        if (!stopped) {
          newDecoder();
          setTimeout(connect, 1000);
        }
      };
    }

    newDecoder();
    connect();

    return {
      canvas,
      stats,
      stop() {
        stopped = true;
        clearInterval(fpsTimer);
        if (ws) ws.close();
        if (decoder && decoder.state !== 'closed') decoder.close();
        canvas.remove();
      },
    };
  }

  window.KartPlayer = { supported, start };
})();
