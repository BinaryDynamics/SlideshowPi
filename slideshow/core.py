import hashlib
import json
import os
from pathlib import Path
import random
import threading
import time

EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
MAX_IMAGES = 10000


class Library:
    def __init__(self, data, usb):
        self.data = Path(data).resolve()
        self.sd = self.data / 'photos'
        self.sd.mkdir(parents=True, exist_ok=True)
        self.usb = Path(usb).resolve()
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.config_file = self.data / 'settings.json'
        self.settings = {'folders': [str(self.sd)], 'seconds': 10, 'shuffle': False,
                         'recursive': True, 'fit': 'contain', 'rotations': {}}
        if self.config_file.exists():
            self.settings.update(json.loads(self.config_file.read_text()))
        self.images = []
        self.folders = []
        self.current = None
        self.playing = True  # Always play after boot; pause is a live control.
        self.deadline = time.monotonic() + self.settings['seconds']
        self.error = ''
        self.scan()

    def allowed(self, value, directory=False):
        path = Path(value).resolve()
        roots = [self.sd]
        # Only existing mounted USB volumes, never the bare mount parent.
        if self.usb.exists():
            roots += [p.resolve() for p in self.usb.iterdir()
                      if p.is_dir() and not p.is_symlink() and os.path.ismount(p)]
        if not any(path == r or r in path.parents for r in roots):
            raise ValueError('Choose a folder on SD storage or a mounted USB drive.')
        if directory and not path.is_dir():
            raise ValueError('Folder is unavailable. Is the USB drive connected?')
        return path

    @staticmethod
    def image_id(path):
        return hashlib.sha256(str(path).encode()).hexdigest()[:24]

    def save(self):
        tmp = self.config_file.with_suffix('.tmp')
        with tmp.open('w', encoding='utf8') as out:
            json.dump(self.settings, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, self.config_file)

    def scan(self):
        with self.scan_lock:
            with self.lock:
                selected = list(self.settings['folders'])
                recursive = self.settings['recursive']
            folders, images, seen = [], [], set()
            roots = [self.sd]
            if self.usb.exists():
                roots += [p for p in self.usb.iterdir() if p.is_dir() and
                          not p.is_symlink() and os.path.ismount(p)]
            for root in roots:
                for base, dirs, files in os.walk(root, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if not d.startswith('.') and
                                     not (Path(base) / d).is_symlink())
                    folders.append(str(Path(base).resolve()))
                    if len(folders) >= 3000:
                        dirs[:] = []
                        break
            for source in selected:
                try:
                    root = self.allowed(source, directory=True)
                except (ValueError, OSError):
                    continue
                for base, dirs, files in os.walk(root, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if not d.startswith('.') and
                                     not (Path(base) / d).is_symlink()) if recursive else []
                    for name in sorted(files, key=str.casefold):
                        path = Path(base) / name
                        if path.is_symlink() or path.suffix.lower() not in EXTENSIONS:
                            continue
                        try:
                            path = self.allowed(path)
                            if path in seen or not path.is_file():
                                continue
                            stat = path.stat()
                        except (OSError, ValueError):
                            continue
                        seen.add(path)
                        images.append({'id': self.image_id(path), 'name': path.name,
                                       'folder': str(path.parent), 'path': str(path),
                                       'stamp': f'{stat.st_mtime_ns}-{stat.st_size}'})
                        if len(images) >= MAX_IMAGES:
                            break
                    if len(images) >= MAX_IMAGES:
                        break
                if len(images) >= MAX_IMAGES:
                    break
            with self.lock:
                self.folders, self.images = sorted(set(folders)), images
                if self.current not in {i['id'] for i in images}:
                    self.current = images[0]['id'] if images else None
                    self.deadline = time.monotonic() + self.settings['seconds']

    def find(self, image_id):
        with self.lock:
            item = next((dict(i) for i in self.images if i['id'] == image_id), None)
        if item is None:
            raise ValueError('Image is no longer in the selected folders.')
        self.allowed(item['path'])
        return item

    def advance(self, direction=1):
        with self.lock:
            ids = [i['id'] for i in self.images]
            if not ids:
                return
            index = ids.index(self.current) if self.current in ids else 0
            if self.settings['shuffle'] and direction == 1 and len(ids) > 1:
                self.current = random.choice([i for i in ids if i != self.current])
            else:
                self.current = ids[(index + direction) % len(ids)]
            self.deadline = time.monotonic() + self.settings['seconds']

    def tick(self):
        with self.lock:
            if self.playing and time.monotonic() >= self.deadline:
                self.advance()

    def update(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Expected settings object.')
        with self.lock:
            updated = dict(self.settings)
            if 'seconds' in payload:
                seconds = payload['seconds']
                if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= 3600:
                    raise ValueError('Slide duration must be from 1 to 3600 seconds.')
                updated['seconds'] = seconds
            for key in ('shuffle', 'recursive'):
                if key in payload:
                    if not isinstance(payload[key], bool):
                        raise ValueError(f'{key} must be true or false.')
                    updated[key] = payload[key]
            if 'fit' in payload:
                if payload['fit'] not in ('contain', 'cover'):
                    raise ValueError('Display mode must be contain or cover.')
                updated['fit'] = payload['fit']
            if 'folders' in payload:
                if not isinstance(payload['folders'], list) or len(payload['folders']) > 100:
                    raise ValueError('Select up to 100 folders.')
                updated['folders'] = list(dict.fromkeys(
                    str(self.allowed(p, directory=True)) for p in payload['folders']))
            self.settings = updated
            self.save()
            self.deadline = time.monotonic() + self.settings['seconds']
        self.scan()

    def control(self, payload):
        action = payload.get('action')
        with self.lock:
            if action in ('play', 'pause', 'toggle'):
                self.playing = not self.playing if action == 'toggle' else action == 'play'
            elif action in ('next', 'previous'):
                self.advance(1 if action == 'next' else -1)
            elif action == 'show':
                self.current = self.find(payload.get('id'))['id']
                self.playing = False  # Hold a chosen image until Play is pressed.
            elif action == 'rotate':
                item = self.find(payload.get('id') or self.current)
                degrees = payload.get('degrees', 90)
                if degrees not in (-90, 90, 180):
                    raise ValueError('Rotation must be -90, 90 or 180 degrees.')
                rotations = self.settings['rotations']
                rotations[item['id']] = (rotations.get(item['id'], 0) + degrees) % 360
                self.save()
            else:
                raise ValueError('Unknown slideshow action.')
            self.deadline = time.monotonic() + self.settings['seconds']

    def state(self):
        with self.lock:
            item = next((i for i in self.images if i['id'] == self.current), None)
            current = None
            if item:
                rotation = self.settings['rotations'].get(item['id'], 0)
                current = {k: v for k, v in item.items() if k != 'path'}
                current['rotation'] = rotation
                current['frame_key'] = f"{item['id']}:{item['stamp']}:{rotation}:{self.settings['fit']}"
            return {'playing': self.playing, 'current': current, 'count': len(self.images),
                    'settings': dict(self.settings, rotations=dict(self.settings['rotations'])),
                    'folders': list(self.folders),
                    'missing_folders': [p for p in self.settings['folders'] if p not in self.folders],
                    'error': self.error, 'limit': MAX_IMAGES}
