import json
from pathlib import Path

import pytest

from slideshow.configuration import load_config, config_text, PLAYBACK_DEFAULTS
from test_admin import manager


def test_default_card_preparation_saves_stable_generated_credentials(tmp_path):
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('default_prepare', root / 'tools/prepare_card.py')
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    card = tmp_path / 'card'
    card.mkdir()
    for name in ('cmdline.txt', 'config.txt', 'bcm2708-rpi-zero-w.dtb'):
        (card / name).write_text('test boot file')
    (card / 'issue.txt').write_text('Raspberry Pi reference')
    (card / 'user-data').write_text('#cloud-config\nssh_pwauth: false\n')
    (card / 'network-config').write_text('network:\n  wifis:\n    wlan0:\n      access-points:\n        TestNetwork: {}\n')
    setup = load_config(default_country='ZA')
    prepare.prepare(card, setup)
    saved = load_config(card / 'slideshowpi.conf')
    assert saved['password'] == setup['password']
    assert saved['admin_password'] == setup['admin_password']
    assert saved['country'] == 'ZA'


def test_empty_config_generates_complete_unique_defaults_and_roundtrips(tmp_path):
    first = load_config(default_country='ZA')
    second = load_config(default_country='ZA')
    assert first['country'] == 'ZA' and first['ssid'] == 'SlideshowPi'
    assert first['network_mode'] == 'hotspot' and first['home'] is None
    assert first['playback'] == PLAYBACK_DEFAULTS
    assert first['password'] != second['password']
    assert first['admin_password'] != second['admin_password']
    assert first['password'] != first['admin_password']
    path = tmp_path / 'settings.conf'
    path.write_text(config_text(first), encoding='utf8')
    resolved = load_config(path)
    assert resolved['password'] == first['password']
    assert resolved['admin_password'] == first['admin_password']
    assert resolved['playback'] == first['playback']


def test_partial_config_and_explicit_invalid_settings(tmp_path):
    path = tmp_path / 'settings.conf'
    path.write_text('[slideshow]\nseconds = 30\n')
    setup = load_config(path)
    assert setup['playback']['seconds'] == 30 and setup['playback']['fit'] == 'contain'
    path.write_text('[hotspot]\npassword = "short"\n')
    with pytest.raises(ValueError):
        load_config(path)


def test_admin_password_change_requires_current_password_and_invalidates_old_auth(manager):
    m, commands, root = manager
    old = (root / 'admin-password.txt').read_text().strip()
    version = m.status()['auth_version']
    with pytest.raises(ValueError):
        m.request(dict(action='admin-password', current_password='incorrect', password='new-test-admin-password'))
    assert not m.busy
    m.request(dict(action='admin-password', current_password=old, password='new-test-admin-password'))
    m.perform(*m.jobs.get_nowait())
    assert m.authenticate('new-test-admin-password') and not m.authenticate(old)
    assert m.status()['auth_version'] != version
    assert 'new-test-admin-password' not in str(commands)


def test_country_change_and_playback_config_preserve_photo_metadata(manager):
    m, commands, root = manager
    m.request(dict(action='device-save', country='ZA'))
    m.perform(*m.jobs.get_nowait())
    assert m.status()['country'] == 'ZA'
    data = root / 'data'
    data.mkdir()
    original = dict(seconds=20, rotations={'sample': 90}, folders=['/var/lib/pi-slideshow/photos'])
    (data / 'settings.json').write_text(json.dumps(original))
    setup = load_config()
    setup['playback_custom'] = False
    from test_admin import service
    service.initialize(root, setup)
    assert json.loads((data / 'settings.json').read_text()) == original
    setup['playback_custom'] = True
    setup['playback']['seconds'] = 30
    service.initialize(root, setup)
    result = json.loads((data / 'settings.json').read_text())
    assert result['seconds'] == 30 and result['rotations'] == original['rotations']
    assert result['folders'] == original['folders']
