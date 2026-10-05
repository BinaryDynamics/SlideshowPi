"""Unprivileged web-to-admin-service interface; never executes root commands."""
import json
import os
import socket
import threading
import time


class AdminUnavailable(Exception):
    pass


class AdminClient:
    def __init__(self, path=None, timeout=8):
        self.path = path or os.environ.get('SLIDESHOW_ADMIN_SOCKET', '/run/pi-slideshow-admin/control.sock')
        self.timeout = timeout
        self.lock = threading.Lock()
        self.cached = None
        self.updated = 0

    def call(self, action, **payload):
        if not hasattr(socket, 'AF_UNIX'):
            raise AdminUnavailable('Device administration requires the Raspberry Pi.')
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.path)
                connection.sendall((json.dumps(dict(payload, action=action)) + '\n').encode())
                data = b''
                while b'\n' not in data:
                    chunk = connection.recv(4096)
                    if not chunk or len(data) + len(chunk) > 32768:
                        raise AdminUnavailable('Admin service returned an invalid response.')
                    data += chunk
            result = json.loads(data.split(b'\n', 1)[0])
        except (OSError, ValueError) as error:
            raise AdminUnavailable('Device administration is unavailable. Check the admin service.') from error
        if 'error' in result:
            raise ValueError(result['error'])
        return result

    def status(self):
        with self.lock:
            if self.cached is None or time.monotonic() - self.updated > 3:
                try:
                    self.cached = self.call('status')
                except AdminUnavailable:
                    self.cached = {**(self.cached or {}), 'available': False,
                                   'hostname': socket.gethostname(), 'busy': False}
                    self.cached.setdefault('addresses', [])
                    self.cached.setdefault('mode', 'unknown')
                self.updated = time.monotonic()
            return dict(self.cached)
