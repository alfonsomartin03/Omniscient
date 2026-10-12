(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const MAX_BYTES = 120_000_000;
  const STEPS = [['map', 'Your map', 'Upload a scan'], ['sensors', 'Sensors', 'Radar and camera'], ['place', 'Place the unit', 'Show where it is'],
                 ['calibrate', 'Calibrate', 'A few reference spots'], ['review', 'Review and go live', 'Save and watch']];
  const UNSUPPORTED = { las: 'LAS', laz: 'LAZ', e57: 'E57', pcd: 'PCD', obj: 'OBJ', fbx: 'FBX', rcp: 'ReCap', pts: 'PTS' };
  const S = { observers: [], csrf: '', site: null, sites: [], step: 'map', inputs: null, preview: null, cam: null, tests: {}, draw: null, stream: null, timers: [],
              ui: { units: 'm', up: 'z', cal: { mark: null, sim: null, click: null, verify: false, note: '' }, place: null, upload: null } };

  // ---------------------------------------------------------------- helpers
  function h(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, value);
    }
    for (const kid of kids.flat()) if (kid !== undefined && kid !== null && kid !== false) node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return node;
  }
  const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v)) ? '—' : Number(v).toFixed(d);
  const cm = m => (m === null || m === undefined) ? '—' : `${Math.round(m * 100)} cm`;
  const chip = (text, kind) => h('span', { class: `sp-chip sp-${kind}`, text });
  const GRADE = { good: ['Good', 'ok'], fair: ['Fair', 'stale'], poor: ['Poor', 'missing'], insufficient: ['Not enough spots yet', 'waiting'], unassessed: ['Not measured', 'waiting'] };
  const STATUS = { nominal: 'Estimate only', measured: 'Measured', verified: 'Verified', uncalibrated: 'Not set up', simulated: 'Simulated' };

  function fail(error) {
    const box = $('su-error');
    const network = error instanceof TypeError;
    box.replaceChildren(h('button', { 'aria-label': 'Dismiss', onclick: () => { box.hidden = true; } , text: '×' }),
      h('b', { text: network ? 'Can’t reach the Omniscient server.' : error.message }),
      h('small', { text: network ? 'Check that it is still running on this computer, then reload this page. Your draft is saved.' : (error.hint || '') }));
    box.hidden = false; window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  const clearError = () => { $('su-error').hidden = true; };

  async function api(method, path, body) {
    const headers = {};
    if (method !== 'GET') { headers['X-Omni-CSRF'] = S.csrf; headers['Content-Type'] = 'application/json'; }
    const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin', cache: 'no-store', redirect: 'error' });
    if (response.status === 401) { window.location.assign('/login'); throw new Error('Session expired'); }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(data.error || `The server returned ${response.status}.`), { hint: data.hint || '', status: response.status });
    return data;
  }
  const guard = fn => async (...args) => { try { clearError(); return await fn(...args); } catch (error) { fail(error); } };
  const markSaved = () => { $('su-saved').textContent = `Draft saved automatically · ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`; };
  const later = (fn, ms) => { const id = setTimeout(fn, ms); S.timers.push(id); return id; };
  const everyMs = (fn, ms) => { const id = setInterval(fn, ms); S.timers.push(id); return id; };
  function leaveStep() {
    S.timers.forEach(id => { clearTimeout(id); clearInterval(id); }); S.timers = [];
    S.observers.forEach(o => o.disconnect()); S.observers = [];
    if (S.stream) { S.stream.getTracks().forEach(t => t.stop()); S.stream = null; }
    S.draw = null;
  }

  // ---------------------------------------------------------------- canvas helpers
  function fitCanvas(canvas) {
    const r = window.devicePixelRatio || 1, box = canvas.getBoundingClientRect();
    const w = Math.max(240, Math.round(box.width * r)), hgt = Math.max(160, Math.round(box.height * r));
    if (canvas.width !== w || canvas.height !== hgt) { canvas.width = w; canvas.height = hgt; return true; }
    return false;
  }
  function frameFor(canvas, bounds, pad = 22) {
    const dpr = window.devicePixelRatio || 1, p = pad * dpr, { min, max } = bounds;
    const sx = Math.max(0.5, max[0] - min[0]), sy = Math.max(0.5, max[1] - min[1]);
    const scale = Math.min((canvas.width - 2 * p) / sx, (canvas.height - 2 * p) / sy);
    const ox = (canvas.width - sx * scale) / 2, oy = (canvas.height - sy * scale) / 2;
    return { scale, dpr, px: x => ox + (x - min[0]) * scale, py: y => canvas.height - (oy + (y - min[1]) * scale),
             mx: cx => (cx - ox) / scale + min[0], my: cy => (canvas.height - cy - oy) / scale + min[1] };
  }
  function drawCloud(g, canvas, frame, preview) {
    g.fillStyle = '#080d13'; g.fillRect(0, 0, canvas.width, canvas.height);
    const { min, max } = preview.bounds, pts = preview.points, zr = Math.max(0.1, max[2] - min[2]);
    for (let i = 0; i < pts.length; i += 3) {
      const t = (pts[i + 2] - min[2]) / zr;
      g.fillStyle = `rgba(${Math.round(60 + 90 * t)},${Math.round(120 + 90 * t)},${Math.round(130 + 70 * t)},${0.3 + 0.45 * t})`;
      g.fillRect(frame.px(pts[i]), frame.py(pts[i + 1]), 2 * frame.dpr / 1.5, 2 * frame.dpr / 1.5);
    }
    // 1 m grid ticks along the axes so operators can sanity-check scale
    g.fillStyle = '#71808c'; g.font = `${11 * frame.dpr}px monospace`;
    g.fillText(`${fmt(max[0] - min[0])} m wide × ${fmt(max[1] - min[1])} m deep`, 8 * frame.dpr, canvas.height - 8 * frame.dpr);
  }
  function wedge(g, frame, x, y, yawDeg, halfDeg, range, fill, stroke) {
    const yaw = yawDeg * Math.PI / 180;
    g.beginPath(); g.moveTo(frame.px(x), frame.py(y));
    for (let i = 0; i <= 18; i++) {
      const th = (-halfDeg + 2 * halfDeg * i / 18) * Math.PI / 180;
      const dx = Math.sin(th) * Math.cos(yaw) - Math.cos(th) * Math.sin(yaw), dy = Math.sin(th) * Math.sin(yaw) + Math.cos(th) * Math.cos(yaw);
      g.lineTo(frame.px(x + dx * range), frame.py(y + dy * range));
    }
    g.closePath(); g.fillStyle = fill; g.fill(); g.strokeStyle = stroke; g.lineWidth = 1; g.stroke();
  }
  function unitGlyph(g, frame, x, y, yawDeg, label) {
    const yaw = yawDeg * Math.PI / 180, d = frame.dpr, cx = frame.px(x), cy = frame.py(y);
    g.fillStyle = '#e7edf0'; g.fillRect(cx - 5 * d, cy - 5 * d, 10 * d, 10 * d);
    g.strokeStyle = '#e7edf0'; g.lineWidth = 2 * d; g.beginPath(); g.moveTo(cx, cy);
    g.lineTo(cx - Math.sin(yaw) * 26 * d, cy - Math.cos(yaw) * 26 * d); g.stroke();
    if (label) { g.fillStyle = '#e7edf0'; g.font = `${11 * d}px monospace`; g.fillText(label, cx + 9 * d, cy + 14 * d); }
  }
  function useCanvas(canvas, redraw) { // redraw on resize
    const ro = new ResizeObserver(() => { fitCanvas(canvas); redraw(); }); ro.observe(canvas); S.observers.push(ro);
    return ro;
  }

  // ---------------------------------------------------------------- home (list of spaces)
  async function showHome() {
    leaveStep(); $('su-wizard').hidden = true; $('spatial').hidden = true; S.site = null;
    const { sites } = await api('GET', '/api/setup/sites'); S.sites = sites;
    const home = $('su-home'); home.hidden = false; home.replaceChildren();
    home.append(h('div', { class: 'su-card' }, h('h2', { text: 'Your spaces' }),
      h('p', { class: 'su-muted', text: 'A space is a room or area you want to monitor. Setup is saved as you go, so you can leave and come back to a draft at any time.' }),
      sites.length ? sites.map(s => h('div', { class: 'su-space' },
        h('div', { class: 'grow' }, h('strong', { text: s.name }), h('div', { class: 'su-muted', text: s.saved_revision ? `Saved (version ${s.saved_revision}) · last changed ${new Date(s.updated).toLocaleString()}` : `Draft · step ${STEPS.findIndex(x => x[0] === s.stage) + 1} of 5 · last changed ${new Date(s.updated).toLocaleString()}` })),
        h('button', { class: 'su-btn', onclick: guard(() => openSite(s.site_id)), text: s.saved_revision ? 'Open' : 'Resume setup' }),
        h('button', { class: 'su-btn danger', onclick: guard(async () => { if (confirm(`Delete “${s.name}” and its uploaded map? This can’t be undone.`)) { await api('DELETE', `/api/setup/sites/${s.site_id}`); showHome(); } }), text: 'Delete' })))
        : h('p', { class: 'su-muted', text: 'No spaces yet. Create your first one below.' })));
    const name = h('input', { id: 'su-new-name', maxlength: '60', placeholder: 'For example: Workshop floor', autocomplete: 'off' });
    const sample = h('input', { type: 'checkbox', id: 'su-sample' });
    home.append(h('div', { class: 'su-card' }, h('h2', { text: 'Add a space' }),
      h('div', { class: 'su-field' }, h('label', { for: 'su-new-name', text: 'Name' }), name),
      h('label', { class: 'su-muted' }, sample, ' Start with the built-in sample room (no scan needed — a good way to try setup)'),
      h('div', { class: 'su-row' }, h('button', { class: 'button-primary', onclick: guard(async () => {
        const site = await api('POST', '/api/setup/sites', { name: name.value, use_sample: sample.checked }); await openSite(site.site_id, site);
      }), text: 'Create space' }))));
  }

  async function openSite(id, known) {
    S.site = known || await api('GET', `/api/setup/sites/${id}`);
    S.inputs = S.inputs || await api('GET', '/api/setup/inputs');
    S.preview = null; S.cam = null; S.ui.cal = { mark: null, sim: null, click: null, verify: false, note: '' }; S.ui.place = null;
    $('su-home').hidden = true; $('su-wizard').hidden = false;
    S.step = STEPS.some(s => s[0] === S.site.stage) ? S.site.stage : 'map';
    if (S.step !== 'map' && !S.site.map) S.step = 'map';
    await go(S.step, false);
  }

  // ---------------------------------------------------------------- stepper
  const available = (key, site = S.site) => key === 'map' || !!(site.map && (key === 'sensors' || site.unit));
  function isDone(key) {
    const s = S.site, r = s.report;
    return key === 'map' ? !!s.map : key === 'sensors' ? !!s.unit : key === 'place' ? !!(s.unit && (s.unit.placement.source === 'manual' || (r && r.pose.source === 'fit')))
      : key === 'calibrate' ? !!(r && r.pose.source === 'fit' && r.camera.source === 'fit') : s.saved_revision > 0;
  }
  async function go(key, persist = true) {
    leaveStep(); S.step = key;
    if (persist) api('POST', `/api/setup/sites/${S.site.site_id}/stage`, { stage: key }).catch(() => {});
    $('su-title').textContent = S.site.name;
    const list = $('su-steps'); list.replaceChildren();
    STEPS.forEach(([k, title, sub], i) => list.append(h('li', { class: `${k === key ? 'current' : ''} ${isDone(k) ? 'done' : ''}` },
      h('button', { disabled: !available(k), onclick: guard(() => beforeLeave().then(ok => ok && go(k))) }, `${isDone(k) ? '✓' : i + 1}  ${title}`, h('small', { text: sub })))));
    const body = $('su-step-body'); body.replaceChildren();
    const index = STEPS.findIndex(s => s[0] === key);
    $('su-back').hidden = index === 0; $('su-next').hidden = index === STEPS.length - 1;
    $('su-back').onclick = guard(async () => { if (await beforeLeave()) go(STEPS[index - 1][0]); });
    $('su-next').onclick = guard(async () => { if (await beforeLeave()) go(STEPS[index + 1][0]); });
    $('su-next').disabled = !available(STEPS[Math.min(index + 1, 4)][0]) && key !== 'sensors';
    $('su-nav-note').textContent = '';
    if (key === 'map') await stepMap(body); else if (key === 'sensors') await stepSensors(body); else if (key === 'place') stepPlace(body);
    else if (key === 'calibrate') await stepCalibrate(body); else stepReview(body);
    markSaved();
  }
  let pendingLeave = null;
  async function beforeLeave() { const fn = pendingLeave; pendingLeave = null; return fn ? fn() : true; }

  // ---------------------------------------------------------------- step 1: map
  async function loadPreview() { S.preview = await api('GET', `/api/setup/sites/${S.site.site_id}/map/preview`); }

  async function stepMap(body) {
    if (S.site.map && !S.preview) await loadPreview();
    $('su-next').disabled = !S.site.map;
    if (!S.site.map) $('su-nav-note').textContent = 'Upload a map to continue.';
    const up = S.ui.upload;
    const drop = h('div', { class: 'su-drop', tabindex: '0', role: 'button', 'aria-label': 'Choose a map file or drop it here' },
      h('strong', { text: S.site.map ? 'Replace the map' : 'Drop your LiDAR scan here, or click to choose a file' }),
      h('div', { class: 'su-muted', text: 'Accepted: PLY (text or binary) and XYZ / TXT / CSV point lists (x y z per line), up to 120 MB.' }),
      h('div', { class: 'su-muted', text: 'LAS, LAZ and E57 aren’t supported yet — export your scan as PLY from the scanner software or a free tool such as CloudCompare.' }));
    const input = h('input', { type: 'file', accept: '.ply,.xyz,.txt,.csv', hidden: true });
    const bar = h('i'), progress = h('div', { class: 'su-progress', hidden: !up }, bar), status = h('p', { class: 'su-muted', 'aria-live': 'polite' });
    if (up) { bar.style.width = `${up.pct}%`; status.textContent = up.text; }
    const start = file => { if (file) guard(() => uploadMap(file, bar, progress, status))(); };
    drop.addEventListener('click', () => input.click());
    drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
    ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', e => start(e.dataTransfer.files[0]));
    input.addEventListener('change', () => start(input.files[0]));
    const units = h('select', { id: 'su-units', 'aria-label': 'Units used in the file' },
      [['m', 'Metres'], ['cm', 'Centimetres'], ['mm', 'Millimetres'], ['ft', 'Feet']].map(([v, t]) => h('option', { value: v, text: t, selected: v === (S.site.map ? S.site.map.source_units : S.ui.units) })));
    units.addEventListener('change', guard(async () => {
      if (!S.site.map) { S.ui.units = units.value; return; }
      status.textContent = 'Re-reading the scan with the new units…'; S.site = await api('POST', `/api/setup/sites/${S.site.site_id}/map/options`, { units: units.value }); S.preview = null; await go('map', false);
    }));
    body.append(h('div', { class: 'su-card' }, h('h2', { text: '1. Upload the map of your space' }),
      h('p', { class: 'su-muted', text: 'The map is a 3D scan of the room. It gives the system a coordinate system to place things in. It can’t tell where your sensors are — you’ll show that in the next steps.' }),
      h('div', { class: 'su-field' }, h('label', { for: 'su-units', text: 'What units is the file in?' }), units, h('small', { text: 'If you’re not sure, leave it as metres. The preview below shows the size so you can check.' })),
      drop, input, progress, status));
    if (!S.site.map) return;
    const p = S.preview, canvas = h('canvas', { class: 'su-map', 'aria-label': 'Top-down preview of the uploaded map' });
    const up2 = h('select', { id: 'su-up' }, [['z', 'Z (most scanners)'], ['y', 'Y (some game / photo-scan tools)']].map(([v, t]) => h('option', { value: v, text: t, selected: v === p.up_axis })));
    up2.addEventListener('change', guard(async () => { S.site = await api('POST', `/api/setup/sites/${S.site.site_id}/map/options`, { up_axis: up2.value }); S.preview = null; await go('map', false); }));
    const floor = h('input', { type: 'number', step: '0.01', value: p.floor_z, id: 'su-floor' });
    floor.addEventListener('change', guard(async () => { S.site = await api('POST', `/api/setup/sites/${S.site.site_id}/map/options`, { floor_z: Number(floor.value) }); S.preview = null; await go('map', false); }));
    body.append(h('div', { class: 'su-card' }, h('h2', { text: 'Preview — view from above' }),
      h('p', { class: 'su-muted', text: `${p.file || 'Map'} · ${p.points_total.toLocaleString()} points (${p.points_shown.toLocaleString()} shown) · ${fmt(p.size_m[0])} × ${fmt(p.size_m[1])} × ${fmt(p.size_m[2])} m` }),
      p.warnings.length ? h('ul', { class: 'su-list' }, p.warnings.map(w => h('li', { class: 'su-warn', text: `⚠ ${w}` }))) : h('p', { class: 'su-ok', text: '✓ The size looks plausible. Check it against your real room before continuing.' }),
      canvas,
      h('details', { class: 'su-adv' }, h('summary', { text: 'Advanced: which way is up, and floor height' }),
        h('div', { class: 'su-row' }, h('div', { class: 'su-field' }, h('label', { for: 'su-up', text: 'Which way is up in the file?' }), up2),
          h('div', { class: 'su-field' }, h('label', { for: 'su-floor', text: 'Floor height (metres, detected automatically)' }), floor)))));
    const draw = () => { fitCanvas(canvas); const g = canvas.getContext('2d'); const f = frameFor(canvas, p.bounds); drawCloud(g, canvas, f, p); };
    useCanvas(canvas, draw); draw();
  }

  async function uploadMap(file, bar, progress, status) {
    const ext = (file.name.split('.').pop() || '').toLowerCase();
    if (UNSUPPORTED[ext]) throw Object.assign(new Error(`${UNSUPPORTED[ext]} files aren’t supported yet.`), { hint: 'Export the scan as PLY from your scanner software (or CloudCompare) and upload that.' });
    if (!['ply', 'xyz', 'txt', 'csv'].includes(ext)) throw Object.assign(new Error('That file type isn’t supported.'), { hint: 'Use a PLY or XYZ point-cloud file.' });
    if (file.size > MAX_BYTES) throw Object.assign(new Error(`That file is ${(file.size / 1e6).toFixed(0)} MB; the limit is 120 MB.`), { hint: 'Export a smaller (decimated) version of the scan and try again.' });
    progress.hidden = false; bar.style.width = '0'; status.textContent = 'Uploading…';
    const units = S.site.map ? S.site.map.source_units : S.ui.units, upAxis = S.site.map ? S.site.map.up_axis : S.ui.up;
    const url = `/api/setup/sites/${S.site.site_id}/map?name=${encodeURIComponent(file.name)}&units=${units}&up=${upAxis}`;
    const result = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest(); xhr.open('PUT', url); xhr.setRequestHeader('X-Omni-CSRF', S.csrf); xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.onprogress = e => { if (e.lengthComputable) { const pct = Math.round(e.loaded / e.total * 100); bar.style.width = `${pct}%`; status.textContent = pct < 100 ? `Uploading… ${pct}%  (${(e.loaded / 1e6).toFixed(1)} of ${(e.total / 1e6).toFixed(1)} MB)` : 'Upload finished. Reading the scan — large files can take a minute…'; } };
      xhr.onload = () => { let data = {}; try { data = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
        if (xhr.status === 401) window.location.assign('/login');
        xhr.status >= 200 && xhr.status < 300 ? resolve(data) : reject(Object.assign(new Error(data.error || `The server returned ${xhr.status}.`), { hint: data.hint || '' })); };
      xhr.onerror = () => reject(Object.assign(new Error('The upload was interrupted.'), { hint: 'Check that the Omniscient server is still running and try again. Your draft is saved.' }));
      xhr.send(file);
    });
    S.site = result; S.preview = null; await go('map', false);
  }

  // ---------------------------------------------------------------- step 2: sensors
  async function stepSensors(body) {
    const unit = S.site.unit, radars = S.inputs.radar, cams = S.inputs.camera;
    const f = { label: unit ? unit.label : 'Wall unit', radar: unit ? unit.radar_input : 'sim-radar', camera: unit ? unit.camera_input : 'sim-camera',
                hfov: unit ? unit.lens.hfov_deg : 70, off: unit ? unit.camera_offset_m.map(v => Math.round(v * 100)) : [0, 0, 0] };
    const radarSel = h('select', { id: 'su-radar' }, radars.map(r => h('option', { value: r.id, text: r.name, selected: r.id === f.radar })));
    const camSel = h('select', { id: 'su-camera' }, cams.map(c => h('option', { value: c.id, text: c.name, selected: c.id === f.camera })));
    const label = h('input', { id: 'su-label', value: f.label, maxlength: '60' });
    const radarStatus = h('div', { 'aria-live': 'polite' }), camStatus = h('div', { 'aria-live': 'polite' });
    const previewBox = h('div'), disc = h('div', { class: 'su-muted' });
    const showTest = (box, t) => box.replaceChildren(t ? h('div', {}, chip(t.ok ? (t.status === 'browser' ? 'READY' : 'CONNECTED') : 'NOT RESPONDING', t.ok ? 'ok' : 'missing'),
      h('span', { class: 'su-muted', text: ` ${t.message || ''}` }), t.ok ? null : h('p', { class: 'su-warn', text: `What to try: ${t.hint}` })) : '');
    const testRadar = guard(async () => { radarStatus.textContent = 'Testing…'; showTest(radarStatus, await api('POST', `/api/setup/inputs/${radarSel.value}/test`)); });
    const testCam = guard(async () => { camStatus.textContent = 'Testing…'; const t = await api('POST', `/api/setup/inputs/${camSel.value}/test`); showTest(camStatus, t); await showCameraPreview(previewBox, camSel.value); });
    radarSel.addEventListener('change', testRadar); camSel.addEventListener('change', testCam);
    const hfov = h('input', { type: 'number', id: 'su-hfov', min: '20', max: '140', step: '1', value: f.hfov });
    const off = ['right', 'forward', 'up'].map((name, i) => h('input', { type: 'number', step: '1', value: f.off[i], 'aria-label': `Camera ${name} of radar, cm` }));
    pendingLeave = guard(async () => {
      S.site = await api('POST', `/api/setup/sites/${S.site.site_id}/unit`, { label: label.value, radar_input: radarSel.value, camera_input: camSel.value, hfov_deg: Number(hfov.value), offset_cm: off.map(i => Number(i.value)) });
      return true;
    });
    const original = pendingLeave; pendingLeave = async () => (await original()) !== undefined;
    $('su-next').onclick = guard(async () => { const ok = await original(); if (ok) go('place'); });
    $('su-next').disabled = false; $('su-nav-note').textContent = 'Next saves these choices.';
    body.append(h('div', { class: 'su-card' }, h('h2', { text: '2. Connect the radar and camera' }),
      h('p', { class: 'su-muted', text: 'A sensor unit is a camera and a radar in one housing. Choose the inputs for this unit; the connection is tested automatically.' }),
      h('div', { class: 'su-field' }, h('label', { for: 'su-label', text: 'Name this unit' }), label),
      h('div', { class: 'su-row' }, h('button', { class: 'su-btn', onclick: guard(async () => { const r = await api('GET', '/api/setup/inputs'); S.inputs = r; disc.textContent = r.discovery.note; }), text: 'Look for devices' }), disc),
      h('div', { class: 'su-even' },
        h('div', {}, h('div', { class: 'su-field' }, h('label', { for: 'su-radar', text: 'Radar' }), radarSel), radarStatus, h('button', { class: 'su-btn', onclick: testRadar, text: 'Test radar' })),
        h('div', {}, h('div', { class: 'su-field' }, h('label', { for: 'su-camera', text: 'Camera' }), camSel), camStatus, h('button', { class: 'su-btn', onclick: testCam, text: 'Test camera / show preview' }))),
      previewBox));
    body.append(h('div', { class: 'su-card' }, h('h2', { text: 'What can be worked out automatically?' }),
      h('ul', { class: 'su-list' },
        h('li', {}, h('span', { class: 'su-no', text: '✗ ' }), h('b', { text: 'Where the unit is on the map. ' }), 'A scan alone can’t tell the system where you mounted the unit. You’ll place it, then measure it with a few reference spots.'),
        h('li', {}, h('span', { class: 'su-no', text: '✗ ' }), h('b', { text: 'How the camera lines up with the radar. ' }), 'Neither of these sensors reports this, so you’ll mark a few spots in both. The system then calculates it and tells you how accurate it is.'),
        h('li', {}, h('span', { class: 'su-warn', text: '△ ' }), h('b', { text: 'The lens. ' }), 'We assume a typical field of view (change it under Advanced). It isn’t measured, so picture edges are less accurate.')),
      h('details', { class: 'su-adv' }, h('summary', { text: 'Advanced: lens, mounting offset, and other connection options' }),
        h('div', { class: 'su-row' }, h('div', { class: 'su-field' }, h('label', { for: 'su-hfov', text: 'Camera horizontal field of view (degrees)' }), hfov, h('small', { text: 'From the camera’s datasheet. 70° is a common default.' })),
          h('div', { class: 'su-field' }, h('label', { text: 'Camera position relative to the radar (cm: right, forward, up)' }), h('div', { class: 'su-row' }, off), h('small', { text: 'Measure with a ruler if you can; 0 is fine if they’re almost on top of each other.' }))),
        h('p', { class: 'su-muted', text: 'Network addresses, ports and device protocols are set by sensor adapters. Only documented adapters are installed, and today that is just the demo devices above. Real devices need an adapter written from their documentation before they appear in these lists.' }))));
    testRadar(); testCam();
  }

  async function showCameraPreview(box, id) {
    box.replaceChildren();
    const item = S.inputs.camera.find(c => c.id === id);
    if (item.preview === 'simulated') {
      const canvas = h('canvas', { class: 'su-video', 'aria-label': 'Simulated camera picture' }); box.append(canvas, h('p', { class: 'su-muted', text: 'Simulated picture: a real camera would show live video here.' }));
      const draw = async () => { try { paintSimFrame(canvas, await api('GET', `/api/setup/sites/${S.site.site_id}/preview/camera`)); } catch (error) { fail(error); } };
      draw(); everyMs(draw, 1500);
    } else {
      const video = h('video', { autoplay: true, playsinline: true, muted: true, controls: item.preview === 'browser_file' });
      const note = h('p', { class: 'su-muted' });
      box.append(video, note);
      if (item.preview === 'browser_camera') {
        try { S.stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false }); video.srcObject = S.stream; note.textContent = 'Live preview from this computer’s camera. It never leaves this device except through your Omniscient server.'; }
        catch (e) { note.className = 'su-warn'; note.textContent = e.name === 'NotAllowedError' ? 'The browser blocked camera access. Click the camera icon in the address bar, allow it, and press Test again.' : 'No camera was found. Plug one in and press Test again.'; }
      } else {
        const pick = h('input', { type: 'file', accept: 'video/*' }); pick.addEventListener('change', () => { if (pick.files[0]) { video.src = URL.createObjectURL(pick.files[0]); video.play().catch(() => {}); } });
        box.append(pick); note.textContent = 'Choose a video file from this computer to preview it.';
      }
      box.append(h('p', { class: 'su-muted', text: 'This camera can be previewed and saved, but guided calibration needs a radar/camera pair that the server can read together (the demo pair).' }));
    }
  }
  function paintSimFrame(canvas, frame, extra) {
    const [iw, ih] = frame.image_size; canvas.width = 960; canvas.height = Math.round(960 * ih / iw);
    const g = canvas.getContext('2d'); g.fillStyle = '#080d13'; g.fillRect(0, 0, canvas.width, canvas.height);
    const p = frame.points;
    for (let i = 0; i < p.length; i += 3) { const s = Math.max(70, 210 - p[i + 2] * 12); g.fillStyle = `rgb(${s * .55},${s * .8},${s * .85})`; g.fillRect(p[i] * canvas.width, p[i + 1] * canvas.height, 2, 2); }
    for (const person of frame.people || []) { const x = person.u * canvas.width, y = person.v * canvas.height; g.strokeStyle = '#8797a0'; g.lineWidth = 2; g.beginPath(); g.ellipse(x, y - 40, 9, 11, 0, 0, Math.PI * 2); g.moveTo(x, y - 29); g.lineTo(x, y + 8); g.stroke(); }
    if (extra) extra(g, canvas);
  }

  // ---------------------------------------------------------------- step 3: place
  function placementState() {
    if (!S.ui.place) {
      const p = S.site.unit.placement;
      S.ui.place = { x: p.x, y: p.y, yaw: p.yaw_deg, height: +(p.z - S.site.map.floor_z).toFixed(2), tilt: p.tilt_deg };
    }
    return S.ui.place;
  }
  function stepPlace(body) {
    if (!S.preview) { loadPreview().then(() => stepPlace(body)).catch(fail); return; }
    body.replaceChildren();
    const P = placementState(), pv = S.preview, spec = S.site.unit.radar_spec;
    const canvas = h('canvas', { class: 'su-map', 'aria-label': 'Map: click to place the unit, drag the round handle to turn it' });
    const info = h('p', { class: 'su-muted' });
    const commit = guard(async () => { S.site = await api('POST', `/api/setup/sites/${S.site.site_id}/placement`, { x: P.x, y: P.y, yaw_deg: P.yaw, height_m: P.height, tilt_deg: P.tilt }); S.ui.place = null; markSaved(); });
    let layer = null, frame = null, drag = null;
    const draw = () => {
      fitCanvas(canvas); const g = canvas.getContext('2d'); frame = frameFor(canvas, pv.bounds);
      if (!layer || layer.width !== canvas.width || layer.height !== canvas.height) { layer = document.createElement('canvas'); layer.width = canvas.width; layer.height = canvas.height; drawCloud(layer.getContext('2d'), layer, frame, pv); }
      g.drawImage(layer, 0, 0);
      wedge(g, frame, P.x, P.y, P.yaw, spec.azimuth_fov_deg[1], spec.max_range_m, 'rgba(170,145,255,.08)', 'rgba(170,145,255,.55)');
      wedge(g, frame, P.x, P.y, P.yaw, S.site.unit.lens.hfov_deg / 2, 7, 'rgba(72,214,197,.10)', 'rgba(72,214,197,.7)');
      unitGlyph(g, frame, P.x, P.y, P.yaw, S.site.unit.label);
      const yaw = P.yaw * Math.PI / 180, hx = frame.px(P.x - Math.sin(yaw) * 1.6), hy = frame.py(P.y + Math.cos(yaw) * 1.6);
      g.beginPath(); g.arc(hx, hy, 8 * frame.dpr, 0, Math.PI * 2); g.fillStyle = '#48d6c5'; g.fill();
      g.fillStyle = '#e7edf0'; g.font = `${11 * frame.dpr}px monospace`; g.fillText('drag to turn', hx + 12 * frame.dpr, hy + 4 * frame.dpr);
      info.textContent = `Position ${fmt(P.x)}, ${fmt(P.y)} m · facing ${Math.round(P.yaw)}° · ${fmt(P.height)} m high · tilted down ${Math.round(P.tilt)}°`;
    };
    const toMap = e => { const r = canvas.getBoundingClientRect(), k = canvas.width / r.width; return [frame.mx((e.clientX - r.left) * k), frame.my((e.clientY - r.top) * k)]; };
    canvas.addEventListener('pointerdown', e => {
      const [mx, my] = toMap(e), yaw = P.yaw * Math.PI / 180, hx = P.x - Math.sin(yaw) * 1.6, hy = P.y + Math.cos(yaw) * 1.6;
      drag = Math.hypot(mx - hx, my - hy) * frame.scale < 18 * frame.dpr ? 'heading' : 'move';
      if (drag === 'move') { P.x = mx; P.y = my; }
      canvas.setPointerCapture(e.pointerId); draw();
    });
    canvas.addEventListener('pointermove', e => { if (!drag) return; const [mx, my] = toMap(e); if (drag === 'move') { P.x = mx; P.y = my; } else P.yaw = Math.atan2(-(mx - P.x), my - P.y) * 180 / Math.PI; syncControls(); draw(); });
    canvas.addEventListener('pointerup', () => { if (drag) { drag = null; commit(); } });
    const yawIn = h('input', { type: 'range', min: '-180', max: '180', step: '1', 'aria-label': 'Direction the unit faces' });
    const heightIn = h('input', { type: 'number', min: '0.5', max: '20', step: '0.1' });
    const tiltIn = h('input', { type: 'range', min: '0', max: '45', step: '1', 'aria-label': 'Downward tilt' });
    const syncControls = () => { yawIn.value = Math.round(P.yaw); heightIn.value = P.height; tiltIn.value = Math.round(P.tilt); };
    yawIn.addEventListener('input', () => { P.yaw = Number(yawIn.value); draw(); }); yawIn.addEventListener('change', commit);
    tiltIn.addEventListener('input', () => { P.tilt = Number(tiltIn.value); draw(); }); tiltIn.addEventListener('change', commit);
    heightIn.addEventListener('change', () => { P.height = Math.max(0.5, Number(heightIn.value) || 2.4); draw(); commit(); });
    syncControls();
    const measured = S.site.report && S.site.report.pose.source === 'fit';
    body.append(h('div', { class: 'su-two' },
      h('div', { class: 'su-card' }, h('h2', { text: '3. Show where the unit is' }), h('p', { class: 'su-muted', text: 'Click the map where the unit is mounted, then drag the round handle to point it the way it faces. Purple is what the radar covers; teal is the camera’s view.' }), canvas),
      h('div', { class: 'su-card' }, h('h2', { text: 'Fine controls' }),
        h('div', { class: 'su-field' }, h('label', { text: 'Direction it faces (degrees, 0 = toward the top of the map)' }), yawIn),
        h('div', { class: 'su-field' }, h('label', { text: 'Mounting height above the floor (metres)' }), heightIn),
        h('div', { class: 'su-field' }, h('label', { text: 'Tilted down by (degrees)' }), tiltIn, h('small', { text: 'Measure with a phone level app if you can.' })), info,
        measured ? h('p', { class: 'su-warn', text: 'You’ve already measured this unit from reference spots, and that measurement is being used. These controls only change the starting guess.' })
          : h('p', { class: 'su-muted', text: 'This is your best estimate for now. The next step measures it and tells you how accurate it is.' }))));
    useCanvas(canvas, draw); draw();
  }

  // ---------------------------------------------------------------- step 4: calibrate
  async function stepCalibrate(body) {
    const unit = S.site.unit, report = S.site.report;
    if (!S.preview) await loadPreview();
    if (!report.capture_supported) {
      body.append(h('div', { class: 'su-card' }, h('h2', { text: '4. Calibrate' }), h('p', { class: 'su-warn', text: report.capture_unsupported_reason }),
        h('p', { class: 'su-muted', text: 'You can still save this setup. The unit position will be marked as an estimate and radar markers on the camera view will be flagged as uncalibrated.' })));
      return;
    }
    if (!S.cam) S.cam = await api('GET', `/api/setup/sites/${S.site.site_id}/preview/camera`);
    const C = S.ui.cal, pv = S.preview, caps = unit.captures, res = report.capture_residuals;
    const mapCanvas = h('canvas', { class: 'su-map', 'aria-label': 'Map: click where the reflector is' });
    const vidCanvas = h('canvas', { class: 'su-video', 'aria-label': 'Camera picture: click the reflector' });
    const msg = h('p', { class: 'su-muted', 'aria-live': 'polite', text: C.note });
    const capBtn = h('button', { class: 'button-primary', disabled: true, text: 'Capture this spot' });
    const verify = h('input', { type: 'checkbox', checked: C.verify }); verify.addEventListener('change', () => { C.verify = verify.checked; });
    const refresh = () => { capBtn.disabled = !(C.mark && C.click && !(C.sim && !C.sim.radar_visible)); };
    let layer = null, frame = null;
    const eff = report.pose.effective;
    const drawMap = () => {
      fitCanvas(mapCanvas); const g = mapCanvas.getContext('2d'); frame = frameFor(mapCanvas, pv.bounds);
      if (!layer || layer.width !== mapCanvas.width || layer.height !== mapCanvas.height) { layer = document.createElement('canvas'); layer.width = mapCanvas.width; layer.height = mapCanvas.height; drawCloud(layer.getContext('2d'), layer, frame, pv); }
      g.drawImage(layer, 0, 0); const spec = unit.radar_spec, d = frame.dpr;
      wedge(g, frame, eff.x, eff.y, eff.yaw_deg, spec.azimuth_fov_deg[1], spec.max_range_m, 'rgba(170,145,255,.07)', 'rgba(170,145,255,.5)');
      wedge(g, frame, eff.x, eff.y, eff.yaw_deg, unit.lens.hfov_deg / 2, 8, 'rgba(72,214,197,.10)', 'rgba(72,214,197,.7)');
      unitGlyph(g, frame, eff.x, eff.y, eff.yaw_deg, 'unit');
      for (const c of caps) {
        const r = res[c.id], x = frame.px(c.map[0]), y = frame.py(c.map[1]), color = c.role === 'verify' ? '#f2b95e' : '#52d7a2';
        if (r && r.map_est) { g.strokeStyle = '#ef8a80'; g.lineWidth = 1.5 * d; g.beginPath(); g.moveTo(x, y); g.lineTo(frame.px(r.map_est[0]), frame.py(r.map_est[1])); g.stroke(); }
        g.strokeStyle = color; g.lineWidth = 2 * d; g.beginPath(); g.arc(x, y, 6 * d, 0, 7); g.stroke();
        g.fillStyle = color; g.font = `${11 * d}px monospace`; g.fillText(String(c.id), x + 8 * d, y - 6 * d);
      }
      if (C.mark) { const x = frame.px(C.mark.x), y = frame.py(C.mark.y); g.strokeStyle = '#ffd37a'; g.lineWidth = 2 * d; g.beginPath(); g.moveTo(x - 9 * d, y); g.lineTo(x + 9 * d, y); g.moveTo(x, y - 9 * d); g.lineTo(x, y + 9 * d); g.stroke(); }
    };
    const drawVid = () => paintSimFrame(vidCanvas, S.cam, (g, canvas) => {
      const sx = canvas.width / S.cam.image_size[0];
      for (const c of caps) {
        const r = res[c.id]; if (!c.px) continue;
        const color = c.role === 'verify' ? '#f2b95e' : '#52d7a2';
        g.strokeStyle = color; g.lineWidth = 2; g.beginPath(); g.arc(c.px[0] * sx, c.px[1] * sx, 7, 0, 7); g.stroke();
        if (r && r.px_est) { g.strokeStyle = '#ef8a80'; g.beginPath(); g.moveTo(c.px[0] * sx, c.px[1] * sx); g.lineTo(r.px_est[0] * sx, r.px_est[1] * sx); g.stroke(); g.beginPath(); g.moveTo(r.px_est[0] * sx - 6, r.px_est[1] * sx - 6); g.lineTo(r.px_est[0] * sx + 6, r.px_est[1] * sx + 6); g.moveTo(r.px_est[0] * sx - 6, r.px_est[1] * sx + 6); g.lineTo(r.px_est[0] * sx + 6, r.px_est[1] * sx - 6); g.stroke(); }
      }
      if (C.sim && C.sim.px) { g.strokeStyle = '#ffd37a'; g.setLineDash([4, 3]); g.lineWidth = 2; const x = C.sim.px[0] * sx, y = C.sim.px[1] * sx; g.strokeRect(x - 12, y - 12, 24, 24); g.setLineDash([]); g.fillStyle = '#ffd37a'; g.font = '12px monospace'; g.fillText('reflector', x + 16, y + 4); }
      if (C.click) { const x = C.click[0] * sx, y = C.click[1] * sx; g.strokeStyle = '#ffffff'; g.lineWidth = 2; g.beginPath(); g.moveTo(x - 10, y); g.lineTo(x + 10, y); g.moveTo(x, y - 10); g.lineTo(x, y + 10); g.stroke(); }
    });
    const redraw = () => { drawMap(); drawVid(); refresh(); };
    mapCanvas.addEventListener('click', guard(async e => {
      const r = mapCanvas.getBoundingClientRect(), k = mapCanvas.width / r.width;
      C.mark = { x: frame.mx((e.clientX - r.left) * k), y: frame.my((e.clientY - r.top) * k) }; C.click = null;
      C.sim = await api('POST', `/api/setup/sites/${S.site.site_id}/sim/reflector`, C.mark);
      msg.className = C.sim.radar_visible ? 'su-muted' : 'su-warn';
      msg.textContent = !C.sim.radar_visible ? 'The radar can’t see a reflector there. Pick a spot inside the purple cone and at least 1 m away.'
        : !C.sim.in_image ? 'The radar sees this spot, but it’s outside the camera picture. Pick a spot inside the teal cone so you can click it in the video.'
        : `The radar sees it ${fmt(C.sim.radar_range_m)} m away. Now click the reflector (the dashed box) in the video.`;
      redraw();
    }));
    vidCanvas.addEventListener('click', e => {
      if (!C.mark) { msg.className = 'su-warn'; msg.textContent = 'First click where the reflector is on the map.'; return; }
      const r = vidCanvas.getBoundingClientRect(); C.click = [(e.clientX - r.left) / r.width * S.cam.image_size[0], (e.clientY - r.top) / r.height * S.cam.image_size[1]]; redraw();
    });
    capBtn.addEventListener('click', guard(async () => {
      capBtn.disabled = true; msg.textContent = 'Capturing… hold still (averaging radar readings).';
      S.site = await api('POST', `/api/setup/sites/${S.site.site_id}/captures`, { x: C.mark.x, y: C.mark.y, px: C.click, role: C.verify ? 'verify' : 'fit' });
      Object.assign(C, { mark: null, sim: null, click: null, note: 'Captured. Move the reflector somewhere new and repeat.' }); markSaved(); await go('calibrate', false);
    }));
    const reflH = h('div', { class: 'su-field' }, h('label', { for: 'su-refl', text: 'Reflector height above floor (m)' }), h('input', { id: 'su-refl', type: 'number', min: '0', max: '3', step: '0.05', value: unit.reflector_height_m, onchange: guard(async ev => { S.site = await api('POST', `/api/setup/sites/${S.site.site_id}/reflector-height`, { height_m: Number(ev.target.value) }); }) }));
    body.append(h('div', { class: 'su-card' }, h('h2', { text: '4. Measure the unit with a few reference spots' }),
      h('p', { class: 'su-muted', text: 'The system can only work out where the unit is — and how the camera lines up with the radar — from evidence. You’ll stand a radar reflector (a small metal corner reflector or any metal object) at a few spots. For each spot: mark it on the map, click it in the video, and capture. Aim for about 6 spots spread near and far, left and right, then 3 more as a check.' }),
      h('p', { class: 'su-muted', text: 'In this demo the simulated reflector appears wherever you click on the map.' }),
      h('div', { class: 'su-even' }, h('div', {}, h('h3', { class: 'su-h3', text: '1 · Map: click the reflector’s spot' }), mapCanvas, h('p', { class: 'su-muted', text: 'Green = spots you captured; red line = how far the current result is from where you marked it.' })),
        h('div', {}, h('h3', { class: 'su-h3', text: '2 · Camera picture: click the reflector' }), vidCanvas, h('p', { class: 'su-muted', text: 'Circle = where you clicked; red ✕ = where the radar reading lands with the current result.' }))),
      h('div', { class: 'su-row' }, h('b', { text: '3 ·' }), capBtn, h('label', { class: 'su-muted' }, verify, ' Use as a check (not used to compute the result)'), reflH), msg));
    body.append(qualitySection(report), spotsTable(caps, res), fineTune(report));
    useCanvas(mapCanvas, redraw); useCanvas(vidCanvas, redraw); redraw();
  }

  function gradeChip(g) { const [t, k] = GRADE[g] || GRADE.unassessed; return chip(t, k); }
  function qualitySection(r) {
    const p = r.pose, c = r.camera, v = r.verification;
    const poseText = p.source === 'placement' ? 'Only your estimate so far — nothing has been measured.'
      : `On the map, a target is placed within about ±${cm(p.sigma_point_m)} (typical). The unit’s direction is good to ±${fmt(p.sigma_rot_deg)}°. Your spots agree with each other to ${cm(p.rms_m)} on average.`;
    const camText = c.source === 'assumed' ? 'The camera is assumed to point exactly where the radar does. Radar markers on the video will be off by however much the real mounting differs.'
      : `Radar markers on the video typically land within about ${fmt(c.rms_px, 0)} px of the real target. The camera direction is good to ±${fmt(c.sigma_rot_deg)}°.`;
    const verText = !v.n ? 'No check spots yet. Add 3 spots in new places and tick “Use as a check” — they test the result without being used to compute it.'
      : `${v.n} check spot(s). ${v.pose ? `Map error ${cm(v.pose.rms_m)} on average — ${v.pose.passed ? 'passed' : v.pose.n < 3 ? 'need 3' : 'not passed'}. ` : ''}${v.camera ? `Video error ${fmt(v.camera.rms_px, 0)} px — ${v.camera.passed ? 'passed' : v.camera.n < 3 ? 'need 3' : 'not passed'}.` : ''}`;
    return h('div', { class: 'su-card' }, h('h2', { text: 'How good is it?' }), h('p', { class: 'su-muted', text: `“Calibrated” alone doesn’t tell you much, so here is the evidence behind each part. Next: ${r.next}` }),
      h('div', { class: 'su-three' },
        h('div', { class: 'su-q' }, h('h3', {}, 'Unit position and direction ', chip(STATUS[p.status] || p.status, p.status === 'verified' ? 'ok' : p.status === 'measured' ? 'stale' : 'waiting'), gradeChip(p.quality)), h('p', { text: poseText }), (p.hints || []).map(t => h('p', { class: 'su-warn', text: `→ ${t}` })), p.nudged ? h('p', { class: 'su-warn', text: 'Adjusted by hand; quality was recomputed from your spots.' }) : null),
        h('div', { class: 'su-q' }, h('h3', {}, 'Camera aligned to radar ', chip(STATUS[c.status] || c.status, c.status === 'verified' ? 'ok' : c.status === 'measured' ? 'stale' : 'waiting'), gradeChip(c.quality)), h('p', { text: camText }), (c.hints || []).map(t => h('p', { class: 'su-warn', text: `→ ${t}` }))),
        h('div', { class: 'su-q' }, h('h3', {}, 'Check spots ', chip(v.pose && v.pose.passed && v.camera && v.camera.passed ? 'Verified' : 'Not verified', v.pose && v.pose.passed && v.camera && v.camera.passed ? 'ok' : 'waiting')), h('p', { text: verText }),
          h('p', { class: 'su-muted', text: `Lens: ${STATUS[r.lens.status]} (${r.lens.hfov_deg}° field of view, not measured).` }))));
  }
  function spotsTable(caps, res) {
    if (!caps.length) return h('div', { class: 'su-card' }, h('h2', { text: 'Reference spots' }), h('p', { class: 'su-muted', text: 'None yet.' }));
    const worst = caps.filter(c => c.role === 'fit').reduce((a, c) => (res[c.id].map_err_m || 0) > (a ? res[a.id].map_err_m || 0 : -1) ? c : a, null);
    return h('div', { class: 'su-card' }, h('h2', { text: 'Reference spots' }), h('table', { class: 'su-spots' },
      h('thead', {}, h('tr', {}, ['#', 'Use', 'Radar range', 'Map error', 'Video error', ''].map(t => h('th', { text: t })))),
      h('tbody', {}, caps.map(c => h('tr', { class: worst && c.id === worst.id && caps.length > 4 ? 'worst' : '' },
        h('td', { text: c.id }), h('td', {}, h('select', { 'aria-label': `Spot ${c.id} use`, onchange: guard(async ev => { S.site = await api('POST', `/api/setup/sites/${S.site.site_id}/captures/${c.id}/role`, { role: ev.target.value }); await go('calibrate', false); }) },
          h('option', { value: 'fit', text: 'Computes result', selected: c.role === 'fit' }), h('option', { value: 'verify', text: 'Check only', selected: c.role === 'verify', disabled: !c.px }))),
        h('td', { text: `${fmt(c.range_m)} m` }), h('td', { text: cm(res[c.id].map_err_m) }), h('td', { text: res[c.id].px_err === null ? '—' : `${fmt(res[c.id].px_err, 0)} px` }),
        h('td', {}, h('button', { class: 'su-btn danger', onclick: guard(async () => { S.site = await api('DELETE', `/api/setup/sites/${S.site.site_id}/captures/${c.id}`); await go('calibrate', false); }), text: 'Remove' })))))),
      h('p', { class: 'su-muted', text: 'A highlighted row is the spot that disagrees most; if the result is poor, remove it and capture it again.' }));
  }
  function fineTune(r) {
    const send = (kind, key, amount) => guard(async () => { S.site = await api('POST', `/api/setup/sites/${S.site.site_id}/nudge`, { [kind]: { [key]: amount } }); await go('calibrate', false); });
    const btn = (kind, key, amount, text) => h('button', { class: 'su-btn', onclick: send(kind, key, amount), 'aria-label': text, text });
    return h('div', { class: 'su-card' }, h('details', { class: 'su-adv', open: r.pose.nudged || r.camera.nudged }, h('summary', { text: 'Fine-tune by hand (optional)' }),
      h('p', { class: 'su-muted', text: 'Nudge the result and watch the red lines on the map and video shrink or grow. The quality figures above are recomputed from your spots, so a nudge only counts if it really helps. Adding or removing a spot recomputes everything and clears these nudges.' }),
      h('div', { class: 'su-even' },
        h('div', {}, h('h3', { class: 'su-h3', text: 'Unit on the map' }), h('div', { class: 'su-nudge' },
          btn('pose', 'dx', -0.05, '← 5 cm'), btn('pose', 'dx', 0.05, '5 cm →'), btn('pose', 'dy', 0.05, '↑ 5 cm'), btn('pose', 'dy', -0.05, '↓ 5 cm'),
          btn('pose', 'dyaw_deg', 0.5, '⟲ 0.5°'), btn('pose', 'dyaw_deg', -0.5, '⟳ 0.5°'))),
        h('div', {}, h('h3', { class: 'su-h3', text: 'Camera on the video' }), h('div', { class: 'su-nudge' },
          btn('camera', 'yaw', 0.25, 'Turn left'), btn('camera', 'yaw', -0.25, 'Turn right'), btn('camera', 'pitch', 0.25, 'Tilt up'), btn('camera', 'pitch', -0.25, 'Tilt down'),
          btn('camera', 'roll', 0.25, 'Roll ⟲'), btn('camera', 'roll', -0.25, 'Roll ⟳')))),
      h('div', { class: 'su-row' }, h('button', { class: 'su-btn', onclick: guard(async () => { S.site = await api('POST', `/api/setup/sites/${S.site.site_id}/nudge`, { reset: true }); await go('calibrate', false); }), text: 'Undo manual adjustments' }))));
  }

  // ---------------------------------------------------------------- step 5: review + live
  function stepReview(body) {
    const r = S.site.report, saved = S.site.saved_revision > 0;
    $('su-nav-note').textContent = '';
    const resultBox = h('div', { 'aria-live': 'polite' });
    body.append(h('div', { class: 'su-card' }, h('h2', { text: '5. Review, save, and watch it live' }),
      h('p', { class: 'su-muted', text: 'This is what will be saved. Anything marked “Estimate only” hasn’t been measured, and the system will say so wherever it shows tracks.' }),
      h('p', { class: 'su-muted', text: `Lens: ${r.lens.hfov_deg}° field of view (nominal, not measured).` })),
      qualitySection(r));
    body.append(h('div', { class: 'su-card' }, h('h2', { text: 'Things to know' }), h('ul', { class: 'su-list' }, r.warnings.map(w => h('li', { class: 'su-warn', text: `△ ${w}` }))),
      h('div', { class: 'su-row' },
        h('button', { class: 'button-primary', onclick: guard(async () => {
          S.site = await api('POST', `/api/setup/sites/${S.site.site_id}/save`, {}); markSaved();
          resultBox.replaceChildren(h('p', { class: 'su-ok', text: `✓ Saved (version ${S.site.saved_revision}). Earlier versions are kept in the space’s history folder.` })); await go('review', false);
        }), text: saved ? 'Save changes' : 'Save setup' }),
        h('button', { class: 'button-secondary', disabled: !saved, onclick: guard(async () => {
          await api('POST', `/api/setup/sites/${S.site.site_id}/activate`, {});
          $('spatial').hidden = false; window.omniSpatial.start(); setTimeout(() => $('spatial').scrollIntoView({ behavior: 'smooth' }), 300);
        }), text: 'Go live' }), saved ? null : h('span', { class: 'su-muted', text: 'Save first to go live.' })), resultBox));
    if (saved && S.site.stage === 'review') { /* leave the live panel for the operator to start */ }
  }

  // ---------------------------------------------------------------- boot
  async function boot() {
    $('su-home-link').addEventListener('click', guard(async e => { e.preventDefault(); await showHome(); }));
    window.addEventListener('beforeunload', () => leaveStep());
    try { const session = await api('GET', '/api/session'); S.csrf = session.csrf; await showHome(); } catch (error) { fail(error); }
  }
  boot();
})();
