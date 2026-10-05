#!/usr/bin/python3
"""Mount only USB filesystems. Runs as root, separate from the web application."""
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import time

ROOT = Path('/media/slideshow')
SUPPORTED = {'vfat', 'exfat', 'ntfs', 'ext4', 'ext3', 'ext2'}


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=20)


def usb_filesystems(blocks, inherited_usb=False):
    for device in blocks:
        is_usb = inherited_usb or device.get('tran') == 'usb'
        if is_usb and device.get('fstype') in SUPPORTED and device.get('type') in ('part', 'disk'):
            yield device
        yield from usb_filesystems(device.get('children', []), is_usb)


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    owner = pwd.getpwnam('slideshow')
    while True:
        try:
            devices = json.loads(run('lsblk', '--json', '--paths', '-o',
                                     'NAME,TYPE,TRAN,FSTYPE,UUID,MOUNTPOINTS').stdout)
            mounted = {str(p) for p in ROOT.iterdir() if os.path.ismount(p)}
            present = set()
            for device in usb_filesystems(devices['blockdevices']):
                uuid = device.get('uuid')
                if not uuid or not re.fullmatch(r'[A-Za-z0-9-]{1,80}', uuid):
                    continue
                target = ROOT / uuid
                if target.is_symlink():
                    continue
                present.add(str(target))
                if any(device.get('mountpoints') or []):
                    continue  # Never steal drives mounted elsewhere.
                target.mkdir(exist_ok=True)
                options = 'nosuid,nodev,noexec'
                if device['fstype'] in ('vfat', 'exfat', 'ntfs'):
                    options += f',uid={owner.pw_uid},gid={owner.pw_gid},umask=0022'
                try:
                    run('mount', '-o', options, '--', device['name'], str(target))
                    print(f'Mounted {device["name"]} at {target}', flush=True)
                except subprocess.SubprocessError as error:
                    print(f'Cannot mount {device["name"]}: {error}', flush=True)
            for missing in mounted - present:
                run('umount', '-l', '--', missing)
            for directory in ROOT.iterdir():
                if directory.is_dir() and not directory.is_symlink() and not os.path.ismount(directory):
                    try:
                        directory.rmdir()  # Only empty directories; never delete USB contents.
                    except OSError:
                        pass
        except Exception as error:
            print(f'USB scan: {error}', flush=True)
        time.sleep(5)


if __name__ == '__main__':
    main()
