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
ip address add 192.168.50.1/24 dev wlan0
ip link set wlan0 up
