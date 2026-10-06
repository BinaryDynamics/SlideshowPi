from io import BytesIO
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import zipfile

import pytest

from test_admin import manager, service, web
from slideshow.configuration import load_config, update_settings, UPDATE_DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('app_updates', ROOT / 'deploy/app_update.py')
updates = importlib.util.module_from_spec(spec); spec.loader.exec_module(updates)


def package(target, version='test-2', extra=None):
    payload = {'VERSION': (version + '\n').encode(), 'run.py': b'pass\n', 'player.py': b'pass\n',
               'slideshow/web.py': b'pass\n', 'slideshow/core.py': b'pass\n',
               'deploy/admin_service.py': b'pass\n', 'deploy/app_update.py': b'pass\n',
               'templates/admin.html': b'<p>admin</p>', 'static/admin.js': b'// admin',
               'deploy/hotspot-start.sh': b'#!/bin/bash\r\nset -euo pipefail\r\n'}
    payload.update(extra or {})
    manifest = dict(format=1, dependency_set=1, version=version,
                    files={name: hashlib.sha256(data).hexdigest() for name, data in payload.items()})
    with zipfile.ZipFile(target, 'w') as archive:
        for name, data in payload.items(): archive.writestr('SlideshowPi/' + name, data)
        archive.writestr('SlideshowPi/release.json', json.dumps(manifest))
    return payload


def test_configuration_defaults_forks_and_rejections(tmp_path):
    assert load_config()['updates'] == UPDATE_DEFAULTS
    assert update_settings({'repository': 'https://github.com/My-Fork/SlideshowPi.git/'})['repository'] == 'https://github.com/My-Fork/SlideshowPi'
    for url in ['http://github.com/a/b', 'https://evil.example/a/b', 'https://github.com/a/b?token=secret',
                'https://user:password@github.com/a/b', 'https://github.com/a/b/releases', 'file:///etc/passwd']:
        with pytest.raises(ValueError): update_settings({'repository': url})
    with pytest.raises(ValueError): update_settings({'include_prereleases': 'true'})


def test_package_checksums_paths_limits_and_shell_line_endings(tmp_path):
    archive = tmp_path / 'release.zip'; payload = package(archive)
    candidate = tmp_path / 'candidate'
    assert updates.extract(archive, candidate) == 'test-2'
    assert (candidate / 'deploy/hotspot-start.sh').read_bytes() == b'#!/bin/bash\nset -euo pipefail\n'
    with zipfile.ZipFile(archive, 'a') as output: output.writestr('SlideshowPi/../../escape', b'bad')
    with pytest.raises(ValueError, match='Unsafe'): updates.extract(archive, tmp_path / 'bad')
    archive = tmp_path / 'broken.zip'; package(archive)
    with zipfile.ZipFile(archive) as source:
        files = {name: source.read(name) for name in source.namelist()}
    files['SlideshowPi/run.py'] = b'raise Exception("corrupted")'
    with zipfile.ZipFile(archive, 'w') as output:
        for name, data in files.items(): output.writestr(name, data)
    with pytest.raises(ValueError, match='checksum'): updates.extract(archive, tmp_path / 'broken')
    assert not (tmp_path / 'broken').exists()


def test_zip_symlinks_and_duplicate_entries_are_rejected(tmp_path):
    archive = tmp_path / 'release.zip'; package(archive)
    with zipfile.ZipFile(archive, 'a') as output:
        info = zipfile.ZipInfo('SlideshowPi/link'); info.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(info, 'outside')
    with pytest.raises(ValueError, match='Unsafe'): updates.extract(archive, tmp_path / 'candidate')


def test_root_snapshot_is_bounded_and_verifies_upload_hash(tmp_path):
    inbox, state = tmp_path / 'inbox', tmp_path / 'state'; inbox.mkdir()
    token = 'a' * 32
    source = inbox / (token + '.zip'); source.write_bytes(b'release bytes')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert updates.snapshot(token, digest, state, inbox).read_bytes() == b'release bytes'
    assert not source.exists()
    source.write_bytes(b'changed')
    token = 'b' * 32; (inbox / (token + '.zip')).write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'): updates.snapshot(token, digest, state, inbox)
    assert not (state / (token + '.zip')).exists()
    with pytest.raises(ValueError): updates.snapshot('../escape', digest, state, inbox)


def test_install_preserves_external_data_and_keeps_rollback_copy(tmp_path):
    app, state = tmp_path / 'app', tmp_path / 'state'; app.mkdir(); state.mkdir()
    (app / 'VERSION').write_text('old')
    photos = tmp_path / 'photos'; photos.mkdir(); (photos / 'family.jpg').write_bytes(b'original')
    settings = tmp_path / 'settings.json'; settings.write_bytes(b'{"seconds":15}')
    token = 'a' * 32; package(state / (token + '.zip'))
    commands = []
    updates.apply(token, app, state, runner=lambda *args, **kwargs: commands.append(args), health=lambda: True)
    assert (app / 'VERSION').read_text().strip() == 'test-2'
    assert (state / ('backup-' + token) / 'VERSION').read_text() == 'old'
    assert updates.read_status(state)['phase'] == 'complete'
    assert (photos / 'family.jpg').read_bytes() == b'original'
    assert settings.read_bytes() == b'{"seconds":15}'
    assert ('systemctl', 'stop', *updates.UNITS) in commands


