#!/usr/bin/env bash
# mk-live on the Steam Deck (SteamOS 3.5+ desktop mode, Konsole):
#
#   ./install/steamdeck.sh
#
# Everything runs in a podman container, so the read-only system image stays
# untouched and SteamOS updates do not break the install. The host only gets
# a systemd unit, a fixed name for the Wi-Fi stick and a launcher for Steam.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=install/common.sh
source "$REPO/install/common.sh"
IMAGE=localhost/mk-live:latest
# /var on the Deck is a ~230 MB partition, far too small for podman's default
# storage. Image, build scratch and data live on /home instead; the container
# still sees its data at $MK_DATA.
DECK_BASE=/home/.mk-live
DECK_DATA=$DECK_BASE/data
PODMAN=(podman --root "$DECK_BASE/containers" --runroot /run/mk-live-containers)

# Game id of the non-Steam shortcut whose executable is $1 (for steam://rungameid).
steam_shortcut_gameid() {
  python3 - "$1" <<'PY'
import glob, os, re, struct, sys
exe = sys.argv[1]
files = sorted(glob.glob(os.path.expanduser('~/.local/share/Steam/userdata/*/config/shortcuts.vdf')),
               key=os.path.getmtime, reverse=True)
for path in files:
    data = open(path, 'rb').read()
    for m in re.finditer(rb'\x02appid\x00(.{4})\x01AppName\x00[^\x00]*\x00\x01Exe\x00"?([^\x00"]*)"?\x00', data, re.S | re.I):
        if m.group(2).decode(errors='replace') == exe:
            print((struct.unpack('<I', m.group(1))[0] << 32) | 0x02000000)
            sys.exit(0)
sys.exit(1)
PY
}

# Stage 1 (the user): run the system part through sudo, then add the launcher
# with the user's own session so Steam picks it up.
if [[ ${1:-} != --system ]]; then
  [[ $EUID -ne 0 ]] || die "Please run without sudo: ./install/steamdeck.sh"
  echo "The setup needs sudo. If you never set a password for the user '$USER',"
  echo "press Ctrl+C, run 'passwd' once and start the script again."
  sudo MK_WIFI_IFACE="${MK_WIFI_IFACE:-}" MK_VIA_SSH="${SSH_CONNECTION:+1}" "$0" --system
  bin="$HOME/.local/bin/mk-live-cockpit"
  desktop="$HOME/.local/share/applications/mk-live-cockpit.desktop"
  install -D -m 0755 "$REPO/install/cockpit-launcher.sh" "$bin"
  install -D -m 0644 "$REPO/install/mk-live-cockpit.svg"     "$HOME/.local/share/icons/hicolor/scalable/apps/mk-live-cockpit.svg"
  install -d "$(dirname "$desktop")"
  cat > "$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=MK Live Cockpit
Comment=Drive Mario Kart Live karts
Exec=$bin
Icon=mk-live-cockpit
Categories=Game;
EOF
  # Chromium-based flatpaks only see controllers with read access to the udev
  # database (same step as Valve's Xbox Cloud Gaming guide for the Deck).
  for app in com.google.Chrome org.chromium.Chromium com.microsoft.Edge; do
    flatpak info "$app" >/dev/null 2>&1 && flatpak --user override --filesystem=/run/udev:ro "$app"
  done
  # Register once with Steam. Launching through Steam matters: only then does
  # Steam Input present the Deck controls as a gamepad; in desktop mode they
  # are otherwise keyboard/mouse (left stick = arrow keys).
  gid=$(steam_shortcut_gameid "$bin" || true)
  if [[ -z $gid ]] && command -v steamos-add-to-steam >/dev/null \
      && steamos-add-to-steam "$desktop" >/dev/null 2>&1; then
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      sleep 1
      gid=$(steam_shortcut_gameid "$bin" || true)
      [[ -n $gid ]] && break
    done
  fi
  if [[ -n $gid ]]; then
    say "\"MK Live Cockpit\" is in the Steam library."
    launch="steam steam://rungameid/$gid"
  else
    warn "Could not add the launcher to Steam automatically: in Steam use \"Add a Game -> Add a Non-Steam Game\" -> \"MK Live Cockpit\"."
    launch=$bin
  fi
  # Desktop icon; Plasma only launches it without a prompt when executable.
  desk_dir=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
  if [[ -d $desk_dir ]]; then
    sed "s|^Exec=.*|Exec=$launch|" "$desktop" > "$desk_dir/mk-live-cockpit.desktop"
    chmod 0755 "$desk_dir/mk-live-cockpit.desktop"
  fi
  echo
  say "Done."
  echo "    Game mode: Library -> Non-Steam -> \"MK Live Cockpit\" (controller layout: \"Gamepad\")."
  echo "    Browser:   http://localhost:9000"
  echo "    The first time, click \"Pair kart\" in the cockpit."
  echo "    Logs: journalctl -u mk-live -f"
  exit 0
