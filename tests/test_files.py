from io import BytesIO
import json
from pathlib import Path
import time

from PIL import Image
import pytest

from test_app import appliance, upload, jpeg
from test_admin import manager
from test_photo_access import protected
from slideshow.files import Files, name


def setup(lib):
    folder = lib.sd / 'Album'
    folder.mkdir()
    child = folder / 'Trip'
    child.mkdir()
    for p in [folder / 'one.jpg', child / 'two.jpg']:
        Image.new('RGB', (20, 10), 'blue').save(p)
    lib.update({'folders': [str(folder), str(child)]})
    lib.control({'action': 'rotate', 'id': lib.images[0]['id'], 'degrees': 90})
    return Files(lib), folder, child


def test_browse_unselected_folders_download_preview_and_create(appliance):
    client, lib, headers = appliance
    folder = lib.sd / 'Unselected'
    folder.mkdir()
    Image.new('RGB', (20, 10), 'green').save(folder / 'photo.jpg')
    (folder / 'notes.txt').write_text('Keep non-photo files')
    assert client.get('/files').status_code == 200
    result = client.get('/api/files', query_string={'path': str(folder)}).json
    assert result['parent'] == str(lib.sd) and result['ignored'] == 1
    assert result['items'][0]['name'] == 'photo.jpg'
    for endpoint in ['thumbnail', 'download']:
        assert client.get('/api/files/' + endpoint, query_string={'path': str(folder / 'photo.jpg')}).status_code == 200
        assert client.get('/api/files/' + endpoint, query_string={'path': str(folder / 'notes.txt')}).status_code == 400
    assert client.post('/api/files/folder', headers=headers, json={'parent': str(folder), 'name': 'New album'}).status_code == 200
    assert (folder / 'New album').is_dir()
    assert client.post('/api/files/folder', headers=headers, json={'parent': str(folder), 'name': 'New album'}).status_code == 400


def test_folder_rename_preserves_rotations_current_and_selected_folders(appliance):
    _, lib, _ = appliance
    files, folder, child = setup(lib)
    old = lib.current
    entries = files.snapshot(folder)
    assert files.operation('rename', files.entry(folder), new_name='Renamed') == 'done'
    target = lib.sd / 'Renamed'
    assert target.is_dir() and not folder.exists()
    assert lib.settings['folders'] == [str(target), str(target / 'Trip')]
    assert old not in lib.settings['rotations']
    assert lib.settings['rotations'][lib.current] == 90
    assert json.loads(lib.config_file.read_text())['folders'] == lib.settings['folders']
    lib.scan()
    assert len(lib.images) == 2 and lib.current in {i['id'] for i in lib.images}


def test_copy_verified_keeps_source_rotation_and_handles_conflicts(appliance):
    _, lib, _ = appliance
    files, folder, child = setup(lib)
    entry = files.entry(folder)
    assert files.operation('copy', entry, str(lib.sd), 'skip') == 'skipped'
    assert files.operation('copy', entry, str(lib.sd), 'keep-both') == 'done'
    target = lib.sd / 'Album (1)'
    assert (target / 'one.jpg').read_bytes() == (folder / 'one.jpg').read_bytes()
    assert (target / 'Trip/two.jpg').exists()
    assert lib.settings['rotations'][lib.image_id(target / 'one.jpg')] == 90
    assert lib.settings['rotations'][lib.image_id(folder / 'one.jpg')] == 90
    assert lib.settings['folders'] == [str(folder), str(child)]
    assert not list(lib.sd.glob('.transfer-*'))


def test_cross_device_move_verifies_before_removing_original(appliance, monkeypatch):
    _, lib, _ = appliance
    files, folder, child = setup(lib)
    dest = lib.sd / 'Destination'; dest.mkdir()
    monkeypatch.setattr(files, 'same_device', lambda *args: False)
    assert files.operation('move', files.entry(folder), str(dest)) == 'done'
    assert not folder.exists() and (dest / 'Album/Trip/two.jpg').is_file()
    assert lib.settings['folders'] == [str(dest / 'Album'), str(dest / 'Album/Trip')]


def test_failed_verification_retains_source_and_cleans_partial_copy(appliance, monkeypatch):
    _, lib, _ = appliance
    files, folder, child = setup(lib)
    dest = lib.sd / 'Destination'; dest.mkdir()
    monkeypatch.setattr(files, 'same_device', lambda *args: False)
    monkeypatch.setattr(files, 'digest', lambda p: b'source' if folder in p.parents else b'destination')
    with pytest.raises(ValueError, match='verification failed'):
        files.operation('move', files.entry(folder), str(dest))
    assert (folder / 'one.jpg').exists() and not (dest / 'Album').exists()
    assert not list(dest.glob('.transfer-*'))


def test_delete_photo_folder_preserves_non_photo_files_and_root(appliance):
    _, lib, _ = appliance
    files, folder, child = setup(lib)
    (child / 'notes.txt').write_text('keep')
    with pytest.raises(ValueError, match='non-photo'):
        files.operation('delete', files.entry(folder))
    assert (folder / 'one.jpg').exists()
    with pytest.raises(ValueError, match='roots'):
        files.operation('delete', files.entry(lib.sd))
    (child / 'notes.txt').unlink()
    assert files.operation('delete', files.entry(folder)) == 'done'
    lib.scan()
    assert not folder.exists() and lib.current is None and lib.settings['folders'] == []
    assert not lib.settings['rotations']


