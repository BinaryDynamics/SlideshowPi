#!/bin/bash
set -euo pipefail
for attempt in {1..30}; do
    if ip link show wlan0 >/dev/null 2>&1; then break; fi
    sleep 1
done
rfkill unblock wifi
if command -v nmcli >/dev/null; then
    nmcli device set wlan0 managed no || true
fi
ip link set wlan0 down
ip address flush dev wlan0
/usr/bin/python3 - <<'PY'
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, '/opt/pi-slideshow')
from slideshow.configuration import hotspot_network
settings = json.loads(Path('/etc/pi-slideshow/network.json').read_text())
network = hotspot_network(settings.get('hotspot_network'))
subprocess.run(['ip', 'address', 'add', f'{network["ip_address"]}/{network["prefix_length"]}', 'dev', 'wlan0'], check=True)
PY
ip link set wlan0 up
