# SlideshowPi

Turn a Raspberry Pi Zero W into a small HDMI photo slideshow appliance. It starts playback automatically, accepts photo uploads over Wi-Fi, and works offline.

## Features

- Select multiple SD-card or USB folders, with optional subfolders.
- Upload JPEG, PNG, WebP or BMP; originals are preserved.
- Play, pause, previous/next, select a specific photo, rotate, shuffle, and set speed.
- Fit the whole photo or fill the screen by cropping; automatic screen-size detection.
- Full HD rendering, Lanczos resizing, high-quality JPEG output and smooth scaling.
- Small current-IP label in the top-left corner of the TV.
- Password-protected `/admin`: restart, change hotspot details, join existing Wi-Fi, or return to hotspot mode.
- Diagnostics: CPU/load, memory/swap, temperature, uptime, storage, services, power flags, HDMI player resolution and frame-loading time.
- Automatic hotspot recovery if joining an existing network fails, or if its Wi-Fi address is lost for 90 seconds.
- Configure application credentials through one private text file: `slideshowpi.conf`.

## Requirements

Use **Raspberry Pi OS Lite (32-bit), Trixie**, installed through Raspberry Pi Imager. The original **Pi Zero W v1.1** is ARMv6 and has 512 MB RAM; 64-bit images and generic ARMv7 Python wheels are unsuitable. This project installs Python/Xorg packages from Raspberry Pi OS repositories. Other Pi models and older Bookworm first-boot formats are not verified.

You need a microSD card (16 GB or larger recommended), a reliable power supply, and a mini-HDMI cable/adapter. For USB storage use an OTG adapter in the data port, or a powered hub for higher-power drives. The Zero W uses **2.4 GHz Wi-Fi**. Installation needs internet access; playback does not.

## Prepare an SD card from Windows

This workflow modifies an **already-flashed** card. It never formats the card. Use it before the Pi's first boot.

1. In Raspberry Pi Imager, select Zero W and flash compatible **Lite 32-bit**. Set a username/password, enable SSH, set your wireless country and an existing Wi-Fi network with internet access. Use `slideshowpi` as the hostname if desired. These OS/SSH settings are separate from the application's admin password.
2. Download/extract the project (or `SlideshowPi.zip`). The extracted folder must contain this README, `player.py`, `slideshow`, `deploy` and `tools`.
3. No application configuration is required. The helper creates complete defaults and unique Wi-Fi/admin passwords, saving a private **`slideshowpi.conf`** beside the application and on bootfs. For custom settings, optionally copy `slideshowpi.conf.example` to `slideshowpi.conf` and edit any values before preparation. Blank passwords are generated automatically. The wireless country is read from Imager’s boot command, falling back to `GB`; select your actual country in Imager or set it explicitly.
4. Install Python **3.11 or newer** on Windows, then run these commands in the extracted project folder:

   ```powershell
   py -m pip install PyYAML
   py tools/prepare_card.py --drive D:\
   ```

   Replace `D:\` with the card's **bootfs** drive letter. To use a configuration stored elsewhere, add `--config path/to/slideshowpi.conf`. Open your private `slideshowpi.conf` and save the generated passwords before booting the Pi. Re-running preparation reuses this file, keeping passwords stable. The helper validates the configuration, copies the application and text file to bootfs, and adds the first-boot command to Imager's cloud-init `user-data`. Existing OS user, SSH and provisioning-network settings are preserved; the original user-data is backed up. It checks file readback. A card without Imager-generated `user-data` and `network-config` is rejected.
5. Safely eject the card, insert it into the Pi, connect HDMI and power it on with the provisioning Wi-Fi available. Keep power connected while packages install. Installation can take **10-30 minutes** on a Zero W. The installer retries failed downloads and then reboots automatically.
6. In hotspot mode, join your configured hotspot, choose to stay connected despite **No internet**, and open **http://192.168.50.1**. The captive-portal prompt is a convenience and depends on the phone; opening the URL manually always remains available. In client mode, connect your phone to the same network and open the **IP shown on the TV**.
7. Upload photos, select folders, and choose playback settings. Open **`/admin`** using the admin password in your private text file. All device/network settings, the admin password, playback options and photo-folder selection can be changed there.

The text file is consumed and removed from bootfs after successful setup. Keep your own secure copy outside Git for recovery. If setup fails, bootfs `pi-slideshow-status.txt` or `journalctl -u pi-slideshow-install -b` provides status. Do not post your real configuration, Imager files, photos or credentials in public issues.

## Configuration reference

See [slideshowpi.conf.example](slideshowpi.conf.example) for the complete format. Omitted hotspot IP settings use the defaults below. Use a private subnet within `10.0.0.0/8`, `172.16.0.0/12` or `192.168.0.0/16`. The Pi and both DHCP endpoints must be usable addresses in the same subnet. The inclusive DHCP range must exclude the Pi's address. For example, set `ip_address = "192.168.60.1"`, `prefix_length = 24`, `dhcp_start = "192.168.60.20"`, and `dhcp_end = "192.168.60.200"` together.

| Setting | Meaning |
| --- | --- |
| `device.country` | Uppercase country code; blank/omitted uses Imager during preparation, fallback `GB` |
| `hotspot.ssid` | Default `SlideshowPi`; 1-32 UTF-8 bytes; spaces supported |
| `hotspot.password` | Blank/omitted generates a unique password; custom values: 8-63 printable ASCII characters |
| `hotspot.ip_address` | Pi's hotspot IPv4 address; default `192.168.50.1` |
| `hotspot.prefix_length` | Subnet prefix, 8-30; default `24` (`255.255.255.0`) |
| `hotspot.dhcp_start` | First address offered to phones; default `192.168.50.20` |
| `hotspot.dhcp_end` | Last address offered to phones; default `192.168.50.200` |
| `admin.password` | Blank/omitted generates a separate unique password; custom values: 12-128 printable ASCII characters |
| `network.mode` | Default `hotspot`; `client` requires actual existing Wi-Fi credentials |
| `home_wifi.ssid` | Saved existing network, optional in hotspot mode |
| `home_wifi.password` | 8-63 character passphrase or 64 hexadecimal PSK for personal WPA/WPA2 |
| `home_wifi.security` | `wpa` or `open` |
| `home_wifi.hidden` | Default `false`; `true` for a hidden network |
| `slideshow.seconds` | Default `10`; 1-3600 seconds per photo |
| `slideshow.shuffle` | Default `false` |
| `slideshow.recursive` | Default `true`; include subfolders |
| `slideshow.fit` | Default `contain` (fit entire image); `cover` fills and crops |

The initial photo folder is SD storage at `/var/lib/pi-slideshow/photos`, playback starts automatically, and HDMI resolution is detected automatically. Existing installations keep playback settings when the supplied file omits `[slideshow]`; an explicit section applies those values while preserving folder selection and per-photo rotations. Existing Wi-Fi uses DHCP. Enterprise authentication, browser-login guest networks and WPA3-only networks are not supported by this form. Some guest networks isolate clients, which prevents phone-to-Pi access. Hotspot and client modes are mutually exclusive on this appliance.

## Admin and networking

Open `http://<current-IP>/admin`. In hotspot mode the IP defaults to `192.168.50.1` or uses your configured `hotspot.ip_address`; in client mode it is assigned by the router and shown on the TV. A small overlay in the upper-left corner shows the network mode and Wi-Fi name above the current IP address, updating even while playback is paused. The web server listens in both modes without restarting when the address changes. `slideshowpi.local` may also work if your OS/network supplies mDNS; use the numeric IP if it does not.

