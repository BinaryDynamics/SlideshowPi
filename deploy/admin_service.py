#!/usr/bin/python3
"""Root-only network/reboot broker with a restricted local Unix socket."""
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import platform
import socket
import struct
import subprocess
import threading
import time
import uuid
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from slideshow.configuration import hotspot_network, dnsmasq_text, password as config_password, playback_settings

CONFIG = Path('/etc/pi-slideshow')
AP_UNITS = ['pi-slideshow-ap', 'pi-slideshow-dns', 'pi-slideshow-hotspot']
PROFILE = Path('/etc/NetworkManager/system-connections/pi-slideshow-home.nmconnection')


def system_stats(previous_cpu=None):
    cpu = [int(n) for n in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    total, idle = sum(cpu), cpu[3] + cpu[4]
    usage = None
    if previous_cpu and total > previous_cpu[0]:
        usage = round(100 * (1 - (idle - previous_cpu[1]) / (total - previous_cpu[0])), 1)
    memory = {line.split(':')[0]: int(line.split()[1]) * 1024
              for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line}
    storage = []
    for name, path in [('SD card', Path('/var/lib/pi-slideshow'))] + [
            (p.name, p) for p in Path('/media/slideshow').glob('*') if p.is_mount()]:
        try:
            disk = shutil.disk_usage(path)
            storage.append(dict(name=name, path=str(path), total=disk.total, used=disk.used, free=disk.free))
        except OSError:
            pass
    try:
        temperature = int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000
    except (OSError, ValueError):
        temperature = None
    stats = dict(cpu_percent=usage, load_average=list(os.getloadavg()), cpu_cores=os.cpu_count(),
                 memory_total=memory['MemTotal'], memory_available=memory.get('MemAvailable', memory.get('MemFree', 0)),
                 swap_total=memory.get('SwapTotal', 0), swap_free=memory.get('SwapFree', 0),
                 temperature_c=temperature, uptime_seconds=float(Path('/proc/uptime').read_text().split()[0]),
                 storage=storage, kernel=platform.release(), timestamp=time.time())
    return stats, (total, idle)


def run(*args, timeout=25):
    # Never use a shell or expose subprocess output (which might contain secrets).
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)


def atomic(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    os.fchmod(fd, mode)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)


def validate_ssid(value):
    if not isinstance(value, str) or not 1 <= len(value.encode('utf-8')) <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Wi-Fi name must be 1-32 UTF-8 bytes without control characters.')
    return value


def validate_password(value, psk=False):
    if psk and isinstance(value, str) and re.fullmatch('[0-9a-fA-F]{64}', value):
        return value
    if not isinstance(value, str) or not 8 <= len(value) <= 63 or any(not 32 <= ord(c) <= 126 for c in value):
        raise ValueError('Wi-Fi password must be 8-63 printable characters.')
    return value


def key_escape(value):
    return value.replace('\\', '\\\\').replace(' ', '\\s')


def profile_text(home, profile_uuid, interface='wlan0'):
    text = ('[connection]\nid=Pi Slideshow Home\nuuid=' + profile_uuid +
            '\ntype=wifi\ninterface-name=' + interface + '\nautoconnect=false\n\n[wifi]\n'
            'mode=infrastructure\nssid=' + key_escape(home['ssid']) +
            '\nhidden=' + str(home['hidden']).lower() + '\n')
    if home['security'] == 'wpa':
        text += '\n[wifi-security]\nkey-mgmt=wpa-psk\npsk=' + key_escape(home['password']) + '\n'
    return text + '\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=disabled\n'


