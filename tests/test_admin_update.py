import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('apply_admin', ROOT / 'deploy/apply_admin_update.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


def fixture(tmp_path):
    source, boot, app, units, data, config, nm = [tmp_path / p for p in ['source', 'boot', 'app', 'units', 'data', 'config', 'nm']]
    for path in [source, boot, app, units, data / 'photos', config, nm]:
        path.mkdir(parents=True)
    manifest = []
    for name in update.FILES:
        content = (ROOT / name).read_bytes()
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        manifest.append(hashlib.sha256(content).hexdigest() + '  ' + name)
    (source / 'SHA256SUMS').write_text('\n'.join(manifest))
    (source / 'cmdline.original').write_bytes(b'root=PARTUUID=test-02 rootwait\n')
    (boot / 'cmdline.txt').write_text('temporary boot hook')
    (boot / 'slideshowpi.conf').write_text((ROOT / 'slideshowpi.conf.example').read_text().replace(
        'CHANGE_ME_HOTSPOT_PASSWORD', 'test-hotspot-password').replace('CHANGE_ME_ADMIN_PASSWORD', 'test-admin-password'))
    (app / 'player.py').write_text('old player')
    (data / 'photos/family.jpg').write_bytes(b'original image bytes')
    (data / 'settings.json').write_bytes(b'{"seconds":20}')
    (config / 'hostapd.conf').write_text('existing hotspot configuration')
    drop = units / 'pi-slideshow-display.service.d'
    drop.mkdir()
    (drop / 'session.conf').write_text('[Service]\nPAMName=login\n')
    return source, boot, app, units, data, config, nm


def test_update_preserves_photo_settings_hotspot_and_session(tmp_path):
    paths = fixture(tmp_path)
    source, boot, app, units, data, config, nm = paths
    assert update.apply(*paths, enable=False) == 1
    assert (data / 'photos/family.jpg').read_bytes() == b'original image bytes'
    assert (data / 'settings.json').read_bytes() == b'{"seconds":20}'
    assert (config / 'hostapd.conf').read_text() == 'existing hotspot configuration'
    assert 'PAMName=login' in (units / 'pi-slideshow-display.service.d/session.conf').read_text()
    assert (boot / 'cmdline.txt').read_bytes() == (source / 'cmdline.original').read_bytes()
    assert (app / 'slideshow/admin_client.py').exists()
    assert next((app / 'admin-update-backups').glob('*/0-player.py')).read_text() == 'old player'


def test_corrupted_update_changes_no_app_files_and_restores_boot(tmp_path):
    paths = fixture(tmp_path)
    source, boot, app, units, data, config, nm = paths
    (source / 'player.py').write_text('corrupted payload')
    with pytest.raises(RuntimeError):
        update.apply(*paths, enable=False)
    assert (app / 'player.py').read_text() == 'old player'
    assert (boot / 'cmdline.txt').read_bytes() == (source / 'cmdline.original').read_bytes()
