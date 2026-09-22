# SPDX-License-Identifier: MIT
"""Local WebSocket bridge for the ns-dlss-yt browser extension (extension/): the content script sends video frames (already cropped to aspect ratio in the
page, cheap DOM/canvas work) and receives back the result of the SAME pipeline the live screen filter uses - DLSS5 (worker.py, rendering at a scaled-down
size and reconstructing straight back up to the crop's native size - it does this internally, so no separate upscaler stage is needed) -> frame generation
(app/framegen) - each stage optional and bypassed when off, matching the extension's two player buttons.

One WebSocket connection = one browser tab's pipeline (its own Worker/framegen instances, created lazily and torn down on disconnect or on a size change).
Wire protocol:

  text (JSON) "config"   -> reconfigure this connection's pipeline; replied to with {"type":"config_ack","ok":bool,"error":str|null}
  binary frame            -> one source frame: b"SRC1" + <IIff  (seq:u32, unused:u32, w:u32, h:u32) + RGBA8 pixels (w*h*4 bytes)
  binary reply/replies    -> b"OUT1" + <6I f  (seq:u32 matches the source frame, idx:u32, count:u32, w:u32, h:u32, flags:u32 bit0=BGRA order,
                             pts_ms:f32 - ms from now to display, 0 for the last/real frame of the set) + pixels

Run: python3 app/yt_bridge.py [--port 8765]
"""
import argparse
import asyncio
import json
import os
import struct
import sys
import time

import numpy as np
import websockets
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths as nspaths  # noqa: E402
from live_state import DlssParams  # noqa: E402
from worker import Worker, WorkerError  # noqa: E402
from framegen import REGISTRY as FRAMEGEN_REGISTRY, FrameGenSettings, uniform_timestamps  # noqa: E402

SRC_HDR = struct.Struct("<4sIIII")      # magic, seq, unused, w, h
OUT_HDR = struct.Struct("<4sIIIIIIf")   # magic, seq, idx, count, w, h, flags (bit0 = BGRA order), pts_ms

DEFAULT_PORT = int(os.environ.get("NS_YT_BRIDGE_PORT", "8765"))


def log(*a):
    print("[yt-bridge]", *a, file=sys.stderr, flush=True)


class Pipeline:
    """One tab's processing chain: DLSS5 (renders at a scaled-down size and reconstructs straight back up to the crop's native size - its own built-in
    upscale, so no separate upscaler stage) -> frame generation. Either stage may be off; the frame just flows through to the next one unchanged. Torn down
    and rebuilt whenever the source size or a stage's own size-affecting settings change (mirrors live_filter.py's `_ensure_fg`/`Worker.reconfigure`)."""

    def __init__(self):
        self.src_w = self.src_h = 0
        self.cfg = {}
        self.worker = None
        self.worker_key = None
        self.fg = None
        self.fg_key = None

    def configure(self, cfg, src_w, src_h):
        self.cfg = cfg
        self.src_w, self.src_h = src_w, src_h
        d = cfg.get("dlss5") or {}
        if d.get("enabled"):
            # Render at `scale` of the cropped size, reconstruct straight back up to src_w/src_h (full_w/full_h below) - DLSS5's own built-in upscale.
            scale = max(0.1, min(1.0, float(d.get("scale", 0.5))))
            ww, wh = max(2, int(src_w * scale) & ~1), max(2, int(src_h * scale) & ~1)
            params = DlssParams(style=int(d.get("style", 1)), auto_mask=int(d.get("auto_mask", 0)),
                                ui_correction=int(d.get("ui_correction", 0)), intensity=float(d.get("intensity", 1.0)),
                                local_tone=float(d.get("local_tone", 1.0)), local_structure=float(d.get("local_structure", 1.0)),
                                skin_structure=float(d.get("skin_structure", -1.0)))
            key = (src_w, src_h, ww, wh)
            if self.worker is None or self.worker_key != key:
                if self.worker is not None:
                    self.worker.close()
                self.worker = Worker(ww, wh, src_w, src_h, params=params, max_w=src_w, max_h=src_h, max_out_w=src_w, max_out_h=src_h)
                self.worker_key = key
            else:
                self.worker.reconfigure(ww, wh, src_w, src_h, params=params)
        elif self.worker is not None:
            self.worker.close()
            self.worker = self.worker_key = None

        f = cfg.get("framegen") or {}
        if f.get("enabled"):
            method = f.get("method", "dlssg")
            multiplier = max(2, min(4, int(f.get("multiplier", 2))))
            key = (method, multiplier, src_w, src_h)
            if self.fg is None or self.fg_key != key:
                if self.fg is not None:
                    self.fg.close()
                if method not in FRAMEGEN_REGISTRY:
                    raise ValueError(f"unknown frame generation method {method!r}")
                self.fg = FRAMEGEN_REGISTRY[method](src_w, src_h, FrameGenSettings(method=method, multiplier=multiplier))
                self.fg_key = key
        elif self.fg is not None:
            self.fg.close()
            self.fg = self.fg_key = None

    def process(self, rgba: np.ndarray):
        """One source frame -> list of (rgba_or_bgra, is_bgra, pts_ms) to send, in display order. Frame generation (if on) yields the generated frames first
        (their true temporal position is BEFORE this real frame, relative to the previous one) followed by this real frame, pts_ms relative to "now"."""
        frame = rgba
        if self.worker is not None:
            if (frame.shape[1], frame.shape[0]) != (self.worker.w, self.worker.h):
                # the client sends the full-size cropped frame; DLSS5 wants it already at its own (scaled-down) work size - it does the upscale back up on
                # the way out, not the way in
                frame = np.asarray(Image.fromarray(frame, "RGBA").resize((self.worker.w, self.worker.h), Image.BILINEAR))
            out = self.worker.process(frame)
            if out is not None:
                frame = out   # None = still priming (first frame(s)) - keep feeding the pre-DLSS5 frame downstream rather than stall the pipeline
        if self.fg is None:
            return [(frame, False, 0.0)]
        gens = self.fg.submit(frame, uniform_timestamps(self.fg.cfg.multiplier) if self.fg.supports_timestamps else None)
        bgra = getattr(self.fg, "output_bgra", False)
        interval_ms = 1000.0 / 30.0   # rough source frame interval estimate; the client repaces against its own rAF anyway
        n = self.fg.cfg.multiplier
        out = [(g, bgra, -interval_ms * (1.0 - (i + 1) / n)) for i, g in enumerate(gens)]
        out.append((frame, False, 0.0))
        return out

    def close(self):
        if self.worker is not None:
            self.worker.close()
        if self.fg is not None:
            self.fg.close()


