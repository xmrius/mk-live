# Shared helpers for install.sh and steamdeck.sh (sourced, not executed).
# shellcheck shell=bash

MK_PREFIX=/opt/mk-live
MK_DATA=/var/lib/mk-live
MK_IFACE=kartap0                 # stable name for the kart access-point adapter
MK_LINK_FILE=/etc/systemd/network/10-mk-live-kartap.link
MK_NM_FILE=/etc/NetworkManager/conf.d/99-mk-live-unmanaged.conf
OPENKART_CONF=/etc/openkart.conf

OPENKART_REPO=https://github.com/OpenKart-SDK/openkart.git
OPENKART_REF=463cfdad0af18466425790b2cf98acaa45139aa6
HOSTAPD_REPO=https://github.com/OpenKart-SDK/hostapd.git
HOSTAPD_REF=88c03b77e55ada883cbe9bced0a5522eb84c84d6

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ $EUID -eq 0 ]] || die "Please run with sudo: sudo $0 $*"
}

# ---------------------------------------------------------------- Wi-Fi adapter
is_ap_capable() {    # $1 = interface; true if unknown (no iw available)
  local phy
  command -v iw >/dev/null || return 0
  phy=$(cat "/sys/class/net/$1/phy80211/name" 2>/dev/null) || return 1
  iw phy "$phy" info \
    | awk '/Supported interface modes/{f=1; next} f && /^[[:space:]]+\* /{print; next} f{exit}' \
    | grep -Eq '\* AP[[:space:]]*$'
}

is_usb() { readlink -f "/sys/class/net/$1/device" 2>/dev/null | grep -q '/usb'; }

# Picks the adapter for the kart AP: $MK_WIFI_IFACE, an already renamed
# kartap0, or an AP-capable wireless adapter that does not carry the default
# route (so the machine keeps its internet/LAN link). USB adapters win.
pick_wifi_iface() {
  local dev default_dev best=""
  if [[ -n "${MK_WIFI_IFACE:-}" ]]; then
    [[ -d /sys/class/net/$MK_WIFI_IFACE/wireless ]] || die "MK_WIFI_IFACE=$MK_WIFI_IFACE is not a Wi-Fi adapter."
    echo "$MK_WIFI_IFACE"; return
  fi
  if [[ -d /sys/class/net/$MK_IFACE ]]; then echo "$MK_IFACE"; return; fi
  default_dev=$(ip route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") print $(i + 1)}' | head -n1)
  local usb_uplink=""
  for dev in /sys/class/net/*; do
    dev=${dev##*/}
    [[ -d /sys/class/net/$dev/wireless ]] || continue
    is_ap_capable "$dev" || continue
    if is_usb "$dev"; then
      [[ $dev == "$default_dev" ]] && { usb_uplink=$dev; continue; }
      echo "$dev"; return
    fi
    [[ $dev == "$default_dev" || -n $best ]] || best=$dev
  done
  # A USB stick always wins over built-in Wi-Fi, even if the network manager
  # has just put the internet connection on it (iwd on the Steam Deck does).
  if [[ -n $usb_uplink ]]; then
    warn "The internet connection currently runs over the stick $usb_uplink - it moves to the built-in Wi-Fi."
    echo "$usb_uplink"; return
  fi
  [[ -n $best ]] && { echo "$best"; return; }
  die "No free Wi-Fi adapter with AP mode found. Plug in a USB Wi-Fi stick (e.g. AR9271) or set MK_WIFI_IFACE=<name>."
}