def ap_values(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            result[key] = value
    if 'ssid2' in result:
        result['ssid'] = bytes.fromhex(result['ssid2']).decode('utf-8')
    return result


def replace_ap(source, ssid, password):
    # ssid2 safely supports spaces, non-ASCII and literal punctuation.
    lines = [line for line in source.splitlines()
             if not line.startswith(('ssid=', 'ssid2=', 'wpa_passphrase='))]
    return '\n'.join(lines) + '\nssid2=' + ssid.encode().hex() + '\nwpa_passphrase=' + password + '\n'


def network_interfaces(root=Path('/sys/class/net')):
    """Detect physical Ethernet, USB networking and Wi-Fi without assuming wlan0."""
    result = []
    for path in sorted(root.iterdir()):
        if path.name == 'lo' or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', path.name):
            continue
        wireless = (path / 'wireless').exists() or (path / 'phy80211').exists()
        # Exclude virtual tunnels/bridges; USB gadget networking is supported too.
        if not wireless and not (path / 'device').exists() and not path.name.startswith(('eth', 'en', 'usb')):
            continue
        try:
            carrier = (path / 'carrier').read_text().strip() == '1'
        except OSError:
            carrier = False
        result.append(dict(name=path.name, wireless=wireless, carrier=carrier))
    return result


def current_addresses():
    devices = json.loads(run('ip', '-j', '-4', 'address', 'show').stdout)
    devices.sort(key=lambda d: d['ifname'] != 'wlan0')
    return [dict(interface=d['ifname'], address=a['local']) for d in devices
            for a in d.get('addr_info', []) if a.get('scope') == 'global'
            and not ipaddress.ip_address(a['local']).is_loopback]


def configure_photo_access(config, enabled, password=''):
    if type(enabled) is not bool or not isinstance(password, str):
        raise ValueError('Choose valid photo access settings.')
    path = config / 'photo-auth.json'
    auth = json.loads(path.read_text()) if path.exists() else {}
    if password:
        config_password(password, admin=True)
        salt = secrets.token_hex(16)
        auth.update(salt=salt, digest=hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 180000).hex())
    if enabled and not auth.get('digest'):
        raise ValueError('Set a photo management password before enabling protection.')
    auth.update(enabled=enabled, version=secrets.token_hex(16))
    atomic(path, json.dumps(auth))


def initialize(config=CONFIG, setup=None):
    config = Path(config)
    config.mkdir(parents=True, exist_ok=True)
    # Validate before modifying credentials or network configuration.
    if setup and setup.get('photo_access_custom'):
        photo = setup['photo_access']
        configure_photo_access(config, photo['enabled'], photo['password'])
    elif not (config / 'photo-auth.json').exists():
        configure_photo_access(config, False)
    previous = json.loads((config / 'network.json').read_text()) if (config / 'network.json').exists() else {}
    ap_network = hotspot_network(setup.get('hotspot_network') if setup else previous.get('hotspot_network'))
    if setup and setup.get('admin_password') or not (config / 'admin-auth.json').exists():
        password = setup['admin_password'] if setup and setup.get('admin_password') else secrets.token_urlsafe(12)
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 180000).hex()
        atomic(config / 'admin-auth.json', json.dumps(dict(salt=salt, digest=digest, version=secrets.token_hex(16))))
        atomic(config / 'admin-password.txt', password + '\n')
    if not (config / 'network.json').exists():
        atomic(config / 'network.json', json.dumps({'mode': 'hotspot', 'home': None,
                                                   'profile_uuid': str(uuid.uuid4())}))
    network = json.loads((config / 'network.json').read_text())
    network['cec_enabled'] = setup['cec_enabled'] if setup and setup.get('cec_custom') else network.get('cec_enabled', True)
    network['hotspot_network'] = ap_network
    if setup:
        network.update(mode=setup.get('network_mode', 'hotspot'), home=setup.get('home'))
        ap_path = config / 'hostapd.conf'
        if ap_path.exists():
            text = replace_ap(ap_path.read_text(), setup['ssid'], setup['password'])
            text = re.sub(r'^country_code=.*$', 'country_code=' + setup['country'], text, flags=re.M)
            atomic(ap_path, text)
    atomic(config / 'network.json', json.dumps(network))
    atomic(config / 'dnsmasq.conf', dnsmasq_text(ap_network))
    if setup and setup.get('playback_custom'):
        data = Path('/var/lib/pi-slideshow') if config == CONFIG else config / 'data'
        target = data / 'settings.json'
        settings = json.loads(target.read_text()) if target.exists() else {}
        playback = playback_settings(setup.get('playback', {}))
        if 'folders' in settings and not setup.get('auto_folders_custom'):
            playback.pop('auto_folders', None)
        settings.update(playback)
        atomic(target, json.dumps(settings), mode=0o644)
        if config == CONFIG:
            import pwd
            owner = pwd.getpwnam('slideshow')
            os.chown(target, owner.pw_uid, owner.pw_gid)
    if (config / 'hostapd.conf').exists():
        ap = ap_values(config / 'hostapd.conf')
        atomic(config / 'credentials.txt', 'Wi-Fi: ' + ap.get('ssid', '') + '\nPassword: ' +
               ap.get('wpa_passphrase', '') + '\nConfiguration: http://' + ap_network['ip_address'] + '\n')


