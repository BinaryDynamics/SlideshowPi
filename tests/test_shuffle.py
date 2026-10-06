import json
from pathlib import Path

from slideshow.playlist import ShufflePlaylist
from slideshow.core import Library
from slideshow.files import Files


def test_many_cycles_visit_every_photo_once_without_boundary_repeat():
    for size in [1, 2, 3, 10, 100]:
        ids = [str(i) for i in range(size)]
        playlist = ShufflePlaylist(ids)
        previous = None
        for cycle in range(15):
            order = [playlist.next() for _ in ids]
            assert len(set(order)) == size and set(order) == set(ids)
            if size > 1: assert order[0] != previous
            previous = order[-1]


def test_previous_retraces_actual_order_across_cycle_boundaries():
    playlist = ShufflePlaylist(['a', 'b', 'c'])
    order = [playlist.next() for _ in range(5)]
    for expected in reversed(order[:-1]): assert playlist.previous() == expected
    assert playlist.previous() == order[0]
    for expected in order[1:]: assert playlist.next() == expected


def test_additions_join_remaining_cycle_and_rescans_do_not_reset_it():
    ids = ['a', 'b', 'c', 'd']
    playlist = ShufflePlaylist(ids)
    shown = [playlist.next(), playlist.next()]
    pending = list(playlist.pending)
    playlist.sync(ids)
    assert playlist.pending == pending
    playlist.sync(ids + ['e', 'f'])
    shown.extend(playlist.next() for _ in range(4))
    assert len(set(shown)) == 6 and set(shown) == set(ids + ['e', 'f'])


def test_removed_photos_never_appear_and_renames_preserve_position():
    playlist = ShufflePlaylist(['a', 'b', 'c', 'd'])
    first = playlist.next()
    expected = list(playlist.pending)
    changes = {expected[0]: 'renamed'}
    playlist.remap(changes)
    available = [first, 'renamed', expected[2]]
    playlist.sync(available)
    assert playlist.pending == ['renamed', expected[2]]
    assert playlist.next() == 'renamed'
    assert playlist.previous() == first
    assert playlist.next() == 'renamed'
    assert playlist.next() == expected[2]


def test_manual_show_consumes_pending_photo_and_history_is_bounded():
    playlist = ShufflePlaylist(['a', 'b', 'c'])
    first = playlist.next()
    chosen = playlist.pending[0]
    playlist.select(chosen)
    assert chosen not in playlist.pending
    assert playlist.next() not in (first, chosen)
    playlist.history = ['a'] * 20000
    playlist.cursor = 19999
    playlist.remember('b')
    assert len(playlist.history) == 20000 and playlist.cursor == 19999


def shuffled_library(tmp_path, count=5):
    sd = tmp_path / 'data/photos'; sd.mkdir(parents=True)
    for i in range(count): (sd / f'{i}.jpg').write_bytes(b'photo')
    (sd.parent / 'settings.json').write_text(json.dumps(dict(shuffle=True)))
    return Library(sd.parent, tmp_path / 'usb')


def test_shuffled_startup_and_settings_rescans_preserve_cycle(tmp_path):
    lib = shuffled_library(tmp_path)
    order = [lib.current]
    for _ in range(4):
        lib.update({'seconds': 20})
        lib.scan()
        lib.advance()
        order.append(lib.current)
    assert len(set(order)) == 5
    lib.advance()
    assert lib.current != order[-1]
    assert lib.shuffle_playlist.previous() == order[-1]


def test_delete_current_uses_next_shuffled_photo_and_preserves_pause(tmp_path):
    lib = shuffled_library(tmp_path)
    item = lib.find(lib.current)
    expected = lib.shuffle_playlist.pending[0]
    lib.control({'action': 'pause'})
    lib.delete(item['id'], item['stamp'])
    assert lib.current == expected and not lib.playing
    assert item['id'] not in lib.shuffle_playlist.pending
    assert item['id'] not in lib.shuffle_playlist.history


def test_file_manager_rename_preserves_cycle_and_delete_next(tmp_path):
    lib = shuffled_library(tmp_path)
    files = Files(lib)
    current_path = Path(lib.find(lib.current)['path'])
    pending = list(lib.shuffle_playlist.pending)
    files.operation('rename', files.entry(current_path), new_name='renamed.jpg')
    lib.scan()
    assert lib.current == lib.image_id(current_path.with_name('renamed.jpg'))
    assert lib.shuffle_playlist.pending == pending
    files.operation('delete', files.entry(current_path.with_name('renamed.jpg')))
    lib.scan()
    assert lib.current == pending[0]


def test_usb_disconnection_prunes_pending_without_replaying_sd_photos(tmp_path, monkeypatch):
    lib = shuffled_library(tmp_path, 3)
    shown = {lib.current}
    drive = lib.usb / 'drive'; drive.mkdir(parents=True)
    (drive / 'usb.jpg').write_bytes(b'photo')
    connected = True
    monkeypatch.setattr('slideshow.core.os.path.ismount', lambda p: connected and Path(p) == drive)
    lib.scan()
    usb_id = lib.image_id(drive / 'usb.jpg')
    assert usb_id in lib.shuffle_playlist.pending
    connected = False; lib.scan()
    assert usb_id not in lib.shuffle_playlist.pending
    for _ in range(2):
        lib.advance()
        assert lib.current not in shown
        shown.add(lib.current)


def test_shuffle_toggle_and_explicit_show_remain_consistent(tmp_path):
    lib = shuffled_library(tmp_path)
    chosen = lib.shuffle_playlist.pending[0]
    lib.control({'action': 'show', 'id': chosen})
    assert not lib.playing and chosen not in lib.shuffle_playlist.pending
    lib.control({'action': 'previous'})
    lib.control({'action': 'next'})
    assert lib.current == chosen
    lib.update({'shuffle': False})
    assert lib.shuffle_playlist is None
    lib.update({'shuffle': True})
    assert lib.current == chosen and chosen not in lib.shuffle_playlist.pending
