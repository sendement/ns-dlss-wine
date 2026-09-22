// SPDX-License-Identifier: MIT
// YouTube content script: crops the video to the player's aspect ratio (no black bars), then optionally runs it through DLSS5 and/or frame generation via
// the local bridge (app/yt_bridge.py, through background.js). Crop geometry is centred (no manual position drag, unlike the sample this project started
// from - see docs/development notes) - the point of this extension is the neural pipeline, not the cropping UI.
//
// Pipeline: crop (here, cheap) -> DLSS5 (own internal upscale, worker.py) -> frame generation (framegen/*). Either stage can be off; the frame flows
// through unchanged. The result is drawn onto a canvas placed exactly over the (hidden) <video> element - see BridgeRenderer.
(function () {
  'use strict';

  const DEBUG = true;
  function log(...a) { if (DEBUG) console.log('[ns-yt]', ...a); }

  const DEFAULT_WS_URL = 'ws://127.0.0.1:8765';
  const DEFAULT_SETTINGS = {
    enabled: false,
    dlss5: { enabled: true, scale: 0.5, intensity: 1.0, local_tone: 1.0, local_structure: 1.0, skin_structure: -1.0, style: 1, auto_mask: 0, ui_correction: 0 },
    framegen: { enabled: false, method: 'dlssg', multiplier: 2 },
    wsUrl: DEFAULT_WS_URL,
  };

  function storageArea() {
    try {
      if (typeof chrome === 'undefined' || !chrome.storage) return null;
      if (chrome.runtime && !chrome.runtime.id) return null;
      return chrome.storage.local || null;
    } catch (e) { return null; }
  }

  const settings = JSON.parse(JSON.stringify(DEFAULT_SETTINGS));
  const listeners = [];
  function onSettingsChanged(fn) { listeners.push(fn); }
  function applySettings(raw) {
    Object.assign(settings, DEFAULT_SETTINGS, raw || {});
    settings.dlss5 = Object.assign({}, DEFAULT_SETTINGS.dlss5, (raw && raw.dlss5) || {});
    settings.framegen = Object.assign({}, DEFAULT_SETTINGS.framegen, (raw && raw.framegen) || {});
    listeners.forEach((fn) => fn());
  }
  function persist() {
    const area = storageArea();
    if (!area) return;
    try { area.set({ nsyt: settings }); } catch (e) { /* not critical */ }
  }
  function loadSettings() {
    const area = storageArea();
    if (!area) { log('no storage - defaults only'); return; }
    try {
      area.get('nsyt', (data) => { applySettings(data && data.nsyt); log('settings loaded'); });
      chrome.storage.onChanged.addListener((changes) => { if (changes.nsyt) applySettings(changes.nsyt.newValue); });
    } catch (e) { log('settings unavailable', e.message); }
  }

  /* ============ Crop geometry (centred "cover": no black bars, no manual reposition) ============ */

  function findVideo(player) {
    return player.querySelector('video.html5-main-video') || player.querySelector('video');
  }
  function findContainer(player) {
    return player.querySelector('.html5-video-container');
  }
  function playerSize(player) {
    const r = player.getBoundingClientRect();
    return { Cw: Math.round(r.width), Ch: Math.round(r.height) };
  }
  function ratioKey(video) {
    if (!video || !video.videoWidth || !video.videoHeight) return null;
    return Math.round((video.videoWidth / video.videoHeight) * 100) / 100;
  }
  // The crop rectangle in the VIDEO's own native pixels, centred, matching the player's own aspect ratio - the same "cover" math as the sample extension.
  function computeSourceCrop(player) {
    const video = findVideo(player);
    if (!video || !video.videoWidth) return null;
    const { Cw, Ch } = playerSize(player);
    if (!Cw || !Ch) return null;
    const vw = video.videoWidth, vh = video.videoHeight;
    const containerRatio = Cw / Ch, videoRatio = vw / vh;
    let sw, sh, sx, sy;
    if (videoRatio > containerRatio) { sh = vh; sw = vh * containerRatio; sx = (vw - sw) / 2; sy = 0; }
    else { sw = vw; sh = vw / containerRatio; sx = 0; sy = (vh - sh) / 2; }
    // even dimensions: the DLSS5/frame-generation backends require them
    sw = Math.max(2, Math.floor(sw / 2) * 2); sh = Math.max(2, Math.floor(sh / 2) * 2);
    return { Cw, Ch, sx: Math.round(sx), sy: Math.round(sy), sw, sh };
  }

  const VIDEO_PROPS = ['position', 'top', 'left', 'width', 'height', 'object-fit', 'object-position', 'transform', 'max-width', 'max-height'];
  const CONTAINER_PROPS = ['position', 'top', 'left', 'width', 'height', 'overflow'];
  function snapshotStyle(el, props) {
    const out = {};
    props.forEach((p) => { out[p] = { value: el.style.getPropertyValue(p), prio: el.style.getPropertyPriority(p) }; });
    return out;
  }
  function restoreStyle(el, snap) {
    if (!el || !snap) return;
    Object.keys(snap).forEach((p) => { el.style.removeProperty(p); if (snap[p].value) el.style.setProperty(p, snap[p].value, snap[p].prio); });
  }

  function isFullscreen() { return !!(document.fullscreenElement || document.webkitFullscreenElement); }

  const STATE = new WeakMap();
  function getState(player) {
    let s = STATE.get(player);
    if (!s) { s = { active: false, origVideoStyle: null, origContainerStyle: null, renderer: null, resizeObserver: null, reassertTimer: null }; STATE.set(player, s); }
    return s;
  }

  function applyLayout(player, s) {
    const video = findVideo(player);
    const crop = computeSourceCrop(player);
    if (!video || !crop) return false;
    const container = findContainer(player);
    if (container) {
      container.style.setProperty('position', 'absolute', 'important');
      container.style.setProperty('top', '0px', 'important');
      container.style.setProperty('left', '0px', 'important');
      container.style.setProperty('width', crop.Cw + 'px', 'important');
      container.style.setProperty('height', crop.Ch + 'px', 'important');
      container.style.setProperty('overflow', 'hidden', 'important');
    }
    video.style.setProperty('position', 'absolute', 'important');
    video.style.setProperty('left', '0px', 'important');
    video.style.setProperty('top', '0px', 'important');
    video.style.setProperty('width', crop.Cw + 'px', 'important');
    video.style.setProperty('height', crop.Ch + 'px', 'important');
    video.style.setProperty('max-width', 'none', 'important');
    video.style.setProperty('max-height', 'none', 'important');
    video.style.setProperty('object-fit', 'cover', 'important');
    video.style.setProperty('object-position', '50% 50%', 'important');
    video.style.setProperty('transform', 'none', 'important');
    if (s.renderer) s.renderer.layout(crop);
    return true;
  }

  function startWatchers(player, s) {
    stopWatchers(s);
    if (typeof ResizeObserver !== 'undefined') {
      s.resizeObserver = new ResizeObserver(() => { if (s.active) applyLayout(player, s); });
      s.resizeObserver.observe(player);
    }
    s.reassertTimer = setInterval(() => { if (!s.active) return stopWatchers(s); applyLayout(player, s); }, 500);
  }
  function stopWatchers(s) {
    if (s.resizeObserver) { s.resizeObserver.disconnect(); s.resizeObserver = null; }
    if (s.reassertTimer) { clearInterval(s.reassertTimer); s.reassertTimer = null; }
  }

  function turnOn(player) {
    const s = getState(player);
    if (s.active) return;
    const video = findVideo(player);
    if (!video || !video.videoWidth) return;
    s.origVideoStyle = snapshotStyle(video, VIDEO_PROPS);
    const container = findContainer(player);
    if (container) s.origContainerStyle = snapshotStyle(container, CONTAINER_PROPS);
    s.active = true;
    s.renderer = new BridgeRenderer(player, video);
    if (!applyLayout(player, s)) { turnOff(player); return; }
    s.renderer.start();
    startWatchers(player, s);
    toggleButtonActive(player, true);
    log('turned on');
  }

  function turnOff(player) {
    const s = getState(player);
    if (s.renderer) { s.renderer.destroy(); s.renderer = null; }
    stopWatchers(s);
    s.active = false;
    const video = findVideo(player);
    if (video) video.style.removeProperty('opacity');
    restoreStyle(video, s.origVideoStyle);
    restoreStyle(findContainer(player), s.origContainerStyle);
    s.origVideoStyle = s.origContainerStyle = null;
    toggleButtonActive(player, false);
    window.dispatchEvent(new Event('resize'));
    log('turned off');
  }

  function toggleButtonActive(player, on) {
    const btn = player.querySelector('.nsyt-button');
    if (btn) btn.classList.toggle('nsyt-active', on);
  }

  /* ============ BridgeRenderer: captures video frames, talks to background.js, draws the result back ============ */

  class BridgeRenderer {
    constructor(player, video) {
      this.player = player;
      this.video = video;
      this.running = false;
      this.crop = null;
      this.seq = 0;
      this.pending = 0;           // frames sent but not yet fully answered - back-pressure (the bridge pipeline is 1-in-flight per docs/worker-protocol.md)
      this.frameInterval = 1000 / 30;
      this.lastArrival = 0;

      this.capture = document.createElement('canvas');
      this.captureCtx = this.capture.getContext('2d', { willReadFrequently: true });

      const display = document.createElement('canvas');
      display.className = 'nsyt-canvas';
      this.display = display;
      this.displayCtx = display.getContext('2d');

      this.port = chrome.runtime.connect({ name: 'ns-yt' });
      this.port.onMessage.addListener((msg) => this._onMessage(msg));
      this.port.onDisconnect.addListener(() => log('port disconnected'));
      this._configured = false;
    }

    layout(crop) {
      const changed = !this.crop || this.crop.sw !== crop.sw || this.crop.sh !== crop.sh;
      this.crop = crop;
      this.display.style.width = crop.Cw + 'px';
      this.display.style.height = crop.Ch + 'px';
      if (this.display.parentElement !== this.player) this.player.appendChild(this.display);
      if (changed) {
        this.capture.width = crop.sw; this.capture.height = crop.sh;
        this.display.width = crop.sw; this.display.height = crop.sh;   // canvas backing store; CSS above stretches it to the player box
        this._sendConfigure();
      }
    }

    _sendConfigure() {
      if (!this.crop) return;
      this.port.postMessage({
        type: 'configure',
        wsUrl: settings.wsUrl || DEFAULT_WS_URL,
        config: { type: 'config', src_w: this.crop.sw, src_h: this.crop.sh, dlss5: settings.dlss5, framegen: settings.framegen },
      });
      this._configured = true;
    }

    start() {
      if (this.running) return;
      this.running = true;
      this._schedule();
    }

    _schedule() {
      if (!this.running) return;
      if (this.video.requestVideoFrameCallback) {
        this._vfcHandle = this.video.requestVideoFrameCallback(() => { this._tick(); this._schedule(); });
      } else {
        this._rafHandle = requestAnimationFrame(() => { this._tick(); this._schedule(); });
      }
    }

    _tick() {
      const v = this.video;
      if (!this.crop || !v || v.readyState < 2 || v.paused || v.seeking) return;
      if (this.pending >= 2) return;   // the bridge answers strictly in order, one (plus one in flight) at a time - drop this tick's frame rather than pile up
      const now = performance.now();
      if (this.lastArrival) {
        const dt = now - this.lastArrival;
        if (dt > 4 && dt < 250) this.frameInterval = this.frameInterval * 0.8 + dt * 0.2;
      }
      this.lastArrival = now;
      const c = this.crop;
      this.captureCtx.drawImage(v, c.sx, c.sy, c.sw, c.sh, 0, 0, c.sw, c.sh);
      let data;
      try { data = this.captureCtx.getImageData(0, 0, c.sw, c.sh); }
      catch (e) { if (!this._readErrorLogged) { this._readErrorLogged = true; console.error('[ns-yt] cannot read the video frame (likely DRM-protected content):', e); } return; }
      this.seq++;
      this.pending++;
      this.port.postMessage({ type: 'frame', seq: this.seq, w: c.sw, h: c.sh, buffer: data.data.buffer }, [data.data.buffer]);
    }

    _onMessage(msg) {
      switch (msg.type) {
        case 'ws_open': log('bridge connected'); break;
        case 'ws_error': console.warn('[ns-yt]', msg.message); break;
        case 'ws_closed': log('bridge disconnected'); break;
        case 'config_ack':
          if (!msg.ok) console.warn('[ns-yt] pipeline configuration rejected:', msg.error);
          break;
        case 'output': this._onOutput(msg); break;
      }
    }

    _onOutput(msg) {
      if (msg.idx === msg.count - 1) this.pending = Math.max(0, this.pending - 1);   // the last reply of the set closes out this source frame
      const bgra = !!(msg.flags & 1);
      const delayMs = Math.max(0, (this.frameInterval / Math.max(1, msg.count)) * msg.idx);
      const draw = () => this._drawFrame(msg.w, msg.h, msg.buffer, bgra);
      if (delayMs < 2) draw(); else setTimeout(draw, delayMs);
    }

    _drawFrame(w, h, buffer, bgra) {
      const u8 = new Uint8ClampedArray(buffer);
      if (bgra) { for (let i = 0; i + 2 < u8.length; i += 4) { const b = u8[i]; u8[i] = u8[i + 2]; u8[i + 2] = b; } }
      if (this.display.width !== w || this.display.height !== h) { this.display.width = w; this.display.height = h; }
      this.displayCtx.putImageData(new ImageData(u8, w, h), 0, 0);
      if (this.video.style.opacity !== '0') this.video.style.setProperty('opacity', '0', 'important');   // keep decoding, just hide - matches the sample's approach
    }

    reconfigure() { this._sendConfigure(); }

    destroy() {
      this.running = false;
      if (this._vfcHandle && this.video.cancelVideoFrameCallback) this.video.cancelVideoFrameCallback(this._vfcHandle);
      if (this._rafHandle) cancelAnimationFrame(this._rafHandle);
      try { this.port.postMessage({ type: 'stop' }); this.port.disconnect(); } catch (e) { /* already gone */ }
      if (this.display.parentElement) this.display.remove();
    }
  }

  /* ============ UI: fill button + two settings panels (DLSS5, FrameGen) ============ */

  const ICON_BUTTON = '<svg viewBox="0 0 24 24"><path d="M4 9V4h5v2H6v3H4zm16 0V4h-5v2h3v3h2zM4 15v5h5v-2H6v-3H4zm16 0v5h-5v-2h3v-3h2z"/></svg>';

  function slider(container, label, key, obj, min, max, step, onChange) {
    const row = document.createElement('div'); row.className = 'nsyt-row';
    const lab = document.createElement('span'); lab.className = 'nsyt-row-label'; lab.textContent = label;
    const val = document.createElement('span'); val.className = 'nsyt-row-val';
    const input = document.createElement('input');
    input.type = 'range'; input.min = String(min); input.max = String(max); input.step = String(step); input.value = String(obj[key]);
    const fmt = (v) => (step < 1 ? Number(v).toFixed(2) : String(Math.round(v)));
    val.textContent = fmt(obj[key]);
    input.addEventListener('input', () => { obj[key] = parseFloat(input.value); val.textContent = fmt(input.value); onChange(); });
    row.appendChild(lab); row.appendChild(input); row.appendChild(val);
    container.appendChild(row);
    return { input, val, refresh: () => { input.value = String(obj[key]); val.textContent = fmt(obj[key]); } };
  }

  function checkbox(container, label, key, obj, onChange) {
    const row = document.createElement('label'); row.className = 'nsyt-row nsyt-row-check';
    const input = document.createElement('input'); input.type = 'checkbox'; input.checked = !!obj[key];
    input.addEventListener('change', () => { obj[key] = input.checked; onChange(); });
    const lab = document.createElement('span'); lab.textContent = label;
    row.appendChild(input); row.appendChild(lab);
    container.appendChild(row);
    return { input, refresh: () => { input.checked = !!obj[key]; } };
  }

  // A button that opens a dropdown panel of arbitrary controls (checkbox + sliders), styled like the fill button's own menus.
  // Panels are appended to <body> (see below), independent of the player element that owns their trigger button - pruneOrphanPanels() sweeps up ones
  // whose button fell out of the document (YouTube's SPA navigation tears down and rebuilds the controls row between videos).
  const allPanels = [];
  function pruneOrphanPanels() {
    for (let i = allPanels.length - 1; i >= 0; i--) {
      if (!allPanels[i].btn.isConnected) { allPanels[i].panel.remove(); allPanels.splice(i, 1); }
    }
  }

  // The trigger button lives inline in the player's own control row (.ytp-right-controls) - see buildPanelButtons(). The dropdown itself is appended to
  // <body> as position:fixed, anchored to the button's on-screen rect, NOT nested under it: YouTube's control bar clips overflowing children on some
  // layouts, which would hide a CSS-relative dropdown even though the trigger button itself is visible.
  function buildPanel(label, buildContent) {
    const wrap = document.createElement('div'); wrap.className = 'nsyt-panel-wrap';
    const btn = document.createElement('button'); btn.className = 'ytp-button nsyt-panel-btn'; btn.textContent = label;
    const panel = document.createElement('div'); panel.className = 'nsyt-panel'; panel.hidden = true;
    const refreshers = [];
    buildContent(panel, (r) => refreshers.push(r));
    document.body.appendChild(panel);
    allPanels.push({ btn, panel });

    const reposition = () => {
      const r = btn.getBoundingClientRect();
      panel.style.left = Math.round(Math.min(r.left, window.innerWidth - panel.offsetWidth - 8)) + 'px';
      panel.style.top = Math.round(r.top - panel.offsetHeight - 6) + 'px';
    };
    const close = () => { panel.hidden = true; btn.classList.remove('nsyt-open'); };
    const open = () => { refreshers.forEach((r) => r.refresh && r.refresh()); panel.hidden = false; btn.classList.add('nsyt-open'); reposition(); };
    btn.addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); panel.hidden ? open() : close(); });
    panel.addEventListener('click', (e) => e.stopPropagation());
    wrap.appendChild(btn);
    return { wrap, btn, panel, close, setOn: (on) => btn.classList.toggle('nsyt-on', on) };
  }

  // The two panel buttons go into the SAME row as the fill button (.ytp-right-controls) - not a separate bar floating over the video, which the player's
  // own chrome (or our own result canvas) can end up stacked over.
  function buildPanelButtons(player) {
    const applyAndPersist = (renderer) => { persist(); if (renderer) renderer.reconfigure(); };
    const currentRenderer = () => { const s = STATE.get(player); return s && s.renderer; };

    const dlss5Panel = buildPanel('DLSS5', (panel, track) => {
      track(checkbox(panel, 'Включено', 'enabled', settings.dlss5, () => { applyAndPersist(currentRenderer()); dlss5Panel.setOn(settings.dlss5.enabled); }));
      track(slider(panel, 'Разрешение', 'scale', settings.dlss5, 0.25, 1.0, 0.05, () => applyAndPersist(currentRenderer())));
      track(slider(panel, 'Интенсивность', 'intensity', settings.dlss5, 0, 2, 0.05, () => applyAndPersist(currentRenderer())));
      track(slider(panel, 'Локальный тон', 'local_tone', settings.dlss5, 0, 2, 0.05, () => applyAndPersist(currentRenderer())));
      track(slider(panel, 'Локальная структура', 'local_structure', settings.dlss5, 0, 2, 0.05, () => applyAndPersist(currentRenderer())));
      track(slider(panel, 'Структура кожи', 'skin_structure', settings.dlss5, -1, 1, 0.05, () => applyAndPersist(currentRenderer())));
      track(checkbox(panel, 'Авто-маска', 'auto_mask', settings.dlss5, () => applyAndPersist(currentRenderer())));
    });
    const fgPanel = buildPanel('Кадры', (panel, track) => {
      track(checkbox(panel, 'Включено', 'enabled', settings.framegen, () => { applyAndPersist(currentRenderer()); fgPanel.setOn(settings.framegen.enabled); }));
      track(slider(panel, 'Множитель', 'multiplier', settings.framegen, 2, 4, 1, () => applyAndPersist(currentRenderer())));
    });
    dlss5Panel.setOn(settings.dlss5.enabled);
    fgPanel.setOn(settings.framegen.enabled);

    document.addEventListener('pointerdown', (e) => {
      if (!dlss5Panel.wrap.contains(e.target) && !dlss5Panel.panel.contains(e.target)) dlss5Panel.close();
      if (!fgPanel.wrap.contains(e.target) && !fgPanel.panel.contains(e.target)) fgPanel.close();
    }, true);
    return [dlss5Panel.wrap, fgPanel.wrap];
  }

  function ensureButton(player) {
    const controls = player.querySelector('.ytp-right-controls');
    if (!controls || controls.querySelector('.nsyt-button')) return;

    const btn = document.createElement('button');
    btn.className = 'ytp-button nsyt-button';
    btn.title = 'DLSS5 / генерация кадров';
    btn.innerHTML = ICON_BUTTON;
    btn.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      const s = getState(player);
      s.active ? turnOff(player) : turnOn(player);
    });
    controls.insertBefore(btn, controls.firstChild);
    // panel buttons to the LEFT of the fill button, same row, same order every time: [DLSS5] [Кадры] [fill]
    buildPanelButtons(player).reverse().forEach((wrap) => controls.insertBefore(wrap, btn));

    updateButtonVisibility(player);
  }

  function updateButtonVisibility(player) {
    const show = isFullscreen();
    player.querySelectorAll('.nsyt-button, .nsyt-panel-wrap').forEach((el) => { el.style.display = show ? '' : 'none'; });
    if (!show) document.querySelectorAll('.nsyt-panel').forEach((p) => { p.hidden = true; });   // panels live on <body> (see buildPanel), not under player
  }

  function eachPlayer(fn) { document.querySelectorAll('.html5-video-player').forEach(fn); }

  function init() { eachPlayer((player) => ensureButton(player)); }

  const observer = new MutationObserver(() => init());
  observer.observe(document.documentElement, { childList: true, subtree: true });

  document.addEventListener('yt-navigate-finish', () => {
    init();
    eachPlayer((player) => { const s = STATE.get(player); if (s && s.active) turnOff(player); });
    pruneOrphanPanels();
  });

  function onFullscreenChange() {
    eachPlayer((player) => {
      updateButtonVisibility(player);
      const s = STATE.get(player);
      if (!isFullscreen() && s && s.active) turnOff(player);
    });
  }
  document.addEventListener('fullscreenchange', onFullscreenChange);
  document.addEventListener('webkitfullscreenchange', onFullscreenChange);

  loadSettings();
  init();
  log('extension loaded');
})();
