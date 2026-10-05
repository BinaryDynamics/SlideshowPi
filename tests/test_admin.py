import importlib.util
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest

from slideshow.web import create_app
from slideshow.configuration import load_config

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('admin_service', ROOT / 'deploy/admin_service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class FakeAdmin:
    def __init__(self):
        self.actions = []

    def status(self):
        return {'available': True, 'mode': 'client', 'hostname': 'slideshowpi',
                'addresses': [{'interface': 'wlan0', 'address': '192.168.1.40'}],
                'hotspot_ssid': 'SlideshowPi', 'home_ssid': 'Test Network', 'busy': False}

    def call(self, action, **payload):
        self.actions.append((action, payload))
        if action == 'authenticate':
            return {'authenticated': payload.get('password') == 'test-admin-password'}
        if action == 'diagnostics':
            return {'stats': {'memory_total': 512 * 1024 * 1024},
                    'display': {'width': 1920, 'height': 1080, 'driver': 'x11'}}
        return {'ok': True}


@pytest.fixture
def web(tmp_path):
    broker = FakeAdmin()
    app = create_app(tmp_path / 'data', tmp_path / 'usb', False, broker)
    app.config['TESTING'] = True
    client = app.test_client()
    token = re.search(rb'name="slideshow-token" content="([^"]+)"', client.get('/admin').data)[1].decode()
    return client, broker, {'X-Slideshow-Token': token}


def test_admin_requires_login_and_csrf(web):
    client, broker, headers = web
    assert client.get('/api/admin/status').status_code == 401
    assert client.post('/api/admin/action', headers=headers, json={'action': 'reboot'}).status_code == 401
    assert client.post('/api/admin/login', json={'password': 'test-admin-password'}).status_code == 403
    assert broker.actions == []
    assert client.post('/api/admin/login', headers=headers, json={'password': 'wrong'}).status_code == 401
    assert client.post('/api/admin/login', headers=headers, json={'password': 'test-admin-password'}).status_code == 200
    assert client.get('/api/admin/status').json['display']['width'] == 1920
    assert client.post('/api/admin/action', json={'action': 'reboot'}).status_code == 403
    assert client.post('/api/admin/action', headers=headers, json={'action': 'reboot'}).status_code == 202
    assert broker.actions[-1] == ('reboot', {})
    assert client.post('/api/admin/action', headers=headers, json={'action': 'shell', 'command': 'anything'}).status_code == 400
    assert client.post('/api/admin/logout', headers=headers, json={}).status_code == 200
    assert client.get('/api/admin/status').status_code == 401


def test_actual_lan_address_and_hostname_are_allowed(web):
    client, broker, headers = web
    for host in ['192.168.1.40', 'slideshowpi.local', 'slideshowpi']:
        assert client.get('/', headers={'Host': host}).status_code == 200
    result = client.get('/', headers={'Host': 'attacker.example'})
    assert result.status_code == 302
    assert result.location == 'http://192.168.1.40/'
    assert 'password' not in json.dumps(client.get('/api/state').json['network'])


def test_login_throttling(web):
    client, broker, headers = web
    for _ in range(5):
        assert client.post('/api/admin/login', headers=headers, json={'password': 'wrong'}).status_code == 401
    assert client.post('/api/admin/login', headers=headers, json={'password': 'wrong'}).status_code == 429


@pytest.fixture
def manager(tmp_path, monkeypatch):
    service.initialize(tmp_path)
    (tmp_path / 'hostapd.conf').write_text('ssid=Test Hotspot\ncountry_code=GB\nwpa_passphrase=test-hotspot-password\n')
    commands = []
    def runner(*args, **kwargs):
        commands.append(args)
        return SimpleNamespace(stdout='')
    monkeypatch.setattr(service, 'PROFILE', tmp_path / 'home.nmconnection')
    manager = service.Manager(tmp_path, runner, lambda: [{'interface': 'wlan0', 'address': '192.168.1.40'}])
    return manager, commands, tmp_path


def test_connect_profile_keeps_secrets_off_command_line(manager):
    m, commands, root = manager
    m.request({'action': 'connect', 'ssid': 'Family Network', 'password': 'private test password', 'hidden': True})
    action, values = m.jobs.get_nowait()
    m.perform(action, values)
    assert m.settings['mode'] == 'client'
    assert 'private test password' not in str(commands)
    profile = (root / 'home.nmconnection').read_text()
    assert 'ssid=Family\\sNetwork' in profile and 'psk=private\\stest\\spassword' in profile
    assert 'private test password' not in json.dumps(m.status())
    assert ('systemctl', 'stop', *service.AP_UNITS) in commands
    assert ('nmcli', 'device', 'set', 'wlan0', 'managed', 'yes') in commands


def test_failed_join_restores_hotspot(manager):
    m, commands, root = manager
    def failing(*args, **kwargs):
        commands.append(args)
        if args[:4] == ('nmcli', '--wait', '45', 'connection'):
            raise subprocess.CalledProcessError(10, args)
        return SimpleNamespace(stdout='')
    m.run = failing
    m.request({'action': 'connect', 'ssid': 'Missing Network', 'password': 'test-home-password'})
    m.perform(*m.jobs.get_nowait())
    assert m.settings['mode'] == 'hotspot'
    assert not m.busy
    assert ('systemctl', 'restart', 'pi-slideshow-ap', 'pi-slideshow-dns') in commands


def test_hotspot_change_preserves_blank_password_and_encodes_ssid(manager):
    m, commands, root = manager
    m.request({'action': 'hotspot-save', 'ssid': 'New Hotspot', 'password': ''})
    m.perform(*m.jobs.get_nowait())
    values = service.ap_values(root / 'hostapd.conf')
    assert values['ssid'] == 'New Hotspot'
    assert values['wpa_passphrase'] == 'test-hotspot-password'
    assert 'country_code=GB' in (root / 'hostapd.conf').read_text()


@pytest.mark.parametrize('payload', [
    {'action': 'shell'}, {'action': 'hotspot-save', 'ssid': 'bad\nname', 'password': 'test-password'},
    {'action': 'connect', 'ssid': 'Test', 'password': 'short'},
    {'action': 'connect', 'ssid': 'Different', 'password': ''},
    {'action': 'connect', 'ssid': 'Test', 'password': 'valid-password', 'hidden': 'yes'}])
def test_invalid_network_requests_make_no_changes(manager, payload):
    m, commands, root = manager
    before = (root / 'network.json').read_bytes()
    with pytest.raises(ValueError):
        m.request(payload)
    assert commands == [] and (root / 'network.json').read_bytes() == before


def test_text_config_and_admin_password_can_be_applied(manager):
    m, commands, root = manager
    config = root / 'slideshowpi.conf'
    config.write_text((ROOT / 'slideshowpi.conf.example').read_text().replace(
        'CHANGE_ME_HOTSPOT_PASSWORD', 'test-hotspot-password').replace('CHANGE_ME_ADMIN_PASSWORD', 'test-admin-password'))
    setup = load_config(config)
    service.initialize(root, setup)
    assert m.authenticate('test-admin-password')
    assert not m.authenticate('incorrect')
    assert 'test-admin-password' not in (root / 'admin-auth.json').read_text()


def test_example_config_requires_personal_passwords():
    with pytest.raises(ValueError):
        load_config(ROOT / 'slideshowpi.conf.example')


def test_imager_hex_psk_is_accepted():
    from slideshow.configuration import password
    assert password('ab' * 32, psk=True) == 'ab' * 32
    assert service.validate_password('ab' * 32, psk=True) == 'ab' * 32
    with pytest.raises(ValueError):
        password('z' * 64, psk=True)
