(() => {
  const $ = (id) => document.getElementById(id);
  const video = $('video');
  const stage = $('video-stage');
  const overlay = $('overlay');
  const ctx = overlay.getContext('2d');
  const probe = document.createElement('canvas');
  const pctx = probe.getContext('2d', { willReadFrequently: true });
  const W = 160, H = 90, CELL = 4;
  probe.width = W; probe.height = H;
  const tracks = new Map();
  const events = [];
  let previous = null, nextId = 1, running = false, raf = 0, lastAnalysis = 0;
  let entryEvents = 0, restrictedEvents = 0, sourceObjectUrl = null, cameraStream = null;
  let eventCooldown = 0;

  const pad = (value) => String(value).padStart(2, '0');
  const clockText = () => new Date().toLocaleTimeString([], { hour12: false });
  function updateClock() { $('clock').textContent = clockText(); $('stage-time').textContent = clockText(); }
  updateClock(); setInterval(updateClock, 1000);

  function setSignal(label, subtitle) {
    $('feed-label').textContent = label.toUpperCase();
    $('feed-subtitle').textContent = subtitle;
    $('source-status').textContent = label;
    $('signal-note').textContent = subtitle;
    $('source-dot').classList.toggle('on', running);
    $('signal-health').innerHTML = running ? 'GOOD<small> · 30 FPS</small>' : '—<small> NO SIGNAL</small>';
    $('signal-health').style.color = running ? 'var(--green)' : '';
    $('frame-info').textContent = running ? 'MOTION ANALYSIS ACTIVE' : 'ANALYSIS STANDBY';
    $('empty-state').classList.toggle('hidden', running);
    stage.classList.toggle('has-video', running);
    document.querySelectorAll('.zone').forEach(zone => zone.classList.toggle('visible', running));
    document.querySelector('.panel-live-dot').style.background = running ? 'var(--green)' : '';
    $('zone-count').textContent = '02';
  }

  function beginSource(kind, subtitle) {
    previous = null; tracks.clear(); nextId = 1;
    running = true;
    $('play-toggle').textContent = 'Ⅱ';
    $('signal-health').style.color = 'var(--green)';
    setSignal(kind, subtitle);
    video.play().catch(() => {});
    cancelAnimationFrame(raf); raf = requestAnimationFrame(analyzeLoop);
  }

  function addInsight(level, title, detail, trackId = null) {
    const now = Date.now();
    if (now - eventCooldown < 900) return;
    eventCooldown = now;
    const time = clockText();
    const event = { level, title, detail, trackId, time, stamp: now };
    events.unshift(event); if (events.length > 35) events.pop();
    renderEvents();
    const marker = document.createElement('i'); marker.className = 'timeline-mark';
    marker.style.left = `${8 + Math.random() * 84}%`; $('timeline-markers').append(marker);
    if (title.includes('Entry')) { entryEvents++; $('zone-entry-events').textContent = entryEvents; }
    if (title.includes('Restricted')) { restrictedEvents++; $('zone-restricted-events').textContent = restrictedEvents; }
  }

  function renderEvents() {
    $('event-count').textContent = pad(events.length);
    $('nav-event-count').textContent = events.length;
    $('insight-count').textContent = events.length;
    $('event-note').textContent = events.length ? 'Activity detected on this feed' : 'No events recorded';
    const list = $('insights-list');
    if (!events.length) {
      list.innerHTML = '<div class="insights-empty"><span>⌁</span><strong>All quiet</strong><p>Security insights will appear here as activity is detected.</p></div>';
      renderActivityChart();
      return;
    }
    list.innerHTML = events.map(event => `<article class="insight-item"><div class="insight-top"><span class="severity ${event.level}">${event.level.toUpperCase()}</span><span class="insight-time">${event.time}</span></div><strong>${event.title}</strong><p>${event.detail}</p>${event.trackId ? `<div class="insight-track">TRACK ${String(event.trackId).padStart(3, '0')}</div>` : ''}</article>`).join('');
    renderActivityChart();
  }

  function renderActivityChart() {
    const line = $('chart-line'), fill = $('chart-fill');
    if (!line || !fill) return;
    const counts = new Array(24).fill(0);
    for (const event of events) {
      const date = new Date(event.stamp);
      if (date.toDateString() === new Date().toDateString()) counts[date.getHours()]++;
    }
    const max = Math.max(1, ...counts), points = counts.map((count, hour) => `${Math.round(hour * 30)},${Math.round(105 - count / max * 76)}`);
    const path = `M${points.join(' L')}`;
    line.setAttribute('d', path);
    fill.setAttribute('d', `${path} L720,118 L0,118Z`);
  }

  function classifyZone(x) { return x < W * .34 ? 'entry' : x > W * .66 ? 'restricted' : 'center'; }
  function detectMotion() {
    pctx.drawImage(video, 0, 0, W, H);
    const data = pctx.getImageData(0, 0, W, H).data;
    const cols = W / CELL, rows = H / CELL, size = cols * rows;
    const current = new Uint8Array(size);
    for (let gy = 0; gy < rows; gy++) for (let gx = 0; gx < cols; gx++) {
      const i = ((gy * CELL + 1) * W + gx * CELL + 1) * 4;
      const lum = (data[i] * 3 + data[i + 1] * 6 + data[i + 2]) / 10;
      current[gy * cols + gx] = lum;
    }
    if (!previous) { previous = current; return []; }
    const moving = new Uint8Array(size);
    let movingCount = 0;
    for (let i = 0; i < size; i++) {
      if (Math.abs(current[i] - previous[i]) > 25) { moving[i] = 1; movingCount++; }
    }
    previous = current;
    // A global exposure change usually means camera auto-adjustment, not a moving object.
    if (movingCount > size * .46) return [];
    const seen = new Uint8Array(size), boxes = [];
    for (let start = 0; start < size; start++) {
      if (!moving[start] || seen[start]) continue;
      const queue = [start]; seen[start] = 1;
      let minX = cols, minY = rows, maxX = 0, maxY = 0, count = 0, sumX = 0, sumY = 0;
      for (let q = 0; q < queue.length; q++) {
        const index = queue[q], x = index % cols, y = (index / cols) | 0;
        count++; minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y); sumX += x; sumY += y;
        for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
          if (!dx && !dy) continue;
          const nx = x + dx, ny = y + dy, ni = ny * cols + nx;
          if (nx >= 0 && nx < cols && ny >= 0 && ny < rows && moving[ni] && !seen[ni]) { seen[ni] = 1; queue.push(ni); }
        }
      }
      if (count >= 5 && maxX - minX >= 1 && maxY - minY >= 1) boxes.push({ x: minX * CELL, y: minY * CELL, w: (maxX - minX + 1) * CELL, h: (maxY - minY + 1) * CELL, cx: sumX / count, cy: sumY / count, area: count });
    }
    return boxes.sort((a, b) => b.area - a.area).slice(0, 8);
  }

  function updateTracks(boxes) {
    const now = Date.now(), matched = new Set();
    for (const box of boxes) {
      let candidate = null, distance = 14;
      for (const track of tracks.values()) {
        if (matched.has(track.id)) continue;
        const d = Math.hypot(track.cx - box.cx, track.cy - box.cy);
        if (d < distance) { candidate = track; distance = d; }
      }
      const zone = classifyZone(box.cx * CELL);
      if (!candidate) {
        candidate = { id: nextId++, cx: box.cx, cy: box.cy, born: now, lastSeen: now, zone, box, dwellAlerted: false, zoneAlerted: false };
        tracks.set(candidate.id, candidate);
        if (zone === 'center') addInsight('info', 'Motion detected', 'Movement detected in the central area of the frame.', candidate.id);
        if (zone === 'restricted') {
          candidate.zoneAlerted = true;
          addInsight('alert', 'Restricted area activity', 'Motion track first appeared inside the restricted zone. Review the clip for context.', candidate.id);
        } else if (zone === 'entry') {
          candidate.zoneAlerted = true;
          addInsight('warning', 'Entry zone activity', 'Motion track first appeared inside the entry zone.', candidate.id);
        }
      } else {
        const oldZone = candidate.zone;
        candidate.cx = box.cx; candidate.cy = box.cy; candidate.zone = zone; candidate.box = box; candidate.lastSeen = now;
        if (zone !== oldZone && zone !== 'center' && !candidate.zoneAlerted) {
          candidate.zoneAlerted = true;
          const restricted = zone === 'restricted';
          addInsight(restricted ? 'alert' : 'warning', restricted ? 'Restricted area activity' : 'Entry zone activity', `Track moved into the ${zone === 'entry' ? 'entry' : 'restricted'} zone. Review the clip for context.`, candidate.id);
        }
        if (now - candidate.born > 8000 && !candidate.dwellAlerted) {
          candidate.dwellAlerted = true;
          addInsight(zone === 'restricted' ? 'alert' : 'warning', 'Extended presence', `Track remained visible for more than 8 seconds in the ${zone === 'center' ? 'central' : zone} area.`, candidate.id);
        }
      }
      matched.add(candidate.id);
    }
    for (const [id, track] of tracks) if (now - track.lastSeen > 1200) tracks.delete(id);
    $('active-count').textContent = pad(tracks.size);
    drawTracks();
  }

  function drawTracks() {
    const bounds = overlay.getBoundingClientRect();
    if (!bounds.width || !bounds.height || !video.videoWidth) return;
    if (overlay.width !== video.videoWidth || overlay.height !== video.videoHeight) { overlay.width = video.videoWidth; overlay.height = video.videoHeight; }
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    for (const track of tracks.values()) {
      const b = track.box, zone = track.zone;
      ctx.strokeStyle = zone === 'restricted' ? '#f0b65f' : '#48d6c5'; ctx.lineWidth = Math.max(2, video.videoWidth / 640);
      ctx.strokeRect(b.x * video.videoWidth / W, b.y * video.videoHeight / H, b.w * video.videoWidth / W, b.h * video.videoHeight / H);
      const label = `TRACK ${String(track.id).padStart(3, '0')}`;
      ctx.font = `${Math.max(11, video.videoWidth / 80)}px monospace`;
      const x = b.x * video.videoWidth / W, y = Math.max(15, b.y * video.videoHeight / H - 4), tw = ctx.measureText(label).width + 9;
      ctx.fillStyle = zone === 'restricted' ? '#eab05d' : '#48d6c5'; ctx.fillRect(x, y - 15, tw, 15); ctx.fillStyle = '#071411'; ctx.fillText(label, x + 4, y - 4);
    }
  }

  function analyzeLoop(now) {
    if (!running) return;
    if (video.readyState >= 2 && now - lastAnalysis > 140) {
      lastAnalysis = now;
      try { updateTracks(detectMotion()); } catch (error) { console.warn('Frame analysis skipped', error); }
      if (video.duration && Number.isFinite(video.duration)) {
        $('timeline-progress').style.width = `${(video.currentTime / video.duration) * 100}%`;
        $('video-time').textContent = `${Math.floor(video.currentTime / 60)}:${pad(Math.floor(video.currentTime % 60))} / ${Math.floor(video.duration / 60)}:${pad(Math.floor(video.duration % 60))}`;
      } else $('video-time').textContent = clockText();
    }
    raf = requestAnimationFrame(analyzeLoop);
  }

  $('upload-trigger').addEventListener('click', () => $('file-input').click());
  $('add-source').addEventListener('click', () => $('file-input').click());
  $('file-input').addEventListener('change', event => {
    const file = event.target.files && event.target.files[0]; if (!file) return;
    if (sourceObjectUrl) URL.revokeObjectURL(sourceObjectUrl);
    if (cameraStream) { cameraStream.getTracks().forEach(track => track.stop()); cameraStream = null; }
    sourceObjectUrl = URL.createObjectURL(file); video.srcObject = null; video.src = sourceObjectUrl; video.load();
    video.onloadedmetadata = () => beginSource(file.name, 'Recorded video · local analysis');
    video.onerror = () => { running = false; setSignal('Source error', 'This video could not be opened'); };
  });
  $('camera-trigger').addEventListener('click', async () => {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      addInsight('warning', 'Camera unavailable', 'Camera access requires HTTPS or localhost in a supported browser.'); return;
    }
    try {
      if (sourceObjectUrl) { URL.revokeObjectURL(sourceObjectUrl); sourceObjectUrl = null; }
      cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
      video.removeAttribute('src'); video.srcObject = cameraStream;
      video.onloadedmetadata = () => beginSource('Live camera', 'Camera feed · local analysis');
    } catch (error) {
      addInsight('warning', 'Camera connection failed', error.name === 'NotAllowedError' ? 'Camera permission was denied. Allow camera access and try again.' : 'No camera feed could be opened. Check that a camera is connected.');
    }
  });
  $('play-toggle').addEventListener('click', () => {
    if (!running) return;
    if (video.paused) { video.play(); $('play-toggle').textContent = 'Ⅱ'; }
    else { video.pause(); $('play-toggle').textContent = '▶'; }
  });
  $('fullscreen').addEventListener('click', () => {
    if (stage.requestFullscreen) stage.requestFullscreen();
  });
  $('clear-events').addEventListener('click', () => { events.length = 0; renderEvents(); });
  $('zones-toggle').addEventListener('click', () => {
    addInsight('info', 'Zone configuration', 'Prototype zones are fixed to the left entry area and right restricted area.');
  });
  video.addEventListener('ended', () => { $('play-toggle').textContent = '▶'; });
  video.addEventListener('play', () => { if (running) $('play-toggle').textContent = 'Ⅱ'; });
  window.addEventListener('resize', drawTracks);
  setSignal('Waiting for signal', 'Connect a video source');
  renderEvents();
})();
