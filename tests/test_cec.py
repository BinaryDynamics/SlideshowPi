import json
import struct

import pytest

from slideshow.cec import Adapter, Buttons, GET_ADDRS, SET_ADDRS, SET_MODE, RECEIVE
from slideshow.configuration import config_text, load_config
from test_admin import manager, service
from test_photo_access import protected


def test_remote_mapping_and_repeat_protection():
    keys = Buttons()
    mask = 1 << 4
    assert keys.receive(bytes((4, 0x44, 0)), mask, 10) == 'toggle'
    assert keys.receive(bytes((4, 0x44, 0)), mask, 10.1) is None
    assert keys.receive(bytes((4, 0x45)), mask, 10.2) is None
    assert keys.receive(bytes((4, 0x44, 0)), mask, 10.3) == 'toggle'
    assert keys.receive(bytes((4, 0x44, 4)), mask, 11) == 'next'
    assert keys.receive(bytes((4, 0x44, 4)), mask, 11.1) is None
    assert keys.receive(bytes((4, 0x44, 4)), mask, 11.4) == 'next'
    for key, action in [(3, 'previous'), (0x44, 'play'), (0x46, 'pause'), (0x45, 'pause'),
                        (0x4b, 'next'), (0x4c, 'previous'), (0x61, 'toggle')]:
        assert Buttons().receive(bytes((4, 0x44, key)), mask, 20) == action
    for message in [b'', bytes((4, 0x44)), bytes((4, 0x44, 0x40)),
                    bytes((0x14, 0x44, 4)), bytes((0x0f, 0x44, 4)), bytes((8, 0x44, 4))]:
        assert keys.receive(message, mask, 22) is None


def test_kernel_adapter_registration_receive_and_release(monkeypatch):
    from slideshow import cec
    state = bytearray(92)
    calls, closes = [], []
    monkeypatch.setattr(cec.os, 'O_NONBLOCK', 2048, raising=False)
    monkeypatch.setattr(cec.os, 'O_CLOEXEC', 524288, raising=False)
    monkeypatch.setattr(cec.os, 'open', lambda *args: 99)
    monkeypatch.setattr(cec.os, 'close', closes.append)
    monkeypatch.setattr(cec.select, 'select', lambda *args: ([99], [], []))
    def ioctl(fd, operation, value):
        assert fd == 99
        calls.append(operation)
        if operation == GET_ADDRS:
            value[:] = state
        elif operation == SET_ADDRS:
            assert len(value) == 92
            state[:] = value
            if value[7]:
                assert value[6] == 5 and value[31] == 4 and value[35] == 3
                assert bytes(value[16:27]) == b'SlideshowPi'
                struct.pack_into('=H', state, 4, 16)
        elif operation == SET_MODE:
            assert struct.unpack('=I', value)[0] == 0x21
        elif operation == RECEIVE:
            assert len(value) == 56
            struct.pack_into('=I', value, 16, 3)
            value[32:35] = bytes((4, 0x44, 4))
            value[49] = 1
    adapter = Adapter('/dev/cec0', ioctl)
    assert adapter.poll() == ('next', True)
    adapter.close()
    adapter.close()
    assert state[7] == 0 and closes == [99]
    assert GET_ADDRS == 0x805c6103 and SET_ADDRS == 0xc05c6104
    assert RECEIVE == 0xc0386106 and SET_MODE == 0x40046109


def test_adapter_does_not_take_over_existing_controller(monkeypatch):
    from slideshow import cec
    monkeypatch.setattr(cec.os, 'O_NONBLOCK', 2048, raising=False)
    monkeypatch.setattr(cec.os, 'O_CLOEXEC', 524288, raising=False)
    monkeypatch.setattr(cec.os, 'open', lambda *args: 99)
    closes = []
    monkeypatch.setattr(cec.os, 'close', closes.append)
    def ioctl(fd, operation, value):
        assert operation == GET_ADDRS
        value[7] = 1
    with pytest.raises(OSError, match='already used'):
        Adapter('/dev/cec0', ioctl)
    assert closes == [99]


