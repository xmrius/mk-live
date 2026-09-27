#!/usr/bin/env bash
# Opens the cockpit full screen in the first installed browser (flatpak).
# Started from Steam (non-Steam shortcut, "Gamepad" layout) so the Deck
# controls arrive as a gamepad instead of keyboard/mouse.
#
# The cockpit gets its own browser profile: otherwise an already open browser
# takes over the window and this process exits at once, Steam thinks the
# "game" ended and switches the controls back to keyboard/mouse.
URL=${MK_COCKPIT_URL:-http://localhost:9000}

for app in com.google.Chrome org.chromium.Chromium com.microsoft.Edge; do
  if flatpak info "$app" >/dev/null 2>&1; then
    profile="$HOME/.var/app/$app/mk-live-cockpit"
    mkdir -p "$profile"
    # --app + full screen instead of --kiosk: F11 / Alt+F4 / "Cockpit beenden" still work.
    exec flatpak run "$app" --user-data-dir="$profile" --app="$URL" --start-fullscreen \
      --no-first-run --no-default-browser-check --disable-features=Translate
  fi
done
if flatpak info org.mozilla.firefox >/dev/null 2>&1; then
  profile="$HOME/.var/app/org.mozilla.firefox/mk-live-cockpit"
  mkdir -p "$profile"
  exec flatpak run org.mozilla.firefox --no-remote --profile "$profile" --kiosk "$URL"
fi
if command -v kdialog >/dev/null; then
  kdialog --title "MK Live" --error "No browser found. Please install Google Chrome from the Discover store."
fi
exec xdg-open "$URL"
