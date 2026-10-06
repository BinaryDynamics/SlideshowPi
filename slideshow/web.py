from io import BytesIO
from functools import wraps
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
from .configuration import HOTSPOT_DEFAULTS

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
    from .files import Files, name as file_name
    files = Files(library)
    app.extensions['files'] = files
    token = secrets.token_urlsafe(32)
    frame_cache = [None, None]

    @app.before_request
    def protect_changes():
        # Captive DNS answers arbitrary domains. Canonicalize before exposing a
        # token or photo, so DNS rebinding cannot turn an external site into a UI.
        host = request.host.split(':', 1)[0].lower()
        network = admin.status()
        hostname = network.get('hostname', '')
        hotspot_ip = network.get('hotspot_network', HOTSPOT_DEFAULTS)['ip_address']
        allowed = {'127.0.0.1', 'localhost', 'slideshow.local'}
        if network.get('mode') in ('hotspot', 'unknown'):
            allowed.add(hotspot_ip)
        allowed.update(item['address'] for item in network.get('addresses', []))
        if hostname:
            allowed.update((hostname.lower(), hostname.lower() + '.local'))
        if host not in allowed:
            address = next((a['address'] for a in network.get('addresses', [])), hotspot_ip)
            return redirect('http://' + address + '/', code=302)
        if request.path == '/api/cec-control':
            if request.environ.get('slideshow.local_playback') is not True or request.method != 'POST':
                return jsonify(error='CEC controls are private to this device.'), 403
            return None
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            if not secrets.compare_digest(request.headers.get('X-Slideshow-Token', ''), token):
                return jsonify(error='Reload this page before making changes.'), 403
        admin_signed_in = time.time() - session.get('admin_since', 0) <= 3600 and session.get('auth_version') == network.get('auth_version')
        local_player = (request.environ.get('slideshow.local_playback') is True and request.method == 'GET'
                        and (request.path == '/api/state' or request.path.startswith('/api/frame/')))
        protected_api = request.path.startswith('/api/') and not request.path.startswith(('/api/admin/', '/api/photo-access/'))
        if request.path in ('/', '/files') or protected_api:
            if not network.get('available', True) and not local_player:
                return jsonify(error='Photo access is temporarily unavailable. Please try again.'), 503
            if network.get('photo_access_enabled') and not admin_signed_in and not local_player:
                valid_photo_session = (time.time() - session.get('photo_since', 0) <= 3600
                                       and session.get('photo_auth_version') == network.get('photo_auth_version'))
                if not valid_photo_session:
                    if request.path in ('/', '/files'):
                        return render_template('photo_login.html', token=token)
                    return jsonify(error='Sign in to manage or view photos.'), 401
        if request.path.startswith('/api/admin/') and request.path != '/api/admin/login':
            if time.time() - session.get('admin_since', 0) > 3600 or session.get('auth_version') != network.get('auth_version'):
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
        return render_template('index.html', token=token, photo_protected=admin.status().get('photo_access_enabled', False))

    @app.get('/files')
    def files_page():
        return render_template('files.html', token=token)

    def storage_mutation(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if not files.lock.acquire(blocking=False):
                raise ValueError('A file operation is running. Wait for it to finish, then retry.')
            try:
                return function(*args, **kwargs)
            finally:
                files.lock.release()
        return wrapped

    @app.get('/api/files')
    def file_listing():
        try: page = int(request.args.get('page', '0'))
        except ValueError: raise ValueError('Invalid page.') from None
        return jsonify(files.listing(request.args.get('path'), page))

    @app.post('/api/files/action')
    def file_action():
        return jsonify(files.start(request.get_json(silent=True))), 202

    @app.get('/api/files/job')
    def file_job():
        return jsonify(job=files.status())

    @app.post('/api/files/folder')
    @storage_mutation
    def file_folder():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict): raise ValueError('Choose a parent and folder name.')
        parent = files.path(payload.get('parent'), directory=True)
        target = parent / file_name(payload.get('name'))
        target.mkdir()  # Never merge with an existing folder or link.
        library.scan()
        return jsonify(ok=True, path=str(target))

    @app.get('/api/files/download')
    def file_download():
        path = files.path(request.args.get('path'))
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            raise ValueError('Choose a photo to download.')
        return send_file(path, as_attachment=True, download_name=path.name)

    @app.get('/api/files/thumbnail')
    def file_thumbnail():
        path = files.path(request.args.get('path'))
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            raise ValueError('Choose a photo to preview.')
        try:
            with DECODE_LOCK, open_image(path) as source:
                source.draft('RGB', (240, 240))
                source.thumbnail((240, 240), Image.Resampling.LANCZOS)
                image = ImageOps.exif_transpose(source).convert('RGB')
                with library.lock:
                    angle = library.settings['rotations'].get(library.image_id(path), 0)
                if angle: image = image.rotate(-angle, expand=True)
                image.thumbnail((240, 160), Image.Resampling.LANCZOS)
                output = BytesIO()
                image.save(output, format='JPEG', quality=82)
                return send_file(BytesIO(output.getvalue()), mimetype='image/jpeg')
        except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ValueError('Cannot decode this photo.') from None

    @app.get('/admin')
    def admin_page():
        return render_template('admin.html', token=token)

    def allow_login_attempt():
        now = time.monotonic()
        # Bound memory and rate-limit both a client and aggregate password attempts.
        with auth_lock:
            for key in list(login_attempts):
                login_attempts[key] = [t for t in login_attempts[key] if now - t < 60]
                if not login_attempts[key]:
                    del login_attempts[key]
            client_key = request.remote_addr
            if len(login_attempts.get(client_key, [])) >= 5 or len(login_attempts.get('*', [])) >= 15:
                return False
            login_attempts.setdefault(client_key, []).append(now)
            login_attempts.setdefault('*', []).append(now)
        return True

    @app.post('/api/photo-access/login')
    def photo_login():
        payload = request.get_json()
        if not isinstance(payload, dict) or not isinstance(payload.get('password'), str):
            raise ValueError('Enter the photo management password.')
        if not allow_login_attempt():
            return jsonify(error='Too many attempts. Wait one minute and try again.'), 429
        if not admin.call('photo-authenticate', password=payload['password']).get('authenticated'):
            return jsonify(error='Incorrect photo management password.'), 401
        session['photo_since'] = time.time()
        session['photo_auth_version'] = admin.status().get('photo_auth_version')
        return jsonify(ok=True)

    @app.post('/api/photo-access/logout')
    def photo_logout():
        session.pop('photo_since', None)
        session.pop('photo_auth_version', None)
        # An admin session also grants photo access: sign out completely.
        session.clear()
        return jsonify(ok=True)

    @app.post('/api/admin/login')
    def admin_login():
        payload = request.get_json()
        if not isinstance(payload, dict) or not isinstance(payload.get('password'), str):
            raise ValueError('Enter your admin password.')
        if not allow_login_attempt():
            return jsonify(error='Too many attempts. Wait one minute and try again.'), 429
        if not admin.call('authenticate', password=payload['password']).get('authenticated'):
            return jsonify(error='Incorrect admin password.'), 401
        session.clear()
        session['admin_since'] = time.time()
        session['auth_version'] = admin.status().get('auth_version')
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
        if not isinstance(payload, dict) or payload.get('action') not in ('reboot', 'hotspot', 'hotspot-save', 'connect', 'admin-password', 'device-save', 'photo-access-save', 'cec-save'):
            raise ValueError('Unknown admin action.')
        result = admin.call(**payload)
        if payload['action'] == 'admin-password':
            session.clear()
        return jsonify(result), 202

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

    @app.post('/api/cec-control')
    def cec_control():
        from .cec import ACTIONS
        network = admin.status()
        if not network.get('available', True) or not network.get('cec_enabled', True):
            return jsonify(error='CEC control is disabled or unavailable.'), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get('action') not in ACTIONS:
            raise ValueError('Unknown CEC control.')
        library.control({'action': payload['action']})
        return jsonify(ok=True)

    @app.post('/api/control')
    def control():
        payload = request.get_json()
        if not isinstance(payload, dict):
            raise ValueError('Expected control object.')
        library.control(payload)
        return jsonify(ok=True)

    @app.post('/api/delete')
    def delete_photo():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError('Choose a photo to delete.')
        with DECODE_LOCK:
            library.delete(payload.get('id'), payload.get('stamp'))
            frame_cache[:] = [None, None]
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
    @storage_mutation
    def upload():
        folder = library.allowed(request.form.get('folder', ''), directory=True)
        file = request.files.get('image')
        if file is None:
            raise ValueError('Choose an image to upload.')
        relative = request.form.get('relative_path', '')
        if relative:
            parts = relative.split('/')
            if len(parts) > 20 or len(parts) < 1:
                raise ValueError('Folder upload is too deeply nested.')
            for part in parts: file_name(part)
            for part in parts[:-1]:
                target_folder = folder / part
                if target_folder.exists(): files.path(str(target_folder), directory=True)
                else: target_folder.mkdir()
                folder = target_folder
            name = parts[-1]
        else:
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
        network = admin.status()
        address = next((a['address'] for a in network.get('addresses', [])),
                       network.get('hotspot_network', HOTSPOT_DEFAULTS)['ip_address'])
        return redirect('http://' + address + '/', code=302)

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
