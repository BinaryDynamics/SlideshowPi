#!/bin/bash
set -euo pipefail
# Xorg needs to own a virtual console. Only the display supervisor is root.
# The Python player is dropped to the slideshow account; TCP X11 is disabled.
install -d -o slideshow -g slideshow -m 700 /run/pi-slideshow-display
export XAUTHORITY=/run/pi-slideshow-display/Xauthority
touch "$XAUTHORITY"
chmod 600 "$XAUTHORITY"
cookie=$(mcookie)
xauth -f "$XAUTHORITY" add :0 . "$cookie"
chown slideshow:slideshow "$XAUTHORITY"
exec /usr/bin/xinit /opt/pi-slideshow/deploy/display-client.sh -- /usr/bin/Xorg :0 vt7 -keeptty -nolisten tcp -auth "$XAUTHORITY" -s 0 -dpms