class Manager:
    def __init__(self, config=CONFIG, runner=run, addresses=current_addresses, interfaces=network_interfaces):
        self.config = Path(config)
        self.run = runner
        self.addresses = addresses
        self.interfaces = interfaces
        self.status_interfaces = []
        self.wifi_interface = None
        self.runtime_mode = None
        self.active_wifi = None
        self.network_retry_at = 0
        self.wired_retry = {}
        self.lock = threading.RLock()
        self.jobs = queue.Queue(maxsize=1)
        self.busy = False
        self.message = 'Ready.'
        self.status_addresses = []
        self.stats = {}
        self.display = None
        self.cec_status = 'Starting HDMI-CEC.'
        self.lost_since = None
        self.settings = json.loads((self.config / 'network.json').read_text())

    def save(self):
        atomic(self.config / 'network.json', json.dumps(self.settings))

    def status(self):
        with self.lock:
            ap = ap_values(self.config / 'hostapd.conf')
            home = self.settings.get('home')
            photo = json.loads((self.config / 'photo-auth.json').read_text())
            return {'cec_enabled': self.settings.get('cec_enabled', True), 'cec_status': self.cec_status,
                    'photo_access_enabled': photo['enabled'], 'photo_auth_version': photo['version'],
                    'available': True, 'mode': self.runtime_mode or self.settings['mode'],
                    'preferred_mode': self.settings['mode'], 'wifi_available': bool(self.wifi_interface),
                    'wifi_interface': self.wifi_interface, 'interfaces': list(self.status_interfaces),
                    'addresses': list(self.status_addresses), 'hostname': socket.gethostname(),
                    'hotspot_ssid': ap.get('ssid', ''), 'country': ap.get('country_code', 'GB'),
                    'auth_version': json.loads((self.config / 'admin-auth.json').read_text()).get('version'),
                    'hotspot_network': hotspot_network(self.settings.get('hotspot_network')),
                    'home_ssid': home['ssid'] if home else '',
                    'home_security': home['security'] if home else 'wpa',
                    'home_hidden': home['hidden'] if home else False,
                    'busy': self.busy, 'message': self.message}

    def authenticate(self, password, photo=False):
        if not isinstance(password, str) or len(password) > 128:
            return False
        auth = json.loads((self.config / ('photo-auth.json' if photo else 'admin-auth.json')).read_text())
        if photo and (not auth.get('enabled') or not auth.get('digest')):
            return False
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(auth['salt']), 180000).hex()
        return hmac.compare_digest(digest, auth['digest'])

    def request(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Expected an action object.')
        action = payload.get('action')
        if action == 'cec-save':
            if type(payload.get('enabled')) is not bool:
                raise ValueError('CEC enabled must be true or false.')
            with self.lock:
                previous = self.settings.get('cec_enabled', True)
                self.settings['cec_enabled'] = payload['enabled']
                try:
                    self.save()
                except Exception:
                    self.settings['cec_enabled'] = previous
                    raise
            return {'ok': True, 'message': 'TV remote settings saved.'}
        if action == 'status':
            return self.status()
        if action == 'photo-authenticate':
            return {'authenticated': self.authenticate(payload.get('password'), photo=True)}
        if action == 'authenticate':
            return {'authenticated': self.authenticate(payload.get('password'))}
        if action == 'diagnostics':
            with self.lock:
                return {'stats': dict(self.stats), 'display': dict(self.display) if self.display else None}
        if action == 'display-report':
            width, height = payload.get('width'), payload.get('height')
            if type(width) is not int or type(height) is not int or not 160 <= width <= 7680 or not 160 <= height <= 4320:
                raise ValueError('Invalid display resolution.')
            driver = payload.get('driver')
            if not isinstance(driver, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', driver):
                raise ValueError('Invalid display driver.')
            with self.lock:
                self.display = {'width': width, 'height': height, 'driver': driver, 'reported_at': time.time()}
                seconds = payload.get('frame_seconds')
                if type(seconds) in (float, int) and 0 <= seconds <= 3600:
                    self.display['frame_seconds'] = round(seconds, 3)
            return {'ok': True}
        if action not in ('reboot', 'hotspot', 'hotspot-save', 'connect', 'admin-password', 'device-save', 'photo-access-save'):
            raise ValueError('Unknown administration action.')
        values = {}
        with self.lock:
            if self.busy:
                raise ValueError('A device operation is already in progress. Please wait.')
            if action == 'photo-access-save':
                enabled = payload.get('enabled')
                if type(enabled) is not bool:
                    raise ValueError('Choose whether to require a photo management password.')
                password = payload.get('password', '')
                if not isinstance(password, str):
                    raise ValueError('Photo management password must be text.')
                if password:
                    config_password(password, admin=True)
                elif enabled and not json.loads((self.config / 'photo-auth.json').read_text()).get('digest'):
                    raise ValueError('Set a photo management password before enabling protection.')
                values = dict(enabled=enabled, password=password)
            if action == 'admin-password':
                if not self.authenticate(payload.get('current_password')):
                    raise ValueError('Current admin password is incorrect.')
                values['password'] = config_password(payload.get('password'), admin=True)
            if action == 'device-save':
                country = payload.get('country')
                if not isinstance(country, str) or not re.fullmatch('[A-Z]{2}', country):
                    raise ValueError('Choose a two-letter uppercase wireless country code.')
                values['country'] = country
            if action == 'hotspot-save':
                values['ssid'] = validate_ssid(payload.get('ssid'))
                ap = ap_values(self.config / 'hostapd.conf')
                values['password'] = validate_password(payload.get('password') or ap.get('wpa_passphrase'))
                values['hotspot_network'] = hotspot_network(payload.get('hotspot_network', self.settings.get('hotspot_network')))
            if action == 'connect':
                ssid = validate_ssid(payload.get('ssid'))
                security = payload.get('security', 'wpa')
                if security not in ('wpa', 'open') or not isinstance(payload.get('hidden', False), bool):
                    raise ValueError('Choose a personal WPA/WPA2 or open Wi-Fi network.')
                password = payload.get('password', '')
                old = self.settings.get('home')
                if not password and old and old['ssid'] == ssid and old['security'] == security:
                    password = old['password']
                values = {'ssid': ssid, 'security': security, 'hidden': payload.get('hidden', False),
                          'password': validate_password(password, psk=True) if security == 'wpa' else ''}
            self.busy = True
            self.message = 'Applying changes. The connection may close.'
            self.jobs.put_nowait((action, values))
        return {'ok': True, 'message': 'Request accepted. Changes start in 3 seconds.'}

    def wifi(self):
        interfaces = self.interfaces()
        names = [item['name'] for item in interfaces if item['wireless']]
        preferred = self.settings.get('wifi_interface', 'wlan0')
        self.wifi_interface = preferred if preferred in names else (names[0] if names else None)
        if not self.wifi_interface:
            raise ValueError('No Wi-Fi adapter is available. Settings can be saved and will apply when one is connected.')
        return self.wifi_interface

    def ap_dns(self, values=None):
        return dnsmasq_text(values or self.settings.get('hotspot_network')).replace(
            'interface=wlan0\n', 'interface=' + (self.wifi_interface or 'wlan0') + '\n')

    def queue_network(self, action, values):
        self.busy = True
        self.jobs.put_nowait((action, values))

    def reconcile_network(self, addresses, now=None):
        """One hotplug tick. Slow network activation runs on the job worker."""
        now = time.monotonic() if now is None else now
        interfaces = self.interfaces()
        wifi_names = [item['name'] for item in interfaces if item['wireless']]
        preferred = self.settings.get('wifi_interface', 'wlan0')
        wifi = preferred if preferred in wifi_names else (wifi_names[0] if wifi_names else None)
        wired = [item for item in interfaces if not item['wireless']]
        wired_addresses = [a for a in addresses if any(i['name'] == a['interface'] for i in wired)]
        with self.lock:
            self.status_interfaces, self.wifi_interface = interfaces, wifi
            self.status_addresses = addresses
            if not wifi:
                self.runtime_mode = 'wired' if wired_addresses else 'no-wifi'
            if self.busy:
                return
            if not wifi:
                self.lost_since = None
                self.message = ('No Wi-Fi adapter. Web controls are available over the wired connection.'
                                if wired_addresses else 'No Wi-Fi. Slideshow continues; waiting for a network adapter or cable.')
                if self.active_wifi:
                    self.active_wifi = None
                    self.queue_network('network-stop', {})
                    return
            elif self.active_wifi != wifi and now >= self.network_retry_at:
                self.message = 'Wi-Fi adapter detected. Enabling saved network settings.'
                self.queue_network('network-start', {})
                return
            elif self.active_wifi == wifi:
                self.runtime_mode = self.settings['mode']
                if self.settings['mode'] == 'client':
                    connected = any(a['interface'] == wifi for a in addresses)
                    self.lost_since = None if connected else self.lost_since if self.lost_since is not None else now
                    if self.lost_since is not None and now - self.lost_since > 90:
                        self.message = 'Home Wi-Fi disconnected. Restoring hotspot.'
                        self.queue_network('hotspot', {})
                        return
            for item in wired:
                interface = item['name']
                if item['carrier'] and not any(a['interface'] == interface for a in addresses) and now >= self.wired_retry.get(interface, 0):
                    self.wired_retry[interface] = now + 60
                    self.queue_network('wired-connect', {'interface': interface})
                    return

    def stop_ap(self):
        self.run('systemctl', 'stop', *AP_UNITS)

    def hotspot(self):
        interface = self.wifi()
        self.settings['wifi_interface'] = interface
        path = self.config / 'hostapd.conf'
        text = '\n'.join(line for line in path.read_text().splitlines() if not line.startswith('interface='))
        atomic(path, 'interface=' + interface + '\n' + text + '\n')
        self.save()
        if shutil.which('iw'):
            self.run('iw', 'reg', 'set', ap_values(self.config / 'hostapd.conf').get('country_code', 'GB'))
        atomic(self.config / 'dnsmasq.conf', self.ap_dns())
        self.run('nmcli', 'device', 'set', interface, 'autoconnect', 'no')
        self.run('nmcli', 'device', 'set', interface, 'managed', 'no')
        self.run('systemctl', 'restart', 'pi-slideshow-hotspot')
        self.run('systemctl', 'restart', 'pi-slideshow-ap', 'pi-slideshow-dns')
        self.settings['mode'] = 'hotspot'
        self.active_wifi, self.runtime_mode = interface, 'hotspot'
        self.network_retry_at = 0
        self.save()
        self.lost_since = None

    def connect(self):
        interface = self.wifi()
        if shutil.which('iw'):
            self.run('iw', 'reg', 'set', ap_values(self.config / 'hostapd.conf').get('country_code', 'GB'))
        home = self.settings.get('home')
        if not home:
            raise ValueError('No home Wi-Fi is saved.')
        atomic(PROFILE, profile_text(home, self.settings['profile_uuid'], interface))
        self.run('nmcli', 'connection', 'load', str(PROFILE))
        self.stop_ap()
        self.run('ip', 'address', 'flush', 'dev', interface)
        self.run('nmcli', 'device', 'set', interface, 'autoconnect', 'no')
        self.run('nmcli', 'device', 'set', interface, 'managed', 'yes')
        self.run('nmcli', '--wait', '45', 'connection', 'up', 'uuid',
                 self.settings['profile_uuid'], 'ifname', interface, timeout=55)
        if not any(a['interface'] == interface for a in self.addresses()):
            raise RuntimeError('No Wi-Fi IPv4 address assigned.')
        self.settings['wifi_interface'] = interface
        self.settings['mode'] = 'client'
        self.active_wifi, self.runtime_mode = interface, 'client'
        self.network_retry_at = 0
        self.save()
        self.lost_since = None

    def execute(self, action, values):
        if action == 'network-stop':
            self.stop_ap()
        elif action == 'wired-connect':
            interface = values['interface']
            if not any(i['name'] == interface and not i['wireless'] and i['carrier'] for i in self.interfaces()):
                return
            self.run('nmcli', 'device', 'set', interface, 'managed', 'yes')
            self.run('nmcli', '--wait', '15', 'device', 'connect', interface, timeout=20)
        elif action == 'network-start':
            self.connect() if self.settings['mode'] == 'client' else self.hotspot()
        elif action == 'reboot':
            self.run('systemctl', 'reboot')
        elif action == 'photo-access-save':
            configure_photo_access(self.config, values['enabled'], values['password'])
        elif action == 'admin-password':
            salt = secrets.token_hex(16)
            digest = hashlib.pbkdf2_hmac('sha256', values['password'].encode(), bytes.fromhex(salt), 180000).hex()
            atomic(self.config / 'admin-auth.json', json.dumps(dict(salt=salt, digest=digest, version=secrets.token_hex(16))))
            atomic(self.config / 'admin-password.txt', values['password'] + '\n')
        elif action == 'device-save':
            path = self.config / 'hostapd.conf'
            original = path.read_text()
            atomic(path, re.sub(r'^country_code=.*$', 'country_code=' + values['country'], original, flags=re.M))
            try:
                self.run('iw', 'reg', 'set', values['country'])
                if self.active_wifi and self.settings['mode'] == 'hotspot':
                    self.run('systemctl', 'restart', 'pi-slideshow-ap')
                    self.run('systemctl', 'is-active', '--quiet', 'pi-slideshow-ap')
            except Exception:
                atomic(path, original)
                self.run('iw', 'reg', 'set', ap_values(path).get('country_code', 'GB'))
                raise
        elif action == 'hotspot':
            self.settings['mode'] = 'hotspot'
            self.save()
            self.hotspot()
        elif action == 'connect':
            self.settings['home'] = values
            self.settings['mode'] = 'client'
            self.save()
            self.connect()
        elif action == 'hotspot-save':
            path = self.config / 'hostapd.conf'
            original = path.read_text()
            old_network = hotspot_network(self.settings.get('hotspot_network'))
            new_network = values['hotspot_network']
            atomic(path, replace_ap(original, values['ssid'], values['password']))
            try:
                self.settings['hotspot_network'] = new_network
                self.save()
                atomic(self.config / 'dnsmasq.conf', dnsmasq_text(new_network))
                if self.active_wifi and self.settings['mode'] == 'hotspot':
                    self.stop_ap()
                    self.hotspot()
                    self.run('systemctl', 'is-active', '--quiet', 'pi-slideshow-ap')
                    self.run('systemctl', 'is-active', '--quiet', 'pi-slideshow-dns')
            except Exception:
                atomic(path, original)
                self.settings['hotspot_network'] = old_network
                self.save()
                atomic(self.config / 'dnsmasq.conf', dnsmasq_text(old_network))
                raise
            atomic(self.config / 'credentials.txt', 'Wi-Fi: ' + values['ssid'] + '\nPassword: ' +
                   values['password'] + '\nConfiguration: http://' + new_network['ip_address'] + '\n')

    def perform(self, action, values):
        try:
            self.execute(action, values)
            self.message = 'Changes applied.'
        except Exception:
            self.message = 'Network operation unavailable. Slideshow continues.'
            if action in ('connect', 'hotspot', 'network-start', 'hotspot-save'):
                try:
                    self.hotspot()
                    self.message = 'Home Wi-Fi unavailable. Hotspot restored.'
                except Exception:
                    self.active_wifi = None
                    try: self.stop_ap()
                    except Exception: pass
                    self.network_retry_at = time.monotonic() + 60
                    self.runtime_mode = 'wired' if any(a['interface'] != self.wifi_interface for a in self.status_addresses) else 'no-wifi'
                    self.message = 'Wi-Fi unavailable. Slideshow continues; networking will retry automatically.'
        finally:
            with self.lock:
                self.busy = False

    def work(self):
        while True:
            action, values = self.jobs.get()
            time.sleep(3)  # Allow the HTTP response to reach the phone first.
            self.perform(action, values)
            self.jobs.task_done()

    def monitor(self, start_network=True):
        if start_network:
            # Web and HDMI are independent of slow or missing network hardware.
            self.busy = False
        previous_cpu = None
        counter = 0
        services, throttling = [], None
        while True:
            try:
                addresses = self.addresses()
                self.reconcile_network(addresses)
                stats, previous_cpu = system_stats(previous_cpu)
                if counter % 3 == 0:
                    output = self.run('systemctl', 'show', '--property=Id,ActiveState,SubState',
                                      'pi-slideshow-web', 'pi-slideshow-display', 'pi-slideshow-admin',
                                      'pi-slideshow-usb', 'pi-slideshow-ap', 'pi-slideshow-dns', 'NetworkManager').stdout
                    services = [dict(line.split('=', 1) for line in group.splitlines() if '=' in line)
                                for group in output.strip().split('\n\n') if group]
                    if shutil.which('vcgencmd'):
                        try:
                            throttling = self.run('vcgencmd', 'get_throttled', timeout=5).stdout.strip().split('=')[-1]
                        except Exception:
                            throttling = None
                counter += 1
                stats.update(services=services, throttling=throttling)
                with self.lock:
                    self.status_addresses = addresses
                    self.stats = stats
            except Exception:
                print('Unable to refresh network addresses.', flush=True)
            time.sleep(5)


def main():
    import pwd
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    boot_config = Path('/boot/firmware/slideshowpi.conf')
    setup = None
    if boot_config.exists():
        from slideshow.configuration import load_config
        try:
            setup = load_config(boot_config)
        except Exception:
            # Never log TOML input: parser errors can include credentials.
            print('Invalid slideshowpi.conf. Existing configuration retained.', flush=True)
    initialize(setup=setup)
    if setup:
        boot_config.unlink()
    manager = Manager()
    manager.busy = True
    owner = pwd.getpwnam('slideshow')
    path = Path('/run/pi-slideshow-admin/control.sock')
    path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        os.chown(path, 0, owner.pw_gid)
        os.chmod(path, 0o660)
        server.listen(8)
        from slideshow.cec import run as cec_run
        threading.Thread(target=cec_run, args=(manager,), daemon=True, name='hdmi-cec').start()
        threading.Thread(target=manager.work, daemon=True).start()
        threading.Thread(target=manager.monitor, daemon=True).start()
        while True:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(8)
                uid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
                if uid not in (0, owner.pw_uid):
                    continue
                try:
                    data = b''
                    while b'\n' not in data:
                        chunk = connection.recv(4096)
                        if not chunk or len(data) + len(chunk) > 8192:
                            raise ValueError('Invalid administration request.')
                        data += chunk
                    result = manager.request(json.loads(data.split(b'\n', 1)[0]))
                except (ValueError, TypeError):
                    result = {'error': 'Invalid request. Check the Wi-Fi details, hotspot subnet and DHCP range, and wait for any pending operation.'}
                except Exception:
                    result = {'error': 'Administration request failed.'}
                try:
                    connection.sendall((json.dumps(result) + '\n').encode())
                except OSError:
                    pass


if __name__ == '__main__':
    import sys
    if '--initialize' in sys.argv:
        setup_path = CONFIG / 'setup.json'
        initialize(setup=json.loads(setup_path.read_text()) if setup_path.exists() else None)
    else:
        main()
