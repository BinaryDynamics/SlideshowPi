"""Linux HDMI-CEC remote input. Kernel CEC handles protocol replies.

ABI: include/uapi/linux/cec.h. No TV power or input-switch commands are sent.
The privileged broker owns the adapter; controls go to the private web listener.
"""
import errno
import json
import os
from pathlib import Path
import select
import struct
import time
from urllib.request import Request, urlopen

# Linux _IOC encoding, stable on Raspberry Pi ARM and arm64.
def ioctl_code(direction, number, size):
    return (direction << 30) | (size << 16) | (ord('a') << 8) | number

GET_ADDRS = ioctl_code(2, 3, 92)
SET_ADDRS = ioctl_code(3, 4, 92)
RECEIVE = ioctl_code(3, 6, 56)
SET_MODE = ioctl_code(1, 9, 4)
ACTIONS = frozenset(('previous', 'next', 'play', 'pause', 'toggle'))
KEYS = {0x00: 'toggle', 0x2b: 'toggle', 0x03: 'previous', 0x04: 'next',
        0x44: 'play', 0x45: 'pause', 0x46: 'pause', 0x48: 'previous',
        0x49: 'next', 0x4b: 'next', 0x4c: 'previous', 0x60: 'play',
        0x61: 'toggle', 0x64: 'pause'}


class Buttons:
    def __init__(self):
        self.held = None
        self.last_seen = 0
        self.last_action = 0

    def receive(self, message, address_mask, now):
        # Accept only directed TV-originated controls for our claimed address.
        if len(message) < 2 or message[0] >> 4 != 0:
            return None
        destination = message[0] & 15
        if destination == 15 or not address_mask & (1 << destination):
            return None
        if message[1] == 0x45:
            self.held = None
            return None
        if message[1] != 0x44 or len(message) < 3:
            return None
        key = message[2]
        repeated = self.held == key and now - self.last_seen < 1.0
        self.held, self.last_seen = key, now
        action = KEYS.get(key)
        if not action:
            return None
        if repeated and (action not in ('next', 'previous') or now - self.last_action < 0.35):
            return None
        self.last_action = now
        return action


def send_control(action):
    if action not in ACTIONS:
        raise ValueError('Unknown CEC control.')
    request = Request('http://127.0.0.1:8081/api/cec-control',
                      data=json.dumps({'action': action}).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
    with urlopen(request, timeout=2) as response:
        if response.status != 200:
            raise OSError('Playback control unavailable.')


class Adapter:
    def __init__(self, path, ioctl=None):
        if ioctl is None:
            import fcntl  # Import only on Linux; configuration tools run on Windows.
            ioctl = fcntl.ioctl
        self.ioctl = ioctl
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        self.owned = False
        self.buttons = Buttons()
        try:
            addresses = self.addresses()
            if addresses[7]:
                if (addresses[7] == 1 and addresses[31] == 4 and addresses[35] == 3
                        and bytes(addresses[16:31]).split(b'\0', 1)[0] == b'SlideshowPi'):
                    # Logical addresses survive fd closure / broker restarts.
                    self.owned = True
                    self.ioctl(self.fd, SET_MODE, struct.pack('=I', 0x21))
                    return
                raise OSError('CEC adapter already used by another application.')
            # One CEC 1.4 playback address. RC passthrough is deliberately off.
            addresses = bytearray(92)
            addresses[6:8] = bytes((5, 1))
            struct.pack_into('=I', addresses, 8, 0xffffffff)
            addresses[16:27] = b'SlideshowPi'
            addresses[31], addresses[35], addresses[39] = 4, 3, 0x10
            self.ioctl(self.fd, SET_ADDRS, addresses)
            self.owned = True
            # Exclusive follower, ordinary initiator: kernel handles CEC replies.
            self.ioctl(self.fd, SET_MODE, struct.pack('=I', 0x21))
        except Exception:
            self.close()
            raise

    def addresses(self):
        result = bytearray(92)
        self.ioctl(self.fd, GET_ADDRS, result)
        return result

    def poll(self, timeout=0.5):
        addresses = self.addresses()
        mask = struct.unpack_from('=H', addresses, 4)[0]
        if not select.select([self.fd], [], [], timeout)[0]:
            return None, bool(mask)
        message = bytearray(56)
        try:
            self.ioctl(self.fd, RECEIVE, message)
        except OSError as error:
            if error.errno == errno.EAGAIN:
                return None, bool(mask)
            raise
        length = struct.unpack_from('=I', message, 16)[0]
        if not message[49] & 1 or not 2 <= length <= 16:
            return None, bool(mask)
        return self.buttons.receive(message[32:32 + length], mask, time.monotonic()), bool(mask)

    def close(self):
        if self.fd is None:
            return
        try:
            if self.owned:
                current = self.addresses()
                if bytes(current[16:31]).split(b'\0', 1)[0] == b'SlideshowPi':
                    self.ioctl(self.fd, SET_ADDRS, bytearray(92))
        except OSError:
            pass
        finally:
            os.close(self.fd)
            self.fd = None


def run(manager, stop=None, adapter_factory=Adapter, send=send_control):
    """Reconcile the saved setting without blocking networking or playback."""
    import threading
    stop = stop or threading.Event()
    adapter = None
    retry_at = 0
    try:
        while not stop.is_set():
            with manager.lock:
                enabled = manager.settings.get('cec_enabled', True)
            status = 'Disabled.' if not enabled else 'No HDMI-CEC adapter found. Retrying automatically.'
            if not enabled:
                if adapter:
                    adapter.close()
                    adapter = None
                retry_at = 0
            elif adapter or time.monotonic() >= retry_at:
                try:
                    if adapter is None:
                        paths = sorted(Path('/dev').glob('cec[0-9]*'))
                        if not paths:
                            raise OSError('No adapter')
                        adapter = adapter_factory(str(paths[0]))
                    action, connected = adapter.poll()
                    status = 'Ready for TV remote controls.' if connected else 'Waiting for HDMI connection / CEC address.'
                    if action:
                        # Re-check after receiving: disabling takes effect immediately.
                        with manager.lock:
                            enabled = manager.settings.get('cec_enabled', True)
                        if enabled:
                            try:
                                send(action)
                            except Exception:
                                status = 'TV remote received; playback service unavailable.'
                except Exception:
                    if adapter:
                        adapter.close()
                        adapter = None
                    retry_at = time.monotonic() + 10
                    status = 'HDMI-CEC unavailable or in use. Retrying automatically.'
            else:
                status = 'HDMI-CEC unavailable or in use. Retrying automatically.'
            with manager.lock:
                manager.cec_status = status
            stop.wait(0.5)
    finally:
        if adapter:
            adapter.close()