fi

# Over SSH the Wi-Fi backend switch can drop the connection; the system part
# must not die with it.
trap '' HUP

grep -q '^ID=steamos' /etc/os-release 2>/dev/null || warn "This is not SteamOS - continuing anyway, but the script is tested on the Steam Deck."
command -v podman >/dev/null || die "podman is missing (preinstalled since SteamOS 3.5). Please update SteamOS."

# Fail before the long build if there is no adapter for the kart.
has_usb_wifi() {
  local phy
  for phy in /sys/class/ieee80211/*; do
    readlink -f "$phy/device" 2>/dev/null | grep -q '/usb' && return 0
  done
  [[ -n ${MK_WIFI_IFACE:-} || -d /sys/class/net/$MK_IFACE ]]
}
has_usb_wifi || die "No USB Wi-Fi stick found. Plug the stick (e.g. AR9271) in via a USB-C adapter and run the script again."
install -d "$DECK_BASE" "$DECK_BASE/tmp" "$DECK_DATA"

build_image() {
  local ctx
  ctx=$(mktemp -d)
  cp -r "$REPO/install" "$REPO/patches" "$REPO/src" "$ctx/"
  find "$ctx" -name __pycache__ -prune -exec rm -rf {} +
  say "Building the container image (first time 5-10 minutes, needs internet, prints nothing meanwhile)"
  TMPDIR=$DECK_BASE/tmp run_logged "$DECK_BASE/image-build.log" \
    "${PODMAN[@]}" build -t "$IMAGE" -f "$ctx/install/Containerfile" "$ctx"
  "${PODMAN[@]}" image prune -f >/dev/null 2>&1 || true   # drop superseded layers
  rm -rf "$ctx"
}

write_unit() {
  local dev_unit="sys-subsystem-net-devices-$MK_IFACE.device"
  cat > /etc/systemd/system/mk-live.service <<EOF
[Unit]
Description=mk-live: Mario Kart Live cockpit (container)
# Runs exactly while the kart Wi-Fi stick is plugged in.
BindsTo=$dev_unit
After=$dev_unit network.target

[Service]
ExecStartPre=-/usr/bin/${PODMAN[*]} rm -f mk-live
ExecStart=/usr/bin/${PODMAN[*]} run --rm --name mk-live --network host --privileged --cgroups=split \\
  --log-driver passthrough -v $OPENKART_CONF:$OPENKART_CONF:ro -v $DECK_DATA:$MK_DATA $IMAGE
ExecStop=/usr/bin/${PODMAN[*]} stop -t 5 mk-live
Restart=always
RestartSec=3

[Install]
WantedBy=$dev_unit
EOF
  systemctl daemon-reload
}

# SteamOS may not ship iw; the image has it.
host_iw() {
  if command -v iw >/dev/null; then
    iw "$@"
  else
    "${PODMAN[@]}" run --rm --privileged --network host --entrypoint iw "$IMAGE" "$@"
  fi
}

# iwd owns the interfaces it creates, so they vanish when it stops. Give a USB
# Wi-Fi adapter that is left without an interface a new one.
ensure_usb_netdev() {
  local phy
  for phy in /sys/class/ieee80211/*; do
    [[ -e $phy ]] || continue
    readlink -f "$phy/device" | grep -q '/usb' || continue
    compgen -G "$phy/device/net/*" >/dev/null && continue
    [[ -d /sys/class/net/$MK_IFACE ]] && return
    say "Creating an interface for ${phy##*/}"
    host_iw phy "${phy##*/}" interface add "$MK_IFACE" type managed
  done
}

main() {
  local dev
  build_image
  systemctl stop mk-live.service 2>/dev/null || true
  stop_units openkart video-worker drive-web mk-live-openkart mk-live-video mk-live-web
  if [[ ${MK_VIA_SSH:-} == 1 ]]; then
    say "From here on output goes to a log, because the SSH connection may drop while iwd restarts:"
    echo "    $DECK_BASE/install.log   (if disconnected: reconnect and run 'tail -f $DECK_BASE/install.log')"
    exec >>"$DECK_BASE/install.log" 2>&1
  fi
  ensure_usb_netdev
  dev=$(pick_wifi_iface)
  setup_wifi_iface "$dev"
  # SteamOS game mode always switches the Wi-Fi backend back to iwd.
  install_iwd_guard "$(cat /sys/class/net/$MK_IFACE/phy80211/macaddress)"
  # Paths as seen inside the container.
  write_openkart_conf /usr/local/sbin/openkart-hostapd /usr/sbin/udhcpd
  write_unit
  systemctl enable -q mk-live.service
  systemctl restart mk-live.service
}

main "$@"
