// MK Live cockpit: video, status HUD, pairing wizard and keyboard driving.
(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const { t } = window.MKI18N;
  const WORKER = 'http://' + location.hostname + ':9001';
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } },
  };

  let status = null;
  let player = null;
  let mode = store.get('mk.videoMode', 'webcodecs');

  // ------------------------------------------------------------------ helpers
  function toast(msg, ms = 3500) {
    const t = $('toast');
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, ms);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, opts);
    let body = null;
    try { body = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) {
      const msg = body && body.code ? t('err.' + body.code, body.params || {}) : body && body.error;
      throw new Error(msg || ('HTTP ' + res.status));
    }
    return body;
  }

  const setOpenKartState = state => api('/api/openkart/state', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ state }),
  });

  function setChip(name, level, text) {
    const chip = document.querySelector(`[data-chip="${name}"]`);
    chip.classList.remove('ok', 'warn', 'alert');
    if (level) chip.classList.add(level);
    if (text !== undefined) {
      const em = chip.querySelector('em');
      if (em) em.textContent = text;
    }
  }

  function setBars(el, n) {
    [...el.children].forEach((bar, i) => bar.classList.toggle('on', i < n));
  }

  // ------------------------------------------------------------------ video
  function setMode(next) {
    if (next === 'webcodecs' && !window.KartPlayer) next = 'mjpeg';
    mode = next;
    store.set('mk.videoMode', next);
    document.querySelectorAll('.seg [data-mode]').forEach(b => b.classList.toggle('on', b.dataset.mode === next));
    if (player) { player.stop(); player = null; }
    const box = $('video');
    box.innerHTML = '';
    if (next === 'webcodecs') {
      player = window.KartPlayer.start(box, {
        wsUrl: WORKER.replace('http', 'ws') + '/video.ws',
        onFail: () => { toast(t('toast.wcFail')); setTimeout(() => setMode('mjpeg'), 0); },
      });
    } else {
      const img = document.createElement('img');
      img.alt = t('a.video');
      img.src = '/worker/video.mjpg?ts=' + Date.now();
      box.appendChild(img);
    }
  }

  function loadPlayer() {
    const s = document.createElement('script');
    s.src = WORKER + '/player.js';
    s.onload = async () => {
      const ok = await window.KartPlayer.supported();
      // Browsers only expose WebCodecs on HTTPS or localhost; over a LAN IP
      // the low-latency path silently disappears, so say why.
      if (!ok && !window.isSecureContext) {
        toast(t('toast.insecure'), 9000);
      }
      setMode(ok && mode === 'webcodecs' ? 'webcodecs' : 'mjpeg');
    };
    s.onerror = () => setMode('mjpeg');
    document.head.appendChild(s);
  }
  document.querySelectorAll('.seg [data-mode]').forEach(b => b.addEventListener('click', () => setMode(b.dataset.mode)));

  // ------------------------------------------------------------------ status HUD
  function videoFps() {
    if (player) return player.stats.fps;
    const v = status && status.video;
    return v && v.ok && v.last_udp_age !== null && v.last_udp_age < 1 ? 30 : 0;
  }

  function renderStatus() {
    if (!status) return;
    const ok = status.openkart && status.openkart.ok;
    const state = ok ? status.openkart.state : null;
    const kart = status.kart;
    const apLabel = ['RUNNING', 'DOWN', 'PAIRING'].includes(state) ? t('ap.' + state) : state;
    setChip('ap', !ok ? 'alert' : state === 'RUNNING' ? 'ok' : state === 'PAIRING' ? 'warn' : '', ok ? apLabel : t('ap.error'));
    setChip('link', kart ? 'ok' : '', kart ? String(kart.character || 'KART').toUpperCase() : '–');
    setChip('drive', driveSocketOpen && status.drive.sending ? 'ok' : driveSocketOpen ? 'warn' : 'alert');

    const fps = videoFps();
    const udpStale = kart && status.video && status.video.ok && (status.video.last_udp_age === null || status.video.last_udp_age > 3);
    setChip('video', udpStale ? 'alert' : fps >= 20 ? 'ok' : fps > 0 ? 'warn' : '', fps ? fps + ' FPS' : '–');
    setChip('cable', !kart ? '' : kart.cable ? 'alert' : 'ok');

    const bat = kart && typeof kart.battery === 'number' ? kart.battery : null;
    setBars($('bat'), bat === null ? 0 : bat);
    setChip('battery', bat === null ? '' : bat <= 0 ? 'alert' : bat <= 1 ? 'warn' : 'ok');

    const sig = kart && typeof kart.signal === 'number' ? kart.signal : null;
    const bars = sig === null ? 0 : sig >= -50 ? 4 : sig >= -60 ? 3 : sig >= -70 ? 2 : sig >= -80 ? 1 : 0;
    setBars($('rssiBars'), bars);
    setChip('rssi', sig === null ? '' : sig < -75 ? 'alert' : sig < -68 ? 'warn' : 'ok', sig === null ? '–' : sig + ' dBm');

    renderNotice(ok, state, kart);
    renderDrawer(kart);
  }

  function renderNotice(ok, state, kart) {
    const box = $('notice');
    if (!$('pairing').hidden) { box.hidden = true; return; }
    const primary = $('noticePrimary');
    const secondary = $('noticeSecondary');
    let kicker = t('notice.status'), title = '', text = '', p = null, s = null;
    if (!ok) {
      kicker = t('notice.system'); title = t('notice.okDown');
      text = t('notice.okDown.text');
    } else if (state === 'DOWN') {
      title = t('notice.apOff');
      text = t('notice.apOff.text');
      p = [t('btn.apStart'), () => setOpenKartState('RUNNING').then(() => toast(t('toast.apStarting'))).catch(e => toast(e.message))];
      s = [t('btn.pair'), openPairing];
    } else if (state === 'RUNNING' && !kart) {
      title = t('notice.waiting');
      text = t('notice.waiting.text');
      p = [t('btn.pair'), openPairing];
    } else if (kart && kart.cable) {
      kicker = t('notice.note'); title = t('notice.cable');
      text = t('notice.cable.text');
    } else {
      box.hidden = true;
      return;
    }
    $('noticeKicker').textContent = kicker;
    $('noticeTitle').textContent = title;
    $('noticeText').textContent = text;
    for (const [btn, spec] of [[primary, p], [secondary, s]]) {
      btn.hidden = !spec;
      if (spec) { btn.textContent = spec[0]; btn.onclick = spec[1]; }
    }
    box.hidden = false;
  }

  function renderDrawer(kart) {
    if ($('drawer').hidden) return;
    const v = status.video || {};
    const ws = v.ws_transcode || {};
    $('stDecoder').textContent = player ? `${player.stats.codec || '…'} · ${player.stats.decodeMs === null ? '–' : player.stats.decodeMs.toFixed(1) + ' ms'}` : t(window.isSecureContext ? 'st.mjpeg' : 'st.mjpegInsecure');
    $('stFps').textContent = videoFps() || '–';
    $('stLatency').textContent = mode === 'webcodecs' ? (ws.latency_ms !== undefined && ws.latency_ms !== null ? ws.latency_ms + ' ms' : '–')
      : (v.pipeline_latency ? v.pipeline_latency.median_ms + ' ms' : '–');
    const ps = player && player.stats;
    $('stSkipped').textContent = ps ? t('st.skippedFmt', { n: ps.skipped, q: ps.maxQueue, d: Math.round(ps.decodeMaxMs) }) : '–';
    $('stRecvGap').textContent = ps ? Math.round(ps.recvGapMaxMs) + ' ms' : '–';
    const loss = v.packet_loss;
    $('stLoss').textContent = loss ? t('st.lossFmt', { n: loss.lost_packets, g: loss.gap_events }) : '–';
    $('stKart').textContent = kart ? `${kart.serial} · FW ${kart.firmware}` : '–';
  }

  async function pollStatus() {
    try {
      status = await api('/api/status');
    } catch (e) {
      status = { openkart: { ok: false }, kart: null, video: {}, drive: {} };
    }
    renderStatus();
    if (pairing.active) pairing.check();
  }

  // ------------------------------------------------------------------ settings drawer
  $('settingsBtn').addEventListener('click', () => {
    const d = $('drawer');
    d.hidden = !d.hidden;
    $('settingsBtn').setAttribute('aria-expanded', String(!d.hidden));
    renderStatus();
  });
  $('resyncBtn').addEventListener('click', async () => {
    toast(t('toast.resyncing'));
    try { await api('/video/resync?attempts=1&wait=6', { method: 'POST' }); toast(t('toast.resynced')); }
    catch (e) { toast(t('toast.resyncFailed', { msg: e.message })); }
  });
  $('apToggleBtn').addEventListener('click', async () => {
    toast(t('toast.apRestarting'));
    try { await setOpenKartState('DOWN'); await setOpenKartState('RUNNING'); }
    catch (e) { toast(t('toast.error', { msg: e.message })); }
  });

  // ------------------------------------------------------------------ pairing wizard
  const pairing = {
    active: false,
    started: false,
    since: 0,
    step(n, done = []) {
      document.querySelectorAll('#pairSteps li').forEach(li => {
        const k = Number(li.dataset.step);
        li.classList.toggle('active', k === n);
        li.classList.toggle('done', done.includes(k));
      });
    },
    say(text, cls = '') {
      const el = $('pairStatus');
      el.textContent = text;
      el.className = 'pair-status ' + cls;
    },
    check() {
      if (!this.started || !status) return;
      const st = status.openkart && status.openkart.state;
      if (st === 'RUNNING' && status.kart) {
        this.started = false;
        this.step(0, [1, 2, 3]);
        this.say(t('pair.paired', { name: status.kart.character || 'Kart' }), 'ok');
        $('pairing').classList.add('success');
        setTimeout(closePairing, 1800);
      } else if (Date.now() - this.since > 90000) {
        this.say(t('pair.nothing'), 'err');
      }
    },
  };

  function openPairing() {
    pairing.active = true;
    pairing.started = false;
    $('pairing').hidden = false;
    $('pairing').classList.remove('success');
    $('pairQr').hidden = true;
    $('qrPlaceholder').hidden = false;
    $('qrMeta').textContent = '';
    $('pairStart').disabled = false;
    pairing.step(1);
    pairing.say(t('pair.ready'));
    renderStatus();
  }

  async function startPairing() {
    $('pairStart').disabled = true;
    pairing.say(t('pair.starting'));
    try {
      const r = await setOpenKartState('PAIRING');
      if (!r.pairing_ready) throw new Error(t('pair.noData'));
      const res = await fetch('/api/pairing/qr.png?ts=' + Date.now());
      if (!res.ok) throw new Error(await res.text());
      const blob = await res.blob();
      const img = $('pairQr');
      if (img.src) URL.revokeObjectURL(img.src);
      img.src = URL.createObjectURL(blob);
      img.hidden = false;
      $('qrPlaceholder').hidden = true;
      $('qrMeta').textContent = `${res.headers.get('X-Pairing-SSID') || ''} · ${t('pair.channel')} ${res.headers.get('X-Pairing-Channel') || '?'}`;
      pairing.started = true;
      pairing.since = Date.now();
      pairing.step(2, [1]);
      pairing.say(t('pair.waiting'));
    } catch (e) {
      pairing.say(t('toast.error', { msg: e.message }), 'err');
      $('pairStart').disabled = false;
    }
  }

  async function closePairing() {
    const wasWaiting = pairing.started;
    pairing.active = false;
    pairing.started = false;
    $('pairing').hidden = true;
    if (wasWaiting) {
      try { await setOpenKartState('RUNNING'); } catch (e) { toast(t('toast.ap', { msg: e.message })); }
    }
    renderStatus();
  }

  $('pairBtn').addEventListener('click', openPairing);
  $('pairStart').addEventListener('click', startPairing);
  $('pairCancel').addEventListener('click', closePairing);

  // ------------------------------------------------------------------ IMU calibration
  // Five still poses give axis mapping, offsets and scale of the accelerometer;
  // one full turn by hand gives the gyro scale for the compass.
  const CALIB_STEPS = ['flat', 'nose_up', 'nose_down', 'left_down', 'right_down', 'spin'];
  let calibTimer = null;
  const calibDone = new Set();
  const postJson = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  function calibSay(text, cls = '') {
    $('calibStatus').textContent = text;
    $('calibStatus').className = 'pair-status ' + cls;
  }

  function renderCalib() {
    calibDone.clear();
    $('calibSave').disabled = true;
    calibSay('');
    const ol = $('calibSteps');
    ol.innerHTML = '';
    CALIB_STEPS.forEach((key, i) => {
      const li = document.createElement('li');
      li.dataset.key = key;
      li.innerHTML = `<b>${i + 1}</b><div><strong></strong><span></span></div><button class="btn" type="button"></button>`;
      li.querySelector('strong').textContent = t('calib.' + key);
      li.querySelector('span').textContent = t('calib.' + key + '.text');
      const btn = li.querySelector('button');
      btn.textContent = t(key === 'spin' ? 'btn.start' : 'btn.capture');
      btn.addEventListener('click', () => (key === 'spin' ? runSpin(li, btn) : capturePose(li, btn, key)));
      ol.appendChild(li);
    });
  }

  function markCalib(li, key, ok) {
    li.classList.remove('running');
    li.classList.toggle('done', ok);
    if (ok) calibDone.add(key); else calibDone.delete(key);
    $('calibSave').disabled = calibDone.size !== CALIB_STEPS.length;
  }

  async function capturePose(li, btn, pose) {
    btn.disabled = true;
    li.classList.add('running');
    try {
      for (let left = 2; left > 0; left--) { btn.textContent = t('calib.hold', { n: left }); await sleep(700); }
      await postJson('/api/imu/pose', { pose });
      markCalib(li, pose, true);
      calibSay('');
    } catch (e) {
      markCalib(li, pose, false);
      calibSay(e.message, 'err');
    }
    btn.textContent = t(calibDone.has(pose) ? 'btn.again' : 'btn.capture');
    btn.disabled = false;
  }

  async function runSpin(li, btn) {
    try {
      if (!li.classList.contains('running')) {
        await postJson('/api/imu/spin', { action: 'start' });
        li.classList.add('running');
        btn.textContent = t('btn.stop');
        calibSay(t('calib.spinNow'));
        return;
      }
      const r = await postJson('/api/imu/spin', { action: 'stop' });
      markCalib(li, 'spin', true);
      calibSay(t('calib.spinDone', { axis: r.spin.col }), 'ok');
    } catch (e) {
      markCalib(li, 'spin', false);
      calibSay(e.message, 'err');
    }
    btn.textContent = t(calibDone.has('spin') ? 'btn.again' : 'btn.start');
  }

  $('calibSave').addEventListener('click', async () => {
    try {
      const r = await postJson('/api/imu/save');
      const warnings = r.warnings.map(w => t('warn.' + w.code, w.params || {}));
      calibSay(t('calib.saved') + (warnings.length ? ' – ' + warnings.join(' ') : ''), warnings.length ? '' : 'ok');
      $('calibSave').disabled = true;
      telemetry.calibrated = true;
    } catch (e) {
      calibSay(e.message, 'err');
    }
  });

  async function pollCalibRates() {
    try {
      const st = await api('/api/telemetry/status');
      const imu = st.types['2'];
      const fresh = imu && imu.age_s < 2;
      $('calibRates').textContent = fresh
        ? t('calib.rates', { rate: imu.rate_hz ?? '…', state: t(st.imu.still ? 'calib.still' : 'calib.moving') })
        : t('calib.noImu');
      $('calibRates').classList.toggle('bad', !fresh);
    } catch (e) {
      $('calibRates').textContent = t('calib.ratesDown');
      $('calibRates').classList.add('bad');
    }
  }

  function openCalib() {
    renderCalib();
    $('calib').hidden = false;
    pollCalibRates();
    calibTimer = setInterval(pollCalibRates, 1000);
  }
  $('calibBtn').addEventListener('click', openCalib);
  $('imuCalHint').addEventListener('click', openCalib);
  $('calibClose').addEventListener('click', () => {
    $('calib').hidden = true;
    clearInterval(calibTimer);
  });

  // ------------------------------------------------------------------ instruments
  // The server pushes decoded IMU values over the drive socket at 20 Hz; the
  // gauges ease toward them every frame so they move smoothly at 60 Hz.
  const telemetry = { data: null, at: 0, calibrated: false };
  const view = { hdg: 0, gLat: 0, gLong: 0, pitch: 0, roll: 0, peak: 0, trail: [] };
  const canvases = {};
  const compassLabel = deg => t('compass')[deg / 45];
  const colors = {};                            // theme tokens, read once from CSS

  function onTelemetry(d) {
    telemetry.data = d;
    telemetry.at = performance.now();
    telemetry.calibrated = d.calibrated;
  }

  function fitCanvas(id) {
    const c = $(id);
    const dpr = window.devicePixelRatio || 1;
    const w = Math.round(c.clientWidth * dpr), h = Math.round(c.clientHeight * dpr);
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    const ctx = canvases[id] || (canvases[id] = c.getContext('2d'));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, c.clientWidth, c.clientHeight);
    return [ctx, c.clientWidth, c.clientHeight];
  }

  function drawCompass(hdg) {
    const [ctx, w] = fitCanvas('compassCanvas');
    const ppd = w / 110;                        // pixels per degree, ~110 degrees visible
    const labelFont = `600 11px ${colors.fontUi}`, numFont = `10px ${colors.fontNum}`;
    ctx.textAlign = 'center';
    const first = Math.floor((hdg - 60) / 5) * 5;
    for (let d = first; d <= hdg + 60; d += 5) {
      const x = w / 2 + (d - hdg) * ppd;
      const deg = ((d % 360) + 360) % 360;
      const major = deg % 45 === 0, mid = deg % 15 === 0;
      ctx.strokeStyle = major ? colors.cyan : colors.line;
      ctx.lineWidth = major ? 2 : 1;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, major ? 14 : mid ? 10 : 6);
      ctx.stroke();
      if (major) {
        ctx.font = labelFont;
        ctx.fillStyle = deg === 0 ? colors.alert : colors.text;
        ctx.fillText(compassLabel(deg), x, 28);
      } else if (mid) {
        ctx.font = numFont;
        ctx.fillStyle = colors.muted;
        ctx.fillText(String(deg), x, 26);
      }
    }
    ctx.fillStyle = colors.cyan;
    ctx.beginPath();
    ctx.moveTo(w / 2 - 6, 0); ctx.lineTo(w / 2 + 6, 0); ctx.lineTo(w / 2, 8);
    ctx.fill();
  }

  function drawGMeter(gLat, gLong, trail) {
    const [ctx, w, h] = fitCanvas('gCanvas');
    const cx = w / 2, cy = h / 2, r = Math.min(w, h) / 2 - 4;
    const FULL = 1.0;                           // g at the outer ring
    ctx.strokeStyle = colors.line;
    ctx.lineWidth = 1;
    for (const f of [0.25, 0.5, 0.75, 1]) {
      ctx.beginPath(); ctx.arc(cx, cy, r * f, 0, Math.PI * 2); ctx.stroke();
    }
    ctx.beginPath(); ctx.moveTo(cx - r, cy); ctx.lineTo(cx + r, cy); ctx.moveTo(cx, cy - r); ctx.lineTo(cx, cy + r); ctx.stroke();
    ctx.fillStyle = colors.muted;
    ctx.font = `9px ${colors.fontNum}`;
    ctx.textAlign = 'left';
    ctx.fillText('0.5', cx + 3, cy - r * 0.5 - 2);
    ctx.fillText('1g', cx + 3, cy - r + 10);
    const pos = (gx, gy) => {
      const m = Math.hypot(gx, gy), k = m > FULL ? FULL / m : 1;
      return [cx + (gx * k / FULL) * r, cy - (gy * k / FULL) * r];
    };
    trail.forEach((p, i) => {
      const [x, y] = pos(p[0], p[1]);
      ctx.globalAlpha = (i / trail.length) * 0.35;
      ctx.fillStyle = colors.cyan;
      ctx.beginPath(); ctx.arc(x, y, 2.5, 0, Math.PI * 2); ctx.fill();
    });
    ctx.globalAlpha = 1;
    const [x, y] = pos(gLat, gLong);
    ctx.fillStyle = colors.cyan;
    ctx.globalAlpha = 0.25;
    ctx.beginPath(); ctx.arc(x, y, 11, 0, Math.PI * 2); ctx.fill();
    ctx.globalAlpha = 1;
    ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.fill();
  }

  function drawAttitude(pitch, roll, valid) {
    const [ctx, w, h] = fitCanvas('attCanvas');
    const cx = w / 2, cy = h / 2, r = Math.min(w, h) / 2 - 4;
    ctx.save();
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.clip();
    if (valid) {
      const ppd = r / 45;                       // +-45 degrees pitch fill the disc
      ctx.translate(cx, cy);
      ctx.rotate(-roll * Math.PI / 180);
      ctx.translate(0, pitch * ppd);
      ctx.globalAlpha = 0.16;
      ctx.fillStyle = colors.warn;
      ctx.fillRect(-2 * r, 0, 4 * r, 4 * r);
      ctx.globalAlpha = 1;
      ctx.strokeStyle = colors.cyan; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(-2 * r, 0); ctx.lineTo(2 * r, 0); ctx.stroke();
      ctx.lineWidth = 1; ctx.strokeStyle = colors.line;
      for (const p of [-30, -20, -10, 10, 20, 30]) {
        const len = p % 20 === 0 ? r * 0.5 : r * 0.3;
        ctx.beginPath(); ctx.moveTo(-len / 2, -p * ppd); ctx.lineTo(len / 2, -p * ppd); ctx.stroke();
      }
    }
    ctx.restore();
    ctx.strokeStyle = colors.line; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
    // fixed kart symbol
    ctx.strokeStyle = colors.text; ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(cx - r * 0.55, cy); ctx.lineTo(cx - r * 0.18, cy); ctx.lineTo(cx, cy + r * 0.12);
    ctx.lineTo(cx + r * 0.18, cy); ctx.lineTo(cx + r * 0.55, cy);
    ctx.stroke();
    if (!valid) {
      ctx.fillStyle = colors.muted;
      ctx.font = `10px ${colors.fontUi}`;
      ctx.textAlign = 'center';
      ctx.fillText(t('att.uncalibrated'), cx, cy - r * 0.35);
    }
  }

  const fmtSigned = (v, digits) => (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(digits);
  const angleDiff = (a, b) => ((a - b + 540) % 360) - 180;

  let instEnabled = store.get('mk.instruments', '1') === '1';
  let lastDrawKey = '';
  let lastDrawAt = 0;
  $('instToggle').checked = instEnabled;
  $('instToggle').addEventListener('change', e => {
    instEnabled = e.target.checked;
    store.set('mk.instruments', instEnabled ? '1' : '0');
    lastDrawKey = '';
  });

  function renderInstruments(dt) {
    const d = telemetry.data;
    $('instruments').hidden = !d || !instEnabled;
    if (!d || !instEnabled) return;
    const fresh = performance.now() - telemetry.at < 1500;
    if (!colors.cyan) {
      const css = getComputedStyle(document.documentElement);
      for (const [key, name] of [['cyan', '--cyan'], ['line', '--line'], ['text', '--text'], ['muted', '--muted'],
        ['alert', '--alert'], ['warn', '--warn'], ['fontUi', '--font-ui'], ['fontNum', '--font-num']]) {
        colors[key] = css.getPropertyValue(name).trim();
      }
    }
    const k = 1 - Math.exp(-dt / 0.06);
    view.hdg = (view.hdg + angleDiff(d.heading, view.hdg) * k + 360) % 360;
    view.gLat += (d.g_lat - view.gLat) * k;
    view.gLong += (d.g_long - view.gLong) * k;
    if (d.pitch !== null) { view.pitch += (d.pitch - view.pitch) * k; view.roll += (d.roll - view.roll) * k; }
    view.peak = Math.max(Math.hypot(view.gLat, view.gLong), view.peak - dt * 0.15);   // peak hold, decays slowly
    view.trail.push([view.gLat, view.gLong]);
    if (view.trail.length > 40) view.trail.shift();

    const now = performance.now();
    const key = [view.hdg.toFixed(1), view.gLat.toFixed(3), view.gLong.toFixed(3), view.pitch.toFixed(1),
      view.roll.toFixed(1), d.pitch !== null, Math.round(d.yaw_rate), fresh].join();
    if (key === lastDrawKey || now - lastDrawAt < 33) return;
    lastDrawKey = key;
    lastDrawAt = now;

    drawCompass(view.hdg);
    drawGMeter(view.gLat, view.gLong, view.trail);
    drawAttitude(view.pitch, view.roll, d.pitch !== null);
    $('hdgVal').textContent = String(Math.round(view.hdg) % 360).padStart(3, '0') + '°';
    $('gLatVal').textContent = fmtSigned(view.gLat, 2);
    $('gLongVal').textContent = fmtSigned(view.gLong, 2);
    $('gPeakVal').textContent = view.peak.toFixed(2);
    $('pitchVal').textContent = d.pitch === null ? '–' : fmtSigned(view.pitch, 0) + '°';
    $('rollVal').textContent = d.roll === null ? '–' : fmtSigned(view.roll, 0) + '°';
    $('yawVal').textContent = Math.round(d.yaw_rate) + '°/s';
    $('imuCalHint').hidden = telemetry.calibrated;
    for (const el of [$('compass'), ...document.querySelectorAll('.inst')]) el.classList.toggle('stale', !fresh);
  }

  $('compass').addEventListener('click', async () => {
    try { await postJson('/api/imu/heading/reset'); view.hdg = 0; toast(t('toast.heading')); }
    catch (e) { toast(t('toast.headingFailed', { msg: e.message })); }
  });

  // ------------------------------------------------------------------ driving
  // One 60 Hz input loop merges gamepad, touch and keyboard into a single command
  // (throttle/steering -127..127, brake light). Commands go out at most at 30 Hz
  // (the drive loop rate) and are re-sent while input is active, which feeds the
  // server-side deadman switch.
  let driveSocket = null;
  let driveSocketOpen = false;

  function connectDrive() {
    driveSocket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    driveSocket.onopen = () => { driveSocketOpen = true; renderStatus(); };
    driveSocket.onclose = () => { driveSocketOpen = false; renderStatus(); setTimeout(connectDrive, 1000); };
    driveSocket.onmessage = e => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.telem) onTelemetry(msg.telem);
      } catch (err) { /* ignore malformed frames */ }
    };
  }

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const approach = (cur, target, rate, dt) => {
    const step = rate * dt;
    return Math.abs(target - cur) <= step ? target : cur + Math.sign(target - cur) * step;
  };
  // Small deadzone plus a gentle curve so small stick moves stay precise.
  const shape = (v, dead = 0.08, expo = 0.35) => {
    const a = Math.abs(v);
    if (a < dead) return 0;
    const x = (a - dead) / (1 - dead);
    return Math.sign(v) * (x * (1 - expo) + x * x * x * expo);
  };

  // --- keyboard (ramped instead of on/off)
  const kb = { up: false, down: false, left: false, right: false, boost: false, brake: false, t: 0, s: 0 };
  const KEYMAP = { ArrowUp: 'up', w: 'up', ArrowDown: 'down', s: 'down', ArrowLeft: 'left', a: 'left', ArrowRight: 'right', d: 'right' };

  function keyboardCommand(dt) {
    const tMax = kb.boost ? 127 : 90;
    const tTarget = (kb.up ? tMax : 0) - (kb.down ? 70 : 0);
    const sTarget = (kb.right ? 127 : 0) - (kb.left ? 127 : 0);
    kb.t = approach(kb.t, tTarget, tTarget === 0 ? 900 : 420, dt);  // units per second
    kb.s = approach(kb.s, sTarget, sTarget === 0 ? 1400 : 800, dt);
    const active = kb.up || kb.down || kb.left || kb.right || kb.brake || kb.t !== 0 || kb.s !== 0;
    return active ? { t: kb.t, s: kb.s, b: kb.brake } : null;
  }

  window.addEventListener('keydown', e => {
    if (!$('pairing').hidden || !$('calib').hidden) return;
    if (e.key === 'f' || e.key === 'F') {
      if (!e.repeat) (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen()).catch(() => {});
      return;
    }
    const k = KEYMAP[e.key] || KEYMAP[e.key.toLowerCase()];
    if (k) { kb[k] = true; e.preventDefault(); }
    else if (e.key === ' ') { kb.brake = true; e.preventDefault(); }
    else if (e.key === 'Shift') kb.boost = true;
  });
  window.addEventListener('keyup', e => {
    const k = KEYMAP[e.key] || KEYMAP[e.key.toLowerCase()];
    if (k) { kb[k] = false; e.preventDefault(); }
    else if (e.key === ' ') { kb.brake = false; e.preventDefault(); }
    else if (e.key === 'Shift') kb.boost = false;
  });

  // --- quitting the full-screen launcher window (Steam Deck has no close button)
  // A window opened by the launcher (--app) may close itself with window.close().
  function quitCockpit() {
    window.close();
    setTimeout(() => toast(t('toast.quitFailed')), 400);
  }
  $('quitBtn').addEventListener('click', quitCockpit);
  let quitComboSince = 0;

  // --- gamepad (standard mapping: left stick X, R2 gas, L2 reverse, R1 brake light)
  let padName = null;
  function gamepadCommand() {
    const pads = navigator.getGamepads ? [...navigator.getGamepads()].filter(Boolean) : [];
    const pad = pads.find(p => p.mapping === 'standard') || pads[0];
    if (!pad) { padName = null; return null; }
    padName = pad.id.replace(/\s*\(.*$/, '').slice(0, 28) || 'Gamepad';
    const btn = i => (pad.buttons[i] ? pad.buttons[i].value : 0);
    const s = shape(pad.axes[0] || 0) * 127;
    // Like the keyboard: R2 goes up to 90, A is boost (full 127, also without R2).
    const gas = btn(7), rev = btn(6), boost = btn(0) > 0.5;
    const fwd = boost ? 127 : (gas > 0.03 ? gas : 0) * 90;
    const t = fwd - (rev > 0.03 ? rev : 0) * 90;
    const b = btn(5) > 0.5;
    // Select + Start held for a second closes the cockpit.
    if (btn(8) > 0.5 && btn(9) > 0.5) {
      if (!quitComboSince) quitComboSince = performance.now();
      else if (performance.now() - quitComboSince > 1000) { quitComboSince = 0; quitCockpit(); }
    } else {
      quitComboSince = 0;
    }
    const active = s !== 0 || t !== 0 || b;
    return active ? { t, s, b } : { idle: true };
  }

  // --- touch (left half steers horizontally, right half throttles vertically)
  const touch = { steer: null, gas: null, s: 0, t: 0 };
  const TOUCH_RANGE = 90; // px of drag for full deflection
  function onTouchDown(e) {
    if (e.pointerType !== 'touch' || !$('pairing').hidden || !$('calib').hidden
        || e.target.closest('button, .drawer, .notice-card, .calib-box, .inst')) return;
    const side = e.clientX < innerWidth / 2 ? 'steer' : 'gas';
    if (touch[side]) return;
    touch[side] = { id: e.pointerId, x: e.clientX, y: e.clientY };
    showStick(side, e.clientX, e.clientY, 0, 0);
    e.preventDefault();
  }
  function onTouchMove(e) {
    for (const side of ['steer', 'gas']) {
      const p = touch[side];
      if (!p || p.id !== e.pointerId) continue;
      const dx = clamp((e.clientX - p.x) / TOUCH_RANGE, -1, 1);
      const dy = clamp((p.y - e.clientY) / TOUCH_RANGE, -1, 1);
      if (side === 'steer') touch.s = shape(dx, 0.05, 0.2) * 127;
      else touch.t = dy > 0 ? shape(dy, 0.05, 0.2) * 127 : shape(dy, 0.05, 0.2) * 90;
      showStick(side, p.x, p.y, side === 'steer' ? dx : 0, side === 'gas' ? -dy : 0);
    }
  }
  function onTouchUp(e) {
    for (const side of ['steer', 'gas']) {
      if (touch[side] && touch[side].id === e.pointerId) {
        touch[side] = null;
        if (side === 'steer') touch.s = 0; else touch.t = 0;
        hideStick(side);
      }
    }
  }
  function touchCommand() {
    return touch.steer || touch.gas ? { t: touch.t, s: touch.s, b: false } : null;
  }
  function showStick(side, x, y, dx, dy) {
    const el = $(side === 'steer' ? 'stickSteer' : 'stickGas');
    el.hidden = false;
    el.style.left = x + 'px';
    el.style.top = y + 'px';
    el.firstElementChild.style.transform = `translate(${dx * TOUCH_RANGE * 0.6}px, ${dy * TOUCH_RANGE * 0.6}px)`;
  }
  function hideStick(side) { $(side === 'steer' ? 'stickSteer' : 'stickGas').hidden = true; }
  $('stage').addEventListener('pointerdown', onTouchDown);
  window.addEventListener('pointermove', onTouchMove);
  window.addEventListener('mousemove', () => { if (padName) source = 'mouse'; });
  window.addEventListener('pointerup', onTouchUp);
  window.addEventListener('pointercancel', onTouchUp);

  // --- merge, render, send
  let lastSent = { t: 0, s: 0, b: false };
  let lastSentAt = 0;
  let lastFrame = performance.now();
  let source = 'keyboard';   // 'keyboard' | 'touch' | 'mouse' | gamepad name

  function releaseAll() {
    Object.assign(kb, { up: false, down: false, left: false, right: false, boost: false, brake: false, t: 0, s: 0 });
    for (const side of ['steer', 'gas']) { touch[side] = null; hideStick(side); }
    touch.s = touch.t = 0;
  }
  // keyup never arrives once the window loses focus.
  window.addEventListener('blur', releaseAll);
  document.addEventListener('visibilitychange', () => { if (document.hidden) releaseAll(); });

  function renderAxis(fill, out, value) {
    const pct = Math.min(1, Math.abs(value) / 127) * 50;
    fill.style.width = pct + '%';
    fill.style.left = value < 0 ? (50 - pct) + '%' : '50%';
    out.textContent = Math.round((value / 127) * 100);
  }

  function tick(now) {
    const dt = Math.min(0.1, (now - lastFrame) / 1000);
    lastFrame = now;
    const pad = gamepadCommand();
    const tc = touchCommand();
    const kc = keyboardCommand(dt);
    let cmd = { t: 0, s: 0, b: false };
    if (pad && !pad.idle) { cmd = pad; source = padName; }
    else if (tc) { cmd = tc; source = 'touch'; }
    else if (kc) { cmd = kc; source = 'keyboard'; }
    else if (pad) { source = padName; }
    cmd = { t: Math.round(clamp(cmd.t, -127, 127)), s: Math.round(clamp(cmd.s, -127, 127)), b: !!cmd.b };

    const changed = cmd.t !== lastSent.t || cmd.s !== lastSent.s || cmd.b !== lastSent.b;
    const active = cmd.t !== 0 || cmd.s !== 0 || cmd.b;
    if ((changed && now - lastSentAt >= 33) || (active && now - lastSentAt >= 200)) {
      if (driveSocketOpen) driveSocket.send(JSON.stringify(cmd));
      lastSent = cmd;
      lastSentAt = now;
    }
    renderAxis($('steerFill'), $('steerVal'), cmd.s);
    renderAxis($('throttleFill'), $('throttleVal'), cmd.t);
    $('throttleFill').classList.toggle('reverse', cmd.t < 0);
    $('brakeLamp').classList.toggle('on', cmd.b);
    $('inputSource').textContent = ['keyboard', 'touch', 'mouse'].includes(source) ? t('input.' + source) : source;
    renderInstruments(dt);
    document.body.classList.toggle('pad-driving', !!padName && source === padName);
    requestAnimationFrame(tick);
  }

  // ------------------------------------------------------------------ language
  function renderLang() {
    document.querySelectorAll('.seg [data-lang]').forEach(b => b.classList.toggle('on', b.dataset.lang === window.MKI18N.lang));
  }
  document.querySelectorAll('.seg [data-lang]').forEach(b => b.addEventListener('click', () => {
    window.MKI18N.setLang(b.dataset.lang);
    renderLang();
    renderStatus();
    if (!$('calib').hidden) renderCalib();
    lastDrawKey = '';            // redraw canvases with the new labels
  }));

  // ------------------------------------------------------------------ boot
  window.MKI18N.apply();
  renderLang();
  connectDrive();
  loadPlayer();
  pollStatus();
  setInterval(pollStatus, 700);
  requestAnimationFrame(tick);
})();
