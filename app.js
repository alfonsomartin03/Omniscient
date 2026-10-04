(() => {
  const $ = id => document.getElementById(id);
  const video = $('video'), stage = $('video-stage'), overlay = $('overlay');
  const ctx = overlay.getContext('2d');
  const probe = document.createElement('canvas'), pctx = probe.getContext('2d');
  const FRAME_W = 640, FRAME_H = 360;
  probe.width = FRAME_W; probe.height = FRAME_H;
  const events = [], spaces = [];
  let csrf = '', running = false, modelReady = false, modelStatus = 'Local model unavailable', analysisTimer = 0, sourceGeneration = 0;
  let sourceObjectUrl = null, cameraStream = null, drawingSpace = false, draftPoints = [], latestObjects = [], sourceStartedAt = 0;

  const pad = value => String(value).padStart(2, '0');
  const escapeHTML = value => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  const clockText = () => new Date().toLocaleTimeString([], { hour12: false });
  function updateClock() { $('clock').textContent = clockText(); $('stage-time').textContent = clockText(); }
  updateClock(); setInterval(updateClock, 1000);

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== 'GET') {
      headers.set('Content-Type', options.body instanceof Blob ? 'image/jpeg' : 'application/json');
      headers.set('X-Omni-CSRF', csrf);
    }
    const response = await fetch(path, { ...options, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error' });
    if (response.status === 401) { window.location.assign('/login'); throw new Error('Session expired'); }
    if (response.status === 429 && path === '/api/frame') return { busy: true };
    if (!response.ok) throw new Error(`Local server returned ${response.status}`);
    return response.json();
  }

  function setSignal(label, subtitle) {
    $('feed-label').textContent = label.toUpperCase(); $('feed-subtitle').textContent = subtitle;
    $('source-status').textContent = label; $('signal-note').textContent = subtitle;
    $('source-dot').classList.toggle('on', running);
    $('signal-health').innerHTML = running ? 'LOCAL<small> · HTTPS</small>' : '—<small> NO SIGNAL</small>';
    $('signal-health').style.color = running ? 'var(--green)' : '';
    $('frame-info').textContent = running ? 'SERVER ANALYSIS ACTIVE' : 'ANALYSIS STANDBY';
    $('empty-state').classList.toggle('hidden', running); stage.classList.toggle('has-video', running);
    document.querySelector('.panel-live-dot').style.background = running ? 'var(--green)' : '';
    $('calibrate-button').disabled = !running;
  }

  function addInsight(event) {
    const item = { ...event, time: clockText(), stamp: Date.now() };
    events.unshift(item); if (events.length > 35) events.pop(); renderEvents();
    const marker = document.createElement('i'); marker.className = 'timeline-mark';
    const position = video.duration && Number.isFinite(video.duration) ? video.currentTime / video.duration : (Date.now() - sourceStartedAt) / 3_600_000;
    marker.style.left = `${Math.max(0, Math.min(100, position * 100))}%`;
    const markers = $('timeline-markers'); markers.append(marker);
    while (markers.childElementCount > 35) markers.firstElementChild.remove();
  }

  function renderEvents() {
    $('event-count').textContent = pad(events.length); $('nav-event-count').textContent = events.length;
    $('insight-count').textContent = events.length; $('event-note').textContent = events.length ? 'Activity detected on this feed' : 'No events recorded';
    const list = $('insights-list');
    list.innerHTML = events.length ? events.map(event => {
      const level = ['info', 'medium', 'high'].includes(event.level) ? event.level : 'info';
      return `<article class="insight-item"><div class="insight-top"><span class="severity ${level}">${level.toUpperCase()}</span><span class="insight-time">${escapeHTML(event.time)}</span></div><strong>${escapeHTML(event.title)}</strong><p>${escapeHTML(event.detail)}</p>${event.trackId ? `<div class="insight-track">OBJECT ${Number(event.trackId).toString().padStart(3, '0')}</div>` : ''}</article>`;
    }).join('') : '<div class="insights-empty"><span>⌁</span><strong>All quiet</strong><p>Security insights will appear here as activity is detected.</p></div>';
    renderActivityChart();
  }

  function renderActivityChart() {
    const line = $('chart-line'), fill = $('chart-fill'); if (!line || !fill) return;
    const counts = new Array(24).fill(0);
    for (const event of events) { const date = new Date(event.stamp); if (date.toDateString() === new Date().toDateString()) counts[date.getHours()]++; }
    const max = Math.max(1, ...counts), points = counts.map((count, hour) => `${Math.round(hour * 30)},${Math.round(105 - count / max * 76)}`);
    const path = `M${points.join(' L')}`; line.setAttribute('d', path); fill.setAttribute('d', `${path} L720,118 L0,118Z`);
  }

  function renderSpaces() {
    $('zone-count').textContent = spaces.length; $('zone-metric-count').textContent = spaces.length;
    const list = $('zones-list');
    list.innerHTML = spaces.length ? spaces.map((space, index) => `<div class="zone-row"><div class="zone-thumb thumb-restricted"><span>${String(index + 1).padStart(2, '0')}</span><i></i></div><div class="zone-copy"><strong>${escapeHTML(space.name)}</strong><small>Custom polygon · ${Number(space.events) || 0} entries</small></div><button class="zone-remove" data-zone-id="${Number(space.id)}" aria-label="Remove ${escapeHTML(space.name)}">×</button></div>`).join('') : '<div class="zones-empty">No restricted spaces configured. Add one to alert on objects entering it.</div>';
    list.querySelectorAll('[data-zone-id]').forEach(button => button.addEventListener('click', async () => {
      try { const result = await api(`/api/zones/${button.dataset.zoneId}`, { method: 'DELETE', body: '{}' }); spaces.splice(0, spaces.length, ...result.zones); renderSpaces(); drawOverlay(); }
      catch (error) { addInsight({ level: 'medium', title: 'Server action failed', detail: error.message }); }
    }));
    drawOverlay();
  }

  function drawOverlay() {
    if (!video.videoWidth || !video.videoHeight) return;
    const scale = Math.min(1280 / video.videoWidth, 720 / video.videoHeight, 1);
    const width = Math.max(1, Math.round(video.videoWidth * scale)), height = Math.max(1, Math.round(video.videoHeight * scale));
    if (overlay.width !== width || overlay.height !== height) { overlay.width = width; overlay.height = height; }
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    for (const space of spaces) {
      if (space.points.length < 3) continue;
      ctx.beginPath(); ctx.moveTo(space.points[0].x * overlay.width, space.points[0].y * overlay.height);
      for (const point of space.points.slice(1)) ctx.lineTo(point.x * overlay.width, point.y * overlay.height);
      ctx.closePath(); ctx.fillStyle = '#eab05d15'; ctx.fill(); ctx.strokeStyle = '#eab05d'; ctx.lineWidth = Math.max(2, overlay.width / 640); ctx.setLineDash([7, 5]); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = '#eab05d'; ctx.font = `${Math.max(11, overlay.width / 80)}px monospace`; ctx.fillText(String(space.name).toUpperCase().slice(0, 48), space.points[0].x * overlay.width + 5, space.points[0].y * overlay.height - 6);
    }
    if (draftPoints.length) {
      ctx.beginPath(); ctx.moveTo(draftPoints[0].x * overlay.width, draftPoints[0].y * overlay.height);
      for (const point of draftPoints.slice(1)) ctx.lineTo(point.x * overlay.width, point.y * overlay.height);
      ctx.strokeStyle = '#48d6c5'; ctx.lineWidth = 2; ctx.setLineDash([6, 4]); ctx.stroke(); ctx.setLineDash([]);
      for (const point of draftPoints) { ctx.beginPath(); ctx.arc(point.x * overlay.width, point.y * overlay.height, 4, 0, Math.PI * 2); ctx.fillStyle = '#48d6c5'; ctx.fill(); }
    }
    for (const object of latestObjects) {
      const color = object.danger === 'high' ? '#f0796e' : object.danger === 'medium' ? '#eab05d' : '#48d6c5';
      const x = object.x * overlay.width, y = object.y * overlay.height, w = object.w * overlay.width, h = object.h * overlay.height;
      ctx.strokeStyle = color; ctx.lineWidth = Math.max(2, overlay.width / 640); ctx.strokeRect(x, y, w, h);
      const label = `${String(object.label || 'object').toUpperCase()} #${String(object.id).padStart(3, '0')} · ${object.danger.toUpperCase()}${object.stale ? ' · TRACKING' : ''}`;
      ctx.font = `${Math.max(11, overlay.width / 80)}px monospace`; const tw = ctx.measureText(label).width + 9;
      ctx.fillStyle = color; ctx.fillRect(x, Math.max(0, y - 17), tw, 17); ctx.fillStyle = '#071411'; ctx.fillText(label, x + 4, Math.max(12, y - 5));
    }
  }

  async function sendFrame(generation) {
    if (!running || !modelReady || video.paused || video.readyState < 2) return;
    try {
      // Encode a bounded JPEG; only the authenticated local endpoint receives it.
      const scale = Math.min(FRAME_W / video.videoWidth, FRAME_H / video.videoHeight, 1);
      const frameWidth = Math.max(16, Math.round(video.videoWidth * scale));
      const frameHeight = Math.max(16, Math.round(video.videoHeight * scale));
      if (probe.width !== frameWidth || probe.height !== frameHeight) { probe.width = frameWidth; probe.height = frameHeight; }
      pctx.drawImage(video, 0, 0, frameWidth, frameHeight);
      const frame = await new Promise(resolve => probe.toBlob(resolve, 'image/jpeg', 0.82));
      if (!frame || generation !== sourceGeneration) return;
      const result = await api('/api/frame', { method: 'POST', body: frame });
      if (generation !== sourceGeneration || result.busy) return;
      latestObjects = result.objects; $('active-count').textContent = pad(latestObjects.length);
      let zoneCountsChanged = false;
      for (const counts of result.zoneEvents || []) {
        const space = spaces.find(candidate => candidate.id === counts.id);
        if (space && space.events !== counts.events) { space.events = counts.events; zoneCountsChanged = true; }
      }
      if (zoneCountsChanged) renderSpaces();
      for (const event of result.events) addInsight(event);
      if (result.calibration < 100) {
        $('calibrate-button').textContent = `Calibrating ${result.calibration}%`;
        $('signal-note').textContent = `Learning normal view · ${result.calibration}%`;
        $('frame-info').textContent = 'CALIBRATING NORMAL VIEW';
      } else if ($('calibrate-button').textContent !== 'Recalibrate view') {
        $('calibrate-button').textContent = 'Recalibrate view'; $('signal-note').textContent = 'Normal view learned · server watching for changes'; $('frame-info').textContent = 'SERVER ANALYSIS ACTIVE';
      }
      drawOverlay();
    } catch (error) {
      if (generation !== sourceGeneration) return;
      console.error('Local analysis request failed', error);
      if (error.message.includes('503')) {
        modelReady = false;
        $('system-status').innerHTML = '<i></i> LOCAL MODEL ERROR';
      }
      $('signal-note').textContent = 'Local server analysis unavailable';
      $('frame-info').textContent = 'SERVER CONNECTION ERROR';
    }
  }

  async function analyzeLoop(generation) {
    if (!running || generation !== sourceGeneration || video.paused) return;
    await sendFrame(generation);
    if (!running || generation !== sourceGeneration) return;
    if (video.duration && Number.isFinite(video.duration)) {
      $('timeline-progress').style.width = `${(video.currentTime / video.duration) * 100}%`;
      $('video-time').textContent = `${Math.floor(video.currentTime / 60)}:${pad(Math.floor(video.currentTime % 60))} / ${Math.floor(video.duration / 60)}:${pad(Math.floor(video.duration % 60))}`;
    } else $('video-time').textContent = clockText();
    analysisTimer = setTimeout(() => analyzeLoop(generation), modelReady ? 350 : 1000);
  }

  function stopAnalysis() { sourceGeneration++; clearTimeout(analysisTimer); analysisTimer = 0; }

  async function beginSource(kind, subtitle) {
    stopAnalysis();
    const generation = sourceGeneration;
    running = true; latestObjects = []; sourceStartedAt = Date.now(); $('timeline-markers').replaceChildren(); $('play-toggle').textContent = 'Ⅱ';
    setSignal(kind, subtitle); $('calibrate-button').textContent = 'Calibrating…';
    if (!modelReady) { $('signal-note').textContent = modelStatus; $('frame-info').textContent = 'MODEL SETUP REQUIRED'; $('calibrate-button').textContent = 'Model setup required'; }
    try { await api('/api/calibrate', { method: 'POST', body: '{}' }); }
    catch (error) { $('signal-note').textContent = `Local server unavailable: ${error.message}`; }
    if (generation !== sourceGeneration) return;
    try { await video.play(); analysisTimer = setTimeout(() => analyzeLoop(generation), 0); }
    catch (_) { $('play-toggle').textContent = '▶'; }
  }

  $('upload-trigger').addEventListener('click', () => $('file-input').click());
  $('add-source').addEventListener('click', () => $('file-input').click());
  $('file-input').addEventListener('change', event => {
    const file = event.target.files && event.target.files[0]; if (!file) return;
    if (sourceObjectUrl) URL.revokeObjectURL(sourceObjectUrl);
    if (cameraStream) { cameraStream.getTracks().forEach(track => track.stop()); cameraStream = null; }
    sourceObjectUrl = URL.createObjectURL(file); video.srcObject = null; video.src = sourceObjectUrl; video.load();
    video.onloadedmetadata = () => beginSource(file.name, 'Recorded video · analysis on local server');
    video.onerror = () => { stopAnalysis(); running = false; setSignal('Source error', 'This video could not be opened'); };
  });
  $('camera-trigger').addEventListener('click', async () => {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { addInsight({ level: 'medium', title: 'Camera unavailable', detail: 'Camera access requires HTTPS in a supported browser.' }); return; }
    try {
      const nextStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
      if (cameraStream) cameraStream.getTracks().forEach(track => track.stop());
      if (sourceObjectUrl) { URL.revokeObjectURL(sourceObjectUrl); sourceObjectUrl = null; }
      cameraStream = nextStream;
      video.removeAttribute('src'); video.srcObject = cameraStream;
      video.onloadedmetadata = () => beginSource('Live camera', 'Camera feed · analysis on local server');
    } catch (error) {
      addInsight({ level: 'medium', title: 'Camera connection failed', detail: error.name === 'NotAllowedError' ? 'Camera permission was denied. Allow access and try again.' : 'No camera feed could be opened.' });
    }
  });
  $('play-toggle').addEventListener('click', async () => {
    if (!running) return;
    stopAnalysis();
    if (video.paused) {
      const generation = sourceGeneration;
      try { await video.play(); analysisTimer = setTimeout(() => analyzeLoop(generation), 0); }
      catch (_) { $('play-toggle').textContent = '▶'; }
    } else { video.pause(); $('play-toggle').textContent = '▶'; }
  });
  $('fullscreen').addEventListener('click', () => { if (stage.requestFullscreen) stage.requestFullscreen(); });
  $('clear-events').addEventListener('click', () => { events.length = 0; $('timeline-markers').replaceChildren(); renderEvents(); });
  $('calibrate-button').addEventListener('click', async () => {
    if (!running) return;
    $('calibrate-button').textContent = 'Calibrating…';
    try { await api('/api/calibrate', { method: 'POST', body: '{}' }); }
    catch (error) { addInsight({ level: 'medium', title: 'Calibration failed', detail: error.message }); }
  });

  function beginZoneDrawing() {
    if (!running) { addInsight({ level: 'medium', title: 'Connect a camera first', detail: 'A video source is required before drawing restricted spaces.' }); return; }
    drawingSpace = true; draftPoints = []; stage.classList.add('drawing'); $('draw-hint').classList.add('visible'); $('zones-toggle').textContent = 'Cancel drawing'; drawOverlay();
  }
  function cancelZoneDrawing() {
    drawingSpace = false; draftPoints = []; stage.classList.remove('drawing'); $('draw-hint').classList.remove('visible'); $('zones-toggle').textContent = '＋ Add area'; drawOverlay();
  }
  async function finishZoneDrawing() {
    if (!drawingSpace || draftPoints.length < 3) return;
    const name = `Restricted space ${String(spaces.length + 1).padStart(2, '0')}`;
    try {
      const result = await api('/api/zones', { method: 'POST', body: JSON.stringify({ name, points: draftPoints }) });
      spaces.splice(0, spaces.length, ...result.zones); cancelZoneDrawing(); renderSpaces();
    } catch (error) { addInsight({ level: 'medium', title: 'Could not save restricted space', detail: error.message }); }
  }
  $('zones-toggle').addEventListener('click', () => drawingSpace ? cancelZoneDrawing() : beginZoneDrawing());
  overlay.addEventListener('click', event => {
    if (!drawingSpace || !video.videoWidth) return;
    const rect = stage.getBoundingClientRect(), scale = Math.min(rect.width / video.videoWidth, rect.height / video.videoHeight);
    const offsetX = (rect.width - video.videoWidth * scale) / 2, offsetY = (rect.height - video.videoHeight * scale) / 2;
    const x = (event.clientX - rect.left - offsetX) / scale / video.videoWidth, y = (event.clientY - rect.top - offsetY) / scale / video.videoHeight;
    if (x >= 0 && x <= 1 && y >= 0 && y <= 1) { draftPoints.push({ x, y }); drawOverlay(); }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && drawingSpace) cancelZoneDrawing();
    if (event.key === 'Enter' && drawingSpace) finishZoneDrawing();
  });
  $('logout-button').addEventListener('click', async () => {
    try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch (_) {}
    window.location.assign('/login');
  });
  video.addEventListener('ended', () => { stopAnalysis(); latestObjects = []; $('active-count').textContent = '00'; drawOverlay(); $('play-toggle').textContent = '↻'; });
  video.addEventListener('play', () => { if (running) $('play-toggle').textContent = 'Ⅱ'; });
  window.addEventListener('resize', drawOverlay);

  async function boot() {
    try {
      const session = await api('/api/session'); csrf = session.csrf;
      const data = await api('/api/zones'); spaces.splice(0, spaces.length, ...data.zones);
      renderSpaces(); renderEvents(); setSignal('Waiting for signal', 'Connect a source · processing remains on this server');
      modelReady = Boolean(session.modelReady); modelStatus = session.modelStatus;
      $('system-status').innerHTML = modelReady ? '<i></i> LOCAL MODEL READY' : '<i></i> MODEL SETUP REQUIRED';
      if (!session.modelReady) { $('signal-note').textContent = session.modelStatus; $('frame-info').textContent = 'MODEL SETUP REQUIRED'; }
    } catch (error) {
      $('signal-note').textContent = 'Unable to initialize secure local session'; console.error(error);
    }
  }
  boot();
})();
