import json
from pathlib import Path
from types import SimpleNamespace
import subprocess

from test_admin import manager, service
from test_photo_access import protected
from slideshow.network import display_network


def test_interface_inventory_excludes_loopback_and_virtual_tunnels(tmp_path):
    for interface in ['lo', 'wlan0', 'wlx123', 'eth0', 'tun0']:
        p = tmp_path / interface; p.mkdir()
        (p / 'carrier').write_text('1')
    (tmp_path / 'wlan0/wireless').mkdir()
    (tmp_path / 'wlx123/phy80211').mkdir()
    (tmp_path / 'eth0/device').mkdir()
    items = service.network_interfaces(tmp_path)
    assert [item['name'] for item in items] == ['eth0', 'wlan0', 'wlx123']
    assert next(i for i in items if i['name'] == 'wlx123')['wireless']


def test_no_adapter_preserves_preferences_and_keeps_admin_available(manager):
    m, commands, root = manager
    m.active_wifi = None
    m.interfaces = lambda: []
    before = (root / 'network.json').read_bytes()
    m.reconcile_network([], now=10)
    assert m.status()['mode'] == 'no-wifi' and m.status()['available']
    assert not m.status()['wifi_available'] and not m.busy
    assert m.jobs.empty() and commands == []
    assert (root / 'network.json').read_bytes() == before
    assert display_network(m.status()) == ('No Wi-Fi', 'Offline slideshow')


def test_wifi_hotplug_enables_dynamic_interface_and_unplug_stops_ap(manager):
    m, commands, root = manager
    m.active_wifi = None
    hardware = []
    m.interfaces = lambda: list(hardware)
    m.reconcile_network([], now=1)
    hardware.append(dict(name='wlx123', wireless=True, carrier=False))
    m.reconcile_network([], now=2)
    assert m.busy and m.jobs.qsize() == 1
    m.perform(*m.jobs.get_nowait())
    assert m.status()['mode'] == 'hotspot' and not m.busy
    assert ('nmcli', 'device', 'set', 'wlx123', 'managed', 'no') in commands
    assert 'interface=wlx123' in (root / 'hostapd.conf').read_text()
    assert 'interface=wlx123' in (root / 'dnsmasq.conf').read_text()
    m.reconcile_network([], now=3)
    assert m.jobs.empty()  # No repeated restart for an unchanged adapter.
    hardware.clear()
    m.reconcile_network([], now=4)
    assert m.status()['mode'] == 'no-wifi'
    m.perform(*m.jobs.get_nowait())
    assert ('systemctl', 'stop', *service.AP_UNITS) in commands
    assert m.settings['mode'] == 'hotspot'


def test_wired_hotplug_activates_dhcp_then_reports_accessible_ip(manager):
    m, commands, root = manager
    m.active_wifi = None
    m.interfaces = lambda: [dict(name='eth0', wireless=False, carrier=True)]
    m.reconcile_network([], now=10)
    action, values = m.jobs.get_nowait()
    assert action == 'wired-connect'
    m.perform(action, values)
    assert ('nmcli', '--wait', '15', 'device', 'connect', 'eth0') in commands
    m.reconcile_network([dict(interface='eth0', address='192.168.1.5')], now=11)
    assert m.status()['mode'] == 'wired' and m.jobs.empty()
    assert display_network(m.status()) == ('Wired network · No Wi-Fi', 'IP: 192.168.1.5')
    m.reconcile_network([], now=12)
    assert m.status()['mode'] == 'no-wifi' and m.jobs.empty()  # DHCP retry is throttled.
    m.reconcile_network([], now=71)
    assert m.jobs.get_nowait()[0] == 'wired-connect'


def test_cable_disconnected_does_not_attempt_dhcp(manager):
    m, commands, root = manager
    m.active_wifi = None
    m.interfaces = lambda: [dict(name='eth0', wireless=False, carrier=False)]
    m.reconcile_network([], now=1)
    assert m.jobs.empty() and m.status()['mode'] == 'no-wifi'


def test_missing_wifi_network_failure_never_terminates_broker(manager):
    m, commands, root = manager
    m.active_wifi = None
    m.interfaces = lambda: []
    m.settings['mode'] = 'client'
    m.perform('network-start', {})
    assert not m.busy and m.runtime_mode == 'no-wifi'
    assert m.settings['mode'] == 'client' and m.network_retry_at > 0
    assert m.status()['available']


