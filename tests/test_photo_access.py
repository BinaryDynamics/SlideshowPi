import json
import re

import pytest
from PIL import Image

from slideshow.configuration import config_text, load_config
from slideshow.web import create_app
from test_admin import manager, service


@pytest.fixture
def protected(manager, tmp_path):
    m, commands, root = manager
    service.configure_photo_access(root, True, 'test-photo-password')
    class Broker:
        def status(self): return m.status()
        def call(self, action, **payload): return m.request(dict(action=action, **payload))
    app = create_app(tmp_path / 'data', tmp_path / 'usb', False, Broker())
    app.config['TESTING'] = True
    lib = app.extensions['library']
    Image.new('RGB', (80, 40), 'blue').save(lib.sd / 'sample.jpg')
    lib.scan()
    client = app.test_client()
    token = re.search(rb'name="slideshow-token" content="([^"]+)"', client.get('/').data)[1].decode()
    return client, m, root, lib, {'X-Slideshow-Token': token}


def test_photo_api_images_and_uploads_require_password(protected):
    client, m, root, lib, headers = protected
    assert b'Sign in to view and manage photos' in client.get('/').data
    image_id = lib.images[0]['id']
    for path in ['/api/state', '/api/images', '/api/thumbnail/' + image_id, '/api/frame/' + image_id,
                 '/api/image/' + image_id]:
        assert client.get(path).status_code == 401
    for path in ['/api/control', '/api/settings', '/api/upload', '/api/folders', '/api/rescan']:
        assert client.post(path, headers=headers, json={}).status_code == 401
    assert client.post('/api/photo-access/login', json={'password': 'test-photo-password'}).status_code == 403
    assert client.post('/api/photo-access/login', headers=headers, json={'password': 'wrong'}).status_code == 401
    assert client.post('/api/photo-access/login', headers=headers, json={'password': 'test-photo-password'}).status_code == 200
    assert client.get('/api/state').status_code == 200
    assert client.get('/api/thumbnail/' + image_id).status_code == 200
    assert client.get('/api/admin/status').status_code == 401
    assert client.post('/api/photo-access/logout', headers=headers, json={}).status_code == 200
    assert client.get('/api/state').status_code == 401


def test_local_player_bypass_is_read_only_and_cannot_be_set_with_headers(protected):
    client, m, root, lib, headers = protected
    image_id = lib.images[0]['id']
    assert client.get('/api/state', headers={'X-Slideshow-Local-Playback': 'true'}, environ_overrides={'REMOTE_ADDR': '127.0.0.1'}).status_code == 401
    local = {'slideshow.local_playback': True}
    assert client.get('/api/state', environ_overrides=local).status_code == 200
    assert client.get('/api/frame/' + image_id, environ_overrides=local).status_code == 200
    assert client.get('/api/images', environ_overrides=local).status_code == 401
    assert client.post('/api/control', headers=headers, json={'action': 'pause'}, environ_overrides=local).status_code == 401


def test_admin_access_password_rotation_disable_and_preservation(protected):
    client, m, root, lib, headers = protected
    client.post('/api/photo-access/login', headers=headers, json={'password': 'test-photo-password'})
    service.configure_photo_access(root, True, 'replacement-photo-password')
    assert client.get('/api/state').status_code == 401
    admin_password = (root / 'admin-password.txt').read_text().strip()
    assert client.post('/api/admin/login', headers=headers, json={'password': admin_password}).status_code == 200
    assert client.get('/api/state').status_code == 200
    assert client.post('/api/admin/action', headers=headers, json={'action': 'photo-access-save', 'enabled': False, 'password': ''}).status_code == 202
    m.perform(*m.jobs.get_nowait())
    assert not m.status()['photo_access_enabled']
    assert b'replacement-photo-password' not in (root / 'photo-auth.json').read_bytes()
    service.initialize(root, load_config())  # Omitted section retains existing policy/hash.
    assert not m.status()['photo_access_enabled']
    service.configure_photo_access(root, True)
    assert m.authenticate('replacement-photo-password', photo=True)


def test_photo_login_is_throttled_and_expired_sessions_are_rejected(protected):
    client, m, root, lib, headers = protected
    for _ in range(5):
        assert client.post('/api/photo-access/login', headers=headers, json={'password': 'wrong'}).status_code == 401
    assert client.post('/api/photo-access/login', headers=headers, json={'password': 'test-photo-password'}).status_code == 429
    with client.session_transaction() as session:
        session['photo_since'] = 1
        session['photo_auth_version'] = m.status()['photo_auth_version']
    assert client.get('/api/state').status_code == 401


def test_optional_config_defaults_and_generated_password_roundtrip(tmp_path):
    assert load_config()['photo_access'] == dict(enabled=False, password='')
    config = tmp_path / 'config.conf'
    config.write_text('[photo_access]\nenabled = true\n')
    setup = load_config(config)
    assert len(setup['photo_access']['password']) >= 12
    config.write_text(config_text(setup))
    assert load_config(config)['photo_access'] == setup['photo_access']
    config.write_text('[photo_access]\nenabled = "yes"\n')
    with pytest.raises(ValueError): load_config(config)


def test_broker_outage_preserves_policy_and_blocks_browser_access(protected, monkeypatch):
    from slideshow.admin_client import AdminClient, AdminUnavailable
    client, m, root, lib, headers = protected
    broker = AdminClient()
    broker.cached = m.status()
    def unavailable(*args, **kwargs): raise AdminUnavailable('Unavailable')
    monkeypatch.setattr(broker, 'call', unavailable)
    assert broker.status()['photo_access_enabled'] is True
    assert broker.status()['available'] is False
    app = create_app(lib.data, lib.usb, False, broker)
    browser = app.test_client()
    assert browser.get('/api/state').status_code == 503
    assert browser.get('/api/state', environ_overrides={'slideshow.local_playback': True}).status_code == 200
