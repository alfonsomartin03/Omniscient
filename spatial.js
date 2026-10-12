(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const section = $('spatial');
  if (!section) return;

  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const COLORS = { ok: '#52d7a2', stale: '#f2b95e', missing: '#ef8a80', waiting: '#71808c', track: '#48d6c5', coast: '#f2b95e' };
  let mapData = null, state = null, lastOk = 0, failures = 0, timer = 0, activeUnit = null;
  const camViews = new Map();
  const mapCanvas = $('spatial-map'), camCanvas = $('spatial-cam');
  const mctx = mapCanvas.getContext('2d'), cctx = camCanvas.getContext('2d');
  let mapLayer = null, mapView = null, listening = false;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v)) ? '—' : Number(v).toFixed(d);

  async function get(path) {
    const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', redirect: 'error' });
    if (response.status === 401) { window.location.assign('/login'); throw new Error('Session expired'); }
    if (!response.ok) throw new Error(`Local server returned ${response.status}`);
    return response.json();
  }

  // ---- top-down map -------------------------------------------------------------------------
  function fitCanvas(canvas) {
    const ratio = window.devicePixelRatio || 1, rect = canvas.getBoundingClientRect();
    const w = Math.max(200, Math.round(rect.width * ratio)), h = Math.max(160, Math.round(rect.height * ratio));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; return true; }
    return false;
  }

  function buildMapLayer() {
    if (!mapData) return;
    fitCanvas(mapCanvas);
    const { min, max } = mapData.bounds, pad = 28 * (window.devicePixelRatio || 1);
    const spanX = Math.max(0.5, max[0] - min[0]), spanY = Math.max(0.5, max[1] - min[1]);
    const scale = Math.min((mapCanvas.width - 2 * pad) / spanX, (mapCanvas.height - 2 * pad) / spanY);
    const ox = (mapCanvas.width - spanX * scale) / 2, oy = (mapCanvas.height - spanY * scale) / 2;
    mapView = { scale, px: x => ox + (x - min[0]) * scale, py: y => mapCanvas.height - (oy + (y - min[1]) * scale) };
    mapLayer = document.createElement('canvas'); mapLayer.width = mapCanvas.width; mapLayer.height = mapCanvas.height;
    const g = mapLayer.getContext('2d'), pts = mapData.points, zr = Math.max(0.1, max[2] - min[2]);
    for (let i = 0; i < pts.length; i += 3) {
      const t = (pts[i + 2] - min[2]) / zr;
      g.fillStyle = `rgba(${Math.round(60 + 90 * t)},${Math.round(120 + 90 * t)},${Math.round(130 + 70 * t)},${0.25 + 0.4 * t})`;
      g.fillRect(mapView.px(pts[i]), mapView.py(pts[i + 1]), 2, 2);
    }
    g.fillStyle = '#71808c'; g.font = `${11 * (window.devicePixelRatio || 1)}px monospace`;
    g.fillText('MAP FRAME · x → right  y ↑ forward · metres', 8, mapCanvas.height - 8);
    for (const unit of mapData.sensor_units) {
      const wedge = (poly, fill, stroke) => {
        g.beginPath(); poly.forEach(([x, y], i) => i ? g.lineTo(mapView.px(x), mapView.py(y)) : g.moveTo(mapView.px(x), mapView.py(y)));
        g.closePath(); g.fillStyle = fill; g.fill(); g.strokeStyle = stroke; g.lineWidth = 1; g.stroke();
      };
      wedge(unit.radar_fov, 'rgba(170,145,255,.07)', 'rgba(170,145,255,.5)');
      wedge(unit.camera_fov, 'rgba(72,214,197,.07)', 'rgba(72,214,197,.5)');
      const x = mapView.px(unit.position[0]), y = mapView.py(unit.position[1]);
      g.fillStyle = '#e7edf0'; g.fillRect(x - 5, y - 5, 10, 10);
      g.fillText(unit.unit_id + (unit.simulated ? ' (sim)' : ''), x + 9, y + 4);
    }
  }

  function drawMap() {
    if (!mapData) return;
    if (fitCanvas(mapCanvas) || !mapLayer) buildMapLayer();
    mctx.clearRect(0, 0, mapCanvas.width, mapCanvas.height);
    mctx.drawImage(mapLayer, 0, 0);
    if (!state) return;
    const dpr = window.devicePixelRatio || 1, stale = isStale();
    for (const track of state.tracks) {
      const [x, y] = track.position, cx = mapView.px(x), cy = mapView.py(y);
      const coast = track.state === 'coasting' || stale, color = coast ? COLORS.coast : COLORS.track;
      if (track.trail.length > 1) {
        mctx.beginPath(); track.trail.forEach(([tx, ty], i) => i ? mctx.lineTo(mapView.px(tx), mapView.py(ty)) : mctx.moveTo(mapView.px(tx), mapView.py(ty)));
        mctx.strokeStyle = color + '66'; mctx.lineWidth = 1.5 * dpr; mctx.stroke();
      }
      // 2-sigma axis-aligned ellipse of the track position estimate
      mctx.beginPath(); mctx.ellipse(cx, cy, Math.max(3, 2 * track.sigma_m[0] * mapView.scale), Math.max(3, 2 * track.sigma_m[1] * mapView.scale), 0, 0, Math.PI * 2);
      mctx.setLineDash(coast ? [4 * dpr, 3 * dpr] : []); mctx.strokeStyle = color; mctx.lineWidth = 1.5 * dpr; mctx.stroke(); mctx.setLineDash([]);
      mctx.beginPath(); mctx.arc(cx, cy, 4 * dpr, 0, Math.PI * 2);
      if (track.dimension === '3d') { mctx.fillStyle = color; mctx.fill(); } else { mctx.strokeStyle = color; mctx.stroke(); }
      mctx.beginPath(); mctx.moveTo(cx, cy); mctx.lineTo(cx + track.velocity[0] * mapView.scale, cy - track.velocity[1] * mapView.scale);
      mctx.strokeStyle = color; mctx.stroke();
      mctx.fillStyle = color; mctx.font = `${11 * dpr}px monospace`;
      mctx.fillText(`${track.id}${track.dimension === '3d' ? '' : ' · 2D'}${coast ? ' · predicted' : ''}`, cx + 8 * dpr, cy - 8 * dpr);
    }
  }

  // ---- synthetic camera view with radar markers ---------------------------------------------
  function drawCamera() {
    if (!activeUnit) return;
    const view = camViews.get(activeUnit), unit = state && state.units.find(u => u.unit_id === activeUnit);
    if (!view) return;
    const [iw, ih] = view.image_size;
    camCanvas.width = 960; camCanvas.height = Math.round(960 * ih / iw);
    cctx.fillStyle = '#080d13'; cctx.fillRect(0, 0, camCanvas.width, camCanvas.height);
    cctx.font = '13px monospace';
    if (!view.image_ready) {
      cctx.fillStyle = COLORS.stale; cctx.fillText('Camera overlay suppressed: calibration missing (see unit status).', 16, 28); return;
    }
    const p = view.points;
    for (let i = 0; i < p.length; i += 3) {
      const shade = Math.max(60, 200 - p[i + 2] * 12);
      cctx.fillStyle = `rgb(${shade * .55},${shade * .8},${shade * .85})`;
      cctx.fillRect(p[i] * camCanvas.width, p[i + 1] * camCanvas.height, 2, 2);
    }
    const stale = isStale();
    for (const person of (state && state.simulated_people) || []) {
      if (person.unit_id !== activeUnit || person.u === null) continue;
      const px = person.u * camCanvas.width, py = person.v * camCanvas.height;
      cctx.strokeStyle = '#8797a0'; cctx.lineWidth = 2; cctx.beginPath(); cctx.ellipse(px, py - 40, 9, 11, 0, 0, Math.PI * 2);
      cctx.moveTo(px, py - 29); cctx.lineTo(px, py + 8); cctx.stroke();
      cctx.fillStyle = '#8797a0'; cctx.fillText('simulated walker', px + 14, py - 30);
    }
    for (const track of (state ? state.tracks : [])) {
      const marker = track.image.find(m => m.unit_id === activeUnit);
      if (!marker || !marker.visible) continue;
      const x = marker.u * camCanvas.width, y = marker.v * camCanvas.height, r = Math.max(8, marker.radius_norm * camCanvas.width);
      const coast = track.state === 'coasting' || stale, color = coast ? COLORS.coast : COLORS.track;
      cctx.strokeStyle = color; cctx.lineWidth = 2; cctx.setLineDash(coast || track.dimension !== '3d' || !marker.calibration_error_quantified ? [6, 4] : []);
      cctx.beginPath(); cctx.arc(x, y, r, 0, Math.PI * 2); cctx.stroke(); cctx.setLineDash([]);
      cctx.beginPath(); cctx.moveTo(x - 6, y); cctx.lineTo(x + 6, y); cctx.moveTo(x, y - 6); cctx.lineTo(x, y + 6); cctx.stroke();
      cctx.fillStyle = color;
      cctx.fillText(`≈ ${track.id} · ${fmt(marker.depth_m)} m${track.dimension === '3d' ? '' : ' · height unknown'}${marker.calibration_error_quantified ? '' : ' · calib. error unquantified'}`, x + r + 6, y - 4);
    }
    cctx.fillStyle = '#71808c';
    cctx.fillText(view.simulated_video ? 'SIMULATED VIDEO. Markers use the saved calibration; an offset from the walker is calibration error.'
      : 'SYNTHETIC VIEW of map points through the camera model — not video. Markers are approximate radar reference points, not body outlines.', 12, camCanvas.height - 12);
  }

  // ---- status panels ------------------------------------------------------------------------
  function isStale() { return !lastOk || performance.now() - lastOk > 3000; }

  function chip(text, status) {
    const node = el('span', `sp-chip sp-${status}`, text);
    return node;
  }

  function renderBanner() {
    const banner = $('spatial-banner');
    banner.replaceChildren();
    if (!state) { banner.append(el('span', 'sp-msg', 'Waiting for spatial data…')); return; }
    if (state.data_origin !== 'live') banner.append(chip(state.data_origin === 'simulated' ? 'SIMULATED DATA' : 'RECORDED DATA REPLAY', 'stale'));
    banner.append(chip('APPROXIMATE · NOT SAFETY-RATED', 'waiting'));
    if (isStale()) banner.append(chip('LOCAL SERVER UNREACHABLE — showing last known state', 'missing'));
    banner.append(el('span', 'sp-msg', `${state.site.name} · map r${state.map.revision} · trail ${state.retention.track_history_seconds}s in memory · video retention: none`));
  }

  function renderUnits() {
    const host = $('spatial-units');
    host.replaceChildren();
    if (!state) return;
    for (const unit of state.units) {
      const card = el('div', 'sp-unit');
      const head = el('div', 'sp-unit-head');
      head.append(el('strong', '', `${unit.unit_id} · ${unit.label}`));
      head.append(chip(`RADAR ${unit.radar.status.toUpperCase()}${unit.radar.age_s === null ? '' : ` · ${fmt(unit.radar.age_s)}s ago`}`, unit.radar.status));
      head.append(chip('CAMERA STREAM NOT MONITORED', 'waiting'));
      head.append(chip(unit.placed ? 'PLACED IN MAP' : 'NOT PLACED', unit.placed ? 'ok' : 'missing'));
      head.append(chip(unit.image_ready ? 'IMAGE OVERLAY READY' : 'OVERLAY SUPPRESSED', unit.image_ready ? 'ok' : 'missing'));
      card.append(head);
      const table = el('table', 'sp-table');
      const header = el('tr'); ['Transform', 'Status', 'Quality', 'Rev', 'Calibrated', 'Declared accuracy (1σ)'].forEach(h => header.append(el('th', '', h))); table.append(header);
      for (const cal of unit.calibrations) {
        const row = el('tr');
        row.append(el('td', '', cal.name.replace(`${unit.unit_id}: `, '')));
        const status = el('td'); status.append(chip(cal.status.toUpperCase(), cal.status === 'verified' || cal.status === 'measured' ? 'ok' : cal.status === 'uncalibrated' ? 'missing' : 'stale')); row.append(status);
        const q = el('td');
        const grade = { good: ['GOOD', 'ok'], fair: ['FAIR', 'stale'], poor: ['POOR', 'missing'], insufficient: ['NOT ENOUGH DATA', 'waiting'], unassessed: ['NOT MEASURED', 'waiting'] }[cal.quality || 'unassessed'];
        q.append(chip(grade[0], grade[1])); if (cal.evidence_points) q.append(el('span', 'sp-msg', ` ${cal.evidence_points} spots`)); row.append(q);
        row.append(el('td', '', String(cal.revision)), el('td', '', cal.calibrated_at ? cal.calibrated_at.slice(0, 10) : '—'));
        row.append(el('td', '', cal.quantified ? `±${fmt(cal.position_sigma_m, 3)} m / ±${fmt(cal.rotation_sigma_deg)}°` : 'not declared'));
        table.append(row);
      }
      card.append(table);
      if (unit.warnings.length) { const list = el('ul', 'sp-warn'); unit.warnings.forEach(w => list.append(el('li', '', w))); card.append(list); }
      host.append(card);
    }
  }

  function renderTracks() {
    const host = $('spatial-tracks');
    host.replaceChildren();
    if (!state) return;
    $('spatial-track-count').textContent = String(state.tracks.length);
    if (!state.tracks.length) { host.append(el('div', 'sp-empty', 'No confirmed radar tracks. IDs are anonymous and carry no identity.')); return; }
    const table = el('table', 'sp-table');
    const header = el('tr'); ['ID', 'State', 'Pos x,y,z (m)', 'Speed', '±σ x,y,z (m)', 'Dim.', 'Since upd.', 'SNR'].forEach(h => header.append(el('th', '', h))); table.append(header);
    for (const t of state.tracks) {
      const row = el('tr');
      row.append(el('td', '', t.id), el('td', '', t.state), el('td', '', t.position.map(v => fmt(v)).join(', ')),
        el('td', '', `${fmt(t.speed_mps)} m/s`), el('td', '', t.sigma_m.map(v => fmt(v, 2)).join(', ')),
        el('td', '', t.dimension === '3d' ? '3D' : '2D · height assumed'), el('td', '', `${fmt(t.since_update_s)} s`),
        el('td', '', t.quality.mean_snr_db === null ? '—' : `${fmt(t.quality.mean_snr_db)} dB`));
      table.append(row);
    }
    host.append(table);
  }

  function renderAll() { renderBanner(); renderUnits(); renderTracks(); drawMap(); drawCamera(); }

  async function poll() {
    clearTimeout(timer);
    if (document.visibilityState === 'visible') {
      try { state = await get('/api/spatial/state'); lastOk = performance.now(); failures = 0; renderAll(); }
      catch (error) { failures++; renderBanner(); drawMap(); drawCamera(); }
    }
    timer = setTimeout(poll, Math.min(5000, 500 * 2 ** Math.min(failures, 4)));
  }

  async function boot() {
    let status;
    try { status = await get('/api/spatial/status'); } catch (_) { return; }
    if (!status.enabled) {
      if (status.error) { section.hidden = false; $('spatial-banner').replaceChildren(chip('SPATIAL TRACKING DISABLED', 'missing'), el('span', 'sp-msg', status.error)); }
      return;
    }
    section.hidden = false; document.querySelectorAll('.spatial-nav').forEach(n => { n.hidden = false; });
    try {
      mapData = await get('/api/spatial/map');
      state = await get('/api/spatial/state'); lastOk = performance.now();
      for (const unit of state.units) camViews.set(unit.unit_id, await get(`/api/spatial/camera/${encodeURIComponent(unit.unit_id)}`));
      activeUnit = state.units[0] && state.units[0].unit_id;
    } catch (error) { $('spatial-banner').replaceChildren(chip('SPATIAL DATA UNAVAILABLE', 'missing'), el('span', 'sp-msg', error.message)); return; }
    $('spatial-map-sub').textContent = `${mapData.map.name} · r${mapData.map.revision} · ${mapData.map.points_shown.toLocaleString()} of ${mapData.map.points_total.toLocaleString()} points shown`;
    buildMapLayer(); renderAll();
    if (!listening) {
      listening = true;
      window.addEventListener('resize', () => { mapLayer = null; drawMap(); });
      document.addEventListener('visibilitychange', poll);
    }
    poll();
  }
  window.omniSpatial = { start: () => { clearTimeout(timer); mapLayer = null; boot(); } };
  if (!section.hasAttribute('data-manual')) boot();
})();
