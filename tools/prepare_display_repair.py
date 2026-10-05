"""Stage a one-time display update on bootfs; do not touch image storage."""
import hashlib
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
HOOK = ('systemd.run="/usr/bin/python3 /boot/firmware/pi-slideshow/display-repair/apply.py" '
        'systemd.run_success_action=reboot systemd.run_failure_action=reboot '
        'systemd.unit=kernel-command-line.target')


def prepare(card):
    card = Path(card).resolve()
    for name in ('cmdline.txt', 'config.txt', 'bcm2708-rpi-zero-w.dtb', 'pi-slideshow-status.txt'):
        if not (card / name).is_file():
            raise ValueError(f'Not the expected installed SD card: missing {name}')
    original = (card / 'cmdline.txt').read_bytes()
    text = original.decode('utf-8').strip()
    if 'systemd.run=' in text or 'systemd.unit=' in text:
        raise ValueError('Card already has a custom boot command; inspect before updating.')
    if 'root=' not in text or '\n' in text:
        raise ValueError('Expected a single-line boot command.')
    destination = card / 'pi-slideshow' / 'display-repair'
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'cmdline.original').write_bytes(original)
    names = {'apply.py': 'deploy/repair_display.py', 'player.py': 'player.py', 'run.py': 'run.py',
             'display-client.sh': 'deploy/display-client.sh',
             'pi-slideshow-display.service': 'deploy/pi-slideshow-display.service',
             'web.py': 'slideshow/web.py'}
    hashes = []
    for name, local in names.items():
        content = (PROJECT / local).read_bytes()
        (destination / name).write_bytes(content)
        if (destination / name).read_bytes() != content:
            raise RuntimeError(f'Card readback failed: {name}')
        hashes.append(f'{hashlib.sha256(content).hexdigest()}  {name}')
    (destination / 'SHA256SUMS').write_text('\n'.join(hashes) + '\n', encoding='ascii', newline='\n')
    armed = (text + ' ' + HOOK + '\n').encode('utf8')
    (card / 'cmdline.txt').write_bytes(armed)
    if (card / 'cmdline.txt').read_bytes() != armed:
        raise RuntimeError('Boot command readback failed.')
    print(f'Verified display repair staged on {card}. Original boot command saved; image storage not written.')


if __name__ == '__main__':
    prepare(sys.argv[1])
