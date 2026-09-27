#!/usr/bin/env bash
# mk-live installer for Debian-based systems: Raspberry Pi OS, Debian, Ubuntu.
#
#   sudo ./install/install.sh             full install with systemd services
#   ./install/install.sh --container-base   image build: system packages, hostapd, OpenKart
#   ./install/install.sh --container-app    image build: cockpit files (fast layer)
#
# Environment: MK_WIFI_IFACE=<name> forces the access-point adapter.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=install/common.sh
source "$REPO/install/common.sh"

MODE=${1:-native}
PY=/usr/bin/python3
SVC_USER=mklive

install_packages() {
  say "Installing packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
    git ca-certificates build-essential patch pkg-config libnl-3-dev libnl-genl-3-dev \
    iw iproute2 rfkill udhcpd ffmpeg \
    python3 python3-venv python3-aiohttp python3-av python3-qrcode python3-pil python3-cryptography
}

# video_worker needs a recent PyAV (codec flags API, libx264). Older distro
# packages get a venv with PyAV from PyPI on top of the system packages.
ensure_pyav() {
  local check='import av; av.codec.context.Flags.low_delay; av.CodecContext.create("h264", "r")'
  if python3 -c "$check" 2>/dev/null; then
    PY=/usr/bin/python3
    return
  fi
  say "The distribution's PyAV is too old - installing a current PyAV into $MK_PREFIX/venv"
  python3 -m venv --system-site-packages "$MK_PREFIX/venv"
  "$MK_PREFIX/venv/bin/pip" install -q --upgrade 'av>=13'
  PY=$MK_PREFIX/venv/bin/python
  "$PY" -c "$check" || die "PyAV could not be installed."
}