- **Device settings:** change the wireless country; hotspot clients may briefly disconnect. OS login, SSH and hostname remain Raspberry Pi Imager settings.
- **Admin password:** supply the current password and a new 12-128 character password. Sign in again after saving; existing admin sessions are invalidated. Keep your private recovery copy up to date.
- **Slideshow settings:** change speed, fit, shuffle, subfolder scanning and photo-folder selection. Upload and rotation controls are available on the photo page.
- **Restart:** confirms the action, responds to the browser, then restarts the Pi.
- **Hotspot settings:** save the name, password, Pi IP address, subnet prefix and DHCP range. Changing IP settings restarts the hotspot and DHCP; reconnect and open the new IP address. A blank password retains the existing one. Changing an active hotspot disconnects phones, and any printed Wi-Fi QR code must be regenerated.
- **Existing Wi-Fi:** enter its name/security/password and select Save and connect. The hotspot switches off. A blank password reuses the saved password only for the same network. Join that network on your phone and use the TV's new IP.
- **Switch to hotspot:** leaves the existing network and restores the hotspot at its configured IP address.
- **Recovery:** a failed join restores hotspot mode. Losing the client's Wi-Fi IPv4 address for 90 seconds also restores hotspot mode. This does not detect guest-network isolation or an internet outage while Wi-Fi remains connected.
- **Diagnostics:** refreshes every 5 seconds. CPU needs two samples. Power flags distinguish current conditions from events since boot. The HDMI player reports its canvas size and a heartbeat; unavailable readings are labelled accordingly. AP/DNS services being inactive in client mode is expected.

Admin sessions last one hour and are invalidated when the web server restarts. Your slideshow continues while network modes change. A reboot uses the saved mode.

To reset settings without SSH, shut down and copy a completed `slideshowpi.conf` to bootfs. The next normal boot applies it and removes the file. This reapplies all application credentials and network settings in the file. Invalid input retains the working settings. From SSH, the recovery admin password is root-readable:

```sh
sudo cat /etc/pi-slideshow/admin-password.txt
```

## Update an existing installation from an SD card

**Do not run the fresh-install helper on a previously booted Pi.** Shut down the Pi cleanly, insert the card into the computer, and use:

```powershell
py tools/prepare_admin_update.py --drive D:\ --config slideshowpi.conf
```

