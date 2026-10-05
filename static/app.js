'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="slideshow-token"]').content;
let state, page = 0, search = '', initialized = false, folderSignature = '', frameKey = '', galleryKey = '', polling = false;
function notice(message, error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); }
async function api(path, data) {
  const response = await fetch('/api/' + path, data === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-Slideshow-Token': token}, body: JSON.stringify(data)
  });
  const result = await response.json();
  if (response.status === 401) { location.reload(); throw new Error('Sign in to manage photos.'); }
  if (!response.ok) throw new Error(result.error || 'Request failed.');
  return result;
}
function guarded(action) { return async event => { event?.preventDefault(); try { await action(); } catch (e) { notice(e.message, true); } }; }
async function control(action, id, degrees) { await api('control', {action, id, degrees}); await poll(); }
function populateFolders(next) {
  const signature = JSON.stringify([next.folders, next.settings.folders]);
  if (folderSignature === signature) return;
  const checked = initialized ? new Set([...$('folder-list').querySelectorAll('input:checked')].map(i => i.value)) : new Set(next.settings.folders);
  folderSignature = signature;
  $('folder-list').replaceChildren();
  for (const path of next.folders) {
    const label = document.createElement('label'), input = document.createElement('input');
    input.type = 'checkbox'; input.value = path; input.checked = checked.has(path);
    label.append(input, document.createTextNode(path.replace('/var/lib/pi-slideshow/photos', 'SD / photos').replace('/media/slideshow/', 'USB / ')));
    $('folder-list').append(label);
  }
  for (const id of ['destination', 'parent']) {
    const selected = $(id).value;
    $(id).replaceChildren(...next.folders.map(path => { const option = document.createElement('option'); option.value = path; option.textContent = path.replace('/var/lib/pi-slideshow/photos', 'SD / photos').replace('/media/slideshow/', 'USB / '); return option; }));
    if (next.folders.includes(selected)) $(id).value = selected;
  }
}
async function loadGallery() {
  const result = await api('images?page=' + page + '&q=' + encodeURIComponent(search));
  const key = JSON.stringify(result);
  if (galleryKey === key) return;
  galleryKey = key;
  $('gallery').replaceChildren();
  for (const photo of result.items) {
    const card = document.createElement('article'); card.className = 'photo';
    const image = document.createElement('img'); image.loading = 'lazy'; image.src = '/api/thumbnail/' + photo.id + '?v=' + encodeURIComponent(photo.stamp + ':' + photo.rotation); image.alt = photo.name;
    const caption = document.createElement('div'); caption.className = 'caption';
    const name = document.createElement('p'); name.textContent = photo.name;
    const buttons = document.createElement('div'); buttons.className = 'buttons';
    const show = document.createElement('button'); show.textContent = 'Show'; show.onclick = guarded(() => control('show', photo.id));
    const rotateLeft = document.createElement('button'); rotateLeft.textContent = '↶ Left'; rotateLeft.setAttribute('aria-label', 'Rotate anticlockwise'); rotateLeft.onclick = guarded(async () => { await control('rotate', photo.id, -90); image.src = '/api/thumbnail/' + photo.id + '?v=' + Date.now(); });
    const rotateRight = document.createElement('button'); rotateRight.textContent = 'Right ↷'; rotateRight.setAttribute('aria-label', 'Rotate clockwise'); rotateRight.onclick = guarded(async () => { await control('rotate', photo.id, 90); image.src = '/api/thumbnail/' + photo.id + '?v=' + Date.now(); });
    buttons.append(show, rotateLeft, rotateRight); caption.append(name, buttons); card.append(image, caption); $('gallery').append(card);
  }
  if (!result.items.length) { const empty = document.createElement('p'); empty.textContent = 'No photos found in the selected folders.'; $('gallery').append(empty); }
  $('page-back').disabled = page === 0;
  $('page-next').disabled = (page + 1) * 36 >= result.total;
  $('page-label').textContent = result.total ? `${page * 36 + 1}–${Math.min((page + 1) * 36, result.total)} of ${result.total}` : '0 photos';
}
async function poll() {
  if (polling) return;
  polling = true;
  try {
    state = await api('state');
    $('connection').textContent = 'Connected';
    $('status').textContent = `${state.playing ? 'Playing' : 'Paused'} · ${state.count} photos · ${state.settings.seconds}s each`;
    $('play').textContent = state.playing ? 'Pause' : 'Play';
    $('current-name').textContent = state.current?.name || 'Ready for photos';
    $('current-preview').hidden = !state.current; $('empty').hidden = !!state.current;
    if (state.current && frameKey !== state.current.frame_key) { frameKey = state.current.frame_key; $('current-preview').src = '/api/frame/' + state.current.id + '?v=' + encodeURIComponent(frameKey); }
    for (const id of ['rotate-left', 'rotate-right', 'next', 'previous']) $(id).disabled = !state.current;
    populateFolders(state);
    $('missing').textContent = state.missing_folders.length ? 'Unavailable selected folders (reconnect drive): ' + state.missing_folders.join(', ') : '';
    if (!initialized) { for (const id of ['seconds', 'fit']) $(id).value = state.settings[id]; for (const id of ['shuffle', 'recursive']) $(id).checked = state.settings[id]; initialized = true; }
    await loadGallery();
  } catch (e) { $('connection').textContent = 'Reconnecting…'; notice(e.message, true); }
  finally { polling = false; }
}
$('play').onclick = guarded(() => control(state?.playing ? 'pause' : 'play'));
$('next').onclick = guarded(() => control('next'));
$('previous').onclick = guarded(() => control('previous'));
$('rotate-left').onclick = guarded(() => control('rotate', undefined, -90));
$('rotate-right').onclick = guarded(() => control('rotate', undefined, 90));
$('playback-form').onsubmit = guarded(async () => { await api('settings', {seconds: Number($('seconds').value), fit: $('fit').value, shuffle: $('shuffle').checked, recursive: $('recursive').checked}); notice('Playback settings saved.'); await poll(); });
$('folders-form').onsubmit = guarded(async () => { await api('settings', {folders: [...$('folder-list').querySelectorAll('input:checked')].map(i => i.value)}); page = 0; galleryKey = ''; notice('Photo folders saved.'); await poll(); });
$('rescan').onclick = guarded(async () => { await api('rescan', {}); galleryKey = ''; await poll(); notice('Storage refreshed.'); });
$('new-folder-form').onsubmit = guarded(async () => { const result = await api('folders', {parent: $('parent').value, name: $('folder-name').value}); await poll(); $('destination').value = result.path; $('folder-name').value = ''; notice('Folder created. Select it under Photo folders to include it in playback.'); });
function upload(file, folder, progress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(), data = new FormData(); data.append('folder', folder); data.append('image', file);
    xhr.open('POST', '/api/upload'); xhr.setRequestHeader('X-Slideshow-Token', token);
    xhr.upload.onprogress = event => { if (event.lengthComputable) progress(event.loaded / event.total); };
    xhr.onerror = () => reject(new Error('Connection lost during upload.'));
    xhr.onload = () => { try { const result = JSON.parse(xhr.responseText); xhr.status >= 200 && xhr.status < 300 ? resolve(result) : reject(new Error(result.error || 'Upload failed.')); } catch { reject(new Error('Upload failed.')); } };
    xhr.send(data);
  });
}
$('upload-form').onsubmit = guarded(async () => {
  const files = [...$('photos').files], folder = $('destination').value;
  $('upload-button').disabled = true; $('progress').hidden = false;
  let done = 0, failures = [];
  try {
    for (const file of files) {
      $('upload-status').textContent = `Uploading ${done + 1} of ${files.length}: ${file.name}`;
      try { if (file.size > 32 * 1024 * 1024 - 4096) throw new Error('Larger than upload limit.'); await upload(file, folder, p => { $('progress').value = 100 * (done + p) / files.length; }); }
      catch (e) { failures.push(`${file.name}: ${e.message}`); }
      done++; $('progress').value = 100 * done / files.length;
    }
    $('upload-status').textContent = `${done - failures.length} uploaded. ${failures.join(' ')}`;
    notice('Upload complete. Make sure the destination folder is selected for playback.', failures.length > 0); galleryKey = ''; await poll();
  } finally { $('upload-button').disabled = false; }
});
$('page-back').onclick = guarded(async () => { page = Math.max(0, page - 1); await loadGallery(); });
$('page-next').onclick = guarded(async () => { page++; await loadGallery(); });
let searchTimer;
$('search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(guarded(async () => { search = $('search').value; page = 0; await loadGallery(); }), 300); };
poll(); setInterval(poll, 2500);

if ($('photo-logout')) $('photo-logout').onclick = guarded(async () => { await api('photo-access/logout', {}); location.reload(); });
