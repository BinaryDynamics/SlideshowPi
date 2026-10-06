'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="slideshow-token"]').content;
let listing, page = 0, selected = new Map(), clipboard = [], lastIndex = null;
let destinationPath, destinationAction, jobRunning = false, jobId = '', loading = false;
let uploadQueue = [], uploading = false, cancelUploads = false, activeXHR;
function notice(message, error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); }
function guarded(fn) { return async event => { event?.preventDefault(); try { await fn(event); } catch (e) { notice(e.message, true); } }; }
async function api(path, data) {
  const response = await fetch('/api/' + path, data === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Slideshow-Token': token}, body: JSON.stringify(data)});
  const result = await response.json();
  if (response.status === 401) { location.reload(); throw new Error('Sign in to manage files.'); }
  if (!response.ok) throw new Error(result.error || 'Request failed.');
  return result;
}
function button(text, fn) { const b = document.createElement('button'); b.textContent = text; b.onclick = guarded(fn); return b; }
function size(bytes) { return bytes == null ? 'Folder' : bytes < 1048576 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / 1048576).toFixed(1)} MB`; }
function crumbs(container, data, navigate) { container.replaceChildren(); for (const item of data.breadcrumbs) container.append(button(item.name, () => navigate(item.path))); }
function selectionChanged() {
  $('selection').textContent = `${selected.size} selected`;
  for (const id of ['move', 'copy', 'cut', 'delete']) $(id).disabled = !selected.size || jobRunning;
  $('rename').disabled = selected.size !== 1 || jobRunning;
  $('download').disabled = selected.size !== 1 || [...selected.values()][0]?.folder;
  $('paste').disabled = !clipboard.length || jobRunning;
  $('create').disabled = jobRunning;
  $('select-all').checked = !!listing?.items.length && selected.size === listing.items.length;
  for (const row of $('file-list').children) { if (!row.dataset.path) continue; const active = selected.has(row.dataset.path); row.classList.toggle('selected', active); row.querySelector('input').checked = active; }
}
async function load(path = listing?.path, newPage = 0) {
  if (loading) return;
  loading = true;
  try {
    let data = await api('files?page=' + newPage + (path ? '&path=' + encodeURIComponent(path) : ''));
    if (newPage > 0 && newPage * 100 >= data.total) { newPage = Math.max(0, Math.ceil(data.total / 100) - 1); data = await api('files?page=' + newPage + '&path=' + encodeURIComponent(data.path)); }
    listing = data; page = newPage; selected.clear(); lastIndex = null;
    $('roots').replaceChildren(...data.roots.map(root => button(root.name, () => load(root.path))));
    crumbs($('breadcrumbs'), data, path => load(path));
    $('up').disabled = !data.parent;
    $('include').textContent = data.selected ? 'Folder selected for slideshow' : 'Use this folder in slideshow';
    $('include').disabled = data.selected;
    $('upload-destination').textContent = data.breadcrumbs.map(b => b.name).join(' / ');
    $('file-list').replaceChildren();
    data.items.forEach((item, index) => {
      const row = document.createElement('article'); row.className = 'file-entry'; row.dataset.path = item.path;
      if (!item.folder) { const img = document.createElement('img'); img.loading = 'lazy'; img.alt = ''; img.src = '/api/files/thumbnail?path=' + encodeURIComponent(item.path) + '&v=' + encodeURIComponent(item.stamp); row.append(img); }
      const label = document.createElement('label'), check = document.createElement('input'); check.type = 'checkbox';
      check.onclick = event => {
        const enabled = check.checked;
        if (event.shiftKey && lastIndex !== null) for (let i = Math.min(index, lastIndex); i <= Math.max(index, lastIndex); i++) { const e = data.items[i]; enabled ? selected.set(e.path, e) : selected.delete(e.path); }
        enabled ? selected.set(item.path, item) : selected.delete(item.path);
        lastIndex = index; selectionChanged();
      };
      label.append(check, document.createTextNode(item.name)); row.append(label);
      if (item.folder) row.append(button('Open folder →', () => load(item.path)));
      const details = document.createElement('small'); details.textContent = size(item.size) + ' · ' + new Date(item.modified * 1000).toLocaleDateString(); row.append(details);
      $('file-list').append(row);
    });
    if (!data.items.length) { const empty = document.createElement('p'); empty.textContent = 'This folder is empty. Add photos or create a folder below.'; $('file-list').append(empty); }
    $('ignored').textContent = data.ignored ? `${data.ignored} hidden, linked or non-photo entries are not shown.` : '';
    $('back').disabled = page === 0; $('next').disabled = (page + 1) * 100 >= data.total;
    $('page-label').textContent = data.total ? `${page * 100 + 1}–${Math.min((page + 1) * 100, data.total)} of ${data.total}` : '0 items';
    selectionChanged();
  } finally { loading = false; }
}
$('select-all').onchange = () => { selected.clear(); if ($('select-all').checked) listing.items.forEach(item => selected.set(item.path, item)); selectionChanged(); };
$('up').onclick = guarded(() => load(listing.parent)); $('refresh').onclick = guarded(() => load(listing.path, page));
$('back').onclick = guarded(() => load(listing.path, page - 1)); $('next').onclick = guarded(() => load(listing.path, page + 1));
$('view').onclick = () => { const list = $('file-list').classList.toggle('list-view'); $('view').textContent = list ? 'Grid view' : 'List view'; };
$('create').onclick = guarded(async () => { const name = prompt('New folder name'); if (!name) return; await api('files/folder', {parent: listing.path, name}); await load(); notice('Folder created.'); });
$('include').onclick = guarded(async () => { const state = await api('state'); await api('settings', {folders: [...new Set([...state.settings.folders, listing.path])]}); await load(); notice('Folder added to the slideshow.'); });
$('download').onclick = () => { const item = [...selected.values()][0]; if (item && !item.folder) location.assign('/api/files/download?path=' + encodeURIComponent(item.path)); };
async function start(action, extra = {}, items = [...selected.values()]) {
  const result = await api('files/action', {action, items, ...extra}); jobId = result.id; jobRunning = true; selectionChanged(); await pollJob();
}
$('delete').onclick = guarded(async () => { const items = [...selected.values()]; if (!confirm(`Permanently delete ${items.length} selected file(s) / folder(s)? Folders include all their photos. This cannot be undone.`)) return; await start('delete'); });
$('rename').onclick = guarded(async () => { const item = [...selected.values()][0]; const name = prompt('New name (keep the photo extension)', item.name); if (name && name !== item.name) await start('rename', {name}); });
$('cut').onclick = () => { clipboard = [...selected.values()]; selectionChanged(); notice(`${clipboard.length} item(s) cut. Open the destination folder and choose Paste here.`); };
$('paste').onclick = guarded(async () => { await start('move', {destination: listing.path, conflict: 'keep-both'}, clipboard); clipboard = []; selectionChanged(); });
async function browseDestination(path) {
  const data = await api('files?path=' + encodeURIComponent(path)); destinationPath = data.path;
  $('destination-roots').replaceChildren(...data.roots.map(root => button(root.name, () => browseDestination(root.path))));
  crumbs($('destination-path'), data, browseDestination);
  // Use folder-only pages too; a large folder may have folders beyond page one.
  const folders = [...data.items.filter(item => item.folder)];
  for (let p = 1; p * 100 < data.total && data.items.every(item => item.folder); p++) { data.items = (await api('files?path=' + encodeURIComponent(path) + '&page=' + p)).items; folders.push(...data.items.filter(item => item.folder)); }
  $('destination-folders').replaceChildren(...folders.map(item => button('📁 ' + item.name, () => browseDestination(item.path))));
}
for (const action of ['move', 'copy']) $(action).onclick = guarded(async () => { destinationAction = action; $('destination-title').textContent = action === 'move' ? 'Move to…' : 'Copy to…'; await browseDestination(listing.path); $('destination').showModal(); });
$('destination-cancel').onclick = () => $('destination').close();
$('destination-confirm').onclick = guarded(async () => { await start(destinationAction, {destination: destinationPath, conflict: $('conflict').value}); $('destination').close(); });
async function pollJob() {
  const result = await api('files/job'), job = result.job;
  if (!job) return;
  const justFinished = jobRunning && !job.running;
  jobRunning = job.running; jobId = job.id;
  $('job-card').hidden = false; $('job-summary').textContent = `${job.action}: ${job.done} of ${job.total} · ${job.current}`;
  $('job-progress').value = job.done / job.total * 100;
  $('job-results').replaceChildren(...job.results.map(item => { const li = document.createElement('li'); li.textContent = `${item.name}: ${item.result}${item.result === 'failed' ? ' — ' + item.message : ''}`; return li; }));
  selectionChanged();
  if (justFinished) { await load(); notice(job.results.some(item => item.result === 'failed') ? 'Finished with errors. See file operation details below.' : 'File operation complete.', job.results.some(item => item.result === 'failed')); }
}
function queueRows() {
  $('queue').replaceChildren(...uploadQueue.map(item => { const row = document.createElement('li'); row.textContent = `${item.relative || item.file.name}: ${item.status}${item.error ? ' — ' + item.error : ''}`; const progress = document.createElement('progress'); progress.max = 100; progress.value = item.progress || 0; row.append(progress); return row; }));
  $('cancel-uploads').disabled = !uploading; $('retry-uploads').disabled = uploading || !uploadQueue.some(item => ['Failed', 'Cancelled'].includes(item.status));
}
function addFiles(files, folderMode = false) {
  if (!listing) return;
  if (uploadQueue.length + files.length > 1000) { notice('Upload queue is limited to 1,000 photos. Upload in smaller batches.', true); return; }
  for (const file of files) {
    const supported = /\.(jpe?g|png|webp|bmp)$/i.test(file.name), tooLarge = file.size > 32 * 1024 * 1024 - 4096;
    uploadQueue.push({file, folder: listing.path, relative: folderMode ? file.webkitRelativePath : '', status: supported && !tooLarge ? 'Queued' : 'Failed', error: !supported ? 'Unsupported file type' : tooLarge ? 'Larger than 32 MB upload limit' : ''});
  }
  queueRows(); runUploads();
}
function upload(item) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(), form = new FormData(); activeXHR = xhr;
    form.append('folder', item.folder); form.append('image', item.file); form.append('relative_path', item.relative || item.file.name);
    xhr.open('POST', '/api/upload'); xhr.setRequestHeader('X-Slideshow-Token', token);
    xhr.upload.onprogress = e => { if (e.lengthComputable) { item.progress = e.loaded / e.total * 100; queueRows(); } };
    xhr.onerror = () => reject(new Error('Connection lost; refresh the folder before retrying.'));
    xhr.onabort = () => reject(new Error('Cancelled; a completed server upload may still appear in the folder.'));
    xhr.onload = () => { try { const result = JSON.parse(xhr.responseText); if (xhr.status === 401) location.reload(); xhr.status >= 200 && xhr.status < 300 ? resolve(result) : reject(new Error(result.error || 'Upload failed')); } catch { reject(new Error('Upload failed')); } };
    xhr.send(form);
  });
}
async function runUploads() {
  if (uploading) return;
  uploading = true; cancelUploads = false; queueRows();
  try {
    for (const item of uploadQueue) {
      if (item.status !== 'Queued') continue;
      if (cancelUploads) { item.status = 'Cancelled'; continue; }
      item.status = 'Uploading'; queueRows();
      try { const result = await upload(item); item.status = 'Uploaded as ' + result.name; item.progress = 100; item.error = ''; }
      catch (e) { item.status = cancelUploads ? 'Cancelled' : 'Failed'; item.error = e.message; }
      queueRows();
    }
  } finally { uploading = false; activeXHR = null; queueRows(); try { await load(); } catch (e) { notice(e.message, true); } }
}
$('uploads').onchange = () => { addFiles([...$('uploads').files]); $('uploads').value = ''; };
$('folder-upload').onchange = () => { addFiles([...$('folder-upload').files], true); $('folder-upload').value = ''; };
$('cancel-uploads').onclick = () => { cancelUploads = true; activeXHR?.abort(); };
$('retry-uploads').onclick = () => { for (const item of uploadQueue) if (['Failed', 'Cancelled'].includes(item.status)) { item.status = 'Queued'; item.error = ''; item.progress = 0; } runUploads(); };
$('drop-zone').ondragover = e => { e.preventDefault(); $('drop-zone').classList.add('dragging'); };
$('drop-zone').ondragleave = () => $('drop-zone').classList.remove('dragging');
$('drop-zone').ondrop = e => { e.preventDefault(); $('drop-zone').classList.remove('dragging'); addFiles([...e.dataTransfer.files]); };
guarded(async () => { await load(); await pollJob(); })();
setInterval(() => guarded(pollJob)(), 1500);
