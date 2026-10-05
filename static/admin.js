'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="slideshow-token"]').content;
let initialized = false, pending = false, polling = false;
let hotspotIP = '192.168.50.1';
function notice(message, error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); }
async function api(path, data) {
  const response = await fetch('/api/admin/' + path, data === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-Slideshow-Token': token}, body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== 'login') signedIn(false);
    throw new Error(result.error || 'Request failed.');
  }
  return result;
}
function signedIn(value) { $('login-card').hidden = value; $('admin-content').hidden = !value; }
function guarded(action) { return async event => { event?.preventDefault(); try { await action(); } catch (e) { notice(e.message, true); } }; }
function bytes(n) { if (n === undefined || n === null) return 'Unavailable'; return n >= 1073741824 ? (n / 1073741824).toFixed(2) + ' GB' : (n / 1048576).toFixed(1) + ' MB'; }
function elapsed(seconds) { return Math.floor(seconds / 86400) + 'd ' + Math.floor(seconds % 86400 / 3600) + 'h ' + Math.floor(seconds % 3600 / 60) + 'm'; }
function stat(label, value) { const box = document.createElement('div'), term = document.createElement('dt'), detail = document.createElement('dd'); term.textContent = label; detail.textContent = value; box.append(term, detail); $('stats').append(box); }
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const {network: n, stats: s, display: d} = await api('status');
    signedIn(true);
    const h = n.hotspot_network || {ip_address: '192.168.50.1', prefix_length: 24, dhcp_start: '192.168.50.20', dhcp_end: '192.168.50.200'};
    hotspotIP = h.ip_address;
    $('network-summary').textContent = n.mode === 'hotspot' ? 'Hotspot: ' + n.hotspot_ssid : n.mode === 'client' ? 'Connected to Wi-Fi: ' + n.home_ssid : 'Network status unavailable';
    $('operation').textContent = n.message || '';
    $('addresses').replaceChildren();
    for (const address of n.addresses || []) { const p = document.createElement('p'); p.textContent = address.interface + ': http://' + address.address; $('addresses').append(p); }
    if (!initialized) { $('device-country').value = n.country || 'GB'; $('photo-access-enabled').checked = !!n.photo_access_enabled; const response = await fetch('/api/state'); const state = await response.json(); $('admin-folder-list').replaceChildren(); for (const path of state.folders) { const label = document.createElement('label'), input = document.createElement('input'); label.className = 'check'; input.type = 'checkbox'; input.value = path; input.checked = state.settings.folders.includes(path); label.append(input, document.createTextNode(path.replace('/var/lib/pi-slideshow/photos', 'SD / photos').replace('/media/slideshow/', 'USB / '))); $('admin-folder-list').append(label); } for (const id of ['seconds', 'fit']) $(id).value = state.settings[id]; for (const id of ['shuffle', 'recursive']) $(id).checked = state.settings[id]; $('hotspot-ip').value = h.ip_address; $('hotspot-prefix').value = h.prefix_length; $('hotspot-dhcp-start').value = h.dhcp_start; $('hotspot-dhcp-end').value = h.dhcp_end; $('hotspot-ssid').value = n.hotspot_ssid || ''; $('home-ssid').value = n.home_ssid || ''; $('home-security').value = n.home_security || 'wpa'; $('home-hidden').checked = !!n.home_hidden; initialized = true; }
    for (const id of ['switch-hotspot', 'reboot']) $(id).disabled = !!n.busy || pending;
    for (const form of ['hotspot-form', 'home-form', 'device-form', 'password-form', 'photo-access-form']) $(form).querySelector('button').disabled = !!n.busy || pending;
    $('stats').replaceChildren();
    stat('CPU usage', s.cpu_percent == null ? 'Waiting for sample' : s.cpu_percent + '%');
    stat('Load average (1 / 5 / 15 min)', s.load_average ? s.load_average.map(v => v.toFixed(2)).join(' / ') : 'Unavailable');
    stat('CPU cores', s.cpu_cores ?? 'Unavailable');
    stat('Memory used / total', s.memory_total ? bytes(s.memory_total - s.memory_available) + ' / ' + bytes(s.memory_total) : 'Unavailable');
    stat('Memory available', bytes(s.memory_available));
    stat('Swap used / total', s.swap_total != null ? bytes(s.swap_total - s.swap_free) + ' / ' + bytes(s.swap_total) : 'Unavailable');
    stat('CPU temperature', s.temperature_c == null ? 'Unavailable' : s.temperature_c.toFixed(1) + ' °C');
    stat('Uptime', s.uptime_seconds != null ? elapsed(s.uptime_seconds) : 'Unavailable');
    const age = d ? Math.round(Date.now() / 1000 - d.reported_at) : null;
    stat('HDMI player resolution', d ? `${d.width} × ${d.height} (${d.driver})` : 'No player report');
    stat('Last player report', age == null ? 'Unavailable' : age + ' seconds ago');
    stat('Last frame load time', d?.frame_seconds == null ? 'Unavailable' : d.frame_seconds.toFixed(2) + ' seconds');
    stat('Kernel', s.kernel || 'Unavailable');
    let throttle = 'Unavailable';
    if (s.throttling && /^0x[0-9a-f]+$/i.test(s.throttling)) {
      const bits = Number.parseInt(s.throttling, 16), current = [], history = [];
      if (bits & 1) current.push('Low voltage'); if (bits & 2) current.push('Clock capped'); if (bits & 4) current.push('Throttling'); if (bits & 8) current.push('Temperature limit');
      if (bits & 65536) history.push('low voltage'); if (bits & 131072) history.push('clock capped'); if (bits & 262144) history.push('throttling'); if (bits & 524288) history.push('temperature limit');
      throttle = (current.join(', ') || 'No current flags') + (history.length ? '; since boot: ' + history.join(', ') : '') + ' (' + s.throttling + ')';
    }
    stat('Power / throttling', throttle);
    $('storage').replaceChildren();
    for (const disk of s.storage || []) { const p = document.createElement('p'); p.textContent = `${disk.name}: ${bytes(disk.free)} free of ${bytes(disk.total)} (${(100 * disk.used / disk.total).toFixed(1)}% used) · ${disk.path}`; $('storage').append(p); }
    $('services').replaceChildren();
    for (const service of s.services || []) { const p = document.createElement('p'); p.textContent = `${service.Id}: ${service.ActiveState} / ${service.SubState}`; $('services').append(p); }
    $('sample-time').textContent = s.timestamp ? 'Sampled: ' + new Date(s.timestamp * 1000).toLocaleString() : 'Waiting for diagnostics.';
  } finally { polling = false; }
}
async function action(payload, prompt, message) {
  if (!confirm(prompt)) return;
  pending = true;
  try { await api('action', payload); $('hotspot-password').value = ''; $('home-password').value = ''; notice(message); }
  finally { pending = false; }
}
$('login-form').onsubmit = guarded(async () => { await api('login', {password: $('admin-password').value}); $('admin-password').value = ''; notice('Signed in.'); await poll(); });
$('logout').onclick = guarded(async () => { await api('logout', {}); signedIn(false); initialized = false; notice('Signed out.'); });
$('hotspot-form').onsubmit = guarded(() => action({action: 'hotspot-save', ssid: $('hotspot-ssid').value, password: $('hotspot-password').value, hotspot_network: {ip_address: $('hotspot-ip').value.trim(), prefix_length: Number($('hotspot-prefix').value), dhcp_start: $('hotspot-dhcp-start').value.trim(), dhcp_end: $('hotspot-dhcp-end').value.trim()}}, 'Save hotspot settings? If the hotspot is active, your phone will disconnect.', 'Hotspot settings are being saved. Rejoin if disconnected, then open http://' + $('hotspot-ip').value.trim() + '/admin.'));
$('home-form').onsubmit = guarded(() => action({action: 'connect', ssid: $('home-ssid').value, password: $('home-password').value, security: $('home-security').value, hidden: $('home-hidden').checked}, 'Connect to this Wi-Fi and turn off the hotspot?', 'Joining Wi-Fi. Connect your phone to the same network and use the TV’s IP address. If joining fails, the hotspot returns.'));
$('switch-hotspot').onclick = guarded(() => action({action: 'hotspot'}, 'Switch to hotspot mode and leave the existing Wi-Fi network?', 'Switching to hotspot. Join the hotspot and open http://' + hotspotIP + '/admin.'));
$('reboot').onclick = guarded(() => action({action: 'reboot'}, 'Restart the Pi now?', 'Restarting. Wait for the slideshow and Wi-Fi to return.'));
$('refresh').onclick = guarded(poll);
$('home-security').onchange = () => { $('home-password').disabled = $('home-security').value === 'open'; };
poll().catch(() => signedIn(false));
setInterval(() => { if (!$('admin-content').hidden) poll().catch(e => notice(e.message, true)); }, 5000);

