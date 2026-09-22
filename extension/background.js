// SPDX-License-Identifier: MIT
// MV3 service worker: holds the WebSocket connection to the local ns-dlss-wine bridge (app/yt_bridge.py, ws://127.0.0.1:8765 by default) and relays
// messages to/from the content script over a long-lived Port ("ns-yt"). The content script never opens the WebSocket itself (page CSP can vary; the
// extension's own background context is the reliable place for this) - it only exchanges structured-clone messages (including transferable ArrayBuffers
// for frame pixels) with this worker.
//
// Port protocol (content -> background):
//   {type: "configure", wsUrl, config: {src_w, src_h, dlss5: {...}, framegen: {...}}}   - (re)connects if wsUrl changed, then sends `config` as JSON
//   {type: "frame", seq, w, h, buffer: ArrayBuffer}                                      - one RGBA8 source frame, framed as SRC1 and sent over the WS
//   {type: "stop"}                                                                       - closes the WebSocket
// Port protocol (background -> content):
//   {type: "ws_open"} / {type: "ws_error", message} / {type: "ws_closed"}
//   {type: "config_ack", ok, error}                                                      - relayed verbatim from the bridge's JSON reply
//   {type: "output", seq, idx, count, w, h, flags, ptsMs, buffer: ArrayBuffer}            - one processed frame (flags bit0 = BGRA pixel order)

const SRC_HDR_BYTES = 20;   // magic(4) seq(u32) unused(u32) w(u32) h(u32)
const OUT_HDR_BYTES = 32;   // magic(4) seq(u32) idx(u32) count(u32) w(u32) h(u32) flags(u32) pts_ms(f32)

function buildSrcHeader(seq, w, h) {
  const buf = new ArrayBuffer(SRC_HDR_BYTES);
  const dv = new DataView(buf);
  dv.setUint8(0, 0x53); dv.setUint8(1, 0x52); dv.setUint8(2, 0x43); dv.setUint8(3, 0x31);   // "SRC1"
  dv.setUint32(4, seq >>> 0, true);
  dv.setUint32(8, 0, true);
  dv.setUint32(12, w, true);
  dv.setUint32(16, h, true);
  return buf;
}

function parseOutHeader(buf) {
  const dv = new DataView(buf, 0, OUT_HDR_BYTES);
  return {
    seq: dv.getUint32(4, true),
    idx: dv.getUint32(8, true),
    count: dv.getUint32(12, true),
    w: dv.getUint32(16, true),
    h: dv.getUint32(20, true),
    flags: dv.getUint32(24, true),
    ptsMs: dv.getFloat32(28, true),
  };
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'ns-yt') return;
  let ws = null;
  let wsUrl = null;
  // The WebSocket handshake is asynchronous even to localhost; content.js's capture loop can call 'frame' before it resolves. A frame silently
  // dropped here (as opposed to queued) leaves the content script's send/receive back-pressure counter permanently off by one - after two such
  // drops content.js's `pending >= 2` gate blocks it from ever sending another frame, matching a "config_ack ok received, then nothing forever"
  // symptom with no error anywhere. Queue the latest of each kind and flush in order once the socket actually opens; only the LATEST frame is kept
  // (an older one queued behind it is stale anyway) - content.js's back-pressure counter only cares that SOME reply eventually arrives, not which.
  let pendingConfig = null;
  let pendingFrame = null;

  const safeSend = (msg, transfer) => {
    try { transfer ? port.postMessage(msg, transfer) : port.postMessage(msg); } catch (e) { /* port already closed */ }
  };

  const flushPending = () => {
    if (pendingConfig !== null) { ws.send(pendingConfig); pendingConfig = null; }
    if (pendingFrame !== null) { ws.send(pendingFrame); pendingFrame = null; }
  };

  const connect = (url) => {
    if (ws && wsUrl === url && ws.readyState <= WebSocket.OPEN) return;
    if (ws) { try { ws.close(); } catch (e) {} }
    wsUrl = url;
    pendingConfig = pendingFrame = null;
    ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => { safeSend({ type: 'ws_open' }); flushPending(); };
    ws.onerror = () => safeSend({ type: 'ws_error', message: 'could not reach the bridge at ' + url + ' (is app/yt_bridge.py running?)' });
    ws.onclose = () => safeSend({ type: 'ws_closed' });
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        try { safeSend(JSON.parse(ev.data)); } catch (e) { /* ignore malformed */ }
        return;
      }
      const hdr = parseOutHeader(ev.data);
      const pixels = ev.data.slice(OUT_HDR_BYTES);
      safeSend({ type: 'output', ...hdr, buffer: pixels }, [pixels]);
    };
  };

  port.onMessage.addListener((msg) => {
    // This listener runs in the service worker's OWN context - an uncaught exception here goes to chrome://extensions' "service worker" console,
    // never to the tab's DevTools console content.js logs to. That makes it invisible to the usual "paste me the console log" workflow, so report
    // it back over the port too (content.js logs it as [ns-yt:bg]) instead of letting it vanish silently.
    try {
      switch (msg.type) {
        case 'configure': {
          connect(msg.wsUrl || 'ws://127.0.0.1:8765');
          const cfgStr = JSON.stringify(msg.config);
          if (ws.readyState === WebSocket.OPEN) ws.send(cfgStr); else pendingConfig = cfgStr;
          break;
        }
        case 'frame': {
          if (!ws) break;
          if (!msg.buffer) { safeSend({ type: 'bg_error', message: 'frame message arrived with no buffer (seq=' + msg.seq + ')' }); break; }
          const hdr = buildSrcHeader(msg.seq, msg.w, msg.h);
          const out = new Uint8Array(SRC_HDR_BYTES + msg.buffer.byteLength);
          out.set(new Uint8Array(hdr), 0);
          out.set(new Uint8Array(msg.buffer), SRC_HDR_BYTES);
          if (ws.readyState === WebSocket.OPEN) ws.send(out.buffer); else pendingFrame = out.buffer;
          break;
        }
        case 'stop':
          pendingConfig = pendingFrame = null;
          if (ws) { try { ws.close(); } catch (e) {} ws = null; wsUrl = null; }
          break;
        // 'ping' (content.js's MV3 keepalive heartbeat) needs no handling - receiving ANY port message resets this service worker's ~30s idle timer,
        // which is the whole point of it: without it, a slow bridge reply (e.g. DLSS5's cold-start) could otherwise let Chrome kill this worker mid-wait.
      }
    } catch (e) {
      safeSend({ type: 'bg_error', message: (e && e.message) || String(e) });
    }
  });

  port.onDisconnect.addListener(() => {
    pendingConfig = pendingFrame = null;
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
  });
});
