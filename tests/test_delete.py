from pathlib import Path

import pytest

from test_app import appliance, upload
from test_photo_access import protected
from test_admin import manager


def test_delete_current_next_last_and_rotation_settings(appliance):
    client, lib, headers = appliance
    for name in ['a.jpg', 'b.jpg', 'c.jpg']:
        assert upload(client, lib, headers, name).status_code == 200
    a, b, c = lib.images
    lib.control({'action': 'show', 'id': b['id']})
    lib.control({'action': 'rotate', 'id': b['id'], 'degrees': 90})
    result = client.post('/api/delete', headers=headers, json={'id': b['id'], 'stamp': b['stamp']})
    assert result.status_code == 200
    assert not Path(b['path']).exists()
    assert lib.current == c['id'] and not lib.playing
    assert b['id'] not in lib.settings['rotations']
    assert client.get('/api/thumbnail/' + b['id']).status_code == 400
    lib.scan()
    assert len(lib.images) == 2
    lib.control({'action': 'play'})
    for item in [a, c]:
        assert client.post('/api/delete', headers=headers, json=item).status_code == 200
    assert lib.current is None and lib.playing
    assert client.get('/api/state').json['count'] == 0


def test_delete_rejects_stale_unknown_invalid_and_csrf(appliance):
    client, lib, headers = appliance
    upload(client, lib, headers)
    item = lib.images[0]
    assert client.post('/api/delete', json=item).status_code == 403
    for payload in [{}, {'id': '../outside.jpg', 'stamp': item['stamp']},
                    {'id': item['id'], 'stamp': 'old'}, {'id': [], 'stamp': item['stamp']}, []]:
        assert client.post('/api/delete', headers=headers, json=payload).status_code == 400
        assert Path(item['path']).exists()
    with Path(item['path']).open('ab') as photo:
        photo.write(b'changed')
    assert client.post('/api/delete', headers=headers, json=item).status_code == 400
    assert Path(item['path']).exists()


def test_read_only_storage_failure_preserves_playback(appliance, monkeypatch):
    client, lib, headers = appliance
    upload(client, lib, headers)
    item = lib.images[0]
    def refuse(path, *args, **kwargs): raise PermissionError('read-only storage')
    monkeypatch.setattr(Path, 'unlink', refuse)
    result = client.post('/api/delete', headers=headers, json=item)
    assert result.status_code == 400 and 'writable' in result.json['error']
    assert lib.current == item['id'] and Path(item['path']).exists()


def test_delete_requires_photo_login_and_is_not_cec_action(protected):
    client, m, root, lib, headers = protected
    item = lib.images[0]
    assert client.post('/api/delete', headers=headers, json=item).status_code == 401
    assert client.post('/api/delete', headers=headers, json=item,
                       environ_overrides={'slideshow.local_playback': True}).status_code == 401
    assert client.post('/api/cec-control', json={'action': 'delete'},
                       environ_overrides={'slideshow.local_playback': True}).status_code == 400
    assert Path(item['path']).exists()
    client.post('/api/photo-access/login', headers=headers, json={'password': 'test-photo-password'})
    assert client.post('/api/delete', headers=headers, json=item).status_code == 200
    assert not Path(item['path']).exists()


def test_symlink_replacement_cannot_delete_its_target(appliance, tmp_path):
    client, lib, headers = appliance
    upload(client, lib, headers)
    item = lib.images[0]
    target = tmp_path / 'outside.jpg'
    target.write_bytes(b'preserve this file')
    path = Path(item['path'])
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip('This Windows account cannot create symlinks; Linux CI covers this case.')
    assert client.post('/api/delete', headers=headers, json=item).status_code == 400
    assert target.read_bytes() == b'preserve this file'


def test_delete_mounted_usb_and_reject_disconnected_volume(appliance, monkeypatch):
    client, lib, headers = appliance
    volume = lib.usb / 'photos'
    volume.mkdir(parents=True)
    monkeypatch.setattr('slideshow.core.os.path.ismount', lambda path: Path(path) == volume)
    from PIL import Image
    Image.new('RGB', (10, 10), 'blue').save(volume / 'usb.jpg')
    lib.update({'folders': [str(volume)]})
    item = lib.images[0]
    monkeypatch.setattr('slideshow.core.os.path.ismount', lambda path: False)
    assert client.post('/api/delete', headers=headers, json=item).status_code == 400
    assert Path(item['path']).exists()
    monkeypatch.setattr('slideshow.core.os.path.ismount', lambda path: Path(path) == volume)
    assert client.post('/api/delete', headers=headers, json=item).status_code == 200
    assert not Path(item['path']).exists()