# Gives the adapter the fixed name kartap0 (by MAC, survives reboots and
# re-plugging) and keeps NetworkManager/dhcpcd away from it.
setup_wifi_iface() {
  local dev=$1 mac
  # The phy's permanent address; a netdev created by iwd may carry another one.
  mac=$(cat "/sys/class/net/$dev/phy80211/macaddress" 2>/dev/null || cat "/sys/class/net/$dev/address")
  say "Wi-Fi adapter $dev ($mac) becomes $MK_IFACE"
  install -d /etc/systemd/network
  cat > "$MK_LINK_FILE" <<EOF
# mk-live: fixed name for the kart access-point adapter
[Match]
MACAddress=$mac

[Link]
Name=$MK_IFACE
EOF
  if [[ -d /etc/NetworkManager/conf.d ]]; then
    cat > "$MK_NM_FILE" <<EOF
[keyfile]
unmanaged-devices=interface-name:$MK_IFACE;interface-name:$dev
EOF
    systemctl reload NetworkManager 2>/dev/null || true
    command -v nmcli >/dev/null && nmcli device set "$dev" managed no 2>/dev/null || true
  fi
  if [[ -f /etc/dhcpcd.conf ]] && ! grep -q "denyinterfaces $MK_IFACE" /etc/dhcpcd.conf; then
    printf '\n# mk-live kart access point\ndenyinterfaces %s\n' "$MK_IFACE" >> /etc/dhcpcd.conf
  fi
  # firewalld (active on SteamOS) puts a new interface into the default zone,
  # which blocks DHCP (port 67) - the kart then never gets an address. The AP
  # network only carries the karts, so trust it.
  if command -v firewall-cmd >/dev/null && systemctl is-active -q firewalld; then
    firewall-cmd -q --permanent --zone=trusted --add-interface="$MK_IFACE" || true
    firewall-cmd -q --reload || true
  fi
  command -v rfkill >/dev/null && rfkill unblock wifi || true
  if [[ $dev != "$MK_IFACE" ]]; then
    ip link set dev "$dev" down
    ip link set dev "$dev" name "$MK_IFACE"
  fi
  ip link set dev "$MK_IFACE" up || true
}

# ---------------------------------------------------------------- iwd guard
MK_PHY=kartphy
MK_UDEV_RULE=/etc/udev/rules.d/70-mk-live-kartphy.rules
MK_IWD_DROPIN=/etc/systemd/system/iwd.service.d/mk-live.conf
MK_CLAIM=/etc/mk-live/claim-stick.sh

# iwd (SteamOS in game mode always switches back to it) grabs every Wi-Fi
# adapter and ignores NetworkManager's unmanaged list. It can skip whole phys
# by name (-P), so the stick's phy gets the fixed name kartphy via udev and iwd
# runs with -P kartphy. If iwd was faster than the rename (hot-plug), the
# claim helper restarts iwd once and recreates the kartap0 interface.
install_iwd_guard() {    # $1 = MAC of the stick
  local mac=$1 iw_bin iwd_bin
  systemctl cat iwd.service >/dev/null 2>&1 || return 0
  iw_bin=$(command -v iw)
  iwd_bin=$(systemctl show -p ExecStart --value iwd.service | sed -n 's/.*path=\([^ ;]*\).*/\1/p' | head -n1)
  [[ -n $iw_bin && -n $iwd_bin ]] || { warn "iw/iwd not found - skipping the iwd guard."; return 0; }
  say "Telling iwd to leave the stick alone (-P $MK_PHY)"
  install -d /etc/mk-live "$(dirname "$MK_IWD_DROPIN")"
  cat > "$MK_UDEV_RULE" <<EOF
# mk-live: stable phy name for the kart Wi-Fi stick, so iwd can ignore it
ACTION=="add", SUBSYSTEM=="ieee80211", ATTR{macaddress}=="$mac", RUN+="$iw_bin phy %k set name $MK_PHY", TAG+="systemd", ENV{SYSTEMD_WANTS}+="mk-live-claim.service"
EOF
  cat > "$MK_IWD_DROPIN" <<EOF
# mk-live: never touch the kart access-point stick
[Service]
ExecStart=
ExecStart=$iwd_bin -P $MK_PHY
EOF
  cat > "$MK_CLAIM" <<EOF
#!/bin/bash
# mk-live: make sure the kart stick is named $MK_PHY, free of iwd, and has an interface.
phy=""
for p in /sys/class/ieee80211/*; do
  [[ \$(cat "\$p/macaddress" 2>/dev/null) == "$mac" ]] && phy=\${p##*/}
