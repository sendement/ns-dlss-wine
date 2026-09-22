// SPDX-License-Identifier: MIT
// YouTube content script: crops the video to the player's aspect ratio (draggable position, like the sample this project's geometry/overlay code is based
// on), then optionally runs it through DLSS5 and/or frame generation via the local bridge (app/yt_bridge.py, through background.js).
//
// Pipeline: crop (here, cheap) -> DLSS5 (own internal upscale, worker.py) -> frame generation (framegen/*). Either stage can be off; the frame flows
// through unchanged. Settings are only sent to the bridge ONCE, when the position-adjustment overlay is confirmed - not live while dragging a slider: the
// DLSS5 worker tears down and rebuilds its own process on a size change (seconds, not milliseconds - see docs/worker-protocol.md), so the settings panels
// live inside that same overlay (closed = no renderer exists yet = nothing to reconfigure), exactly mirroring how the sample's own upscale-mode menus only
// ever showed up during that same adjustment step.
(function () {
  'use strict';

  const DEBUG = true;
  function log(...a) { if (DEBUG) console.log('[ns-yt]', ...a); }

  const DEFAULT_WS_URL = 'ws://127.0.0.1:8765';
  const DEFAULT_SETTINGS = {
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
  function applySettings(raw) {
    Object.assign(settings, DEFAULT_SETTINGS, raw || {});
    settings.dlss5 = Object.assign({}, DEFAULT_SETTINGS.dlss5, (raw && raw.dlss5) || {});
    settings.framegen = Object.assign({}, DEFAULT_SETTINGS.framegen, (raw && raw.framegen) || {});
  }
  function persist() {
    const area = storageArea();
    if (!area) return;
    try { area.set({ nsyt: settings }); } catch (e) { /* not critical */ }
  }
  function loadSettings() {
    const area = storageArea();
    if (!area) { log('no storage - defaults only'); return; }
    try { area.get('nsyt', (data) => { applySettings(data && data.nsyt); log('settings loaded'); }); }
    catch (e) { log('settings unavailable', e.message); }
  }

  // Remembers the crop between videos and fullscreen toggles - not persisted to storage (session-only, like the sample).
  const memory = { enabled: false, suspended: false, ratio: null, posX: 50, posY: 50 };

  const PREVIEW_FIT = 0.85;   // how much the video is shrunk in adjust mode so the whole frame (incl. the parts that will be cropped) is visible

  /* ============ Crop geometry (centred "cover" + draggable offset - same math as the sample) ============ */

  function findVideo(player) { return player.querySelector('video.html5-main-video') || player.querySelector('video'); }
  function findContainer(player) { return player.querySelector('.html5-video-container'); }
  function playerSize(player) { const r = player.getBoundingClientRect(); return { Cw: Math.round(r.width), Ch: Math.round(r.height) }; }
  function ratioKey(video) {
    if (!video || !video.videoWidth || !video.videoHeight) return null;
    return Math.round((video.videoWidth / video.videoHeight) * 100) / 100;
  }

  // Full geometry: the cover size (no black bars), how far it overhangs the player box, and the preview scale.
  function computeGeom(player) {
    const video = findVideo(player);
    if (!video || !video.videoWidth || !video.videoHeight) return null;
    const { Cw, Ch } = playerSize(player);
    if (!Cw || !Ch) return null;
    const videoRatio = video.videoWidth / video.videoHeight, containerRatio = Cw / Ch;
    let coverW, coverH;
    if (videoRatio > containerRatio) { coverH = Ch; coverW = Ch * videoRatio; } else { coverW = Cw; coverH = Cw / videoRatio; }
    const slackW = Math.max(0, coverW - Cw), slackH = Math.max(0, coverH - Ch);
    const k = PREVIEW_FIT * Math.min(Cw / coverW, Ch / coverH);
    return { Cw, Ch, coverW, coverH, slackW, slackH, k, canX: slackW > 1, canY: slackH > 1 };
  }

  // The crop rectangle in the VIDEO's own native pixels, at the given position (0..100, 50 = centred).
  function computeSourceCrop(player, posX, posY) {
    const video = findVideo(player);
    const geom = computeGeom(player);
    if (!video || !geom) return null;
    const vw = video.videoWidth, vh = video.videoHeight;
    const containerRatio = geom.Cw / geom.Ch, videoRatio = vw / vh;
    let sw, sh, sx, sy;
    if (videoRatio > containerRatio) { sh = vh; sw = vh * containerRatio; sx = (vw - sw) * (posX / 100); sy = 0; }
    else { sw = vw; sh = vw / containerRatio; sx = 0; sy = (vh - sh) * (posY / 100); }
    // even dimensions: the DLSS5/frame-generation backends require them
    sw = Math.max(2, Math.floor(sw / 2) * 2); sh = Math.max(2, Math.floor(sh / 2) * 2);
    return { Cw: geom.Cw, Ch: geom.Ch, sx: Math.round(sx), sy: Math.round(sy), sw, sh };
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
  function shouldBeActive(player) { return memory.enabled && !memory.suspended && ratioKey(findVideo(player)) !== null && Math.abs(ratioKey(findVideo(player)) - memory.ratio) < 0.02; }

  const STATE = new WeakMap();
  function getState(player) {
    let s = STATE.get(player);
    if (!s) {
      s = { zoomed: false, adjusting: false, posX: 50, posY: 50, origVideoStyle: null, origContainerStyle: null,
            resizeObserver: null, reassertTimer: null, renderer: null, overlay: null };
      STATE.set(player, s);
    }
    return s;
  }

  // Pixel-exact layout (percentages resolve against the wrong box during YouTube's own transitions - see the sample this is based on).
  function applyLayout(player, s) {
    const video = findVideo(player);
    const geom = computeGeom(player);
    if (!video || !geom) return false;
    const container = findContainer(player);
    const { Cw, Ch } = geom;
    if (container) {
      container.style.setProperty('position', 'absolute', 'important');
      container.style.setProperty('top', '0px', 'important');
      container.style.setProperty('left', '0px', 'important');
      container.style.setProperty('width', Cw + 'px', 'important');
      container.style.setProperty('height', Ch + 'px', 'important');
      container.style.setProperty('overflow', 'hidden', 'important');
    }
    let w, h, left, top, fit, objPos;
    if (s.adjusting) {
      w = Math.round(geom.coverW * geom.k); h = Math.round(geom.coverH * geom.k);
      const offsetX = (0.5 - s.posX / 100) * geom.slackW * geom.k, offsetY = (0.5 - s.posY / 100) * geom.slackH * geom.k;
      left = Math.round((Cw - w) / 2 + offsetX); top = Math.round((Ch - h) / 2 + offsetY);
      fit = 'fill'; objPos = '50% 50%';
    } else {
      w = Cw; h = Ch; left = 0; top = 0; fit = 'cover'; objPos = `${s.posX}% ${s.posY}%`;
    }
    video.style.setProperty('position', 'absolute', 'important');
    video.style.setProperty('left', left + 'px', 'important');
    video.style.setProperty('top', top + 'px', 'important');
    video.style.setProperty('width', w + 'px', 'important');
    video.style.setProperty('height', h + 'px', 'important');
    video.style.setProperty('max-width', 'none', 'important');
    video.style.setProperty('max-height', 'none', 'important');
    video.style.setProperty('object-fit', fit, 'important');
    video.style.setProperty('object-position', objPos, 'important');
    video.style.setProperty('transform', 'none', 'important');
    if (s.zoomed && !s.adjusting && s.renderer) {
      const crop = computeSourceCrop(player, s.posX, s.posY);
      if (crop) s.renderer.layout(crop);
    }
    return true;
  }

  function verifyLayout(player) {
    const video = findVideo(player);
    if (!video) return false;
    const r = video.getBoundingClientRect();
    return r.width >= 10 && r.height >= 10;
  }

  function startWatchers(player, s) {
    stopWatchers(s);
    if (typeof ResizeObserver !== 'undefined') {
      s.resizeObserver = new ResizeObserver(() => { if (s.adjusting || s.zoomed) { applyLayout(player, s); syncOverlayFrame(player); } });
      s.resizeObserver.observe(player);
    }
    s.reassertTimer = setInterval(() => { if (!s.adjusting && !s.zoomed) return stopWatchers(s); applyLayout(player, s); }, 500);
  }
  function stopWatchers(s) {
    if (s.resizeObserver) { s.resizeObserver.disconnect(); s.resizeObserver = null; }
    if (s.reassertTimer) { clearInterval(s.reassertTimer); s.reassertTimer = null; }
  }

  /* ============ BridgeRenderer: captures video frames, talks to background.js, draws the result back ============ */

  class BridgeRenderer {
    constructor(player, video) {
      this.player = player;
      this.video = video;
      this.running = false;
      this.crop = null;
      this.seq = 0;
      this.pending = 0;           // frames sent but not yet fully answered - back-pressure (the bridge answers one source frame at a time)
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
      log('configuring pipeline:', this.crop.sw + 'x' + this.crop.sh, settings.dlss5, settings.framegen);
      this.port.postMessage({
        type: 'configure',
        wsUrl: settings.wsUrl || DEFAULT_WS_URL,
        config: { type: 'config', src_w: this.crop.sw, src_h: this.crop.sh, dlss5: settings.dlss5, framegen: settings.framegen },
      });
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
      if (this.pending >= 2) return;   // the bridge answers strictly in order - drop this tick's frame rather than pile up
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
      if (this.seq === 1) log('sending the first frame (' + c.sw + 'x' + c.sh + ') - a first DLSS5/frame-generation reply can take several seconds while its worker process starts up');
      else if (this.seq % 60 === 0) log('sent', this.seq, 'frames, received', this.received || 0);
      // Kept for _onOutput's objective before/after diff (postMessage below TRANSFERS data.data.buffer - it becomes unusable here right after, hence the copy).
      if (!this._sourceCache) this._sourceCache = new Map();
      this._sourceCache.set(this.seq, data.data.slice());
      if (this._sourceCache.size > 4) this._sourceCache.delete(Math.min(...this._sourceCache.keys()));
      this.port.postMessage({ type: 'frame', seq: this.seq, w: c.sw, h: c.sh, buffer: data.data.buffer }, [data.data.buffer]);
    }

    _onMessage(msg) {
      switch (msg.type) {
        case 'ws_open': log('bridge connected'); break;
        case 'ws_error': console.warn('[ns-yt]', msg.message); this.pending = 0; break;
        case 'ws_closed': log('bridge disconnected'); this.pending = 0; break;
        case 'config_ack':
          log('config_ack', msg.ok ? 'ok' : 'REJECTED: ' + msg.error);
          if (!msg.ok) console.warn('[ns-yt] pipeline configuration rejected:', msg.error);
          break;
        case 'output': this._onOutput(msg); break;
      }
    }

    _onOutput(msg) {
      this.received = (this.received || 0) + 1;
      if (this.received === 1) log('first processed frame received (' + msg.w + 'x' + msg.h + (msg.flags & 1 ? ', BGRA' : '') + ') - showing it now');
      const isReal = msg.idx === msg.count - 1;   // the last reply of a set is the real (non-generated) frame, matching the source frame `msg.seq`
      if (isReal) {
        this.pending = Math.max(0, this.pending - 1);
        this._logDiff(msg);
      }
      const bgra = !!(msg.flags & 1);
      const delayMs = Math.max(0, (this.frameInterval / Math.max(1, msg.count)) * msg.idx);
      const draw = () => this._drawFrame(msg.w, msg.h, msg.buffer, bgra);
      if (delayMs < 2) draw(); else setTimeout(draw, delayMs);
    }

    // Objective yes/no answer to "is the pipeline actually changing the picture": mean absolute difference between the source frame this reply's `seq`
    // was captured from and what came back for it, sampled every ~2s so it doesn't spam the console. ~0 = the bridge is returning the frame unchanged
    // (DLSS5/frame generation aren't doing anything visible, or the picture on screen isn't actually this reply); a real value = it is processing frames -
    // a subtle look on screen is then a matter of the settings/content, not a broken pipeline.
    _logDiff(msg) {
      if (!this._sourceCache) return;
      const now = performance.now();
      if (this._lastDiffLog && now - this._lastDiffLog < 2000) { this._sourceCache.delete(msg.seq); return; }
      const src = this._sourceCache.get(msg.seq);
      this._sourceCache.delete(msg.seq);
      if (!src || src.length !== msg.buffer.byteLength) return;
      this._lastDiffLog = now;
      const out = new Uint8Array(msg.buffer.slice(0));
      const bgra = !!(msg.flags & 1);
      let sum = 0, n = 0;
      for (let i = 0; i < out.length; i += 4 * 37) {   // sparse sample, plenty for a mean estimate, cheap
        const r = bgra ? out[i + 2] : out[i], g = out[i + 1], b = bgra ? out[i] : out[i + 2];
        sum += Math.abs(r - src[i]) + Math.abs(g - src[i + 1]) + Math.abs(b - src[i + 2]);
        n += 3;
      }
      log('output vs source mean abs diff:', (sum / n).toFixed(2), '(0..255; ~0 means the pipeline is returning the frame unchanged)');
    }

    _drawFrame(w, h, buffer, bgra) {
      const u8 = new Uint8ClampedArray(buffer);
      if (bgra) { for (let i = 0; i + 2 < u8.length; i += 4) { const b = u8[i]; u8[i] = u8[i + 2]; u8[i + 2] = b; } }
      if (this.display.width !== w || this.display.height !== h) { this.display.width = w; this.display.height = h; }
      this.displayCtx.putImageData(new ImageData(u8, w, h), 0, 0);
      if (this.video.style.opacity !== '0') this.video.style.setProperty('opacity', '0', 'important');   // keep decoding, just hide
    }

    destroy() {
      this.running = false;
      if (this._vfcHandle && this.video.cancelVideoFrameCallback) this.video.cancelVideoFrameCallback(this._vfcHandle);
      if (this._rafHandle) cancelAnimationFrame(this._rafHandle);
      try { this.port.postMessage({ type: 'stop' }); this.port.disconnect(); } catch (e) { /* already gone */ }
      if (this.display.parentElement) this.display.remove();
    }
  }

  /* ============ on/off/adjust state machine (mirrors the sample: click = adjust position + pick settings, confirm = apply, click again = off) ============ */

  function turnOff(player, keepMemory) {
    const s = getState(player);
    if (s.renderer) { s.renderer.destroy(); s.renderer = null; }
    const video = findVideo(player);
    if (video) video.style.removeProperty('opacity');
    if (!keepMemory) { memory.enabled = false; memory.suspended = false; }
    stopWatchers(s);
    s.zoomed = false; s.adjusting = false; s.posX = 50; s.posY = 50;
    removeOverlay(player);
    restoreStyle(video, s.origVideoStyle);
    restoreStyle(findContainer(player), s.origContainerStyle);
    s.origVideoStyle = s.origContainerStyle = null;
    toggleButtonActive(player, false);
    window.dispatchEvent(new Event('resize'));
    log('turned off');
  }

  function startAdjust(player) {
    const video = findVideo(player);
    if (!video || !video.videoWidth || !video.videoHeight) return;
    const geom = computeGeom(player);
    if (!geom) return;
    const s = getState(player);
    s.origVideoStyle = snapshotStyle(video, VIDEO_PROPS);
    const container = findContainer(player);
    if (container) s.origContainerStyle = snapshotStyle(container, CONTAINER_PROPS);
    s.posX = 50; s.posY = 50; s.adjusting = true;
    if (!applyLayout(player, s) || !verifyLayout(player)) { turnOff(player); return; }
    buildOverlay(player, s, geom);
    startWatchers(player, s);
  }

  function confirmAdjust(player) {
    const s = getState(player);
    s.adjusting = false; s.zoomed = true;
    removeOverlay(player);
    applyLayout(player, s);
    if (!verifyLayout(player)) { turnOff(player); return; }
    memory.enabled = true; memory.suspended = false;
    memory.ratio = ratioKey(findVideo(player)); memory.posX = s.posX; memory.posY = s.posY;
    const crop = computeSourceCrop(player, s.posX, s.posY);
    s.renderer = new BridgeRenderer(player, findVideo(player));
    if (crop) s.renderer.layout(crop);
    s.renderer.start();
    toggleButtonActive(player, true);
    log('confirmed', { posX: s.posX, posY: s.posY, crop });
  }

  function cancelAdjust(player) { getState(player).adjusting = false; turnOff(player, true); }

  function applyRemembered(player) {
    const video = findVideo(player);
    if (!video || !video.videoWidth || !video.videoHeight) return false;
    const s = getState(player);
    if (s.zoomed || s.adjusting) return false;
    if (!s.origVideoStyle) {
      s.origVideoStyle = snapshotStyle(video, VIDEO_PROPS);
      const container = findContainer(player);
      if (container) s.origContainerStyle = snapshotStyle(container, CONTAINER_PROPS);
    }
    s.posX = memory.posX; s.posY = memory.posY; s.adjusting = false; s.zoomed = true;
    if (!applyLayout(player, s) || !verifyLayout(player)) { turnOff(player, true); return false; }
    memory.ratio = memory.ratio ?? ratioKey(video);
    const crop = computeSourceCrop(player, s.posX, s.posY);
    s.renderer = new BridgeRenderer(player, video);
    if (crop) s.renderer.layout(crop);
    s.renderer.start();
    toggleButtonActive(player, true);
    startWatchers(player, s);
    return true;
  }

  function syncPlayer(player) {
    const s = getState(player);
    if (s.adjusting) return;
    if (shouldBeActive(player)) { if (!s.zoomed) applyRemembered(player); }
    else if (s.zoomed) turnOff(player, true);
  }

  function toggleButtonActive(player, on) {
    const btn = player.querySelector('.nsyt-button');
    if (btn) btn.classList.toggle('nsyt-active', on);
  }

  function onButtonClick(player) {
    const s = getState(player);
    if (s.adjusting) return;
    if (s.zoomed) turnOff(player); else startAdjust(player);
  }

  /* ============ Adjustment overlay: drag-to-position + apply/cancel + the DLSS5/frame-generation settings menus ============ */

  const ICON_BUTTON = '<svg viewBox="0 0 24 24"><path d="M4 9V4h5v2H6v3H4zm16 0V4h-5v2h3v3h2zM4 15v5h5v-2H6v-3H4zm16 0v5h-5v-2h3v-3h2z"/></svg>';
  const ICON_CHECK = '<svg viewBox="0 0 24 24"><path d="M9 16.2l-3.5-3.5-1.4 1.4L9 19 20 8l-1.4-1.4z"/></svg>';
  const ICON_CROSS = '<svg viewBox="0 0 24 24"><path d="M18.3 5.7L12 12l6.3 6.3-1.4 1.4L10.6 13.4 4.3 19.7l-1.4-1.4L9.2 12 2.9 5.7l1.4-1.4 6.3 6.3 6.3-6.3z"/></svg>';

  function clamp(v, min, max) { return Math.min(max, Math.max(min, v)); }

  function sliderRow(container, label, key, obj, min, max, step) {
    const row = document.createElement('div'); row.className = 'nsyt-row';
    const lab = document.createElement('span'); lab.className = 'nsyt-row-label'; lab.textContent = label;
    const val = document.createElement('span'); val.className = 'nsyt-row-val';
    const input = document.createElement('input');
    input.type = 'range'; input.min = String(min); input.max = String(max); input.step = String(step); input.value = String(obj[key]);
    const fmt = (v) => (step < 1 ? Number(v).toFixed(2) : String(Math.round(v)));
    val.textContent = fmt(obj[key]);
    input.addEventListener('input', () => { obj[key] = parseFloat(input.value); val.textContent = fmt(input.value); persist(); });
    row.appendChild(lab); row.appendChild(input); row.appendChild(val);
    container.appendChild(row);
  }

  function checkboxRow(container, label, key, obj, onChange) {
    const row = document.createElement('label'); row.className = 'nsyt-row nsyt-row-check';
    const input = document.createElement('input'); input.type = 'checkbox'; input.checked = !!obj[key];
    input.addEventListener('change', () => { obj[key] = input.checked; persist(); if (onChange) onChange(); });
    const lab = document.createElement('span'); lab.textContent = label;
    row.appendChild(input); row.appendChild(lab);
    container.appendChild(row);
  }

  // A round pill button with a menu that drops UP from it, nested (not body-fixed) - safe here because the overlay it lives in is a plain absolutely
  // positioned div over the whole player, not clipped the way YouTube's own control row can be.
  function buildSettingsMenu(title, buildContent, openMenus) {
    const wrap = document.createElement('div'); wrap.className = 'nsyt-menu-wrap';
    const btn = document.createElement('button'); btn.className = 'nsyt-menu-btn'; btn.textContent = title;
    const menu = document.createElement('div'); menu.className = 'nsyt-menu'; menu.hidden = true;
    buildContent(menu);
    const close = () => { menu.hidden = true; btn.classList.remove('nsyt-open'); };
    const open = () => { openMenus.forEach((fn) => fn()); menu.hidden = false; btn.classList.add('nsyt-open'); };
    openMenus.push(close);
    btn.addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); menu.hidden ? open() : close(); });
    wrap.appendChild(menu); wrap.appendChild(btn);
    return { wrap, setOn: (on) => btn.classList.toggle('nsyt-on', on) };
  }

  function buildOverlay(player, s, geom) {
    removeOverlay(player);
    const overlay = document.createElement('div'); overlay.className = 'nsyt-overlay';

    const frame = document.createElement('div'); frame.className = 'nsyt-frame'; overlay.appendChild(frame);

    const hint = document.createElement('div'); hint.className = 'nsyt-hint';
    hint.textContent = geom.canX && geom.canY ? 'Перетащите видео: затемнённое будет обрезано'
      : geom.canX ? 'Двигайте влево/вправо: затемнённое будет обрезано'
      : geom.canY ? 'Двигайте вверх/вниз: затемнённое будет обрезано'
      : 'Пропорции совпадают — обрезка не требуется, нажмите ✓';
    overlay.appendChild(hint);

    const controlsWrap = document.createElement('div'); controlsWrap.className = 'nsyt-controls';

    const openMenus = [];
    const dlss5Menu = buildSettingsMenu('DLSS5', (menu) => {
      checkboxRow(menu, 'Включено', 'enabled', settings.dlss5, () => dlss5Menu.setOn(settings.dlss5.enabled));
      sliderRow(menu, 'Разрешение', 'scale', settings.dlss5, 0.25, 1.0, 0.05);
      sliderRow(menu, 'Интенсивность', 'intensity', settings.dlss5, 0, 2, 0.05);
      sliderRow(menu, 'Локальный тон', 'local_tone', settings.dlss5, 0, 2, 0.05);
      sliderRow(menu, 'Локальная структура', 'local_structure', settings.dlss5, 0, 2, 0.05);
      sliderRow(menu, 'Структура кожи', 'skin_structure', settings.dlss5, -1, 1, 0.05);
      checkboxRow(menu, 'Авто-маска', 'auto_mask', settings.dlss5);
    }, openMenus);
    const fgMenu = buildSettingsMenu('Кадры', (menu) => {
      checkboxRow(menu, 'Включено', 'enabled', settings.framegen, () => fgMenu.setOn(settings.framegen.enabled));
      sliderRow(menu, 'Множитель', 'multiplier', settings.framegen, 2, 4, 1);
    }, openMenus);
    dlss5Menu.setOn(settings.dlss5.enabled);
    fgMenu.setOn(settings.framegen.enabled);

    const applyBtn = document.createElement('button'); applyBtn.className = 'nsyt-apply'; applyBtn.innerHTML = ICON_CHECK; applyBtn.title = 'Применить';
    applyBtn.addEventListener('click', (e) => { e.stopPropagation(); confirmAdjust(player); });
    const cancelBtn = document.createElement('button'); cancelBtn.className = 'nsyt-cancel'; cancelBtn.innerHTML = ICON_CROSS; cancelBtn.title = 'Отмена';
    cancelBtn.addEventListener('click', (e) => { e.stopPropagation(); cancelAdjust(player); });

    overlay.addEventListener('pointerdown', (e) => { if (!controlsWrap.contains(e.target)) openMenus.forEach((fn) => fn()); }, true);

    controlsWrap.appendChild(dlss5Menu.wrap);
    controlsWrap.appendChild(fgMenu.wrap);
    controlsWrap.appendChild(applyBtn);
    controlsWrap.appendChild(cancelBtn);
    overlay.appendChild(controlsWrap);

    let dragging = false, startX = 0, startY = 0, startPosX = 50, startPosY = 50, live = geom;
    overlay.addEventListener('pointerdown', (e) => {
      if (e.target !== overlay) return;
      live = computeGeom(player) || live;
      dragging = true;
      overlay.classList.add('nsyt-dragging');
      overlay.setPointerCapture(e.pointerId);
      startX = e.clientX; startY = e.clientY; startPosX = s.posX; startPosY = s.posY;
    });
    overlay.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const dx = e.clientX - startX, dy = e.clientY - startY;
      if (live.canX) s.posX = clamp(startPosX - (dx / (live.slackW * live.k)) * 100, 0, 100);
      if (live.canY) s.posY = clamp(startPosY - (dy / (live.slackH * live.k)) * 100, 0, 100);
      applyLayout(player, s);
    });
    const endDrag = () => { if (dragging) { dragging = false; overlay.classList.remove('nsyt-dragging'); } };
    overlay.addEventListener('pointerup', endDrag);
    overlay.addEventListener('pointercancel', endDrag);
    overlay.addEventListener('pointerleave', endDrag);
    overlay.addEventListener('click', (e) => e.stopPropagation());
    overlay.addEventListener('dblclick', (e) => e.stopPropagation());

    player.appendChild(overlay);
    s.overlay = overlay;
    syncOverlayFrame(player);
  }

  function syncOverlayFrame(player) {
    const overlay = player.querySelector('.nsyt-overlay');
    if (!overlay) return;
    const frame = overlay.querySelector('.nsyt-frame');
    const geom = computeGeom(player);
    if (!frame || !geom) return;
    const w = Math.round(geom.Cw * geom.k), h = Math.round(geom.Ch * geom.k);
    frame.style.width = w + 'px'; frame.style.height = h + 'px';
    frame.style.left = Math.round((geom.Cw - w) / 2) + 'px'; frame.style.top = Math.round((geom.Ch - h) / 2) + 'px';
  }

  function removeOverlay(player) {
    const existing = player.querySelector('.nsyt-overlay');
    if (existing) existing.remove();
  }

  /* ============ Player button + lifecycle wiring ============ */

  function ensureButton(player) {
    const controls = player.querySelector('.ytp-right-controls');
    if (!controls || controls.querySelector('.nsyt-button')) return;
    const btn = document.createElement('button');
    btn.className = 'ytp-button nsyt-button';
    btn.title = 'DLSS5 / генерация кадров';
    btn.innerHTML = ICON_BUTTON;
    btn.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); onButtonClick(player); });
    controls.insertBefore(btn, controls.firstChild);
    updateButtonVisibility(player);
  }

  function updateButtonVisibility(player) {
    const btn = player.querySelector('.nsyt-button');
    if (btn) btn.style.display = isFullscreen() ? '' : 'none';
  }

  function eachPlayer(fn) { document.querySelectorAll('.html5-video-player').forEach(fn); }
  function init() { eachPlayer((player) => { ensureButton(player); updateButtonVisibility(player); watchVideoMetadata(player); }); }

  const observer = new MutationObserver(() => init());
  observer.observe(document.documentElement, { childList: true, subtree: true });

  document.addEventListener('yt-navigate-finish', () => {
    init();
    eachPlayer((player) => {
      const s = STATE.get(player);
      if (s && s.adjusting) cancelAdjust(player);
      else if (s && s.zoomed) turnOff(player, true);
      watchVideoMetadata(player);
    });
  });

  function onFullscreenChange() {
    const fs = isFullscreen();
    if (!fs) { if (memory.enabled) memory.suspended = true; } else { memory.suspended = false; }
    eachPlayer((player) => {
      updateButtonVisibility(player);
      const s = STATE.get(player);
      if (s && s.adjusting) cancelAdjust(player);
      syncPlayer(player);
      setTimeout(() => { const st = STATE.get(player); if (st && st.zoomed) applyLayout(player, st); }, 120);
    });
  }
  document.addEventListener('fullscreenchange', onFullscreenChange);
  document.addEventListener('webkitfullscreenchange', onFullscreenChange);

  function watchVideoMetadata(player) {
    const video = findVideo(player);
    if (!video || video.dataset.nsytBound === '1') return;
    video.dataset.nsytBound = '1';
    const onReady = () => syncPlayer(player);
    video.addEventListener('loadedmetadata', onReady);
    video.addEventListener('canplay', onReady);
    video.addEventListener('resize', onReady);
  }

  loadSettings();
  init();
  log('extension loaded');
})();
