import importlib.util
import hashlib
from pathlib import Path


def test_repair_preserves_photos_settings_and_normal_boot(tmp_path):
    spec = importlib.util.spec_from_file_location('repair', Path(__file__).parents[1] / 'deploy' / 'repair_display.py')
    repair = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repair)
    source, boot, app, units, data = [tmp_path / p for p in ('payload', 'boot', 'app', 'units', 'data')]
    for path in (source, boot, app / 'deploy', app / 'slideshow', units, data / 'photos'):
        path.mkdir(parents=True)
    (source / 'cmdline.original').write_bytes(b'root=PARTUUID=test-02 rootwait\n')
    (boot / 'cmdline.txt').write_text('temporary maintenance command')
    hashes = []
    for name in ('player.py', 'run.py', 'display-client.sh', 'pi-slideshow-display.service', 'web.py'):
        content = ('new ' + name).encode()
        (source / name).write_bytes(content)
        hashes.append(f'{hashlib.sha256(content).hexdigest()}  {name}')
    (source / 'SHA256SUMS').write_text('\n'.join(hashes))
    (app / 'player.py').write_text('old player')
    (data / 'photos' / 'holiday.jpg').write_bytes(b'original-photo-bytes')
    (data / 'settings.json').write_bytes(b'{"seconds":20}')
    assert repair.apply_repair(source, boot, app, units, data) == 1
    assert (boot / 'cmdline.txt').read_bytes() == (source / 'cmdline.original').read_bytes()
    assert (data / 'photos' / 'holiday.jpg').read_bytes() == b'original-photo-bytes'
    assert (data / 'settings.json').read_bytes() == b'{"seconds":20}'
    assert (app / 'player.py').read_text() == 'new player.py'
    assert next((app / 'display-repair-backups').rglob('0-player.py')).read_text() == 'old player'