done
[[ -n \$phy ]] || exit 0
[[ \$phy == $MK_PHY ]] || $iw_bin phy "\$phy" set name $MK_PHY
# iwd picked the phy up (before the rename, or it still runs without our -P):
# restart it so the drop-in's -P applies. Built-in Wi-Fi reconnects in seconds.
if systemctl is-active -q iwd \\
    && iwctl adapter list 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | grep -qw $MK_PHY; then
  systemctl restart iwd
  sleep 2
fi
# iwd removes the interfaces it created; give the stick one again.
if ! compgen -G "/sys/class/ieee80211/$MK_PHY/device/net/*" >/dev/null; then
  $iw_bin phy $MK_PHY interface add $MK_IFACE type managed
fi
EOF
  chmod 0755 "$MK_CLAIM"
  cat > /etc/systemd/system/mk-live-claim.service <<EOF
[Unit]
Description=mk-live: keep iwd away from the kart Wi-Fi stick

[Service]
Type=oneshot
ExecStart=$MK_CLAIM
EOF
  udevadm control --reload 2>/dev/null || true
  systemctl daemon-reload
  # Renames the phy first, then restarts iwd only if it is holding the stick.
  "$MK_CLAIM"
}

remove_iwd_guard() {
  rm -f "$MK_UDEV_RULE" "$MK_IWD_DROPIN" "$MK_CLAIM" /etc/systemd/system/mk-live-claim.service
  rmdir /etc/mk-live "$(dirname "$MK_IWD_DROPIN")" 2>/dev/null || true
  udevadm control --reload 2>/dev/null || true
  systemctl daemon-reload
  systemctl is-active -q iwd && systemctl restart iwd || true
}

# The native units, the container unit and older manual setups all claim the
# same adapter and ports (8181, 9000, 9001, 19001/2); only one may run.
stop_units() {
  local unit
  for unit in "$@"; do
    [[ -f /etc/systemd/system/$unit.service ]] || continue
    warn "Stopping and disabling $unit.service."
    systemctl disable --now "$unit.service" 2>/dev/null || true
  done
}

# Runs a noisy build step with its output in a log; shows the tail on failure.
run_logged() {    # $1 = log file, rest = command
  local log=$1; shift
  if ! "$@" >"$log" 2>&1; then
    tail -n 40 "$log" >&2
    die "Step failed, full log: $log"
  fi
}

# ---------------------------------------------------------------- OpenKart config
# Keeps an existing secret: a new one would un-pair every kart.
write_openkart_conf() {    # $1 = hostapd path, $2 = udhcpd path
  local secret
  secret=$(awk -F' *= *' '$1 == "secret" {print $2; exit}' "$OPENKART_CONF" 2>/dev/null || true)
  if [[ -z $secret ]]; then
    secret=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
    say "Created a new OpenKart secret - karts have to be paired once."
  else
    say "Kept the existing OpenKart secret - paired karts stay paired."
  fi
  [[ -f $OPENKART_CONF ]] && cp -p "$OPENKART_CONF" "$OPENKART_CONF.bak-$(date +%Y%m%d%H%M%S)"
  umask 027
  cat > "$OPENKART_CONF" <<EOF
# Written by mk-live install. The secret pairs the karts - keep it private.
[general]
secret = $secret
http = 127.0.0.1:8181
autostart = yes
loglevel = INFO

[wireless]
interface = $MK_IFACE
network = 169.254.98.64/26
channel = 6
hostapd_path = $1
udhcpd_path = $2
EOF
  umask 022
}

primary_ip() {
  ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' | head -n1
}
