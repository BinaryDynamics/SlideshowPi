from io import BytesIO
import math
import os
from pathlib import Path
import secrets
import shutil
import threading
import time
import warnings

from flask import Flask, jsonify, redirect, render_template, request, send_file, session
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.utils import secure_filename

from .core import Library, EXTENSIONS
from .admin_client import AdminClient, AdminUnavailable

Image.MAX_IMAGE_PIXELS = 24_000_000
warnings.simplefilter('error', Image.DecompressionBombWarning)
DECODE_LOCK = threading.Lock()


def display_frame_size(width, height):
    if not 160 <= width <= 7680 or not 160 <= height <= 4320:
        raise ValueError('Invalid display dimensions.')
    # Render Full HD natively; cap larger screens to bound Zero W memory use.
    scale = min(1, 1920 / width, 1920 / height, math.sqrt(2073600 / (width * height)))
    return max(1, round(width * scale)), max(1, round(height * scale))


def open_image(path):
    image = Image.open(path)
    if image.width * image.height > Image.MAX_IMAGE_PIXELS:
        image.close()
        raise ValueError('Images must be 24 megapixels or smaller.')
    return image


def create_app(data=None, usb=None, start_worker=True, admin=None):
    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
    app.secret_key = secrets.token_bytes(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict')
    admin = admin or AdminClient()
    login_attempts = {}
    auth_lock = threading.Lock()
    library = Library(data or os.environ.get('SLIDESHOW_DATA', '/var/lib/pi-slideshow'),
                      usb or os.environ.get('SLIDESHOW_USB', '/media/slideshow'))
    app.extensions['library'] = library
    token = secrets.token_urlsafe(32)
    frame_cache = [None, None]

    @app.before_request
    def protect_changes():
        # Captive DNS answers arbitrary domains. Canonicalize before exposing a
        # token or photo, so DNS rebinding cannot turn an external site into a UI.
        host = request.host.split(':', 1)[0].lower()
        network = admin.status()
        hostname = network.get('hostname', '')
        allowed = {'192.168.50.1', '127.0.0.1', 'localhost', 'slideshow.local'}
        allowed.update(item['address'] for item in network.get('addresses', []))
        if hostname:
            allowed.update((hostname.lower(), hostname.lower() + '.local'))
        if host not in allowed:
            address = next((a['address'] for a in network.get('addresses', [])), '192.168.50.1')
            return redirect('http://' + address + '/', code=302)
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            if not secrets.compare_digest(request.headers.get('X-Slideshow-Token', ''), token):
                return jsonify(error='Reload this page before making changes.'), 403
        if request.path.startswith('/api/admin/') and request.path != '/api/admin/login':
            if time.time() - session.get('admin_since', 0) > 3600:
                return jsonify(error='Sign in to administer this device.'), 401

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
            "connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
        return response

    @app.errorhandler(ValueError)
    def bad_value(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(OSError)
    def storage_error(error):
        app.logger.warning('Storage error: %s', error)
        return jsonify(error='Storage is unavailable, read-only, or full. Check your drive.'), 400

    @app.errorhandler(AdminUnavailable)
    def admin_error(error):
        return jsonify(error=str(error)), 503

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error='Upload one image at a time, up to 32 MB per image.'), 413

    @app.get('/')
    def index():
        return render_template('index.html', token=token)

    @app.get('/admin')
    def admin_page():
        return render_template('admin.html', token=token)

    @app.post('/api/admin/login')
    def admin_login():
        payload = request.get_json()
        if not isinstance(payload, dict) or not isinstance(payload.get('password'), str):
            raise ValueError('Enter your admin password.')
        now = time.monotonic()
        # Bound memory and rate-limit both a client and aggregate password attempts.
        with auth_lock:
            for key in list(login_attempts):
                login_attempts[key] = [t for t in login_attempts[key] if now - t < 60]
                if not login_attempts[key]:
                    del login_attempts[key]
            client_key = request.remote_addr
            if len(login_attempts.get(client_key, [])) >= 5 or len(login_attempts.get('*', [])) >= 15:
                return jsonify(error='Too many attempts. Wait one minute and try again.'), 429
            login_attempts.setdefault(client_key, []).append(now)
            login_attempts.setdefault('*', []).append(now)
        if not admin.call('authenticate', password=payload['password']).get('authenticated'):
            return jsonify(error='Incorrect admin password.'), 401
        session.clear()
        session['admin_since'] = time.time()
        return jsonify(ok=True)

    @app.post('/api/admin/logout')
    def admin_logout():
        session.clear()
        return jsonify(ok=True)

    @app.get('/api/admin/status')
    def admin_status():
        return jsonify(network=admin.status(), **admin.call('diagnostics'))

    @app.post('/api/admin/action')
    def admin_action():
        payload = request.get_json()
        if not isinstance(payload, dict) or payload.get('action') not in ('reboot', 'hotspot', 'hotspot-save', 'connect'):
            raise ValueError('Unknown admin action.')
        return jsonify(admin.call(**payload)), 202

    @app.get('/api/state')
    def state():
        return jsonify(dict(library.state(), network=admin.status()))

    @app.get('/api/images')
    def images():
        page = max(0, request.args.get('page', 0, type=int))
        query = request.args.get('q', '').casefold()
        with library.lock:
            items = [i for i in library.images if query in i['name'].casefold()]
            return jsonify(items=[dict({k: v for k, v in i.items() if k != 'path'},
                                       rotation=library.settings['rotations'].get(i['id'], 0))
                                  for i in items[page * 36:(page + 1) * 36]],
                           total=len(items), page=page)

    @app.post('/api/settings')
    def settings():
        library.update(request.get_json())
        return jsonify(ok=True)

    @app.post('/api/control')
    def control():
        payload = request.get_json()
        if not isinstance(payload, dict):
            raise ValueError('Expected control object.')
        library.control(payload)
        return jsonify(ok=True)

    @app.post('/api/rescan')
    def rescan():
        library.scan()
        return jsonify(ok=True)

    @app.post('/api/folders')
    def folders():
        payload = request.get_json()
        parent = library.allowed(payload.get('parent', ''), directory=True)
        name = secure_filename(payload.get('name', ''))
        if not name or len(name) > 100:
            raise ValueError('Enter a folder name, up to 100 characters.')
        target = library.allowed(parent / name)
        target.mkdir(exist_ok=True)
        library.scan()
        return jsonify(ok=True, path=str(target))

    @app.post('/api/upload')
    def upload():
        folder = library.allowed(request.form.get('folder', ''), directory=True)
        file = request.files.get('image')
        if file is None:
            raise ValueError('Choose an image to upload.')
        name = secure_filename(file.filename or '')
        if Path(name).suffix.lower() not in EXTENSIONS:
            raise ValueError('Use JPEG, PNG, WebP or BMP images.')
        if shutil.disk_usage(folder).free < 64 * 1024 * 1024:
            raise ValueError('Less than 64 MB free. Free some space before uploading.')
        temp = folder / f'.upload-{secrets.token_hex(12)}'
        try:
            file.save(temp)
            try:
                with DECODE_LOCK, open_image(temp) as image:
                    if image.format not in ('JPEG', 'PNG', 'WEBP', 'BMP'):
                        raise ValueError('Unsupported image format.')
                    image.verify()
            except (UnidentifiedImageError, Image.DecompressionBombError,
                    Image.DecompressionBombWarning, SyntaxError) as error:
                raise ValueError('Invalid image or image larger than 24 megapixels.') from error
            # Exclusive creation also works on FAT/exFAT and never overwrites a photo.
            target = library.allowed(folder / name)
            for number in range(10000):
                candidate = target if number == 0 else target.with_name(f'{target.stem}-{number}{target.suffix}')
                try:
                    with candidate.open('xb') as out, temp.open('rb') as source:
                        try:
                            shutil.copyfileobj(source, out)
                            out.flush()
                            os.fsync(out.fileno())
                        except Exception:
                            candidate.unlink(missing_ok=True)
                            raise
                    break
                except FileExistsError:
                    continue
            else:
                raise ValueError('Too many files with that name.')
        finally:
            temp.unlink(missing_ok=True)
        library.scan()
        return jsonify(ok=True, name=candidate.name)

    def render(image_id, size, preview):
        item = library.find(image_id)
        with library.lock:
            angle = library.settings['rotations'].get(image_id, 0)
            fit = library.settings['fit']
        key = (image_id, item['stamp'], angle, fit, size)
        with DECODE_LOCK:
            if not preview and frame_cache[0] == key:
                return frame_cache[1]
            with open_image(item['path']) as source:
                source.draft('RGB', (max(size), max(size)))
                # Shrink before orientation/alpha conversion; otherwise several
                # full-resolution copies can exhaust a Zero W's 512 MB of RAM.
                source.thumbnail((max(size), max(size)), Image.Resampling.LANCZOS)
                image = ImageOps.exif_transpose(source)
                if angle:
                    image = image.rotate(-angle, expand=True)
                # Flatten transparency on black, like the TV background.
                if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                    rgba = image.convert('RGBA')
                    image = Image.new('RGB', rgba.size, 'black')
                    image.paste(rgba, mask=rgba.getchannel('A'))
                else:
                    image = image.convert('RGB')
                if fit == 'cover' and not preview:
                    image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
                else:
                    if preview:
                        image.thumbnail(size)
                    else:
                        image = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
                    if not preview:
                        canvas = Image.new('RGB', size, 'black')
                        canvas.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
                        image = canvas
                output = BytesIO()
                image.save(output, format='JPEG', quality=85 if preview else 95,
                           subsampling=2 if preview else 0)
                result = output.getvalue()
                if not preview:
                    frame_cache[:] = [key, result]
                return result

    @app.get('/api/thumbnail/<image_id>')
    def thumbnail(image_id):
        try:
            return send_file(BytesIO(render(image_id, (240, 160), True)), mimetype='image/jpeg')
        except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ValueError('Cannot decode this image.')

    @app.get('/api/frame/<image_id>')
    def frame(image_id):
        try:
            width = int(request.args.get('width', '1280'))
            height = int(request.args.get('height', '720'))
        except (ValueError, TypeError):
            raise ValueError('Display dimensions must be whole numbers.')
        size = display_frame_size(width, height)
        try:
            return send_file(BytesIO(render(image_id, size, False)), mimetype='image/jpeg')
        except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ValueError('Cannot decode this image.')

    # Android, iOS/macOS and Windows probes deliberately get a redirect, not their success body.
    @app.get('/<path:unknown>')
    def captive(unknown):
        if unknown.startswith('api/'):
            return jsonify(error='Unknown API endpoint.'), 404
        return redirect('http://192.168.50.1/', code=302)

    if start_worker:
        def worker():
            last_scan = time.monotonic()
            while True:
                try:
                    library.tick()
                    if time.monotonic() - last_scan >= 15:
                        library.scan()
                        last_scan = time.monotonic()
                except Exception:
                    app.logger.exception('Library scan failed')
                time.sleep(0.25)
        threading.Thread(target=worker, daemon=True, name='slideshow-clock').start()
    return app
