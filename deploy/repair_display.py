"""One-time bootfs updater for an installed slideshow. Never runs install.sh."""
import hashlib
import os
from pathlib import Path
import shutil
import time
import traceback


def storage_fingerprint(data):
    settings = data / 'settings.json'
    config_hash = hashlib.sha256(settings.read_bytes()).hexdigest() if settings.exists() else None
    entries = []
    for base, dirs, files in os.walk(data / 'photos', followlinks=False):
        for name in files:
            path = Path(base) / name
            stat = path.lstat()
            entries.append((str(path.relative_to(data)), stat.st_size, stat.st_mtime_ns))
    return config_hash, sorted(entries)


def apply_repair(source, boot, app, units, data):
    # Restore the normal boot command line before doing any app work. Even a
    # failed repair must not create a boot loop or leave the Pi in maintenance.
    original = (source / 'cmdline.original').read_bytes()
    if b'root=' not in original or b'systemd.run=' in original:
        raise ValueError('Invalid saved boot command line.')
    with (boot / 'cmdline.txt').open('wb') as out:
        out.write(original)
        out.flush()
        os.fsync(out.fileno())
    if not (app / 'player.py').exists():
        raise RuntimeError('Slideshow is not installed; no application files changed.')
    before = storage_fingerprint(data)
    changes = [(app / 'player.py', 'player.py'), (app / 'run.py', 'run.py'),
               (app / 'deploy' / 'display-client.sh', 'display-client.sh'),
               (app / 'deploy' / 'pi-slideshow-display.service', 'pi-slideshow-display.service'),
               (units / 'pi-slideshow-display.service', 'pi-slideshow-display.service'),
               (app / 'slideshow' / 'web.py', 'web.py')]
    backup = app / 'display-repair-backups' / str(time.time_ns())
    backup.mkdir(parents=True)
    # Validate the whole payload before replacing any installed files.
    manifest = {}
    for line in (source / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ', 1)
        manifest[name] = digest
    for _, name in changes:
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != manifest.get(name):
            raise RuntimeError(f'Repair payload verification failed: {name}')
    for index, (destination, name) in enumerate(changes):
        if destination.exists():
            shutil.copy2(destination, backup / f'{index}-{destination.name}')
        temporary = destination.with_name(destination.name + '.repair-tmp')
        shutil.copyfile(source / name, temporary)
        temporary.chmod(0o755 if name.endswith('.sh') else 0o644)
        os.replace(temporary, destination)
    after = storage_fingerprint(data)
    if before != after:
        raise RuntimeError('Storage fingerprint changed unexpectedly; inspect repair log.')
    return len(before[1])


def main():
    source = Path(__file__).resolve().parent
    boot = source.parent.parent
    report = boot / 'pi-slideshow-display-repair.txt'
    try:
        count = apply_repair(source, boot, Path('/opt/pi-slideshow'),
                             Path('/etc/systemd/system'), Path('/var/lib/pi-slideshow'))
        message = (f'Display logging and resolution update applied. {count} photo files and saved settings preserved.\n'
                   'Normal boot restored. The Pi will reboot automatically.\n'
                   'Read display errors with: sudo journalctl -u pi-slideshow-display -b --no-pager -n 100\n')
        report.write_text(message)
        print(message, flush=True)
    except Exception:
        report.write_text('Display update failed.\n' + traceback.format_exc())
        raise
    finally:
        os.sync()


if __name__ == '__main__':
    main()
