"""Small HDMI client. The web service owns the playlist and playback clock."""
from io import BytesIO
import json
import os
import time
from urllib.request import build_opener, ProxyHandler

import pygame
from slideshow.admin_client import AdminClient, AdminUnavailable

BASE = os.environ.get('SLIDESHOW_URL', 'http://127.0.0.1:8081')
urlopen = build_opener(ProxyHandler({})).open


def main():
    print('Initializing HDMI player', flush=True)
    pygame.display.init()
    pygame.font.init()
    # A desktop-sized borderless window avoids exclusive mode changes on bare
    # Xorg. It still covers the TV, without requiring a window manager.
    flags = pygame.NOFRAME if os.environ.get('SLIDESHOW_BORDERLESS') == '1' else pygame.FULLSCREEN
    screen = pygame.display.set_mode((0, 0), flags)
    print(f'Display ready: {pygame.display.get_driver()}, {screen.get_size()}', flush=True)
    pygame.mouse.set_visible(False)
    font = pygame.font.Font(None, 32)
    ip_font = pygame.font.Font(None, max(16, min(24, round(screen.get_height() / 60))))
    base_frame, network_text, drawn_network = None, (), None
    reporter = AdminClient(timeout=0.5)
    next_report, frame_seconds = 0, None
    previous, failed = None, None
    retry_at = 0

    def draw():
        nonlocal drawn_network
        if base_frame is not None:
            screen.blit(base_frame, (0, 0))
        if network_text:
            labels = []
            max_width = max(1, min(screen.get_width() - 28, round(screen.get_width() * 0.35)))
            for line in network_text:
                shortened = line
                while shortened and ip_font.size(shortened)[0] > max_width:
                    shortened = shortened[:-1]
                if shortened != line:
                    shortened = shortened[:-3] + '...'
                labels.append(ip_font.render(shortened, True, (210, 210, 210)))
            line_height = ip_font.get_linesize() + 2
            backing = pygame.Surface((max(label.get_width() for label in labels) + 12,
                                      line_height * len(labels) + 6), pygame.SRCALPHA)
            backing.fill((0, 0, 0, 135))
            for n, label in enumerate(labels):
                backing.blit(label, (6, 4 + n * line_height))
            screen.blit(backing, (8, 8))
        pygame.display.flip()
        drawn_network = network_text

    def message(lines):
        nonlocal base_frame
        screen.fill('black')
        wrapped = []
        for line in lines:
            words, current_line = line.split(), ''
            for word in words:
                candidate = (current_line + ' ' + word).strip()
                if current_line and font.size(candidate)[0] > screen.get_width() - 40:
                    wrapped.append(current_line)
                    current_line = word
                else:
                    current_line = candidate
            wrapped.append(current_line)
        top = max(20, (screen.get_height() - len(wrapped) * 40) // 2)
        for n, line in enumerate(wrapped):
            text = font.render(line, True, 'white')
            screen.blit(text, ((screen.get_width() - text.get_width()) // 2, top + n * 40))
        base_frame = screen.copy()
        draw()

    message(['SlideshowPi is starting...'])
    print('Startup message drawn', flush=True)
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return
            if event.type == pygame.WINDOWEXPOSED:
                draw()
        try:
            with urlopen(BASE + '/api/state', timeout=10) as response:
                state = json.load(response)
            current = state['current']
            network = state.get('network', {})
            from slideshow.network import display_network
            network_text = display_network(network)
            display_size = screen.get_size()
            frame_key = (current['frame_key'], display_size) if current else None
            if not current:
                if previous != 'empty':
                    message(['SlideshowPi', 'Add photos to SD storage or connect a USB drive.',
                             'When a network is available, open the shown IP address to manage photos.'])
                    previous = 'empty'
            elif frame_key != previous:
                if failed == frame_key and time.monotonic() < retry_at:
                    time.sleep(0.25)
                    continue
                try:
                    frame_start = time.monotonic()
                    frame_url = BASE + '/api/frame/' + current['id'] + f'?width={display_size[0]}&height={display_size[1]}'
                    with urlopen(frame_url, timeout=30) as response:
                        image = pygame.image.load(BytesIO(response.read()), 'frame.jpg').convert()
                    if image.get_size() != screen.get_size():
                        image = pygame.transform.smoothscale(image, screen.get_size())
                    base_frame = image
                    draw()
                    frame_seconds = time.monotonic() - frame_start
                    print(f'Frame drawn: {current["name"]}, {display_size}', flush=True)
                    previous, failed = frame_key, None
                except Exception as error:
                    print(f'Image failed: {error}', flush=True)
                    failed, retry_at = frame_key, time.monotonic() + 10
                    message(['Unable to display this image.', 'Choose another image on the configuration page.'])
            if drawn_network != network_text:
                draw()
            if time.monotonic() >= next_report:
                try:
                    reporter.call('display-report', width=screen.get_width(), height=screen.get_height(),
                                  driver=pygame.display.get_driver(), frame_seconds=frame_seconds)
                except (AdminUnavailable, ValueError):
                    pass
                next_report = time.monotonic() + 15
        except Exception as error:
            print(f'Waiting for web service: {error}', flush=True)
            # Keep the last successful frame during a temporary server restart.
            time.sleep(2)
        time.sleep(0.25)


if __name__ == '__main__':
    main()
