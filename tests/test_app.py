from io import BytesIO
import re
import time

from PIL import Image
import pytest

from slideshow.web import create_app
from slideshow.core import Library
from test_admin import FakeAdmin


@pytest.fixture
def appliance(tmp_path):
    broker = FakeAdmin()
    broker.status = lambda: dict(available=True, mode='hotspot', addresses=[dict(interface='wlan0', address='192.168.50.1')])
    app = create_app(tmp_path / 'data', tmp_path / 'usb', start_worker=False, admin=broker)
    app.config['TESTING'] = True
    client = app.test_client()
    token = re.search(rb'name="slideshow-token" content="([^"]+)"', client.get('/').data)[1].decode()
    return client, app.extensions['library'], {'X-Slideshow-Token': token}


def jpeg(color='red'):
    data = BytesIO()
    Image.new('RGB', (80, 40), color).save(data, 'JPEG')
    data.seek(0)
    return data


def upload(client, lib, headers, name='photo.jpg'):
    return client.post('/api/upload', headers=headers,
                       data={'folder': str(lib.sd), 'image': (jpeg(), name)})


def test_upload_playback_rotation_and_restart(appliance):
    client, lib, headers = appliance
    assert upload(client, lib, headers).status_code == 200
    item = client.get('/api/state').json['current']
    assert client.post('/api/control', headers=headers,
                       json={'action': 'show', 'id': item['id']}).status_code == 200
    assert not client.get('/api/state').json['playing']
    client.post('/api/control', headers=headers, json={'action': 'rotate', 'id': item['id'], 'degrees': 90})
    assert client.get('/api/state').json['current']['rotation'] == 90
    assert Image.open(BytesIO(client.get('/api/frame/' + item['id']).data)).size == (1280, 720)
    assert client.get('/api/thumbnail/' + item['id']).status_code == 200
    rebooted = Library(lib.data, lib.usb)
    assert rebooted.playing
    assert rebooted.state()['current']['rotation'] == 90


def test_duplicate_upload_preserves_original(appliance):
    client, lib, headers = appliance
    assert upload(client, lib, headers).json['name'] == 'photo.jpg'
    original = (lib.sd / 'photo.jpg').read_bytes()
    assert upload(client, lib, headers).json['name'] == 'photo-1.jpg'
    assert (lib.sd / 'photo.jpg').read_bytes() == original
    assert len(lib.images) == 2
    assert not list(lib.sd.glob('.upload-*'))


def test_csrf_and_captive_redirects(appliance):
    client, lib, headers = appliance
    assert client.post('/api/control', json={'action': 'pause'}).status_code == 403
    for path in ['/generate_204', '/hotspot-detect.html', '/connecttest.txt']:
        result = client.get(path, headers={'Host': 'connectivitycheck.gstatic.com'})
        assert result.status_code == 302
        assert result.location == 'http://192.168.50.1/'
    assert client.get('/', headers={'Host': 'attacker.example'}).status_code == 302
    assert client.get('/api/no-such-endpoint').status_code == 404


def test_storage_traversal_and_unmounted_usb_rejected(appliance, tmp_path):
    client, lib, headers = appliance
    for path in [str(tmp_path), str(lib.sd / '..'), str(lib.usb / 'fake-volume')]:
        result = client.post('/api/settings', headers=headers, json={'folders': [path]})
        assert result.status_code == 400
    result = client.post('/api/upload', headers=headers,
                         data={'folder': str(tmp_path), 'image': (jpeg(), 'outside.jpg')})
    assert result.status_code == 400
    assert not (tmp_path / 'outside.jpg').exists()


def test_invalid_and_oversize_images(appliance):
    client, lib, headers = appliance
    for name, content in [('invalid.jpg', BytesIO(b'not an image')), ('script.html', jpeg())]:
        assert client.post('/api/upload', headers=headers,
                           data={'folder': str(lib.sd), 'image': (content, name)}).status_code == 400
    data = BytesIO()
    Image.new('1', (6000, 5000)).save(data, 'PNG')
    data.seek(0)
    assert client.post('/api/upload', headers=headers,
                       data={'folder': str(lib.sd), 'image': (data, 'huge.png')}).status_code == 400
    assert len(lib.images) == 0


