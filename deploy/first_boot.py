#!/usr/bin/python3
"""Cloud-init entry point and retryable installation for Windows-prepared cards."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import traceback

APP = Path('/opt/pi-slideshow')
CONFIG = Path('/etc/pi-slideshow/setup.json')
DONE = Path('/var/lib/pi-slideshow/installation-complete')
UNIT = 'pi-slideshow-install.service'


def run(*args):
    subprocess.run(args, check=True)


def boot_root():
    return Path('/boot/firmware') if Path('/boot/firmware/cmdline.txt').exists() else Path('/boot')


def status(message):
    print(message, flush=True)
    try:
        (boot_root() / 'pi-slideshow-status.txt').write_text(message + '\n')
    except OSError:
        pass


def stage():
    source = Path(__file__).resolve().parents[1]
    if DONE.exists():
        return
    # Copy off FAT bootfs before executing: Linux permissions apply on rootfs.
    APP.mkdir(parents=True, exist_ok=True)
    for folder in ('slideshow', 'templates', 'static', 'deploy'):
        shutil.copytree(source / folder, APP / folder, dirs_exist_ok=True)
    for name in ('run.py', 'player.py'):
        shutil.copy2(source / name, APP / name)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    text_config = boot_root() / 'slideshowpi.conf'
    if text_config.exists():
        sys.path.insert(0, str(source))
        from slideshow.configuration import load_config
        try:
            setup = load_config(text_config)
        except Exception:
            raise ValueError('Invalid slideshowpi.conf. Check the text file; credential input is not logged.') from None
    else:
        with (source / 'setup.json').open() as file:
            setup = json.load(file)
    with open(CONFIG, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
        json.dump(setup, file)
    CONFIG.chmod(0o600)
    for script in (APP / 'deploy').glob('*.sh'):
        script.chmod(0o755)
    shutil.copy2(APP / 'deploy' / UNIT, Path('/etc/systemd/system') / UNIT)
    run('systemctl', 'daemon-reload')
    # Starting synchronously from cloud-final would deadlock its After= ordering.
    run('systemctl', 'enable', UNIT)
    run('systemctl', 'start', '--no-block', UNIT)
    status('Prepared. Installation starts after Raspberry Pi OS first-boot setup completes.')


def install():
    if DONE.exists():
        return
    status('Installing slideshow packages. Keep power on and home Wi-Fi available.')
    with CONFIG.open() as file:
        setup = json.load(file)
    env = dict(os.environ, COUNTRY=setup['country'], SSID=setup['ssid'],
               HOTSPOT_PASSWORD=setup['password'], DEBIAN_FRONTEND='noninteractive',
               SLIDESHOW_QUIET_CREDENTIALS='1')
    subprocess.run(['bash', str(APP / 'deploy' / 'install.sh')], env=env, check=True)
    DONE.write_text('Installation completed successfully.\n')
    (boot_root() / 'pi-slideshow' / 'setup.json').unlink(missing_ok=True)
    (boot_root() / 'slideshowpi.conf').unlink(missing_ok=True)
    CONFIG.unlink(missing_ok=True)
    run('systemctl', 'disable', UNIT)
    status('Installation complete. Rebooting into the slideshow hotspot. Open http://192.168.50.1/')
    run('systemctl', 'reboot', '--no-block')


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('First-boot setup must run as root.')
    try:
        {'stage': stage, 'install': install}[sys.argv[1]]()
    except Exception as error:
        phase = sys.argv[1] if len(sys.argv) > 1 else 'unknown'
        if phase == 'install':
            status('Installation failed; the service will retry. Check journalctl -u pi-slideshow-install.')
        else:
            status('First-boot staging failed. Check journalctl -u cloud-final and rerun the stage command after repair.')
        # Never print the setup dictionary or environment, which contain secrets.
        traceback.print_exc()
        raise SystemExit(1)
