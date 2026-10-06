"""Build a clean installation ZIP, without local photos or development dependencies."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
destination = root / 'dist' / 'SlideshowPi.zip'
destination.parent.mkdir(exist_ok=True)
included = ['slideshow', 'templates', 'static', 'deploy', 'tests', 'tools', '.github', 'docs/screenshots']
files = [root / name for name in ['README.md', 'LICENSE', 'SECURITY.md', 'slideshowpi.conf.example',
                                 'run.py', 'player.py', 'requirements.txt', 'requirements-dev.txt', 'pytest.ini', '.gitignore', '.gitattributes']]
for folder in included:
    files.extend(p for p in (root / folder).rglob('*') if p.is_file() and
                 '__pycache__' not in p.parts and p.suffix != '.pyc' and
                 not p.name.startswith('create_family_card'))
with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(files):
        content = path.read_bytes()
        if path.suffix == '.sh':
            content = content.replace(b'\r\n', b'\n')
        archive.writestr('SlideshowPi/' + path.relative_to(root).as_posix(), content)
print(destination)