def test_recursive_selection_clock_and_validation(appliance):
    client, lib, headers = appliance
    sub = lib.sd / 'album'
    sub.mkdir()
    (lib.sd / 'one.jpg').write_bytes(jpeg().getvalue())
    (sub / 'two.jpg').write_bytes(jpeg('blue').getvalue())
    lib.scan()
    assert len(lib.images) == 2
    previous = lib.current
    lib.deadline = time.monotonic() - 1
    lib.tick()
    assert lib.current != previous
    lib.control({'action': 'pause'})
    previous = lib.current
    lib.deadline = time.monotonic() - 1
    lib.tick()
    assert lib.current == previous
    assert client.post('/api/settings', headers=headers,
                       json={'folders': [str(lib.sd), str(sub)]}).status_code == 200
    assert len(lib.images) == 2  # Overlapping folder selections do not duplicate images.
    client.post('/api/settings', headers=headers, json={'folders': [str(lib.sd)], 'recursive': False})
    assert len(lib.images) == 1
    for payload in [{'seconds': 0}, {'seconds': True}, {'fit': 'bad'}, {'shuffle': 'yes'}, {'folders': 'bad'}]:
        assert client.post('/api/settings', headers=headers, json=payload).status_code == 400


def test_folder_creation_sanitizes_name(appliance):
    client, lib, headers = appliance
    result = client.post('/api/folders', headers=headers,
                         json={'parent': str(lib.sd), 'name': '../../Holiday Photos'})
    assert result.status_code == 200
    assert (lib.sd / 'Holiday_Photos').is_dir()


def test_missing_folder_restored_and_removed_file(appliance):
    client, lib, headers = appliance
    upload(client, lib, headers)
    (lib.sd / 'photo.jpg').unlink()
    lib.scan()
    assert lib.current is None
    assert client.get('/api/state').json['count'] == 0


def test_exif_orientation_and_alpha(appliance):
    client, lib, headers = appliance
    image = Image.new('RGB', (80, 40), 'green')
    exif = Image.Exif()
    exif[274] = 6
    image.save(lib.sd / 'oriented.jpg', exif=exif)
    Image.new('RGBA', (20, 20), (255, 0, 0, 0)).save(lib.sd / 'transparent.png')
    lib.scan()
    oriented = next(i for i in lib.images if i['name'] == 'oriented.jpg')
    thumb = Image.open(BytesIO(client.get('/api/thumbnail/' + oriented['id']).data))
    assert thumb.height > thumb.width
    transparent = next(i for i in lib.images if i['name'] == 'transparent.png')
    frame = Image.open(BytesIO(client.get('/api/frame/' + transparent['id']).data))
    assert frame.getpixel((640, 360)) == (0, 0, 0)


@pytest.mark.parametrize('size', [(640, 480), (800, 600), (1280, 720), (1920, 1080), (720, 1280)])
def test_screen_aspect_ratio_and_fit(appliance, size):
    from slideshow.web import display_frame_size
    client, lib, headers = appliance
    upload(client, lib, headers)
    image_id = lib.current
    width, height = size
    response = client.get(f'/api/frame/{image_id}?width={width}&height={height}')
    assert response.status_code == 200
    frame = Image.open(BytesIO(response.data))
    assert frame.size == display_frame_size(*size)
    assert abs(frame.width / frame.height - width / height) < .003
    # A 2:1 photo has black bars at the top on each of these screen shapes.
    assert frame.getpixel((frame.width // 2, 0)) == (0, 0, 0)
    client.post('/api/settings', headers=headers, json={'fit': 'cover'})
    filled = Image.open(BytesIO(client.get(f'/api/frame/{image_id}?width={width}&height={height}').data))
    assert filled.getpixel((filled.width // 2, 0))[0] > 240


def test_invalid_screen_dimensions(appliance):
    client, lib, headers = appliance
    upload(client, lib, headers)
    for query in ('width=0', 'height=-1', 'width=no', 'width=999999'):
        assert client.get(f'/api/frame/{lib.current}?{query}').status_code == 400


def test_full_hd_preserves_fine_detail(appliance):
    client, lib, headers = appliance
    # Two-pixel stripes would lose contrast if reduced to 720p before display.
    image = Image.new('RGB', (1920, 1080))
    image.putdata([(255, 255, 255) if x % 4 < 2 else (0, 0, 0)
                   for y in range(1080) for x in range(1920)])
    image.save(lib.sd / 'detail.png')
    lib.scan()
    frame = Image.open(BytesIO(client.get(
        f'/api/frame/{lib.current}?width=1920&height=1080').data))
    assert frame.size == (1920, 1080)
    assert frame.getpixel((1000, 500))[0] > 240
    assert frame.getpixel((1002, 500))[0] < 15


def test_large_screen_frame_is_bounded():
    from slideshow.web import display_frame_size
    assert display_frame_size(3840, 2160) == (1920, 1080)
    assert display_frame_size(1080, 1920) == (1080, 1920)
