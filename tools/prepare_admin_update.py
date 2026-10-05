"""Stage an admin update on bootfs without accessing uploaded photo storage."""
import hashlib
import importlib.util
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from slideshow.configuration import load_config
spec = importlib.util.spec_from_file_location('apply_admin_update', ROOT / 'deploy/apply_admin_update.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)
HOOK = ('systemd.run="/usr/bin/python3 /boot/firmware/pi-slideshow/admin-update/apply.py" '
        'systemd.run_success_action=reboot systemd.run_failure_action=reboot '
        'systemd.unit=kernel-command-line.target')


def prepare(card, configuration=None):
    if configuration is not None:
        load_config(configuration)  # Validate before any writes.
    card = Path(card).resolve()
    if card == ROOT or ROOT in card.parents or card in ROOT.parents:
        raise ValueError('Specify the SD boot partition, not the project directory.')
    for name in ['cmdline.txt', 'config.txt', 'bcm2708-rpi-zero-w.dtb', 'pi-slideshow-status.txt']:
        if not (card / name).is_file():
            raise ValueError('Not the expected installed Pi SD card: ' + name)
    if configuration is None:
        report = card / 'pi-slideshow-admin-update.txt'
        if not report.is_file() or 'installed' not in report.read_text().lower():
            raise ValueError('First admin installation requires --config with your private settings.')
    original = (card / 'cmdline.txt').read_bytes()
    command = original.decode().strip()
    destination = card / 'pi-slideshow/admin-update'
    saved = destination / 'cmdline.original'
    if saved.exists() and command == saved.read_text().strip() + ' ' + HOOK:
        original = saved.read_bytes()
        command = original.decode().strip()
    if 'systemd.run=' in command or 'systemd.unit=' in command or '\n' in command or 'root=' not in command:
        raise ValueError('Unexpected boot command. Inspect before updating.')
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'cmdline.original').write_bytes(original)
    manifest = []
    for name in updater.FILES + ['apply.py']:
        content = (ROOT / ('deploy/apply_admin_update.py' if name == 'apply.py' else name)).read_bytes()
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if target.read_bytes() != content:
            raise RuntimeError('Card readback failed: ' + name)
        manifest.append(hashlib.sha256(content).hexdigest() + '  ' + name)
    (destination / 'SHA256SUMS').write_text('\n'.join(manifest) + '\n', encoding='ascii', newline='\n')
    if configuration is not None and Path(configuration).resolve() != (card / 'slideshowpi.conf').resolve():
        shutil.copyfile(configuration, card / 'slideshowpi.conf')
    if configuration is not None and (card / 'slideshowpi.conf').read_bytes() != Path(configuration).read_bytes():
        raise RuntimeError('Configuration readback failed.')
    armed = (command + ' ' + HOOK + '\n').encode()
    (card / 'cmdline.txt').write_bytes(armed)
    if (card / 'cmdline.txt').read_bytes() != armed:
        raise RuntimeError('Boot command readback failed.')
    print('Verified admin update staged on bootfs. Image storage was not accessed.')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--drive', required=True)
    parser.add_argument('--config', help='Optional private settings to apply; omit to retain an existing admin configuration.')
    args = parser.parse_args()
    prepare(args.drive, args.config)