def test_cec_default_configuration_and_admin_preservation(manager, tmp_path):
    m, commands, root = manager
    assert m.status()['cec_enabled'] is True
    assert m.request({'action': 'cec-save', 'enabled': False})['ok']
    assert json.loads((root / 'network.json').read_text())['cec_enabled'] is False
    service.initialize(root, load_config())
    assert json.loads((root / 'network.json').read_text())['cec_enabled'] is False
    path = tmp_path / 'settings.conf'
    path.write_text('[cec]\nenabled = true\n')
    setup = load_config(path)
    service.initialize(root, setup)
    assert json.loads((root / 'network.json').read_text())['cec_enabled'] is True
    path.write_text(config_text(setup))
    assert load_config(path)['cec_enabled'] is True
    for invalid in ['"true"', '0']:
        path.write_text('[cec]\nenabled = ' + invalid)
        with pytest.raises(ValueError):
            load_config(path)
    with pytest.raises(ValueError):
        m.request({'action': 'cec-save', 'enabled': 'false'})
    assert commands == []


def test_cec_private_controls_bypass_photo_password_but_not_disabled_setting(protected):
    client, m, root, lib, headers = protected
    local = {'slideshow.local_playback': True}
    route = '/api/cec-control'
    assert client.post(route, headers=headers, json={'action': 'pause'}).status_code == 403
    assert client.post(route, headers={'X-Slideshow-Local-Playback': 'true'},
                       environ_overrides={'REMOTE_ADDR': '127.0.0.1'}, json={'action': 'pause'}).status_code == 403
    assert client.post(route, environ_overrides=local, json={'action': 'pause'}).status_code == 200
    assert lib.playing is False
    assert client.post(route, environ_overrides=local, json={'action': 'toggle'}).status_code == 200
    assert lib.playing is True
    assert client.post(route, environ_overrides=local, json={'action': 'rotate'}).status_code == 400
    m.request({'action': 'cec-save', 'enabled': False})
    assert client.post(route, environ_overrides=local, json={'action': 'pause'}).status_code == 403
    assert lib.playing is True
    assert client.post('/api/admin/action', headers=headers, json={'action': 'cec-save', 'enabled': True}).status_code == 401
    with client.session_transaction() as session:
        import time
        session['admin_since'] = time.time()
        session['auth_version'] = m.status()['auth_version']
    assert client.post('/api/admin/action', headers=headers, json={'action': 'cec-save', 'enabled': True}).status_code == 202
    assert m.status()['cec_enabled'] is True


def test_worker_disable_releases_adapter_and_stops_delivering(monkeypatch):
    from slideshow import cec
    from types import SimpleNamespace
    import threading
    state = SimpleNamespace(lock=threading.RLock(), settings={'cec_enabled': True})
    events = []
    class FakeAdapter:
        def __init__(self, path): events.append('open')
        def poll(self): return 'next', True
        def close(self): events.append('close')
    class Stop:
        steps = 0
        def is_set(self): return self.steps >= 2
        def wait(self, seconds):
            self.steps += 1
            state.settings['cec_enabled'] = False
    monkeypatch.setattr(cec.Path, 'glob', lambda *args: [cec.Path('/dev/cec0')])
    cec.run(state, Stop(), FakeAdapter, events.append)
    assert events == ['open', 'next', 'close']
    assert state.cec_status == 'Disabled.'


def test_missing_adapter_does_not_stop_broker(monkeypatch):
    from slideshow import cec
    from types import SimpleNamespace
    import threading
    state = SimpleNamespace(lock=threading.RLock(), settings={})
    class Stop:
        done = False
        def is_set(self): return self.done
        def wait(self, seconds): self.done = True
    monkeypatch.setattr(cec.Path, 'glob', lambda *args: [])
    cec.run(state, Stop(), lambda path: pytest.fail('No device should be opened'),
            lambda action: pytest.fail('No action should be sent'))
    assert 'unavailable' in state.cec_status


def test_adapter_recovers_own_registration_after_service_restart(monkeypatch):
    from slideshow import cec
    monkeypatch.setattr(cec.os, 'O_NONBLOCK', 2048, raising=False)
    monkeypatch.setattr(cec.os, 'O_CLOEXEC', 524288, raising=False)
    monkeypatch.setattr(cec.os, 'open', lambda *args: 99)
    monkeypatch.setattr(cec.os, 'close', lambda fd: None)
    state = bytearray(92)
    state[7], state[31], state[35] = 1, 4, 3
    state[16:27] = b'SlideshowPi'
    operations = []
    def ioctl(fd, operation, value):
        operations.append(operation)
        if operation == GET_ADDRS: value[:] = state
        elif operation == SET_ADDRS: state[:] = value
    adapter = Adapter('/dev/cec0', ioctl)
    assert operations == [GET_ADDRS, SET_MODE]
    adapter.close()
    assert state[7] == 0
