"""Validate user-owned text configuration; no example credentials are accepted."""
from pathlib import Path
import re
import tomllib


def ssid(value):
    if not isinstance(value, str) or not 1 <= len(value.encode()) <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Wi-Fi name must be 1-32 UTF-8 bytes without control characters.')
    return value


def password(value, admin=False, psk=False):
    if psk and isinstance(value, str) and re.fullmatch('[0-9a-fA-F]{64}', value):
        return value
    minimum, maximum = (12, 128) if admin else (8, 63)
    if not isinstance(value, str) or not minimum <= len(value) <= maximum or any(not 32 <= ord(c) <= 126 for c in value) or value.startswith('CHANGE_ME'):
        raise ValueError(f'Choose a password of {minimum}-{maximum} printable characters; replace the example placeholder.')
    return value


def load_config(path):
    with Path(path).open('rb') as source:
        raw = tomllib.load(source)
    country = raw.get('device', {}).get('country')
    if not isinstance(country, str) or not re.fullmatch('[A-Z]{2}', country):
        raise ValueError('Set device.country to your uppercase two-letter wireless country code.')
    hotspot = raw.get('hotspot', {})
    mode = raw.get('network', {}).get('mode', 'hotspot')
    if mode not in ('hotspot', 'client'):
        raise ValueError('Set network.mode to hotspot or client.')
    saved = raw.get('home_wifi', {})
    home = None
    if saved.get('ssid'):
        security = saved.get('security', 'wpa')
        if security not in ('wpa', 'open') or not isinstance(saved.get('hidden', False), bool):
            raise ValueError('Home Wi-Fi must use personal WPA/WPA2 or open security.')
        home = dict(ssid=ssid(saved['ssid']), security=security, hidden=saved.get('hidden', False),
                    password=password(saved.get('password'), psk=True) if security == 'wpa' else '')
    if mode == 'client' and not home:
        raise ValueError('Client mode requires home_wifi credentials.')
    return dict(country=country, ssid=ssid(hotspot.get('ssid')), password=password(hotspot.get('password')),
                admin_password=password(raw.get('admin', {}).get('password'), admin=True),
                network_mode=mode, home=home)
