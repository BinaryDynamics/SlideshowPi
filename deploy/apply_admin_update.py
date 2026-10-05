"""One-time SD updater. Preserves photos, playback settings and HDMI configuration."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import traceback

FILES = ['player.py', 'run.py', 'slideshow/web.py', 'slideshow/admin_client.py',
         'slideshow/configuration.py', 'templates/index.html', 'templates/admin.html', 'templates/photo_login.html',
         'static/admin.js', 'static/app.js', 'static/photo_login.js', 'static/style.css', 'deploy/admin_service.py',
         'deploy/pi-slideshow-admin.service', 'deploy/pi-slideshow-web.service', 'deploy/hotspot-start.sh', 'slideshow/cec.py', 'slideshow/core.py']
NM_CONFIG = '[device-pi-slideshow]\nmatch-device=interface-name:wlan0\nmanaged=0\n'


def fingerprint(data):
    settings = data / 'settings.json'
    digest = hashlib.sha256(settings.read_bytes()).hexdigest() if settings.exists() else None
    photos = []
    for base, dirs, files in os.walk(data / 'photos', followlinks=False):
        for name in files:
            path = Path(base) / name
            stat = path.lstat()
            photos.append((str(path.relative_to(data)), stat.st_size, stat.st_mtime_ns))
    return digest, sorted(photos)


def write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.update-tmp')
    with temporary.open('wb') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def apply(source, boot, app, units, data, config, nm_dir, enable=True):
    original = (source / 'cmdline.original').read_bytes()
    if b'root=' not in original or b'systemd.run=' in original:
        raise ValueError('Invalid saved boot command.')
    write(boot / 'cmdline.txt', original)  # Restore first to prevent maintenance boot loops.
    if not (app / 'player.py').exists() or not (config / 'hostapd.conf').exists():
        raise RuntimeError('Existing slideshow installation missing; no app files changed.')
    before = fingerprint(data)
    manifest = dict((name, digest) for digest, name in
                    (line.split('  ', 1) for line in (source / 'SHA256SUMS').read_text().splitlines()))
    for name in FILES:
        content = (source / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest.get(name):
            raise RuntimeError('Update verification failed: ' + name)
        if name.endswith('.py'):
            compile(content, name, 'exec')
    text_config = boot / 'slideshowpi.conf'
    if not text_config.exists() and not (config / 'admin-auth.json').exists():
        raise RuntimeError('Add slideshowpi.conf to bootfs with your chosen admin password.')
    if text_config.exists():
        spec = importlib.util.spec_from_file_location('update_configuration', source / 'slideshow/configuration.py')
        loader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loader)
        try:
            loader.load_config(text_config)
        except Exception:
            raise ValueError('Invalid slideshowpi.conf; credential input is not logged.') from None
    changes = [(app / name, (source / name).read_bytes(), 0o755 if name.endswith('.sh') else 0o644) for name in FILES]
    changes += [(units / name, (source / 'deploy' / name).read_bytes(), 0o644)
                for name in ['pi-slideshow-admin.service', 'pi-slideshow-web.service']]
    changes += [(units / 'pi-slideshow-display.service.d/admin-update.conf',
                 b'[Service]\nEnvironment=SLIDESHOW_BORDERLESS=1\n', 0o644),
                (nm_dir / '90-pi-slideshow.conf', NM_CONFIG.encode(), 0o644)]
    backup = app / 'admin-update-backups' / str(time.time_ns())
    backup.mkdir(parents=True)
    for index, (destination, content, mode) in enumerate(changes):
        if destination.exists():
            shutil.copy2(destination, backup / f'{index}-{destination.name}')
        write(destination, content, mode)
    if fingerprint(data) != before:
        raise RuntimeError('Photo/settings fingerprint changed unexpectedly.')
    if enable:
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'disable', 'pi-slideshow-hotspot', 'pi-slideshow-ap', 'pi-slideshow-dns'], check=True)
        subprocess.run(['systemctl', 'enable', 'pi-slideshow-admin', 'pi-slideshow-web', 'pi-slideshow-display'], check=True)
    return len(before[1])


def main():
    source = Path(__file__).resolve().parent
    boot = source.parent.parent
    report = boot / 'pi-slideshow-admin-update.txt'
    try:
        count = apply(source, boot, Path('/opt/pi-slideshow'), Path('/etc/systemd/system'),
                      Path('/var/lib/pi-slideshow'), Path('/etc/pi-slideshow'),
                      Path('/etc/NetworkManager/conf.d'))
        report.write_text(f'Admin/IP/diagnostics update installed. {count} photos and settings preserved.\n'
                          'HDMI configuration preserved. Normal boot restored. Rebooting.\n'
                          'Open /admin at the configured hotspot IP (default 192.168.50.1). Use the password in your slideshowpi.conf.\n')
    except Exception:
        report.write_text('Admin update failed. Normal boot restored.\n' + traceback.format_exc())
        raise
    finally:
        os.sync()


if __name__ == '__main__':
    main()
