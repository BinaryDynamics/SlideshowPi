"""Apply the quality update to an existing Pi without replacing its configuration."""
from pathlib import Path
import os
import shutil
import subprocess
import time

WEB_PATCHES = [
    ('scale = min(1, 1280 / width, 1280 / height, math.sqrt(921600 / (width * height)))',
     'scale = min(1, 1920 / width, 1920 / height, math.sqrt(2073600 / (width * height)))'),
    ('source.thumbnail((max(size), max(size)))',
     'source.thumbnail((max(size), max(size)), Image.Resampling.LANCZOS)'),
    ('image = ImageOps.fit(image, size)',
     'image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)'),
    ('image = ImageOps.contain(image, size)',
     'image = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)'),
    ("image.save(output, format='JPEG', quality=85)",
     "image.save(output, format='JPEG', quality=85 if preview else 95, subsampling=2 if preview else 0)"),
]
PLAYER_PATCHES = [
    ('image = pygame.transform.scale(image, screen.get_size())',
     'if image.get_size() != screen.get_size():\n'
     '                        image = pygame.transform.smoothscale(image, screen.get_size())'),
]


def apply(root):
    root = Path(root)
    staged = []
    for relative, replacements in [('slideshow/web.py', WEB_PATCHES), ('player.py', PLAYER_PATCHES)]:
        path = root / relative
        original = path.read_text(encoding='utf-8')
        updated = original
        for old, new in replacements:
            if old in updated:
                if updated.count(old) != 1:
                    raise RuntimeError(f'Ambiguous update in {relative}; nothing changed.')
                updated = updated.replace(old, new)
            elif new not in updated:
                # Accept the same quality change formatted over two lines.
                compact = ' '.join(updated.split())
                if ' '.join(new.split()) not in compact:
                    raise RuntimeError(f'Unexpected version of {relative}; nothing changed.')
        compile(updated, str(path), 'exec')
        if updated != original:
            staged.append((path, relative, updated))
    if not staged:
        return None
    backup = root / 'quality-update-backups' / str(time.time_ns())
    for path, relative, updated in staged:
        saved = backup / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, saved)
    for path, relative, updated in staged:
        path.write_text(updated, encoding='utf-8')
    return backup


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo python3 upgrade_image_quality.py')
    backup = apply('/opt/pi-slideshow')
    print(f'Quality update applied. Backup: {backup}' if backup else 'Quality update already installed.')
    subprocess.run(['systemctl', 'restart', 'pi-slideshow-web'], check=True)
    subprocess.run(['systemctl', 'restart', 'pi-slideshow-display'], check=True)
    print('Slideshow restarted. Photos, settings and HDMI configuration were preserved.')
