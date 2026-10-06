"""Add a cloud-init slideshow installer to an already flashed bootfs partition.

Does not format or access raw disks. Configuration uses optional TOML input or generated defaults;
passwords are never accepted as process arguments. Requires PyYAML on Windows.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from slideshow.configuration import load_config, config_text

PROJECT = Path(__file__).resolve().parents[1]
HOOK = ['python3', '/boot/firmware/pi-slideshow/deploy/first_boot.py', 'stage']


def validate_setup(setup):
    if not isinstance(setup, dict):
        raise ValueError('Expected setup object.')
    for key, pattern in [('country', r'[A-Z]{2}')]:
        if not isinstance(setup.get(key), str) or not re.fullmatch(pattern, setup[key]):
            raise ValueError(f'Invalid {key}.')
    from slideshow.configuration import ssid, password
    ssid(setup.get('ssid'))
    password(setup.get('password'))
    result = {key: setup[key] for key in ('country', 'ssid', 'password')}
    for key in ('admin_password', 'network_mode', 'home', 'hotspot_network', 'photo_access', 'photo_access_custom', 'playback', 'playback_custom', 'auto_folders_custom', 'cec_enabled', 'cec_custom', 'updates', 'updates_custom'):
        if key in setup:
            result[key] = setup[key]
    defaults = load_config(default_country=result['country'])
    defaults.update(result)
    return defaults


def add_hook(content):
    if not content.startswith('#cloud-config'):
        raise ValueError('Card does not have cloud-config user-data; do not modify automatically.')
    original = yaml.safe_load(content)
    if not isinstance(original, dict) or not isinstance(original.get('runcmd', []), list):
        raise ValueError('Unexpected cloud-init configuration.')
    expected = dict(original)
    expected['runcmd'] = list(original.get('runcmd', []))
    if HOOK in expected['runcmd']:
        return content
    expected['runcmd'].append(HOOK)
    # Append to the existing block without reserializing hashed passwords,
    # boolean settings or other Imager fields. Validate the complete result.
    newline = '\r\n' if '\r\n' in content else '\n'
    lines = content.splitlines()
    matches = [i for i, line in enumerate(lines) if re.fullmatch(r'runcmd:\s*', line)]
    if len(matches) > 1:
        raise ValueError('Duplicate runcmd blocks.')
    hook_line = '  - [ python3, /boot/firmware/pi-slideshow/deploy/first_boot.py, stage ]'
    if matches:
        start = matches[0]
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i] and not lines[i][0].isspace() and not lines[i].startswith('#')), len(lines))
        lines.insert(end, hook_line)
    else:
        lines.extend(['runcmd:', hook_line])
    updated = newline.join(lines) + newline
    if yaml.safe_load(updated) != expected:
        raise ValueError('Configuration validation failed; no files changed.')
    return updated


def prepare(card, setup):
    setup = validate_setup(setup)
    card = Path(card).resolve()
    if card == PROJECT or PROJECT in card.parents or card in PROJECT.parents:
        raise ValueError('Specify the SD card boot partition, not the project directory.')
    for filename in ('cmdline.txt', 'config.txt', 'issue.txt', 'bcm2708-rpi-zero-w.dtb', 'user-data', 'network-config'):
        if not (card / filename).is_file():
            raise ValueError(f'Missing boot file: {filename}. Check the SD drive letter.')
    issue = (card / 'issue.txt').read_text()
    if 'Raspberry Pi reference' not in issue:
        raise ValueError('Expected a Raspberry Pi OS image.')
    original = (card / 'user-data').read_bytes()
    new_data = add_hook(original.decode('utf-8-sig')).encode('utf8')
    network = yaml.safe_load((card / 'network-config').read_text())
    wifi = network.get('network', {}).get('wifis', {}).get('wlan0', {})
    if not wifi.get('access-points'):
        raise ValueError('First boot requires a configured Wi-Fi network with Internet access.')
    destination = card / 'pi-slideshow'
    destination.mkdir(exist_ok=True)
    for directory in ('slideshow', 'templates', 'static', 'deploy'):
        shutil.copytree(PROJECT / directory, destination / directory, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for script in (destination / 'deploy').rglob('*.sh'):
        script.write_bytes(script.read_bytes().replace(b'\r\n', b'\n'))
    for filename in ('VERSION', 'run.py', 'player.py', 'README.md'):
        shutil.copy2(PROJECT / filename, destination / filename)
    with (destination / 'setup.json').open('w', encoding='utf8', newline='\n') as out:
        json.dump(setup, out, indent=2)
        out.write('\n')
        out.flush()
        os.fsync(out.fileno())
    backup = card / 'pi-slideshow-user-data.original'
    if not backup.exists():
        backup.write_bytes(original)
    with (card / 'user-data').open('wb') as out:
        out.write(new_data)
        out.flush()
        os.fsync(out.fileno())
    if yaml.safe_load((card / 'user-data').read_text()) != yaml.safe_load(new_data):
        raise ValueError('SD card readback failed.')
    for local in [p for directory in ('slideshow', 'templates', 'static', 'deploy')
                  for p in (PROJECT / directory).rglob('*')
                  if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']:
        copied = destination / local.relative_to(PROJECT)
        expected = local.read_bytes()
        if local.suffix == '.sh':
            expected = expected.replace(b'\r\n', b'\n')
        if hashlib.sha256(expected).digest() != hashlib.sha256(copied.read_bytes()).digest():
            raise ValueError(f'File verification failed: {local.name}')
    (card / 'pi-slideshow-status.txt').write_text(
        'Prepared on Windows. Insert into Pi and power on with configured home Wi-Fi available.\n'
        'The Pi installs packages, then reboots into the slideshow hotspot.\n'
        'Do not disconnect power during installation.\n')
    resolved = config_text(setup)
    (card / 'slideshowpi.conf').write_text(resolved, encoding='utf8', newline='\n')
    if (card / 'slideshowpi.conf').read_text(encoding='utf8') != resolved:
        raise RuntimeError('Private configuration readback failed.')
    print(f'Prepared and verified {card}. Hotspot: {setup["ssid"]}. Password was saved without being printed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--drive', required=True, help='Boot partition root, e.g. D:\\')
    parser.add_argument('--config', help='Path to your private slideshowpi.conf text file.')
    args = parser.parse_args()
    try:
        command = (Path(args.drive) / 'cmdline.txt').read_text()
        match = re.search(r'cfg80211.ieee80211_regdom=([A-Z]{2})', command)
        country = match[1] if match else 'GB'
        configuration = Path(args.config) if args.config else PROJECT / 'slideshowpi.conf'
        setup = load_config(configuration if configuration.exists() else None, default_country=country)
        if args.config and not configuration.exists():
            raise ValueError('The requested configuration file does not exist.')
        prepare(args.drive, setup)
        if not configuration.exists():
            configuration.write_text(config_text(setup), encoding='utf8', newline='\n')
        print('Resolved settings and generated passwords are on bootfs in slideshowpi.conf. Keep a private copy before first boot.')
    except Exception as error:
        print(f'Preparation failed: {error}', file=sys.stderr)
        raise SystemExit(1)