For a device that has already applied an admin update, omit `--config` to keep all current passwords, network and playback settings. Supply it only when you also want to apply the settings from that file.

This stages an application update on bootfs and arms a one-time maintenance boot. It backs up replaced application/systemd files on the Pi, verifies the payload, preserves uploaded photos and playback settings, and restores the original boot command before applying changes. An explicitly supplied `[slideshow]` section overrides its playback options; omitting it preserves existing choices. It preserves Xorg configuration and existing display-service overrides, including working gamma fixes. The admin service applies your text configuration on the subsequent normal boot. Start in hotspot mode if you want to preserve easy local access.

After staging, safely eject and boot the Pi. Allow the maintenance update and one automatic reboot. Check `pi-slideshow-admin-update.txt` on bootfs if the update fails. A failed update restores normal boot rather than looping in maintenance.

## Photos and display

SD uploads live under `/var/lib/pi-slideshow/photos`. USB filesystems with UUIDs mount under `/media/slideshow/<UUID>`. FAT32, exFAT, NTFS and ext2/3/4 are supported. Linux filesystems retain their own permissions; uploads require directories writable by the `slideshow` account. Already-mounted drives and encrypted volumes are left alone. Disconnecting a selected drive retains its selection for the next insertion.

The app allows at most 32 MB and 24 megapixels per upload and limits the playlist to 10,000 photos. Folder lists have a 3,000-folder limit per storage root. Duplicate upload filenames get a new suffix. Rotation affects display metadata, not the original image. Moving a photo changes its identity. Folder/rotation/speed settings persist, while a paused slideshow resumes playing after reboot.

Full HD is rendered natively. Larger screens keep their aspect ratio but frames are bounded to 2.07 megapixels and a 1920-pixel edge for memory use. The detected display resolution is taken at player startup: restart the display service after manually changing HDMI modes. A very short slide interval may be slower than a large image's decode time on the original Zero W. No animated transitions or video playback are included.

Finish uploads before removing storage. To unplug a USB drive, unmount it over SSH and promptly remove it; the automounter can remount a connected drive. Shut down with `sudo poweroff` before unplugging the Pi's power.

## Troubleshooting

Start with `/admin` diagnostics, then use SSH if needed:

```sh
systemctl status pi-slideshow-admin pi-slideshow-web pi-slideshow-display
journalctl -u pi-slideshow-admin -u pi-slideshow-web -u pi-slideshow-display -b --no-pager
journalctl -b _COMM=python3 --no-pager -n 100
```

- **No page:** use the exact `http://` address shown on the TV. In hotspot mode stay connected despite no internet. A VPN/private DNS setting can interfere with captive-portal detection.
- **No hotspot:** inspect `pi-slideshow-admin`, `pi-slideshow-ap` and `pi-slideshow-dns`; check wireless country and power.
- **Black HDMI screen with a working web page:** distinguish image loading from physical display output. `/var/log/Xorg.0.log` contains Xorg errors. A known modesetting gamma issue can be worked around with `Option "UseGammaLUT" "false"` in the Xorg Device section; the tested configuration is provided in `deploy/20-slideshowpi-rendering.conf`. Restart the display service after installation of that file. Existing SD updates preserve such fixes.
- **Player logs missing from `journalctl -u`:** if using a PAM login session, inspect `loginctl list-sessions` and `journalctl -b _SYSTEMD_SESSION=<session-id>`.
- **Low voltage or throttling:** check the supply/cable and ventilation. Historic flags remain until reboot.
- **Insufficient space:** delete or move photos using SSH; the uploader reserves 64 MB free space.

Internal paths and service names retain `pi-slideshow` for compatibility with existing installations. See [SECURITY.md](SECURITY.md) for access boundaries and credential handling.

## Development and releases

Python 3.11+ is required. On a computer:

```sh
python -m venv .venv
# Activate the virtual environment for your platform.
python -m pip install -r requirements-dev.txt
python -m pytest -q
python tools/package.py
```

`dist/SlideshowPi.zip` is an allowlisted source/provisioning archive. It contains no real credentials, generated cards, photos, attachments or local environment. Do not add any of those to Git. Local UI preview can be started with `SLIDESHOW_DATA` and `SLIDESHOW_USB` pointed to disposable folders, then `python run.py --host 127.0.0.1 --port 8080`; hardware administration is unavailable outside the Pi unless using a test broker.

Automated tests cover playback, uploads, storage boundaries, CSRF/admin authentication, actual-LAN host checks, configuration parsing, root-operation allowlisting, failed Wi-Fi recovery and SD-update preservation. Tests cannot establish real Wi-Fi association, DHCP, boot, HDMI or Pi performance. Slideshow display and quality have been exercised on an original Zero W; the new administration/network-switching release still requires hardware verification.

Licensed under [MIT](LICENSE). Contributions are welcome; include reproduction steps and tests, and remove credentials/photos/private network details from reports.
