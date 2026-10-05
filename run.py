import argparse
import threading
from waitress import serve
from slideshow.web import create_app

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=80)
    args = parser.parse_args()
    app = create_app()
    # Both listeners share one library/clock. Browser thumbnail requests cannot
    # consume the worker threads used by the local HDMI player.
    def local_playback(environ, start_response):
        # Set by this loopback-only listener, never by a client header.
        environ['slideshow.local_playback'] = True
        return app(environ, start_response)
    threading.Thread(target=serve, kwargs={'app': local_playback, 'host': '127.0.0.1',
                     'port': 8081, 'threads': 2}, daemon=True,
                     name='local-playback-http').start()
    serve(app, host=args.host, port=args.port, threads=2,
          max_request_body_size=32 * 1024 * 1024)