def test_failed_service_startup_rolls_back_application(tmp_path):
    app, state = tmp_path / 'app', tmp_path / 'state'; app.mkdir(); state.mkdir()
    (app / 'VERSION').write_text('old')
    token = 'a' * 32; package(state / (token + '.zip'))
    with pytest.raises(ValueError, match='startup'):
        updates.apply(token, app, state, runner=lambda *args, **kwargs: None, health=lambda: False)
    assert (app / 'VERSION').read_text() == 'old'
    assert updates.read_status(state)['phase'] == 'failed'
    assert not (state / ('failed-' + token)).exists()


def test_interrupted_install_recovers_even_if_app_directory_is_missing(tmp_path):
    app, state = tmp_path / 'app', tmp_path / 'state'; state.mkdir()
    token = 'a' * 32; backup = state / ('backup-' + token); backup.mkdir()
    (backup / 'VERSION').write_text('old')
    updates.write_status(state, phase='installing', token=token)
    updates.recover(state, app)
    assert (app / 'VERSION').read_text() == 'old'
    assert updates.read_status(state)['phase'] == 'failed'


class Response(BytesIO):
    status = 200
    def geturl(self): return 'https://release-assets.githubusercontent.com/release.zip'


def release(tag, date, prerelease=False):
    return dict(tag_name=tag, published_at=date, prerelease=prerelease, draft=False,
                assets=[dict(name='SlideshowPi.zip', size=4,
                             browser_download_url='https://github.com/BinaryDynamics/SlideshowPi/releases/download/' + tag + '/SlideshowPi.zip')])


def test_latest_release_channels_and_bad_downloads(tmp_path):
    data = [release('stable', '2026-01-01'), release('beta', '2026-02-01', True)]
    open_url = lambda *args, **kwargs: Response(json.dumps(data).encode())
    assert updates.latest(UPDATE_DEFAULTS, open_url)['tag'] == 'beta'
    assert updates.latest(dict(UPDATE_DEFAULTS, include_prereleases=False), open_url)['tag'] == 'stable'
    item = updates.latest(UPDATE_DEFAULTS, open_url)
    path = tmp_path / 'release.zip'
    assert updates.download(item, path, lambda *args, **kwargs: Response(b'ZIP!')) == hashlib.sha256(b'ZIP!').hexdigest()
    path.unlink(); item['digest'] = 'sha256:' + '0' * 64
    with pytest.raises(ValueError, match='checksum'): updates.download(item, path, lambda *args, **kwargs: Response(b'ZIP!'))
    assert not path.exists()


def test_admin_update_actions_require_authentication_and_csrf(web):
    client, broker, headers = web
    assert client.post('/api/admin/action', headers=headers, json={'action': 'update-online'}).status_code == 401
    assert client.post('/api/admin/update-upload', headers=headers, data={'package': (BytesIO(b'ZIP!'), 'release.zip')}).status_code == 401
    client.post('/api/admin/login', headers=headers, json={'password': 'test-admin-password'})
    assert client.post('/api/admin/action', json={'action': 'update-check'}).status_code == 403
    assert client.post('/api/admin/action', headers=headers, json={'action': 'update-check'}).status_code == 202
    assert client.post('/api/admin/action', headers=headers, json={'action': 'update-online'}).status_code == 202
    assert client.post('/api/admin/action', headers=headers, json={'action': 'update-upload', 'token': 'a' * 32}).status_code == 400  # Upload references cannot be supplied through public action endpoint.
    result = client.post('/api/admin/update-upload', headers=headers, data={'package': (BytesIO(b'ZIP!'), 'release.zip')})
    assert result.status_code == 202
    assert broker.actions[-1][0] == 'update-upload'
    assert broker.actions[-1][1]['sha256'] == hashlib.sha256(b'ZIP!').hexdigest()


def test_source_settings_are_persisted_and_old_config_retains_them(manager):
    m, commands, root = manager
    m.request(dict(action='update-source-save', settings=dict(repository='https://github.com/Fork/SlideshowPi', include_prereleases=False)))
    service.initialize(root, load_config())
    assert json.loads((root / 'network.json').read_text())['updates']['repository'] == 'https://github.com/Fork/SlideshowPi'
    assert not commands


def test_current_release_package_is_updateable(tmp_path):
    import runpy
    runpy.run_path(str(ROOT / 'tools/package.py'), run_name='__main__')
    version = updates.extract(ROOT / 'dist/SlideshowPi.zip', tmp_path / 'candidate')
    assert version == (ROOT / 'VERSION').read_text().strip()
    assert (tmp_path / 'candidate/deploy/app_update.py').exists()


