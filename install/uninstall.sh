#!/usr/bin/env bash
# Removes mk-live (Raspberry Pi/Debian and Steam Deck installs).
#   sudo ./install/uninstall.sh          keeps /etc/openkart.conf (pairing) and /var/lib/mk-live
#   sudo ./install/uninstall.sh --purge  removes those too
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=install/common.sh
source "$REPO/install/common.sh"
require_root "$@"

for unit in mk-live mk-live-openkart mk-live-video mk-live-web; do
  systemctl disable --now "$unit.service" 2>/dev/null || true
  rm -f "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload
rm -f "$MK_LINK_FILE" "$MK_NM_FILE" /usr/local/sbin/openkart-hostapd
remove_iwd_guard
if command -v firewall-cmd >/dev/null && systemctl is-active -q firewalld; then
  firewall-cmd -q --permanent --zone=trusted --remove-interface="$MK_IFACE" 2>/dev/null && firewall-cmd -q --reload || true
fi
rm -rf "$MK_PREFIX"
systemctl reload NetworkManager 2>/dev/null || true
if [[ -f /etc/dhcpcd.conf ]]; then
  sed -i '/^# mk-live kart access point$/d; /^denyinterfaces kartap0$/d' /etc/dhcpcd.conf
fi
command -v podman >/dev/null && podman rmi -f localhost/mk-live:latest >/dev/null 2>&1 || true
# Steam Deck: image storage lives on /home (see steamdeck.sh); data only with --purge
rm -rf /home/.mk-live/containers /home/.mk-live/tmp /home/.mk-live/*.log
if [[ -n ${SUDO_USER:-} ]]; then
  home=$(getent passwd "$SUDO_USER" | cut -d: -f6)
  rm -f "$home/.local/bin/mk-live-cockpit" "$home/.local/share/applications/mk-live-cockpit.desktop"     "$home/.local/share/icons/hicolor/scalable/apps/mk-live-cockpit.svg" "$home"/Desktop/mk-live-cockpit.desktop
fi
if [[ ${1:-} == --purge ]]; then
  rm -rf "$MK_DATA" "$OPENKART_CONF" /home/.mk-live
  id mklive >/dev/null 2>&1 && userdel mklive
  say "Removed mk-live including pairing and data."
else
  say "Removed mk-live. Kept: $OPENKART_CONF (pairing) and $MK_DATA."
fi
say "The Wi-Fi stick keeps the name $MK_IFACE until it is unplugged once."