def test_reject_stale_paths_nested_moves_and_unsupported_rename(appliance, tmp_path):
    client, lib, headers = appliance
    files, folder, child = setup(lib)
    with pytest.raises(ValueError, match='inside itself'):
        files.operation('move', files.entry(folder), str(child))
    with pytest.raises(ValueError): files.path(str(tmp_path))
    entry = files.entry(folder / 'one.jpg')
    with pytest.raises(ValueError, match='extension'):
        files.operation('rename', entry, new_name='one.txt')
    entry['stamp'] = 'stale'
    with pytest.raises(ValueError, match='changed'): files.operation('delete', entry)
    for value in ['../escape', '.hidden', 'a/b', '', 'test.', 'name?']:
        with pytest.raises(ValueError): name(value)
    assert client.post('/api/files/action', headers=headers, json={'action': 'shell', 'items': []}).status_code == 400
    assert client.post('/api/files/action', headers=headers, json={'action': 'delete', 'items': [files.entry(folder), files.entry(child)]}).status_code == 400


def test_async_jobs_and_authenticated_api(protected):
    client, m, root, lib, headers = protected
    assert b'Sign in' in client.get('/files').data
    assert client.get('/api/files').status_code == 401
    assert client.get('/api/files/job').status_code == 401
    assert client.post('/api/files/action', headers=headers, json={}).status_code == 401
    client.post('/api/photo-access/login', headers=headers, json={'password': 'test-photo-password'})
    item = client.get('/api/files').json['items'][0]
    assert client.post('/api/files/action', json={'action': 'rename', 'items': [item], 'name': 'new.jpg'}).status_code == 403
    assert client.post('/api/files/action', headers=headers, json={'action': 'rename', 'items': [item], 'name': 'new.jpg'}).status_code == 202
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = client.get('/api/files/job').json['job']
        if not job['running']: break
        time.sleep(.01)
    assert job['results'][0]['result'] == 'done' and not job['running']
    assert (lib.sd / 'new.jpg').exists()


def test_folder_upload_keeps_structure_and_rejects_traversal(appliance):
    client, lib, headers = appliance
    result = client.post('/api/upload', headers=headers, data={'folder': str(lib.sd), 'relative_path': 'Family/Trip/photo.jpg', 'image': (jpeg(), 'photo.jpg')})
    assert result.status_code == 200 and (lib.sd / 'Family/Trip/photo.jpg').exists()
    for relative in ['../escape.jpg', '/absolute.jpg', 'folder/../escape.jpg', 'folder\\escape.jpg']:
        assert client.post('/api/upload', headers=headers, data={'folder': str(lib.sd), 'relative_path': relative, 'image': (jpeg(), 'photo.jpg')}).status_code == 400


def test_symlink_is_not_browsed_or_mutated(appliance, tmp_path):
    client, lib, headers = appliance
    outside = tmp_path / 'outside'; outside.mkdir()
    link = lib.sd / 'link'
    try: link.symlink_to(outside, target_is_directory=True)
    except OSError: pytest.skip('Windows symlink privilege unavailable')
    assert client.get('/api/files', query_string={'path': str(link)}).status_code == 400
    assert not client.get('/api/files').json['items']


def test_pagination_and_job_exclusion(appliance):
    client, lib, headers = appliance
    for i in range(105): (lib.sd / f'{i:03}.jpg').write_bytes(b'photo')
    first = client.get('/api/files').json
    second = client.get('/api/files?page=1').json
    assert len(first['items']) == 100 and len(second['items']) == 5 and first['total'] == 105
    files = Files(lib); files.job = {'running': True}
    with pytest.raises(ValueError, match='already running'):
        files.start({'action': 'delete', 'items': [files.entry(lib.sd / '000.jpg')]})


def test_file_manager_delete_advances_to_next_and_keeps_play_state(appliance):
    client, lib, headers = appliance
    for filename in ['a.jpg', 'b.jpg', 'c.jpg']:
        upload(client, lib, headers, filename)
    files = Files(lib)
    lib.control({'action': 'show', 'id': lib.images[1]['id']})
    expected = lib.images[2]['id']
    files.operation('delete', files.entry(Path(lib.images[1]['path'])))
    lib.scan()
    assert lib.current == expected and not lib.playing


def test_empty_folder_and_source_mutation_during_copy(appliance, monkeypatch):
    client, lib, headers = appliance
    files, folder, child = setup(lib)
    dest = lib.sd / 'Empty'; dest.mkdir()
    assert files.listing(str(dest))['items'] == []
    original_digest = files.digest
    def mutate(path):
        digest = original_digest(path)
        if path == folder / 'one.jpg':
            with path.open('ab') as source: source.write(b'changed during copy')
        return digest
    monkeypatch.setattr(files, 'digest', mutate)
    with pytest.raises(ValueError, match='Source changed'):
        files.operation('copy', files.entry(folder), str(dest))
    assert (folder / 'one.jpg').exists() and not (dest / 'Album').exists()


def test_destination_safety_skips_existing_photo_and_rejects_low_space(appliance, monkeypatch):
    client, lib, headers = appliance
    upload(client, lib, headers, 'photo.jpg')
    files = Files(lib)
    source = lib.sd / 'photo.jpg'
    destination = lib.sd / 'Destination'; destination.mkdir()
    (destination / 'photo.jpg').write_bytes(b'keep existing')
    assert files.operation('copy', files.entry(source), str(destination), 'skip') == 'skipped'
    assert (destination / 'photo.jpg').read_bytes() == b'keep existing'
    from types import SimpleNamespace
    monkeypatch.setattr('slideshow.files.shutil.disk_usage', lambda path: SimpleNamespace(free=0))
    with pytest.raises(ValueError, match='free space'):
        files.operation('copy', files.entry(source), str(destination))
    assert source.exists() and not list(destination.glob('.transfer-*'))
