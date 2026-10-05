#!/bin/bash
set -euo pipefail
echo "Starting slideshow display client on DISPLAY=${DISPLAY:-unset}"
xset s off || echo 'Screen-saver extension unavailable; continuing.'
xset -dpms || echo 'DPMS extension unavailable; continuing.'
xset s noblank || echo 'Screen blanking option unavailable; continuing.'
exec /usr/bin/setpriv --reuid=slideshow --regid=slideshow --init-groups /usr/bin/python3 -u /opt/pi-slideshow/player.py
