import json
from pathlib import Path

from PIL import Image

from slideshow.core import Library


def test_new_library_automatically_includes_sd_and_mounted_usb_subfolders(tmp_path, monkeypatch):
    data, usb = tmp_path / 'data', tmp_path / 'usb'
    sd = data / 'photos'; sd.mkdir(parents=True)
    drive = usb / 'drive'; (drive / 'Album').mkdir(parents=True)
    Image.new('RGB', (20, 10), 'blue').save(sd / 'preloaded.jpg')
    Image.new('RGB', (20, 10), 'green').save(drive / 'Album/usb.jpg')
    monkeypatch.setattr('slideshow.core.os.path.ismount', lambda p: Path(p) == drive)
    lib = Library(data, usb)
    assert lib.settings['auto_folders'] is True
    assert {i['name'] for i in lib.images} == {'preloaded.jpg', 'usb.jpg'}
    assert lib.settings['folders'] == [str(sd), str(drive)]
    assert lib.playing


def test_usb_mount_and_unmount_refresh_default_playlist(tmp_path, monkeypatch):
    usb = tmp_path / 'usb'; drive = usb / 'drive'; drive.mkdir(parents=True)
    Image.new('RGB', (20, 10)).save(drive / 'usb.jpg')
    connected = False
    monkeypatch.setattr('slideshow.core.os.path.ismount', lambda p: connected and Path(p) == drive)
    lib = Library(tmp_path / 'data', usb)
    assert not lib.images
    connected = True; lib.scan()
    assert len(lib.images) == 1
    connected = False; lib.scan()
    assert not lib.images and lib.current is None


def test_existing_manual_selection_survives_upgrade_and_can_enable_auto(tmp_path, monkeypatch):
    data = tmp_path / 'data'; sd = data / 'photos'; selected = sd / 'Chosen'; selected.mkdir(parents=True)
    Image.new('RGB', (20, 10)).save(selected / 'chosen.jpg')
    Image.new('RGB', (20, 10)).save(sd / 'other.jpg')
    (data / 'settings.json').write_text(json.dumps(dict(folders=[str(selected)])))
    lib = Library(data, tmp_path / 'usb')
    assert not lib.settings['auto_folders'] and len(lib.images) == 1
    lib.update({'auto_folders': True})
    assert len(lib.images) == 2
    assert Library(data, tmp_path / 'usb').settings['auto_folders'] is True
    lib.update({'folders': [str(selected)]})
    assert not lib.settings['auto_folders'] and len(lib.images) == 1