async def handle(ws):
    peer = getattr(ws, "remote_address", "?")
    log("connected", peer)
    pipe = Pipeline()
    try:
        async for msg in ws:
            if isinstance(msg, str):
                try:
                    m = json.loads(msg)
                except ValueError:
                    continue
                if m.get("type") == "config":
                    try:
                        await asyncio.to_thread(pipe.configure, m, int(m["src_w"]), int(m["src_h"]))
                        await ws.send(json.dumps({"type": "config_ack", "ok": True}))
                    except Exception as exc:  # noqa: BLE001
                        log("config failed:", exc)
                        await ws.send(json.dumps({"type": "config_ack", "ok": False, "error": str(exc)}))
                continue
            if len(msg) < SRC_HDR.size:
                continue
            magic, seq, _unused, w, h = SRC_HDR.unpack_from(msg, 0)
            if magic != b"SRC1" or len(msg) < SRC_HDR.size + w * h * 4:
                continue
            rgba = np.frombuffer(msg, dtype=np.uint8, count=w * h * 4, offset=SRC_HDR.size).reshape(h, w, 4)
            t0 = time.perf_counter()
            try:
                # Worker/upscaler/framegen calls are synchronous (time.sleep spin-waits, designed for live_filter.py's own threads) - run this off the event
                # loop, or a slow frame (worst case: the first one, while the model loads) would stall every other connection and the WS keepalive itself.
                results = await asyncio.to_thread(pipe.process, rgba)
            except Exception:  # noqa: BLE001
                import traceback
                log("process failed:\n" + traceback.format_exc())
                continue
            dt = (time.perf_counter() - t0) * 1000
            count = len(results)
            for idx, (frame, is_bgra, pts_ms) in enumerate(results):
                fh, fw = frame.shape[:2]
                payload = frame.tobytes()
                header = OUT_HDR.pack(b"OUT1", seq, idx, count, fw, fh, 1 if is_bgra else 0, pts_ms)
                await ws.send(header + payload)
            if dt > 40:
                log(f"slow frame: {dt:.1f} ms for {count} output(s)")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        await asyncio.to_thread(pipe.close)
        log("disconnected", peer)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    log(f"listening on ws://{args.host}:{args.port}")
    async with websockets.serve(handle, args.host, args.port, max_size=64 * 1024 * 1024):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