$('device-form').onsubmit = guarded(() => action({action: 'device-save', country: $('device-country').value.trim().toUpperCase()}, 'Save wireless country settings? Hotspot clients may disconnect.', 'Device settings are being saved.'));
$('playback-form').onsubmit = guarded(async () => {
  const response = await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Slideshow-Token': token}, body: JSON.stringify({seconds: Number($('seconds').value), fit: $('fit').value, shuffle: $('shuffle').checked, recursive: $('recursive').checked})});
  const result = await response.json(); if (!response.ok) throw new Error(result.error || 'Could not save slideshow settings.'); notice('Slideshow settings saved.');
});
$('password-form').onsubmit = guarded(async () => {
  if ($('new-admin-password').value !== $('confirm-admin-password').value) throw new Error('New passwords do not match.');
  await api('action', {action: 'admin-password', current_password: $('current-admin-password').value, password: $('new-admin-password').value});
  for (const id of ['current-admin-password', 'new-admin-password', 'confirm-admin-password']) $(id).value = '';
  signedIn(false); initialized = false; notice('Password change accepted. Wait a few seconds, then sign in with the new password.');
});

$('admin-folders-form').onsubmit = guarded(async () => {
  const folders = [...$('admin-folder-list').querySelectorAll('input:checked')].map(input => input.value);
  const response = await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Slideshow-Token': token}, body: JSON.stringify({folders})});
  const result = await response.json(); if (!response.ok) throw new Error(result.error || 'Could not save photo folders.'); notice('Photo folders saved.');
});

$('photo-access-form').onsubmit = guarded(async () => { await api('action', {action: 'photo-access-save', enabled: $('photo-access-enabled').checked, password: $('photo-access-password').value}); $('photo-access-password').value = ''; notice('Photo access settings accepted. Changes apply in a few seconds.'); });
