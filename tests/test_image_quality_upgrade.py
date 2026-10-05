import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('quality_upgrade', ROOT / 'deploy/upgrade_image_quality.py')
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


def legacy_app(root):
    for relative, patches in [('slideshow/web.py', upgrade.WEB_PATCHES),
                              ('player.py', upgrade.PLAYER_PATCHES)]:
        source = (ROOT / relative).read_text()
        # The current web implementation wraps JPEG options over two lines.
        source = source.replace("quality=85 if preview else 95,\n                           subsampling=2 if preview else 0",
                                'quality=85 if preview else 95, subsampling=2 if preview else 0')
        for old, new in patches:
            assert new in source
            source = source.replace(new, old)
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source)


def test_upgrade_preserves_data_and_other_player_changes(tmp_path):
    root = tmp_path / 'app'
    legacy_app(root)
    player = root / 'player.py'
    player.write_text(player.read_text().replace('pygame.FULLSCREEN', 'pygame.NOFRAME'))
    original_player = player.read_bytes()
    photos = tmp_path / 'photos'
    photos.mkdir()
    photo = photos / 'family.jpg'
    photo.write_bytes(b'original upload')
    backup = upgrade.apply(root)
    assert (backup / 'player.py').read_bytes() == original_player
    assert photo.read_bytes() == b'original upload'
    assert 'pygame.FULLSCREEN' not in player.read_text()
    assert 'pygame.transform.smoothscale' in player.read_text()
    assert upgrade.apply(root) is None


def test_unknown_version_does_not_partially_apply(tmp_path):
    legacy_app(tmp_path)
    before = (tmp_path / 'slideshow/web.py').read_bytes()
    (tmp_path / 'player.py').write_text('print("unexpected version")')
    with pytest.raises(RuntimeError):
        upgrade.apply(tmp_path)
    assert (tmp_path / 'slideshow/web.py').read_bytes() == before