def test_wifi_errors_are_throttled_and_wired_access_is_kept(manager):
    m, commands, root = manager
    m.active_wifi = None
    m.interfaces = lambda: [dict(name='wlan0', wireless=True, carrier=False), dict(name='eth0', wireless=False, carrier=True)]
    m.status_addresses = [dict(interface='eth0', address='192.168.1.5')]
    def fail(*args, **kwargs): raise subprocess.CalledProcessError(1, args)
    m.run = fail
    m.perform('network-start', {})
    assert not m.busy and m.runtime_mode == 'wired'
    m.reconcile_network(m.status_addresses, now=m.network_retry_at - 1)
    assert m.jobs.empty()


def test_client_mode_is_restored_on_new_wifi_adapter(manager):
    m, commands, root = manager
    m.settings['mode'] = 'client'
    m.settings['home'] = dict(ssid='Test network', security='open', hidden=False, password='')
    m.active_wifi = None
    m.interfaces = lambda: [dict(name='wlx123', wireless=True, carrier=True)]
    m.addresses = lambda: [dict(interface='wlx123', address='192.168.1.6')]
    m.reconcile_network([], now=1)
    m.perform(*m.jobs.get_nowait())
    assert m.runtime_mode == 'client'
    assert 'interface-name=wlx123' in (root / 'home.nmconnection').read_text()
    assert m.settings['wifi_interface'] == 'wlx123'


def test_existing_wired_ip_is_not_reconfigured(manager):
    m, commands, root = manager
    m.active_wifi = None
    m.interfaces = lambda: [dict(name='usb0', wireless=False, carrier=True)]
    m.reconcile_network([dict(interface='usb0', address='10.1.1.2')], now=1)
    assert m.status()['mode'] == 'wired' and m.jobs.empty() and commands == []


def test_client_disconnect_restores_hotspot_after_grace_period(manager):
    m, commands, root = manager
    m.settings['mode'] = 'client'
    m.reconcile_network([], now=1)
    m.reconcile_network([], now=91)
    assert m.jobs.empty()
    m.reconcile_network([], now=92)
    assert m.jobs.get_nowait()[0] == 'hotspot'


def test_no_wifi_does_not_block_local_player_or_cec(protected):
    client, m, root, lib, headers = protected
    m.interfaces = lambda: []
    m.active_wifi = None
    m.reconcile_network([], now=1)
    local = {'slideshow.local_playback': True}
    state = client.get('/api/state', environ_overrides=local)
    assert state.status_code == 200 and state.json['network']['mode'] == 'no-wifi'
    assert client.get('/api/frame/' + lib.current, environ_overrides=local).status_code == 200
    assert client.post('/api/cec-control', json={'action': 'pause'}, environ_overrides=local).status_code == 200
    assert not lib.playing
    assert client.get('/api/files').status_code == 401


def test_stale_dhcp_address_does_not_hide_lost_wifi_association(manager):
    m, commands, root = manager
    m.settings['mode'] = 'client'
    m.interfaces = lambda: [dict(name='wlan0', wireless=True, carrier=False)]
    old_address = [dict(interface='wlan0', address='192.168.1.40')]
    m.reconcile_network(old_address, now=1)
    m.reconcile_network(old_address, now=92)
    assert m.jobs.get_nowait()[0] == 'hotspot'


def test_startup_missing_home_network_restores_hotspot(manager):
    m, commands, root = manager
    m.active_wifi = None
    m.settings['mode'] = 'client'
    m.settings['home'] = dict(ssid='Absent home', security='open', password='', hidden=False)
    def runner(*args, **kwargs):
        commands.append(args)
        if args[:4] == ('nmcli', '--wait', '45', 'connection'):
            raise subprocess.CalledProcessError(10, args)
        return SimpleNamespace(stdout='')
    m.run = runner
    m.reconcile_network([], now=1)
    m.perform(*m.jobs.get_nowait())
    assert m.runtime_mode == 'hotspot' and not m.busy
    assert ('systemctl', 'restart', 'pi-slideshow-hotspot') in commands
