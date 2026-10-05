# Security model

SlideshowPi is a local appliance. Anyone with access to its hotspot or LAN
can view photos, upload images and use slideshow controls by default. Optional
photo-access protection requires a separate password for browser photos and controls.
Photo and admin passwords use salted PBKDF2 hashes; signed-in admins also have photo access. `/admin` additionally
requires a separate password, stored as a salted PBKDF2 hash. Admin sessions expire
after one hour. Login attempts are rate-limited; modifying requests require a
CSRF token. Pages use same-origin scripts and contain no external analytics.

The web server and HDMI player run as the unprivileged `slideshow` account.
A separate root service accepts a fixed list of network/restart operations
over a Unix socket, restricted to root and the slideshow account. It does not
accept shell commands, arbitrary file paths or executable names. USB mounting
and Xorg supervision also use separate root services.

HTTP is unencrypted. Use a trusted network and do not forward the web port
to the internet. Enterprise Wi-Fi and hospital/guest captive-portal logins
are not supported by the personal Wi-Fi form. Client isolation on a guest
network can prevent phones from reaching the Pi even when it has an IP address.

`slideshowpi.conf` contains real credentials and must never be committed.
It is copied into root-owned configuration during setup and deleted from
bootfs after successful application. Private credentials, photos, generated
family cards and machine-specific attachments are excluded from release archives.
Passwords are not supplied to subprocesses as command-line arguments.
An admin password recovery copy is root-readable at
`/etc/pi-slideshow/admin-password.txt`; Wi-Fi profiles are also root-readable.

To reset credentials offline, shut down, insert the card into another computer,
and copy a completed `slideshowpi.conf` onto bootfs. On the next normal boot the
admin service applies it and removes the file. This also reapplies the Wi-Fi
mode and hotspot settings in that file. Invalid configuration is retained
for correction and does not replace the working configuration.

Report sensitive vulnerabilities through GitHub's private vulnerability
reporting when enabled, rather than including passwords, photos or network
details in a public issue.

Default setup generates separate random hotspot and admin passwords; no shared default password is published. Card preparation saves the resolved private configuration for the owner. Changing the admin password requires the current password and invalidates existing admin sessions.

The HDMI player uses a loopback-only listener with an internal server marker allowing only GET state and rendered frames without a browser login. Client headers and a loopback source address do not grant this bypass on the public listener. Browser photo access fails closed while the admin broker is unavailable.
