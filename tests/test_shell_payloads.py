import importlib.util
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def test_hotspot_shell_bootstrap_executes_without_windows_line_endings():
    shell = str(Path('C:/Program Files/Git/bin/bash.exe')) if os.name == 'nt' else shutil.which('bash')
    script = (ROOT / 'deploy/hotspot-start.sh').read_bytes()
    assert b'\r' not in script
    bootstrap = b'\n'.join(script.split(b'\n')[:2]) + b'\n'
    result = subprocess.run([shell, '-c', bootstrap.decode()], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_release_archive_shell_scripts_use_unix_line_endings():
    runpy.run_path(str(ROOT / 'tools/package.py'), run_name='__main__')
    with zipfile.ZipFile(ROOT / 'dist/SlideshowPi.zip') as archive:
        scripts = [name for name in archive.namelist() if name.endswith('.sh')]
        assert scripts
        for name in scripts:
            assert b'\r' not in archive.read(name), name


def test_sd_staging_normalizes_and_hashes_windows_shell_payloads(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('prepare_normalized', ROOT / 'tools/prepare_admin_update.py')
    prepare = importlib.util.module_from_spec(spec); spec.loader.exec_module(prepare)
    project = tmp_path / 'project'; project.mkdir()
    for name in prepare.updater.FILES + ['deploy/apply_admin_update.py']:
        target = project / name; target.parent.mkdir(parents=True, exist_ok=True)
        data = (ROOT / name).read_bytes()
        if name.endswith('.sh'): data = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        target.write_bytes(data)
    monkeypatch.setattr(prepare, 'ROOT', project)
    card = tmp_path / 'card'; card.mkdir()
    for name in ['config.txt', 'bcm2708-rpi-zero-w.dtb', 'pi-slideshow-status.txt']:
        (card / name).write_text('fixture')
    (card / 'cmdline.txt').write_text('root=PARTUUID=test-02 rootwait\n')
    (card / 'pi-slideshow-admin-update.txt').write_text('installed')
    prepare.prepare(card)
    staged = card / 'pi-slideshow/admin-update'
    import hashlib
    manifest = dict(line.split('  ', 1)[::-1] for line in (staged / 'SHA256SUMS').read_text().splitlines())
    for name in prepare.updater.FILES:
        content = (staged / name).read_bytes()
        assert hashlib.sha256(content).hexdigest() == manifest[name]
        if name.endswith('.sh'): assert b'\r' not in content
