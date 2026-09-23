# SPDX-License-Identifier: MIT
"""Local WebSocket bridge for the ns-dlss-yt browser extension (extension/): the content script sends video frames (already cropped to aspect ratio in the
page, cheap DOM/canvas work) and receives back the result of the SAME pipeline the live screen filter uses - VSR downscale (an independent stage, its own
button/settings, not tied to DLSS5) -> DLSS5 (worker.py, processes whatever size it's handed, 1:1, no internal reconstruction) -> RTX VSR upscale (back to
the on-screen DISPLAY size, not the crop's native size - see disp_w/disp_h below) -> frame generation (app/framegen). Each stage is independently
optional/skippable, matching the extension's three player buttons.
DLSS5's own internal reconstruction (full_w/full_h) was tried first and dropped: confirmed via a raw frame dump that it corrupts its output (a sheared,
mostly-black image) for any non-1:1 work/full ratio, not just extreme ones - so DLSS5 here always renders and returns at its own work size, and a real VSR
stage (also used by live_filter.py) does the actual up/downscale.

One WebSocket connection = one browser tab's pipeline (its own Worker/VSR/framegen instances, created lazily and torn down on disconnect or on a size
change). Wire protocol:

  text (JSON) "config"   -> {type, src_w, src_h, disp_w, disp_h, dlss5: {...}, vsr: {...}, framegen: {...}} - src_w/src_h is the crop's native video-pixel
                             size (what VSR downscales FROM, and DLSS5 processes at, once downscaled); disp_w/disp_h is the on-screen CSS pixel size (what
                             VSR upscales back TO) - reconfigures this connection's pipeline, replied to with {"type":"config_ack","ok":bool,"error":str|null}
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
from upscalers import REGISTRY as UPSCALER_REGISTRY  # noqa: E402

SRC_HDR = struct.Struct("<4sIIII")      # magic, seq, unused, w, h
OUT_HDR = struct.Struct("<4sIIIIIIf")   # magic, seq, idx, count, w, h, flags (bit0 = BGRA order), pts_ms

DEFAULT_PORT = int(os.environ.get("NS_YT_BRIDGE_PORT", "8765"))


def log(*a):
    print("[yt-bridge]", *a, file=sys.stderr, flush=True)


class Pipeline:
    """One tab's processing chain: VSR downscale (an ordinary resize, if enabled - a real, independent stage now, not tied to DLSS5) -> DLSS5 (processes
    at whatever size it's handed, 1:1, no internal reconstruction - see the module docstring) -> RTX VSR upscale (back to the on-screen display size,
    only when the frame is actually smaller than that) -> frame generation. Any stage may be off/a no-op; the frame just flows through to the next one
    unchanged. Torn down and rebuilt whenever the source size or a stage's own size-affecting settings change (mirrors live_filter.py's
    `_ensure_fg`/`Worker.reconfigure`)."""

    def __init__(self):
        self.src_w = self.src_h = 0
        self.disp_w = self.disp_h = 0
        self.work_w = self.work_h = 0
        self.cfg = {}
        self.worker = None
        self.worker_key = None
        self.vsr = None
        self.vsr_key = None
        self.fg = None
        self.fg_key = None

    def configure(self, cfg, src_w, src_h, disp_w=0, disp_h=0):
        self.cfg = cfg
        self.src_w, self.src_h = src_w, src_h
        self.disp_w, self.disp_h = disp_w or src_w, disp_h or src_h
        log(f"configure: src={src_w}x{src_h} disp={self.disp_w}x{self.disp_h} dlss5={cfg.get('dlss5')} vsr={cfg.get('vsr')} framegen={cfg.get('framegen')}")

        v = cfg.get("vsr") or {}
        vsr_on = bool(v.get("enabled"))
        if vsr_on:
            # wh is derived from ww via the crop's own aspect ratio rather than floored independently, so the work rectangle stays proportional to the
            # crop (VSR's DLPack path needs 8-aligned rows internally - see hosts/vsr_native_host.py - handled there, not a concern for this ratio math).
            scale = max(0.1, min(1.0, float(v.get("scale", 0.5))))
            self.work_w = max(2, int(src_w * scale) & ~1)
            self.work_h = max(2, int(round(self.work_w * src_h / src_w)) & ~1)
        else:
            self.work_w, self.work_h = src_w, src_h   # no downscale - DLSS5 (if on) processes at the crop's native resolution

        d = cfg.get("dlss5") or {}
        if d.get("enabled"):
            params = DlssParams(style=int(d.get("style", 1)), auto_mask=int(d.get("auto_mask", 0)),
                                ui_correction=int(d.get("ui_correction", 0)), intensity=float(d.get("intensity", 1.0)),
                                local_tone=float(d.get("local_tone", 1.0)), local_structure=float(d.get("local_structure", 1.0)),
                                skin_structure=float(d.get("skin_structure", -1.0)))
            key = (self.work_w, self.work_h)
            if self.worker is None or self.worker_key != key:
                if self.worker is not None:
                    self.worker.close()
                self.worker = Worker(self.work_w, self.work_h, params=params, max_w=self.work_w, max_h=self.work_h)
                self.worker_key = key
            else:
                self.worker.reconfigure(self.work_w, self.work_h, params=params)
        elif self.worker is not None:
            self.worker.close()
            self.worker = self.worker_key = None

        if vsr_on and (self.work_w, self.work_h) != (self.disp_w, self.disp_h):
            if self.vsr is None:
                self.vsr = UPSCALER_REGISTRY["rtx_vsr"]()
            quality = max(1, min(4, int(v.get("quality", 4))))
            self.vsr.configure(self.work_w, self.work_h, self.disp_w, self.disp_h, quality=quality)
            self.vsr_key = (self.work_w, self.work_h, self.disp_w, self.disp_h, quality)
        elif self.vsr is not None:
            self.vsr.close()
            self.vsr = self.vsr_key = None

        f = cfg.get("framegen") or {}
        if f.get("enabled"):
            method = f.get("method", "dlssg")
            multiplier = max(2, min(4, int(f.get("multiplier", 2))))
            fg_w, fg_h = (self.disp_w, self.disp_h) if self.vsr is not None else (self.work_w, self.work_h)
            key = (method, multiplier, fg_w, fg_h)
            if self.fg is None or self.fg_key != key:
                if self.fg is not None:
                    self.fg.close()
                if method not in FRAMEGEN_REGISTRY:
                    raise ValueError(f"unknown frame generation method {method!r}")
                self.fg = FRAMEGEN_REGISTRY[method](fg_w, fg_h, FrameGenSettings(method=method, multiplier=multiplier))
                self.fg_key = key
        elif self.fg is not None:
            self.fg.close()
            self.fg = self.fg_key = None

        log(f"configure done: worker={'on ' + str(self.worker_key) if self.worker else 'off'} vsr={'on ' + str(self.vsr_key) if self.vsr else 'off'} "
            f"framegen={'on ' + str(self.fg_key) if self.fg else 'off'}")

    def process(self, rgba: np.ndarray):
        """One source frame -> list of (rgba_or_bgra, is_bgra, pts_ms) to send, in display order. Frame generation (if on) yields the generated frames first
        (their true temporal position is BEFORE this real frame, relative to the previous one) followed by this real frame, pts_ms relative to "now"."""
        t = {}
        t0 = time.perf_counter()
        frame = rgba
        if (frame.shape[1], frame.shape[0]) != (self.work_w, self.work_h):
            frame = np.asarray(Image.fromarray(frame, "RGBA").resize((self.work_w, self.work_h), Image.BILINEAR))
        t1 = time.perf_counter(); t["resize"] = (t1 - t0) * 1000
        if self.worker is not None:
            out = self.worker.process(frame)
            if out is not None:
                frame = out   # None = still priming (first frame(s)) - keep feeding the pre-DLSS5 frame downstream rather than stall the pipeline
        t2 = time.perf_counter(); t["dlss5"] = (t2 - t1) * 1000
        if self.vsr is not None:
            frame = self.vsr.upscale(frame)
        t3 = time.perf_counter(); t["vsr"] = (t3 - t2) * 1000
        if self.fg is None:
            self.last_timings = t
            return [(frame, False, 0.0)]
        gens = self.fg.submit(frame, uniform_timestamps(self.fg.cfg.multiplier) if self.fg.supports_timestamps else None)
        t4 = time.perf_counter(); t["framegen"] = (t4 - t3) * 1000
        self.last_timings = t
        bgra = getattr(self.fg, "output_bgra", False)
        interval_ms = 1000.0 / 30.0   # rough source frame interval estimate; the client repaces against its own rAF anyway
        n = self.fg.cfg.multiplier
        out = [(g, bgra, -interval_ms * (1.0 - (i + 1) / n)) for i, g in enumerate(gens)]
        out.append((frame, False, 0.0))
        return out

    def close(self):
        if self.worker is not None:
            self.worker.close()
        if self.vsr is not None:
            self.vsr.close()
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
                        await asyncio.to_thread(pipe.configure, m, int(m["src_w"]), int(m["src_h"]),
                                                 int(m.get("disp_w") or 0), int(m.get("disp_h") or 0))
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
            if seq == 1:
                log(f"received first frame ({w}x{h}), dispatching to pipeline - can take up to 90s if the worker is still loading its model")
                if os.environ.get("NS_YT_DUMP"):
                    try:
                        Image.fromarray(rgba, "RGBA").save("/home/sendem/логгг_source.png")
                        log("dumped source frame to /home/sendem/логгг_source.png")
                    except Exception as exc:  # noqa: BLE001
                        log("source dump failed:", exc)
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
            if seq == 1 and os.environ.get("NS_YT_DUMP"):
                try:
                    real_frame, real_bgra, _ = results[-1]
                    arr = real_frame[..., [2, 1, 0, 3]] if real_bgra else real_frame
                    Image.fromarray(arr, "RGBA").save("/home/sendem/логгг_output.png")
                    log(f"dumped first output frame to /home/sendem/логгг_output.png {arr.shape}")
                except Exception as exc:  # noqa: BLE001
                    log("output dump failed:", exc)
            for idx, (frame, is_bgra, pts_ms) in enumerate(results):
                fh, fw = frame.shape[:2]
                payload = frame.tobytes()
                header = OUT_HDR.pack(b"OUT1", seq, idx, count, fw, fh, 1 if is_bgra else 0, pts_ms)
                await ws.send(header + payload)
            t = getattr(pipe, "last_timings", {})
            breakdown = " ".join(f"{k}={v:.1f}ms" for k, v in t.items())
            if seq == 1:
                log(f"first frame processed in {dt:.1f} ms -> {count} output(s) [{breakdown}]")
            elif dt > 40 or seq % 60 == 0:
                log(f"{'slow frame' if dt > 40 else 'frame'} {seq}: {dt:.1f} ms total for {count} output(s) [{breakdown}]")
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
