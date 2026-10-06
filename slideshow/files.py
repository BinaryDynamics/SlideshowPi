"""Bounded photo storage operations; asynchronous jobs keep the Pi UI responsive."""
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import threading
import time

from .core import EXTENSIONS


def name(value):
    if (not isinstance(value, str) or not value.strip() or value in ('.', '..')
            or value.startswith('.') or len(value.encode('utf8')) > 180
            or any(c in value for c in '/\\<>:"|?*') or any(ord(c) < 32 for c in value)
            or value.endswith((' ', '.'))):
        raise ValueError('Use a name of up to 180 UTF-8 bytes without slashes or special characters.')
    return value


def stamp(path):
    stat = path.stat()
    return f'{stat.st_mtime_ns}-{stat.st_size}'


class Files:
    def __init__(self, library):
        self.library = library
        self.lock = threading.RLock()
        self.job_lock = threading.Lock()
        self.job = None

    def roots(self):
        lib = self.library
        roots = [dict(path=str(lib.sd), name='SD / photos')]
        if lib.usb.exists():
            roots += [dict(path=str(p.resolve()), name='USB / ' + p.name)
                      for p in sorted(lib.usb.iterdir()) if p.is_dir() and not p.is_symlink()
                      and os.path.ismount(p)]
        return roots

    def path(self, value, directory=False):
        if not isinstance(value, str) or not value:
            raise ValueError('Choose an available storage location.')
        raw = Path(value)
        resolved = self.library.allowed(raw, directory=directory)
        if not raw.is_absolute() or raw != resolved or raw.is_symlink():
            raise ValueError('Links and paths outside photo storage are not supported.')
        return resolved

    def entry(self, path):
        return dict(path=str(path), name=path.name, folder=path.is_dir(),
                    stamp=stamp(path), size=path.stat().st_size if path.is_file() else None,
                    modified=path.stat().st_mtime)

    def listing(self, value=None, page=0):
        roots = self.roots()
        path = self.path(value or roots[0]['path'], directory=True)
        root = next(Path(r['path']) for r in roots if Path(r['path']) == path or Path(r['path']) in path.parents)
        entries, ignored = [], 0
        with os.scandir(path) as children:
            for child in children:
                if child.name.startswith('.') or child.is_symlink():
                    ignored += 1
                    continue
                if child.is_dir(follow_symlinks=False) or child.is_file(follow_symlinks=False) and Path(child.name).suffix.lower() in EXTENSIONS:
                    entries.append(self.entry(Path(child.path)))
                else:
                    ignored += 1
                if len(entries) > 20000:
                    raise ValueError('This folder has too many entries. Split it into smaller folders.')
        entries.sort(key=lambda e: (not e['folder'], e['name'].casefold()))
        if type(page) is not int or not 0 <= page <= 100:
            raise ValueError('Invalid page.')
        crumbs = [dict(path=str(root), name=next(r['name'] for r in roots if r['path'] == str(root)))]
        parent = root
        for part in path.relative_to(root).parts:
            parent = parent / part
            crumbs.append(dict(path=str(parent), name=part))
        return dict(path=str(path), roots=roots, breadcrumbs=crumbs,
                    parent=str(path.parent) if path != root else None,
                    items=entries[page * 100:(page + 1) * 100], total=len(entries), page=page,
                    ignored=ignored, selected=str(path) in self.library.settings['folders'])

    def source(self, entry):
        if not isinstance(entry, dict):
            raise ValueError('Choose files or folders from the current listing.')
        path = self.path(entry.get('path'))
        if str(path) in {r['path'] for r in self.roots()}:
            raise ValueError('Storage roots cannot be renamed, moved or deleted.')
        if not path.exists() or stamp(path) != entry.get('stamp'):
            raise ValueError('File or folder changed. Refresh before trying again.')
        if not path.is_dir() and (not path.is_file() or path.suffix.lower() not in EXTENSIONS):
            raise ValueError('Only photo files and photo folders can be managed.')
        return path

    def snapshot(self, path):
        entries = [(path, stamp(path), path.is_dir())]
        if path.is_dir():
            for base, dirs, files in os.walk(path, followlinks=False):
                for item in sorted(dirs + files):
                    child = Path(base) / item
                    self.path(str(child))
                    if (child.name.startswith('.') or not child.is_dir()
                            and (not child.is_file() or child.suffix.lower() not in EXTENSIONS)):
                        raise ValueError('This folder contains hidden, linked or non-photo files. Manage those files separately first.')
                    entries.append((child, stamp(child), child.is_dir()))
                    if len(entries) > 20000:
                        raise ValueError('Folder operation exceeds the 20,000 entry limit.')
        return entries

    def metadata(self, entries, source, target=None, copy=False):
        lib = self.library
        with lib.lock:
            rotations = lib.settings['rotations']
            removed_ids = {lib.image_id(p) for p, _, directory in entries if not directory}
            old_current = lib.current
            for path, _, directory in entries:
                if directory:
                    continue
                old = lib.image_id(path)
                new = lib.image_id(target if path == source else target / path.relative_to(source)) if target else None
                if old in rotations:
                    value = rotations[old]
                    if not copy: rotations.pop(old)
                    if new: rotations[new] = value
                if not copy and lib.current == old:
                    lib.current = new
            if not copy and target is None and old_current in removed_ids:
                ids = [image['id'] for image in lib.images]
                start = ids.index(old_current) if old_current in ids else 0
                lib.current = next((ids[(start + offset) % len(ids)] for offset in range(1, len(ids) + 1)
                                    if ids[(start + offset) % len(ids)] not in removed_ids), None)
            if not copy:
                folders = []
                for folder in lib.settings['folders']:
                    p = Path(folder)
                    if p == source or source in p.parents:
                        if target: folders.append(str(target if p == source else target / p.relative_to(source)))
                    else: folders.append(folder)
                lib.settings['folders'] = list(dict.fromkeys(folders))
            lib.save()

    def verify(self, entries):
        for path, expected, directory in entries:
            self.path(str(path), directory=directory)
            if not path.exists() or path.is_dir() != directory or stamp(path) != expected:
                raise ValueError('Source changed during the operation; original retained.')

    @staticmethod
    def digest(path):
        h = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(128 * 1024), b''):
                h.update(chunk)
        return h.digest()

    def destination(self, source, folder, conflict):
        target = folder / source.name
        if target.exists():
            if conflict == 'skip': return None
            for index in range(1, 10000):
                candidate = folder / (f'{source.stem} ({index}){source.suffix}' if source.is_file() else f'{source.name} ({index})')
                if not candidate.exists(): return candidate
            raise ValueError('Too many duplicate names.')
        return target

    @staticmethod
    def same_device(source, target):
        return source.stat().st_dev == target.parent.stat().st_dev

    def operation(self, action, entry, destination=None, conflict='keep-both', new_name=None):
        lib = self.library
        with lib.scan_lock:
            source = self.source(entry)
            entries = self.snapshot(source)
            target = None
            if action == 'rename':
                target = source.with_name(name(new_name))
                if source.is_file() and target.suffix.lower() not in EXTENSIONS:
                    raise ValueError('Keep a supported photo extension.')
                if target == source: return 'unchanged'
                if target.exists(): raise ValueError('That name already exists. Choose another name.')
            elif action in ('move', 'copy'):
                folder = self.path(destination, directory=True)
                if folder == source or source in folder.parents:
                    raise ValueError('Cannot move or copy a folder inside itself.')
                if action == 'move' and folder == source.parent: return 'unchanged'
                target = self.destination(source, folder, conflict)
                if target is None: return 'skipped'
            if action == 'delete':
                self.verify(entries)
                for path, _, directory in reversed(entries):
                    path.rmdir() if directory else path.unlink()
            elif action == 'rename' or action == 'move' and self.same_device(source, target):
                self.verify(entries)
                source.rename(target)
            else:
                size = sum(path.stat().st_size for path, _, directory in entries if not directory)
                if shutil.disk_usage(target.parent).free < size + 64 * 1024 * 1024:
                    raise ValueError('Not enough free space for a verified copy (including 64 MB reserve).')
                stage = target.parent / ('.transfer-' + secrets.token_hex(12))
                try:
                    if source.is_dir(): stage.mkdir()
                    for path, _, directory in entries:
                        dest = stage if path == source else stage / path.relative_to(source)
                        if directory:
                            dest.mkdir(exist_ok=True)
                        else:
                            with path.open('rb') as inp, dest.open('xb') as out:
                                shutil.copyfileobj(inp, out, 128 * 1024)
                                out.flush()
                                os.fsync(out.fileno())
                            shutil.copystat(path, dest)
                            if self.digest(path) != self.digest(dest):
                                raise ValueError('Copy verification failed; original retained.')
                    self.verify(entries)
                    if target.exists(): raise ValueError('Destination changed; choose another name.')
                    stage.rename(target)
                    if action == 'move':
                        for path, _, directory in reversed(entries):
                            path.rmdir() if directory else path.unlink()
                finally:
                    if stage.exists():
                        shutil.rmtree(stage) if stage.is_dir() else stage.unlink()
            self.metadata(entries, source, target, copy=action == 'copy')
        return 'done'

    def start(self, payload):
        if not isinstance(payload, dict): raise ValueError('Expected a file operation.')
        action = payload.get('action')
        if action not in ('move', 'copy', 'rename', 'delete'): raise ValueError('Unknown file operation.')
        entries = payload.get('items')
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError('Select between 1 and 100 entries.')
        sources = [self.source(entry) for entry in entries]
        if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(sources) for b in sources[i + 1:]):
            raise ValueError('Select each entry once without selecting both a folder and its contents.')
        if action == 'rename' and len(entries) != 1: raise ValueError('Rename one entry at a time.')
        conflict = payload.get('conflict', 'keep-both')
        if conflict not in ('skip', 'keep-both'): raise ValueError('Choose Skip or Keep both for conflicts.')
        if action in ('move', 'copy'): self.path(payload.get('destination'), directory=True)
        if action == 'rename': name(payload.get('name'))
        with self.job_lock:
            if self.job and self.job['running']: raise ValueError('A file operation is already running. Please wait.')
            self.job = dict(id=secrets.token_hex(12), running=True, action=action, done=0,
                            total=len(entries), results=[], current='Starting', started=time.time())
            identifier = self.job['id']
        threading.Thread(target=self.work, args=(payload, identifier), daemon=True, name='file-operation').start()
        return dict(id=identifier)

    def work(self, payload, identifier):
        with self.lock:
            for entry in payload['items']:
                label = Path(entry['path']).name
                with self.job_lock: self.job['current'] = label
                try:
                    result = self.operation(payload['action'], entry, payload.get('destination'),
                                            payload.get('conflict', 'keep-both'), payload.get('name'))
                    message = result
                except ValueError as error:
                    result, message = 'failed', str(error)
                except OSError:
                    result, message = 'failed', 'Storage unavailable, entry changed, conflict, or folder contains unsupported files. Refresh and check the source/destination.'
                except Exception:
                    result, message = 'failed', 'Operation failed. Refresh storage before retrying.'
                finally:
                    try: self.library.scan()
                    except Exception: pass
                with self.job_lock:
                    self.job['done'] += 1
                    self.job['results'].append(dict(name=label, result=result, message=message))
            with self.job_lock:
                self.job['running'], self.job['current'] = False, 'Finished'

    def status(self):
        with self.job_lock:
            return dict(self.job, results=list(self.job['results'])) if self.job else None
