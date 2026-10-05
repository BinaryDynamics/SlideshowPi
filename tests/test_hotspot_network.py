import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from slideshow.configuration import HOTSPOT_DEFAULTS, dnsmasq_text, hotspot_network, load_config
from test_admin import service, manager
from slideshow.web import create_app

CUSTOM = dict(ip_address='10.20.30.1', prefix_length=24, dhcp_start='10.20.30.10', dhcp_end='10.20.30.100')


@pytest.mark.parametrize('changes', [
    {'prefix_length': 31}, {'prefix_length': '24'}, {'ip_address': '8.8.8.8'},
    {'dhcp_start': '192.168.51.20'}, {'dhcp_end': '192.168.50.255'},
    {'dhcp_start': '192.168.50.0'}, {'ip_address': '192.168.50.20'},
    {'dhcp_start': '192.168.50.201'}, {'ip_address': '127.0.0.1'},
    {'ip_address': '192.168.50.1\ndhcp-option=3,evil'}, {'ip_address': 1}])
def test_invalid_ranges_are_rejected(changes):
    with pytest.raises(ValueError):
        hotspot_network(changes)


def test_custom_config_drives_dns_dhcp_and_survives_restart(manager):
    m, commands, root = manager
    example = Path(__file__).resolve().parents[1] / 'slideshowpi.conf.example'
    text = example.read_text().replace('password = \'\' # Generate automatically, or enter 8-63 printable characters.', 'password = "test-hotspot-password"').replace(
        'password = \'\' # Generate automatically, or enter 12-128 printable characters.', 'password = "test-admin-password"')
    for key, old in HOTSPOT_DEFAULTS.items():
        text = text.replace(f'{key} = ' + (f'"{old}"' if isinstance(old, str) else str(old)),
                            f'{key} = ' + (f'"{CUSTOM[key]}"' if isinstance(CUSTOM[key], str) else str(CUSTOM[key])))
    config = root / 'custom.conf'
    config.write_text(text)
    setup = load_config(config)
    assert setup['hotspot_network'] == CUSTOM
    service.initialize(root, setup)
    service.initialize(root)
    assert json.loads((root / 'network.json').read_text())['hotspot_network'] == CUSTOM
    dns = (root / 'dnsmasq.conf').read_text()
    assert 'dhcp-range=10.20.30.10,10.20.30.100,255.255.255.0,12h' in dns
    assert 'address=/#/10.20.30.1' in dns and 'dhcp-option=6,10.20.30.1' in dns
    assert 'http://10.20.30.1' in (root / 'credentials.txt').read_text()
    # Legacy config files can omit all four new options.
    for key in HOTSPOT_DEFAULTS:
        text = '\n'.join(line for line in text.splitlines() if not line.startswith(key + ' ='))
    config.write_text(text)
    assert load_config(config)['hotspot_network'] == HOTSPOT_DEFAULTS


def test_admin_change_applies_network_and_rolls_back_on_failure(manager):
    m, commands, root = manager
    m.request(dict(action='hotspot-save', ssid='Updated', password='', hotspot_network=CUSTOM))
    m.perform(*m.jobs.get_nowait())
    assert m.status()['hotspot_network'] == CUSTOM
    assert (root / 'dnsmasq.conf').read_text() == dnsmasq_text(CUSTOM)
    assert ('systemctl', 'restart', 'pi-slideshow-hotspot') in commands
    old_ap = (root / 'hostapd.conf').read_text()
    def fail_dns_once(*args, **kwargs):
        if args == ('systemctl', 'is-active', '--quiet', 'pi-slideshow-dns'):
            raise RuntimeError('DNS failed')
        return SimpleNamespace(stdout='')
    m.run = fail_dns_once
    m.request(dict(action='hotspot-save', ssid='Failed change', password='', hotspot_network=HOTSPOT_DEFAULTS))
    m.perform(*m.jobs.get_nowait())
    assert m.settings['hotspot_network'] == CUSTOM
    assert (root / 'hostapd.conf').read_text() == old_ap
    assert (root / 'dnsmasq.conf').read_text() == dnsmasq_text(CUSTOM)


def test_custom_captive_redirect_and_host_allowlist(tmp_path):
    class Admin:
        def status(self):
            return dict(mode='hotspot', hotspot_network=CUSTOM, addresses=[], hostname='slideshowpi')
    client = create_app(tmp_path / 'data', tmp_path / 'usb', False, Admin()).test_client()
    assert client.get('/', headers={'Host': CUSTOM['ip_address']}).status_code == 200
    assert client.get('/generate_204', headers={'Host': CUSTOM['ip_address']}).location == 'http://10.20.30.1/'
    assert client.get('/', headers={'Host': '192.168.50.1'}).location == 'http://10.20.30.1/'
