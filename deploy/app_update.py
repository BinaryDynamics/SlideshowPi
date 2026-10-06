#!/usr/bin/python3
"""Transactional application updates. Never run package install scripts or touch photos/settings."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys
import time
import uuid
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler
import zipfile

STATE = Path('/var/lib/pi-slideshow-updates')
APP = Path('/opt/pi-slideshow')
INBOX = Path('/var/lib/pi-slideshow/update-inbox')
MAX_ZIP = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
UNITS = ['pi-slideshow-display', 'pi-slideshow-web', 'pi-slideshow-admin']
ACTIVE = {'queued', 'checking', 'downloading', 'validating', 'installing', 'restarting', 'rolling-back'}
TOKEN = re.compile(r'[a-f0-9]{32}')
opener = build_opener(ProxyHandler({}))


def write_status(state=STATE, **values):
    state.mkdir(parents=True, exist_ok=True)
    path = state / 'status.json'
    current = read_status(state)
    current.update(values, updated_at=time.time())
    temp = state / 'status.tmp'
    with temp.open('w', encoding='utf8') as output:
        json.dump(current, output)
        output.flush(); os.fsync(output.fileno())
    os.replace(temp, path)
    if os.name == 'posix':
        fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    return current


def read_status(state=STATE):
    try: return json.loads((state / 'status.json').read_text(encoding='utf8'))
    except (OSError, ValueError): return dict(phase='idle', message='No update has been started.')


def installed_version(app=APP):
    try: return (app / 'VERSION').read_text(encoding='utf8').strip()[:80]
    except OSError: return 'Unknown (install update support first)'


def latest(settings, open_url=None):
    from slideshow.configuration import update_settings
    settings = update_settings(settings)
    slug = settings['repository'].removeprefix('https://github.com/')
    request = Request('https://api.github.com/repos/' + slug + '/releases?per_page=100',
                      headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'SlideshowPi-Updater',
                               'X-GitHub-Api-Version': '2022-11-28'})
    with (open_url or opener.open)(request, timeout=20) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024: raise ValueError('GitHub response too large.')
    releases = json.loads(data)
    if not isinstance(releases, list): raise ValueError('Invalid GitHub release response.')
    eligible = sorted((r for r in releases if not r.get('draft') and r.get('published_at')
                       and (settings['include_prereleases'] or not r.get('prerelease'))),
                      key=lambda r: r['published_at'], reverse=True)
    if not eligible: raise ValueError('No published releases are available for this repository/channel.')
    release = eligible[0]
    asset = next((a for a in release.get('assets', []) if a.get('name') == 'SlideshowPi.zip'), None)
    if not asset: raise ValueError('The newest release has no SlideshowPi.zip asset. Build it with tools/package.py.')
    url = asset.get('browser_download_url', '')
    prefix = settings['repository'] + '/releases/download/'
    if not isinstance(url, str) or not url.lower().startswith(prefix.lower()) or urlparse(url).hostname != 'github.com':
        raise ValueError('Invalid GitHub release asset URL.')
    if type(asset.get('size')) is not int or not 0 < asset['size'] <= MAX_ZIP:
        raise ValueError('Release ZIP is missing or larger than 64 MB.')
    return dict(tag=str(release.get('tag_name', ''))[:80], published_at=release['published_at'],
                repository=settings['repository'], url=url, digest=asset.get('digest'), size=asset['size'])


def download(release, target, open_url=None):
    total, digest = 0, hashlib.sha256()
    request = Request(release['url'], headers={'User-Agent': 'SlideshowPi-Updater'})
    try:
        with (open_url or opener.open)(request, timeout=30) as response, target.open('xb') as output:
            final = urlparse(response.geturl())
            if final.scheme != 'https' or final.hostname not in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
                raise ValueError('Unexpected download redirect.')
            while True:
                chunk = response.read(128 * 1024)
                if not chunk: break
                total += len(chunk)
                if total > MAX_ZIP: raise ValueError('Release ZIP exceeds 64 MB.')
                output.write(chunk); digest.update(chunk)
            output.flush(); os.fsync(output.fileno())
        if total != release['size']: raise ValueError('Incomplete release download.')
        expected = release.get('digest')
        if expected and expected != 'sha256:' + digest.hexdigest(): raise ValueError('GitHub asset checksum mismatch.')
        return digest.hexdigest()
    except Exception:
        target.unlink(missing_ok=True)
        raise


def runtime_file(name):
    path = PurePosixPath(name)
    if name in ('VERSION', 'release.json', 'run.py', 'player.py', 'requirements.txt'): return True
    return (len(path.parts) >= 2 and path.parts[0] in ('slideshow', 'deploy', 'templates', 'static')
            and not any(part.startswith('.') or part == '__pycache__' for part in path.parts))


def archive_bounds(package):
    # Bound the central directory before ZipFile allocates member metadata.
    size = package.stat().st_size
    if not 22 <= size <= MAX_ZIP: raise ValueError('Invalid release ZIP size.')
    with package.open('rb') as source:
        source.seek(max(0, size - 65557))
        tail = source.read(65557)
    index = tail.rfind(b'PK\x05\x06')
    if index < 0 or len(tail) - index < 22: raise ValueError('Not a supported release ZIP.')
    _, disk, central_disk, disk_entries, entries, central_size, offset, comment = struct.unpack_from('<4s4H2IH', tail, index)
    if (disk or central_disk or disk_entries != entries or entries > 2000 or central_size > 1024 * 1024
            or offset == 0xffffffff or offset + central_size > size or index + 22 + comment != len(tail)):
        raise ValueError('Multi-volume, ZIP64 or oversized ZIP directory is not supported.')


def extract(package, destination):
    """Validate every member before extracting allowlisted runtime files."""
    archive_bounds(package)
    with zipfile.ZipFile(package) as archive:
        members = archive.infolist()
        if len(members) > 2000 or sum(m.file_size for m in members) > MAX_EXPANDED or any(m.file_size > 8 * 1024 * 1024 for m in members):
            raise ValueError('Release contains too many files or exceeds 128 MB expanded.')
        entries = {}
        for member in members:
            path = PurePosixPath(member.filename)
            if ('\\' in member.filename or path.is_absolute() or '..' in path.parts or ':' in member.filename
                    or len(path.parts) < 2 or path.parts[0] != 'SlideshowPi'
                    or stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1
                    or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise ValueError('Unsafe or encrypted ZIP entry.')
            if member.is_dir(): continue
            name = str(PurePosixPath(*path.parts[1:]))
            if name in entries: raise ValueError('Duplicate ZIP entry.')
            entries[name] = member
        if 'release.json' not in entries or entries['release.json'].file_size > 512 * 1024:
            raise ValueError('Missing release manifest. Use a release built with the current tools/package.py.')
        manifest = json.loads(archive.read(entries['release.json']))
        if manifest.get('format') != 1 or manifest.get('dependency_set') != 1:
            raise ValueError('This release needs an unsupported updater or dependency upgrade.')
        version = manifest.get('version')
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9.+_-]{1,80}', version):
            raise ValueError('Invalid release version.')
        hashes = manifest.get('files')
        if not isinstance(hashes, dict) or set(hashes) != set(entries) - {'release.json'}:
            raise ValueError('Manifest file list does not match the ZIP.')
        required = {'VERSION', 'run.py', 'player.py', 'slideshow/web.py', 'slideshow/core.py',
                    'deploy/admin_service.py', 'deploy/app_update.py', 'templates/admin.html', 'static/admin.js'}
        if not required.issubset(entries): raise ValueError('Release is missing required application files.')
        destination.mkdir(parents=True)
        destination.chmod(0o755)
        try:
            for name, member in entries.items():
                content = archive.read(member)
                if name != 'release.json' and hashlib.sha256(content).hexdigest() != hashes[name]:
                    raise ValueError('Release file checksum mismatch.')
                if name == 'VERSION' and content.decode('utf8').strip() != version:
                    raise ValueError('Version does not match the manifest.')
                if not runtime_file(name): continue
                if name.endswith('.sh'): content = content.replace(b'\r\n', b'\n')
                if name.endswith('.py'): compile(content, name, 'exec')
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o755 if name.endswith('.sh') else 0o644)
        except Exception:
            shutil.rmtree(destination)
            raise
        for directory in destination.rglob('*'):
            if directory.is_dir(): directory.chmod(0o755)
        return version


def snapshot(token, expected, state=STATE, inbox=INBOX):
    if not isinstance(token, str) or not TOKEN.fullmatch(token) or not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
        raise ValueError('Invalid upload reference.')
    state.mkdir(parents=True, exist_ok=True)
    target = state / (token + '.zip')
    source = inbox / (token + '.zip')
    digest, total = hashlib.sha256(), 0
    try:
        fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as inp, target.open('xb') as out:
            if not stat.S_ISREG(os.fstat(inp.fileno()).st_mode): raise ValueError('Upload must be a regular ZIP file.')
            for chunk in iter(lambda: inp.read(128 * 1024), b''):
                total += len(chunk)
                if total > MAX_ZIP: raise ValueError('Upload exceeds 64 MB.')
                digest.update(chunk); out.write(chunk)
            out.flush(); os.fsync(out.fileno())
        if digest.hexdigest() != expected: raise ValueError('Upload changed or is incomplete.')
        source.unlink(missing_ok=True)
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, timeout=kwargs.get('timeout', 30))


def healthy(runner=run, open_url=None, timeout=75):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for unit in UNITS:
                runner('systemctl', 'is-active', '--quiet', unit)
            with (open_url or opener.open)('http://127.0.0.1:8081/api/health', timeout=3) as response:
                payload = json.loads(response.read(2 * 1024 * 1024))
                if response.status == 200 and isinstance(payload, dict) and payload.get('ok') is True: return True
        except Exception: pass
        time.sleep(2)
    return False


def apply(token, app=APP, state=STATE, runner=run, health=healthy):
    if not TOKEN.fullmatch(token): raise ValueError('Invalid update ID.')
    candidate = state / ('candidate-' + token)
    backup = state / ('backup-' + token)
    moved = False
    try:
        write_status(state, phase='validating', message='Validating release package.', token=token)
        package = state / (token + '.zip')
        if shutil.disk_usage(state).free < MAX_EXPANDED + 64 * 1024 * 1024:
            raise ValueError('At least 192 MB free is required for staging and rollback.')
        version = extract(package, candidate)
        if app.stat().st_dev != candidate.stat().st_dev:
            raise ValueError('Application and update staging must be on the same filesystem.')
        # Imported release code runs as slideshow, never as root; no installer is invoked.
        runner('/usr/bin/setpriv', '--reuid=slideshow', '--regid=slideshow', '--init-groups', '--no-new-privs',
               '/usr/bin/python3', '-c', "import sys; sys.path.insert(0, sys.argv[1]); import slideshow.web, pygame", str(candidate), timeout=30)
        write_status(state, phase='installing', message='Installing; the slideshow will restart.', version=version)
        runner('systemctl', 'stop', *UNITS)
        app.rename(backup); moved = True
        candidate.rename(app)
        write_status(state, phase='restarting', message='Checking restarted application.')
        runner('systemctl', 'start', *reversed(UNITS))
        if not health(): raise ValueError('Updated services did not pass the startup check.')
        previous = read_status(state).get('backup')
        write_status(state, phase='complete', message='Update installed. Sign in again.', backup=backup.name)
        if previous and re.fullmatch(r'backup-[a-f0-9]{32}', previous) and previous != backup.name:
            shutil.rmtree(state / previous, ignore_errors=True)
    except Exception as error:
        if moved:
            write_status(state, phase='rolling-back', message='Update failed; restoring previous application.')
            runner('systemctl', 'stop', *UNITS)
            if app.exists(): app.rename(state / ('failed-' + token))
            backup.rename(app)
            runner('systemctl', 'start', *reversed(UNITS))
        message = str(error) if isinstance(error, ValueError) else 'Update failed. Check storage and the update service log.'
        write_status(state, phase='failed', message=message + (' Previous application restored.' if moved else ' Application unchanged.'))
        raise
    finally:
        (state / (token + '.zip')).unlink(missing_ok=True)
        if candidate.exists(): shutil.rmtree(candidate)
        failed = state / ('failed-' + token)
        if failed.exists(): shutil.rmtree(failed)


def recover(state=STATE, app=APP):
    status = read_status(state)
    token = status.get('token', '')
    if status.get('phase') not in ACTIVE or not isinstance(token, str) or not TOKEN.fullmatch(token): return
    backup = state / ('backup-' + token)
    restored = backup.exists()
    discarded = None
    if restored:
        if app.exists():
            discarded = state / ('interrupted-' + uuid.uuid4().hex)
            app.rename(discarded)
        backup.rename(app)
    if discarded and discarded.exists(): shutil.rmtree(discarded)
    candidate = state / ('candidate-' + token)
    if candidate.exists(): shutil.rmtree(candidate)
    (state / (token + '.zip')).unlink(missing_ok=True)
    write_status(state, phase='failed', message='Interrupted update recovered; previous application restored.' if restored else 'Interrupted update stopped. Please retry.')


def initialize(state=STATE, runner=run):
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o755)  # Candidate code must be readable for unprivileged import checks.
    target = state / 'runner.py'
    shutil.copyfile(Path(__file__).resolve(), target)
    target.chmod(0o600)
    unit = Path('/etc/systemd/system/pi-slideshow-update-recovery.service')
    unit.write_text('[Unit]\nDescription=Recover interrupted SlideshowPi update\nAfter=local-fs.target\nBefore=pi-slideshow-admin.service pi-slideshow-web.service pi-slideshow-display.service\n\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /var/lib/pi-slideshow-updates/runner.py --recover\n\n[Install]\nWantedBy=multi-user.target\n', encoding='utf8')
    runner('systemctl', 'daemon-reload')
    runner('systemctl', 'enable', 'pi-slideshow-update-recovery')


if __name__ == '__main__':
    sys.path.insert(0, str(APP))
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply'); parser.add_argument('--recover', action='store_true'); parser.add_argument('--initialize', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0: raise SystemExit('Updates require the root broker.')
    if args.initialize: initialize()
    elif args.recover: recover()
    elif args.apply: apply(args.apply)
