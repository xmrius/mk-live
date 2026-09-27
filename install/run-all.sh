#!/usr/bin/env bash
# Runs OpenKart, the video receiver and the cockpit in one container
# (Steam Deck). Each process is restarted if it exits.
set -uo pipefail

set -a
# shellcheck source=/dev/null
source /opt/mk-live/mk-live.env
set +a
mkdir -p "$KART_TELEMETRY_RECORD_DIR" "$KART_VIDEO_DUMP_DIR" "$KART_VIDEO_CAPTURE_DIR"

supervise() {
  while true; do
    "$@"
    echo "mk-live: '$*' ended with $?, restarting in 2 s" >&2
    sleep 2
  done
}

trap 'kill 0' TERM INT
supervise "$PYTHON" -m openkartd /etc/openkart.conf &
sleep 2
supervise "$PYTHON" /opt/mk-live/app/video_worker.py &
supervise "$PYTHON" /opt/mk-live/app/drive_web.py &
wait
