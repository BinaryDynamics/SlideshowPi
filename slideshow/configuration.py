"""Validate user-owned text configuration; no example credentials are accepted."""
from pathlib import Path
import re
import tomllib
import ipaddress
import secrets
import json

HOTSPOT_DEFAULTS = dict(ip_address='192.168.50.1', prefix_length=24,
                        dhcp_start='192.168.50.20', dhcp_end='192.168.50.200')


def hotspot_network(values=None):
    if values is not None and not isinstance(values, dict):
        raise ValueError('Hotspot network settings must be an object.')
    values = {**HOTSPOT_DEFAULTS, **(values or {})}
    if any(not isinstance(values[k], str) for k in ('ip_address', 'dhcp_start', 'dhcp_end')):
        raise ValueError('Enter IPv4 addresses as text.')
    prefix = values['prefix_length']
    if type(prefix) is not int or not 8 <= prefix <= 30:
        raise ValueError('Hotspot prefix length must be an integer from 8 to 30.')
    try:
        address, start, end = (ipaddress.IPv4Address(values[k]) for k in ('ip_address', 'dhcp_start', 'dhcp_end'))
        network = ipaddress.IPv4Network(f'{address}/{prefix}', strict=False)
    except (ValueError, TypeError):
        raise ValueError('Enter valid IPv4 addresses for the hotspot and DHCP range.') from None
    private = [ipaddress.IPv4Network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    if not any(network.subnet_of(n) for n in private):
        raise ValueError('Use a private hotspot subnet: 10.x.x.x, 172.16-31.x.x or 192.168.x.x.')
    if any(a not in network or a in (network.network_address, network.broadcast_address) for a in (address, start, end)):
        raise ValueError('Hotspot and DHCP addresses must be usable hosts in the same subnet.')
    if start > end or start <= address <= end:
        raise ValueError('DHCP start must not exceed its end, and the range must exclude the hotspot address.')
    return dict(ip_address=str(address), prefix_length=prefix, dhcp_start=str(start), dhcp_end=str(end))


def dnsmasq_text(values):
    values = hotspot_network(values)
    address = values['ip_address']
    mask = ipaddress.IPv4Network(f'{address}/{values["prefix_length"]}', strict=False).netmask
    return (f'interface=wlan0\nbind-dynamic\nlisten-address={address}\nno-resolv\nno-hosts\n'
            f'address=/#/{address}\nlocal=/#/\ndhcp-range={values["dhcp_start"]},{values["dhcp_end"]},{mask},12h\n'
            f'dhcp-option=3,{address}\ndhcp-option=6,{address}\ndhcp-authoritative\n'
            'dhcp-leasefile=/var/lib/misc/pi-slideshow.leases\n')


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


def load_config(path=None, default_country='GB'):
    raw = {}
    if path is not None:
        with Path(path).open('rb') as source:
            raw = tomllib.load(source)
    country = raw.get('device', {}).get('country') or default_country
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
    cec = raw.get('cec', {})
    if not isinstance(cec, dict) or type(cec.get('enabled', True)) is not bool:
        raise ValueError('cec.enabled must be true or false.')
    photo = raw.get('photo_access', {})
    enabled = photo.get('enabled', False)
    if type(enabled) is not bool:
        raise ValueError('photo_access.enabled must be true or false.')
    photo_password = photo.get('password', '')
    if not isinstance(photo_password, str):
        raise ValueError('photo_access.password must be text.')
    if enabled or photo_password:
        photo_password = password(photo_password or secrets.token_urlsafe(18), admin=True)
    return dict(cec_enabled=cec.get('enabled', True), cec_custom='cec' in raw, country=country, ssid=ssid(hotspot.get('ssid', 'SlideshowPi')), password=password(hotspot.get('password') or secrets.token_urlsafe(12)),
                admin_password=password(raw.get('admin', {}).get('password') or secrets.token_urlsafe(18), admin=True),
                network_mode=mode, home=home, photo_access=dict(enabled=enabled, password=photo_password),
                photo_access_custom='photo_access' in raw, playback=playback_settings(raw.get('slideshow', {})),
                playback_custom='slideshow' in raw, auto_folders_custom='auto_folders' in raw.get('slideshow', {}), hotspot_network=hotspot_network(
                    {k: hotspot[k] for k in HOTSPOT_DEFAULTS if k in hotspot}))


PLAYBACK_DEFAULTS = dict(auto_folders=True, seconds=10, shuffle=False, recursive=True, fit='contain')


def playback_settings(values):
    if not isinstance(values, dict):
        raise ValueError('Slideshow settings must be an object.')
    settings = {**PLAYBACK_DEFAULTS, **values}
    if type(settings['seconds']) not in (int, float) or not 1 <= settings['seconds'] <= 3600:
        raise ValueError('Slide duration must be from 1 to 3600 seconds.')
    if any(type(settings[k]) is not bool for k in ('shuffle', 'recursive', 'auto_folders')) or settings['fit'] not in ('contain', 'cover'):
        raise ValueError('Choose valid slideshow fit, shuffle and recursive settings.')
    return {k: settings[k] for k in PLAYBACK_DEFAULTS}


def config_text(setup):
    """Write resolved defaults, so generated passwords remain stable between boots."""
    sections = dict(cec=dict(enabled=setup.get('cec_enabled', True)), device=dict(country=setup['country']),
                    hotspot=dict(ssid=setup['ssid'], password=setup['password'], **setup['hotspot_network']),
                    admin=dict(password=setup['admin_password']), photo_access=setup.get('photo_access', dict(enabled=False, password='')), network=dict(mode=setup['network_mode']),
                    home_wifi=setup.get('home') or dict(ssid='', password='', security='wpa', hidden=False),
                    slideshow=setup.get('playback', PLAYBACK_DEFAULTS))
    return '# Private SlideshowPi settings. Keep this file outside Git.\n\n' + '\n\n'.join(
        '[' + section + ']\n' + '\n'.join(key + ' = ' + json.dumps(value, ensure_ascii=False) for key, value in values.items())
        for section, values in sections.items()) + '\n'