build_hostapd() {
  local src=$MK_PREFIX/src/hostapd bin=/usr/local/sbin/openkart-hostapd
  local stamp=$MK_PREFIX/src/.hostapd-$HOSTAPD_REF
  if [[ -x $bin && -f $stamp ]]; then
    say "hostapd (OpenKart patch) is already built"
    return
  fi
  say "Building hostapd with the OpenKart patch (takes a few minutes on a Pi)"
  rm -rf "$src"
  git clone -q "$HOSTAPD_REPO" "$src"
  git -C "$src" checkout -q "$HOSTAPD_REF"
  # The submodule points to git://, which many networks block.
  git -C "$src" config submodule.hostap.url https://w1.fi/hostap.git
  if ! git -C "$src" submodule update -q --init; then
    git -C "$src" config submodule.hostap.url git://w1.fi/hostap.git
    git -C "$src" submodule update -q --init
  fi
  (cd "$src/hostap" && cat ../patches/*.patch | patch -stup1)
  cp "$src/config" "$src/hostap/hostapd/.config"
  # hostapd 2.9 predates GCC 10/14 defaults.
  echo 'CFLAGS += -fcommon -Wno-error=implicit-function-declaration -Wno-error=incompatible-pointer-types -Wno-error=int-conversion' \
    >> "$src/hostap/hostapd/.config"
  run_logged "$MK_PREFIX/src/hostapd-build.log" make -C "$src/hostap/hostapd" -j"$(nproc)" hostapd
  install -m 0755 "$src/hostap/hostapd/hostapd" "$bin"
  strip "$bin" 2>/dev/null || true
  rm -f "$MK_PREFIX"/src/.hostapd-*
  touch "$stamp"
}

install_openkart() {
  local src=$MK_PREFIX/src/openkart
  say "Installing the OpenKart SDK"
  if [[ ! -d $src/.git ]]; then
    rm -rf "$src"
    git clone -q "$OPENKART_REPO" "$src"
  fi
  git -C "$src" fetch -q origin 2>/dev/null || true
  git -C "$src" checkout -q "$OPENKART_REF"
  rm -rf "$MK_PREFIX/lib/openkartd"
  install -d "$MK_PREFIX/lib"
  cp -r "$src/openkartd" "$MK_PREFIX/lib/"
  # mk-live patches: video forwarding, telemetry mirror, hostapd timeout
  cp -r "$REPO/patches/openkartd/." "$MK_PREFIX/lib/openkartd/"
  find "$MK_PREFIX/lib" -name __pycache__ -prune -exec rm -rf {} +
}

install_app() {
  say "Installing the cockpit"
  rm -rf "$MK_PREFIX/app"
  install -d "$MK_PREFIX/app/static" "$MK_PREFIX/bin"
  install -m 0644 "$REPO"/src/mk_live/{drive_web.py,imu.py,video_worker.py,player.js} "$MK_PREFIX/app/"
  install -m 0644 "$REPO"/src/mk_live/static/* "$MK_PREFIX/app/static/"
  install -m 0755 "$REPO/install/run-all.sh" "$MK_PREFIX/bin/run-all"
  cat > "$MK_PREFIX/mk-live.env" <<EOF
# mk-live runtime settings (read by the systemd units and bin/run-all)
PYTHON=$PY
PYTHONPATH=$MK_PREFIX/lib
HOME=$MK_DATA
# OpenKart video control: LVNI keepalive, no PI-ACK
OPENKART_VIDEO_LVNI_KEEPALIVE=1
OPENKART_VIDEO_LVNI_MODE=meta_tail
OPENKART_VIDEO_LVNI_ON_FRAME=0
OPENKART_VIDEO_LVNI_FRAME_MIN_INTERVAL=0.03
OPENKART_VIDEO_PI_ACK=0
# video worker
KART_VIDEO_SPS=real_1280x800
KART_VIDEO_CONT_SKIP=1
KART_VIDEO_RESTART_ON_IDR=0
KART_VIDEO_FAKE_IDR=0
KART_VIDEO_FPS=0
KART_MJPEG_MAX_FPS=30
KART_VIDEO_BATCH_DECODE_INTERVAL=0
KART_VIDEO_STALE_RESTART_SEC=0
KART_VIDEO_QUALITY_GATE=0
KART_VIDEO_DUMP_DIR=$MK_DATA/video_dumps
# raw frame dumps are a debugging aid; they fill the disk over many restarts
KART_VIDEO_DUMP_MAX=0
KART_VIDEO_CAPTURE_DIR=$MK_DATA/captures
# cockpit
KART_IP=169.254.98.97
KART_VIDEO_WATCHDOG=1
KART_VIDEO_UDP_STALE_SEC=15
KART_VIDEO_JPEG_STALE_SEC=9999
KART_VIDEO_WORKER_RESET_COOLDOWN=9999
KART_VIDEO_OPENKART_RESYNC_COOLDOWN=90
KART_VIDEO_WATCHDOG_DOWN_SEC=3
KART_TELEMETRY_RECORD_DIR=$MK_DATA/telemetry
KART_IMU_CALIBRATION=$MK_DATA/imu_calibration.json
EOF
}

# Older manual setups used these unit names; they would
# fight over the adapter and the ports, so stop them before touching either.
stop_legacy_units() {
  stop_units openkart video-worker drive-web mk-live
  systemctl stop mk-live-openkart.service 2>/dev/null || true
}

write_units() {
  say "Setting up services"
  local dev_unit="sys-subsystem-net-devices-$MK_IFACE.device"
  cat > /etc/systemd/system/mk-live-openkart.service <<EOF
[Unit]
Description=mk-live: OpenKart access point for the karts
# Runs exactly while the kart Wi-Fi adapter is present: starts when it is
# plugged in (or passed through to a VM), stops when it disappears.
BindsTo=$dev_unit
After=$dev_unit network.target

[Service]
EnvironmentFile=$MK_PREFIX/mk-live.env
ExecStart=$PY -m openkartd $OPENKART_CONF
Restart=always
RestartSec=2

[Install]
WantedBy=$dev_unit
EOF
  local name desc script
  for name in video web; do
    if [[ $name == video ]]; then desc='kart video receiver'; script=video_worker.py
    else desc='cockpit web interface'; script=drive_web.py; fi
    cat > "/etc/systemd/system/mk-live-$name.service" <<EOF
[Unit]
Description=mk-live: $desc
After=network.target mk-live-openkart.service

[Service]
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$MK_DATA
EnvironmentFile=$MK_PREFIX/mk-live.env
ExecStart=$PY $MK_PREFIX/app/$script
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
  done
  systemctl daemon-reload
}

create_user() {
  if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$MK_DATA" --shell /usr/sbin/nologin "$SVC_USER"
  fi
  install -d -o "$SVC_USER" -g "$SVC_USER" "$MK_DATA" "$MK_DATA/telemetry" "$MK_DATA/video_dumps" "$MK_DATA/captures"
  # The cockpit reads the adapter name from the config; the secret stays unreadable for others.
  chgrp "$SVC_USER" "$OPENKART_CONF"
  chmod 0640 "$OPENKART_CONF"
}

main_native() {
  require_root "$@"
  [[ -f /etc/debian_version ]] || die "This script is for Debian, Ubuntu and Raspberry Pi OS. Steam Deck: install/steamdeck.sh"
  command -v systemctl >/dev/null || die "systemd is required."
  install_packages
  local dev
  dev=$(pick_wifi_iface)
  install -d "$MK_PREFIX/src"
  ensure_pyav
  build_hostapd
  install_openkart
  install_app
  stop_legacy_units
  setup_wifi_iface "$dev"
  install_iwd_guard "$(cat /sys/class/net/$MK_IFACE/phy80211/macaddress)"
  write_openkart_conf /usr/local/sbin/openkart-hostapd "$(command -v udhcpd || echo /usr/sbin/udhcpd)"
  create_user
  write_units
  systemctl enable -q mk-live-openkart.service mk-live-video.service mk-live-web.service
  systemctl restart mk-live-openkart.service mk-live-video.service mk-live-web.service
  local ip
  ip=$(primary_ip)
  echo
  say "Done. Open the cockpit in a browser:"
  echo "      http://$(hostname).local:9000${ip:+   or   http://$ip:9000}"
  echo "    The first time, click \"Pair kart\" in the cockpit."
  echo "    Logs: journalctl -u mk-live-openkart -u mk-live-video -u mk-live-web -f"
}

main_container_base() {
  install_packages
  install -d "$MK_PREFIX/src"
  ensure_pyav
  build_hostapd
  install_openkart
  install -d "$MK_DATA/telemetry" "$MK_DATA/video_dumps" "$MK_DATA/captures" /var/lib/misc  # udhcpd leases
  apt-get purge -y -qq build-essential libnl-3-dev libnl-genl-3-dev pkg-config >/dev/null
  apt-get autoremove -y -qq >/dev/null
  rm -rf /var/lib/apt/lists/* "$MK_PREFIX/src/hostapd"
}

main_container_app() {
  [[ -x $MK_PREFIX/venv/bin/python ]] && PY=$MK_PREFIX/venv/bin/python
  install_app
}

case $MODE in
  --container-base) main_container_base ;;
  --container-app) main_container_app ;;
  native) main_native "$@" ;;
  *) die "Unknown option: $MODE" ;;
esac