def test_health_requires_all_services_and_a_working_admin_broker(monkeypatch):
    ticks = iter([0, 1, 20])
    monkeypatch.setattr(updates.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(updates.time, 'sleep', lambda seconds: None)
    seen = []
    def runner(*args):
        seen.append(args)
        if args[-1] == 'pi-slideshow-display': raise OSError('display failed')
    assert not updates.healthy(runner, lambda *args, **kwargs: pytest.fail('Failed service must prevent HTTP acceptance'), timeout=10)
    assert seen == [('systemctl', 'is-active', '--quiet', 'pi-slideshow-display')]


def test_online_broker_queues_download_and_standalone_updater(manager, monkeypatch, tmp_path):
    m, commands, root = manager
    from deploy import app_update as broker_updates
    state = tmp_path / 'updates'
    monkeypatch.setattr(broker_updates, 'STATE', state)
    monkeypatch.setattr(broker_updates, 'read_status', lambda: updates.read_status(state))
    monkeypatch.setattr(broker_updates, 'write_status', lambda **values: updates.write_status(state, **values))
    monkeypatch.setattr(broker_updates, 'latest', lambda values: dict(tag='vTest', url='https://github.com/BinaryDynamics/SlideshowPi/releases/download/vTest/SlideshowPi.zip'))
    monkeypatch.setattr(broker_updates, 'download', lambda release, target: target.write_bytes(b'ZIP!'))
    monkeypatch.setattr(broker_updates, 'initialize', lambda **values: None)
    m.request(dict(action='update-online'))
    assert updates.read_status(state)['phase'] == 'queued'
    m.perform(*m.jobs.get_nowait())
    assert any(command[0] == 'systemd-run' and '--apply' in command for command in commands)
    assert not any('restart' in command or 'hotspot' in command for command in commands)


def test_photo_mutations_are_blocked_while_application_updates_run(web):
    client, broker, headers = web
    original = broker.status
    broker.status = lambda: dict(original(), update_status={'phase': 'installing'})
    assert client.post('/api/settings', headers=headers, json={'seconds': 20}).status_code == 409
    assert client.get('/api/state').status_code == 200
    assert client.post('/api/control', headers=headers, json={'action': 'pause'}).status_code == 200


def test_preflight_failure_keeps_original_and_cleans_staging(tmp_path):
    app, state = tmp_path / 'app', tmp_path / 'state'; app.mkdir(); state.mkdir()
    (app / 'VERSION').write_text('old')
    token = 'b' * 32; package(state / (token + '.zip'))
    commands = []
    def runner(*args, **kwargs):
        commands.append(args)
        if args[0] == '/usr/bin/setpriv': raise OSError('missing dependency')
    with pytest.raises(OSError): updates.apply(token, app, state, runner=runner, health=lambda: True)
    assert (app / 'VERSION').read_text() == 'old'
    assert not any(command[0] == 'systemctl' for command in commands)
    assert not (state / ('candidate-' + token)).exists()


def test_recovery_discards_partial_candidate_without_changing_existing_app(tmp_path):
    app, state = tmp_path / 'app', tmp_path / 'state'; app.mkdir(); state.mkdir()
    (app / 'VERSION').write_text('old')
    token = 'c' * 32
    (state / ('candidate-' + token)).mkdir()
    (state / (token + '.zip')).write_bytes(b'partial')
    updates.write_status(state, phase='validating', token=token)
    updates.recover(state, app)
    assert (app / 'VERSION').read_text() == 'old'
    assert not (state / ('candidate-' + token)).exists()
    assert not (state / (token + '.zip')).exists()


def test_fork_static_assets_are_included_in_runtime(tmp_path):
    archive = tmp_path / 'release.zip'
    package(archive, extra={'static/icons/favicon.ico': b'icon', 'static/font.woff2': b'font'})
    candidate = tmp_path / 'candidate'; updates.extract(archive, candidate)
    assert (candidate / 'static/font.woff2').read_bytes() == b'font'


def test_archive_directory_and_compression_are_bounded_before_extraction(tmp_path):
    bad = tmp_path / 'bad.zip'
    bad.write_bytes(b'not a release package')
    with pytest.raises(ValueError): updates.extract(bad, tmp_path / 'candidate')
    compressed = tmp_path / 'unsupported.zip'
    with zipfile.ZipFile(compressed, 'w', compression=zipfile.ZIP_BZIP2) as archive:
        archive.writestr('SlideshowPi/run.py', b'pass')
    with pytest.raises(ValueError, match='Unsafe'): updates.extract(compressed, tmp_path / 'candidate')
    archive = tmp_path / 'oversized.zip'
    package(archive, extra={'static/large.bin': b'x' * (8 * 1024 * 1024 + 1)})
    with pytest.raises(ValueError, match='exceeds'): updates.extract(archive, tmp_path / 'candidate')


def test_github_repository_case_does_not_reject_canonical_asset_url():
    data = [release('vTest', '2026-01-01')]
    result = updates.latest(dict(UPDATE_DEFAULTS, repository='https://github.com/binarydynamics/slideshowpi'),
                            lambda *args, **kwargs: Response(json.dumps(data).encode()))
    assert result['tag'] == 'vTest'
