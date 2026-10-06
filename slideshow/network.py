"""Small network labels shared with the HDMI player; no hardware imports."""
def display_network(network):
    mode = network.get('mode')
    addresses = ' / '.join(a['address'] for a in network.get('addresses', []))
    if mode == 'hotspot':
        label = 'Hotspot: ' + (network.get('hotspot_ssid') or 'SlideshowPi')
    elif mode == 'client':
        label = 'Wi-Fi: ' + (network.get('home_ssid') or 'connected')
    elif mode == 'wired':
        label = 'Wired network · No Wi-Fi'
    elif mode == 'no-wifi':
        return ('No Wi-Fi', 'Offline slideshow' if not addresses else 'IP: ' + addresses)
    else:
        label = 'Network: connecting...'
    return (label, 'IP: ' + (addresses or 'connecting...'))
