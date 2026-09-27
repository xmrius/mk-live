# Installation

mk-live turns a Raspberry Pi or a Steam Deck into a base station for
Mario Kart Live: Home Circuit. With a USB Wi-Fi stick the device opens its own
network for the kart, receives the camera feed and serves the cockpit in a
browser. No Switch and no game are needed.

## What you need

- **Kart:** Mario Kart Live: Home Circuit (Mario or Luigi)
- **USB Wi-Fi stick with AP mode.** Tested with an **Atheros AR9271** stick
  (ath9k_htc), e.g. TP-Link TL-WN722N **v1** or ALFA AWUS036NHA. The built-in Wi-Fi
  stays free for internet and your home network.
- **One of these:**
  - Raspberry Pi 4 (4 GB) or Pi 5 with Raspberry Pi OS (64-bit), network via cable or Wi-Fi
  - Steam Deck with SteamOS 3.5 or newer, plus a USB-C adapter/hub for the stick
  - any other computer running Debian 13 or Ubuntu 24.04+
- Internet access once, during installation

## Raspberry Pi (and Debian/Ubuntu)

1. Flash Raspberry Pi OS (64-bit) with the Raspberry Pi Imager; in the Imager set a
   hostname (e.g. `mklive`), a user and the network, and enable SSH.
2. Plug in the Wi-Fi stick, log in via SSH and install:

   ```bash
   sudo apt install -y git
   git clone https://github.com/xmrius/mk-live.git
   cd mk-live
   sudo bash install/install.sh
   ```

   This takes 5–15 minutes depending on the Pi (hostapd is built with the OpenKart patch).
3. At the end the installer prints the cockpit address, e.g. `http://mklive.local:9000`.
   Open it in a browser (PC, tablet or phone on the same network).
4. The first time, click **Pair kart** and follow the wizard.

From now on the services start automatically whenever the stick is plugged in:

| Service | Job |
|---|---|
| `mk-live-openkart` | access point and connection to the kart (OpenKart SDK) |
| `mk-live-video` | receives and prepares the camera feed (port 9001) |
| `mk-live-web` | cockpit (port 9000) |

Logs: `journalctl -u mk-live-openkart -u mk-live-video -u mk-live-web -f`

**Performance:** the video is re-encoded for the browser. A Pi 5 handles that
easily, a Pi 4 gets close to its limit. If the picture stutters on a Pi 4, switch to
**MJPEG** under ⚙ in the cockpit.

## Steam Deck

Set up in desktop mode, drive in **game mode**.

1. Switch to **desktop mode** (Steam button → Power → Switch to Desktop).
2. If you have never done it, set a password in **Konsole**: `passwd`
3. Install **Google Chrome** from the **Discover** store (Chromium, Edge or
   Firefox work too; Chrome is tested).
4. Plug in the Wi-Fi stick – ideally via a short USB extension cable, so it does not
   block other ports and has a clearer line to the kart. Then in Konsole:

   ```bash
   git clone https://github.com/xmrius/mk-live.git
   cd mk-live
   ./install/steamdeck.sh
   ```

   The first run builds a container (5–10 minutes, no output meanwhile). SteamOS
   itself stays untouched, so system updates do not break the installation.
   The built-in Wi-Fi drops for a moment while iwd restarts.
5. Switch to **game mode**: Library → **Non-Steam** → **MK Live Cockpit**.
   The first time, pair the kart (see below).

In game mode the Deck controls arrive as a gamepad:

| Control | Function |
|---|---|
| Left stick | Steer |
| R2 | Throttle (analog) |
| A | Boost (full power) |
| L2 | Reverse |
| R1 | Brake light |
| Select + Start (hold 1 s) | Quit the cockpit |

In **desktop mode** Steam does not recognise the browser window as a game; the
controls then act as keyboard/mouse (left stick = arrow keys). Use game mode for
driving.

The services start by themselves as soon as the stick is plugged in: Deck on, kart on,
start the cockpit. Logs: `journalctl -u mk-live -f`

**What the installer changes on the system:** fixed names for the stick (`kartap0`,
phy `kartphy`), iwd ignores that phy (SteamOS always switches back to iwd in game
mode), the firewall trusts the kart network, and a systemd service `mk-live`.
Image and data live in `/home/.mk-live` (the Deck's `/var` partition is too small).

## Language

The cockpit is in English by default. Switch to German under ⚙ → **Language**;
the choice is remembered per browser.

## Opening the cockpit from another device

Browsers only allow the low-latency video (WebCodecs, ~60 ms) on `localhost` or
HTTPS. Via the network address, e.g. `http://192.168.1.50:9000`, the cockpit
falls back to MJPEG: that works, but with noticeably more delay.

For full speed from a PC, open an SSH tunnel and then go to
`http://localhost:9000`:

```bash
ssh -N -L 9000:localhost:9000 -L 9001:localhost:9001 deck@<device-ip>
```

On the Steam Deck itself (launcher "MK Live Cockpit") and on a Pi with a monitor
attached, the cockpit runs on `localhost` and therefore always uses WebCodecs.

## Pairing a kart

A kart only remembers one base station. If it is new, or was connected to a
Switch in between, pair it once:

1. In the cockpit: **Pair kart** → **Show QR code**.
2. Switch the kart on and unplug the USB cable. After about 30 seconds it looks for a code.
3. Hold the kart's camera 10–20 cm in front of the QR code. The cockpit switches to
   driving by itself as soon as the kart is connected.

Pairing depends on the secret in `/etc/openkart.conf`. Re-installing keeps it, so
paired karts stay paired.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No free Wi-Fi adapter with AP mode found" | Plug in the stick. With several adapters: `sudo MK_WIFI_IFACE=wlan1 bash install/install.sh` |
| Cockpit shows "OpenKart not reachable" | Unplug the stick and plug it back in; the service restarts with the stick |
| Kart does not connect | Unplug the kart's USB cable; if it was connected to a Switch, pair it again |
| Picture stutters | Switch the video mode under ⚙ in the cockpit; move the kart closer to the stick |
| Picture lags noticeably | Open the cockpit via `localhost` (on the Deck/Pi itself) or through an SSH tunnel; otherwise MJPEG runs instead of WebCodecs |
| Steam Deck: stick steers like arrow keys | Start the cockpit from the library in game mode |
| Steam Deck: "localhost refused to connect" | `journalctl -u mk-live -n 50`; run the installer once more |

## Uninstall

```bash
sudo bash install/uninstall.sh           # keeps pairing and data
sudo bash install/uninstall.sh --purge   # removes everything
```
