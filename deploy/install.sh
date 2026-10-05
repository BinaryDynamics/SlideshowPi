#!/bin/bash
# Run on a dedicated Raspberry Pi OS Lite 32-bit installation.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo 'Run with sudo bash deploy/install.sh'; exit 1; fi
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$source_dir"
if [[ ! -f /etc/rpi-issue ]]; then echo 'This installer requires Raspberry Pi OS.'; exit 1; fi
if [[ $(getconf LONG_BIT) != 32 ]]; then echo 'Use Raspberry Pi OS Lite (32-bit) for the original Zero W.'; exit 1; fi
country=${COUNTRY:-ZA}
ssid=${SSID:-SlideshowPi}
if [[ ! $country =~ ^[A-Z]{2}$ ]]; then echo 'COUNTRY must be a two-letter uppercase code.'; exit 1; fi
export SSID="$ssid"
python3 -c 'import os; from slideshow.configuration import ssid; ssid(os.environ["SSID"])'
echo 'Installing slideshow packages. This will dedicate wlan0 to the hotspot after reboot.'
apt-get update
apt-get -o DPkg::Lock::Timeout=300 install -y python3-flask python3-pil python3-waitress python3-pygame hostapd dnsmasq-base \
  xserver-xorg-core xserver-xorg-legacy xserver-xorg-video-fbdev xinit xauth x11-xserver-utils \
  rfkill exfatprogs ntfs-3g util-linux network-manager
if ! id slideshow >/dev/null 2>&1; then useradd --system --user-group --create-home --shell /usr/sbin/nologin slideshow; fi
usermod -aG video,render,input slideshow
install -d /opt/pi-slideshow /etc/pi-slideshow /media/slideshow
install -d -o slideshow -g slideshow /var/lib/pi-slideshow /var/lib/pi-slideshow/photos
if [[ $source_dir != /opt/pi-slideshow ]]; then
  cp -r "$source_dir/slideshow" "$source_dir/templates" "$source_dir/static" "$source_dir/deploy" /opt/pi-slideshow/
  cp "$source_dir/run.py" "$source_dir/player.py" /opt/pi-slideshow/
fi
find /opt/pi-slideshow -type d -name __pycache__ -prune -o -type f -name '*.sh' -exec chmod 755 {} +
if [[ ! -f /etc/pi-slideshow/hostapd.conf ]]; then
  password=${HOTSPOT_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_hex(8))')}
  export HOTSPOT_PASSWORD="$password"
  python3 -c 'import os; from slideshow.configuration import password; password(os.environ["HOTSPOT_PASSWORD"])'
  umask 077
  cat > /etc/pi-slideshow/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid2=$(python3 -c 'import os; print(os.environ["SSID"].encode().hex())')
country_code=$country
ieee80211d=1
hw_mode=g
channel=6
wmm_enabled=1
auth_algs=1
wpa=2
wpa_passphrase=$password
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF
  umask 022
fi
# The admin initializer generates DNS/DHCP settings from the saved hotspot subnet.
install -d /var/lib/misc
install -d /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/90-pi-slideshow.conf <<'EOF'
[device-pi-slideshow]
match-device=interface-name:wlan0
managed=0
EOF
cat > /etc/X11/Xwrapper.config <<'EOF'
allowed_users=rootonly
needs_root_rights=yes
EOF
install -d /etc/X11/xorg.conf.d
if [[ ! -f /etc/X11/xorg.conf.d/20-slideshowpi-rendering.conf && ! -f /etc/X11/xorg.conf.d/20-pi-slideshow-rendering.conf ]]; then
  install -m 644 /opt/pi-slideshow/deploy/20-slideshowpi-rendering.conf /etc/X11/xorg.conf.d/
fi
for unit in /opt/pi-slideshow/deploy/pi-slideshow-*.service; do install -m 644 "$unit" /etc/systemd/system/; done
systemctl daemon-reload
python3 /opt/pi-slideshow/deploy/admin_service.py --initialize
systemctl disable pi-slideshow-hotspot pi-slideshow-ap pi-slideshow-dns
systemctl enable pi-slideshow-admin pi-slideshow-web pi-slideshow-display pi-slideshow-usb
echo
if [[ ${SLIDESHOW_QUIET_CREDENTIALS:-0} != 1 ]]; then cat /etc/pi-slideshow/credentials.txt; fi
echo
echo 'Installation complete. Save the credentials, then run sudo reboot.'
echo 'After reboot, wlan0 will provide the hotspot instead of joining your home Wi-Fi.'
