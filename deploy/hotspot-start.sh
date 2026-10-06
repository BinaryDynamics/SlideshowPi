#!/bin/bash
set -euo pipefail
interface=$(/usr/bin/python3 - <<'PY'
import json, re
from pathlib import Path
settings = json.loads(Path('/etc/pi-slideshow/network.json').read_text())
interface = settings.get('wifi_interface', 'wlan0')
if not isinstance(interface, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', interface):
    raise SystemExit('Invalid Wi-Fi interface')
print(interface)
PY
)
for attempt in {1..5}; do
    if ip link show "$interface" >/dev/null 2>&1; then break; fi
    sleep 1
done
rfkill unblock wifi
if command -v nmcli >/dev/null; then
    nmcli device set "$interface" managed no || true
fi
ip link set "$interface" down
ip address flush dev "$interface"
/usr/bin/python3 - <<'PY'
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, '/opt/pi-slideshow')
from slideshow.configuration import hotspot_network
settings = json.loads(Path('/etc/pi-slideshow/network.json').read_text())
network = hotspot_network(settings.get('hotspot_network'))
subprocess.run(['ip', 'address', 'add', f'{network["ip_address"]}/{network["prefix_length"]}', 'dev', settings.get('wifi_interface', 'wlan0')], check=True)
PY
ip link set "$interface" up
