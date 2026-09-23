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
  binary reply/replies    -> b"OUT1" + <6I f  (seq:u32 matches the source frame, idx:u32, count:u32, w:u32, h:u32, flags:u32 reserved, pts_ms:f32 - ms
                             from now to display, 0 for the last/real frame of the set) + a JPEG-encoded image (the rest of the message) - raw RGBA output
                             frames turned out to dominate round-trip time even with WebSocket compression off (several MB/frame, doubled by frame
                             generation); JPEG cuts that by ~8-15x for photographic content at negligible visible cost, and both PIL (encode) and the
                             browser's own decoder (createImageBitmap, hardware-accelerated) are far faster at it than generic deflate ever was on raw
                             pixels. Always RGB - no BGRA flag needed, JPEG has no alpha channel and the encode step normalizes color order itself.

Run: python3 app/yt_bridge.py [--port 8765]
"""
import argparse
import asyncio
import concurrent.futures
import io
import json
import os
import struct
import sys
import threading
import time
import traceback

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
OUT_HDR = struct.Struct("<4sIIIIIIf")   # magic, seq, idx, count, w, h, flags (reserved, always 0), pts_ms

DEFAULT_PORT = int(os.environ.get("NS_YT_BRIDGE_PORT", "8765"))
JPEG_QUALITY = int(os.environ.get("NS_YT_JPEG_QUALITY", "85"))


def _encode_jpeg(frame: np.ndarray, is_bgra: bool) -> bytes:
    rgb = frame[..., [2, 1, 0]] if is_bgra else frame[..., :3]   # JPEG has no alpha channel and no BGR order - normalize once, here
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def log(*a):
    print("[yt-bridge]", *a, file=sys.stderr, flush=True)


class Mailbox:
    """Single-slot latest-value handoff between pipeline stages: put() always replaces whatever's waiting, get() returns the newest value since the
    caller's last get() (blocking until one exists). This is the drop-old-frame primitive that makes the pipeline below a real pipeline instead of a
    queue that backs up: a stage that's still busy on an old frame is handed the NEWEST one, not the next one in line, once it's free - matching
    live_filter.py's own `submit()`/single-`_slot` pattern."""

    def __init__(self):
        self._cv = threading.Condition()
        self._item = None
        self._version = 0
        self._seen = 0

    def put(self, item):
        with self._cv:
            self._item = item
            self._version += 1
            self._cv.notify_all()

    def get(self, stop: threading.Event):
        """None if `stop` was set before a new item arrived."""
        with self._cv:
            while self._version == self._seen:
                if stop.is_set():
                    return None
                self._cv.wait(timeout=0.5)
            self._seen = self._version
            return self._item


class Pipeline:
    """One tab's processing chain, PIPELINED across frames (not run start-to-finish per frame): VSR downscale (an ordinary resize, if enabled - a real,
    independent stage, not tied to DLSS5) -> DLSS5 (processes whatever size it's handed, 1:1, no internal reconstruction - see the module docstring) ->
    RTX VSR upscale (back to the on-screen display size) -> frame generation, each running in its OWN thread connected to the next by a `Mailbox`. This
    means stage N can be working on frame K+1 while stage N+1 is still finishing frame K - measured serially (the original design) total per-frame
    latency was resize+dlss5+vsr+framegen added together (~100-180ms, ~6-9fps); pipelined, steady-state THROUGHPUT is bounded by the single SLOWEST
    stage instead of their sum. The cost: a source frame can be silently superseded by a newer one before an idle stage picks it up - normal for a live
    filter (mirrors live_filter.py), and the client's own back-pressure counter only cares that replies keep arriving, not that every `seq` gets one.

    Each stage's own resources (self.worker/self.vsr/self.fg) are guarded by a per-stage lock, held by BOTH `configure()` while rebuilding that stage and
    the stage's own thread while using it - so a reconfigure can never run concurrently with (or interrupt) that stage's in-flight frame."""

    def __init__(self, loop, on_output):
        self._loop = loop
        self._on_output = on_output   # on_output(seq, idx, count, frame, is_bgra, pts_ms) - called via call_soon_threadsafe, from a stage thread
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
        self._dlss_lock = threading.RLock()
        self._vsr_lock = threading.RLock()
        self._fg_lock = threading.RLock()
        self._stop = threading.Event()
        self._in_box = Mailbox()     # (seq, rgba, t_submit) - raw captured frames
        self._vsr_box = Mailbox()    # (seq, frame, t_submit) - post-resize/DLSS5
        self._fg_box = Mailbox()     # (seq, frame, t_submit) - post-VSR
        self._dumped = False         # NS_YT_DUMP: only the very first frame
        self._stats = {"dlss5": [0, 0.0], "vsr": [0, 0.0], "framegen": [0, 0.0], "jpeg": [0, 0.0]}   # stage -> [count, sum_ms], logged every 60
        self._stats_lock = threading.Lock()   # "jpeg" can be updated from either of the 2 encode-pool workers concurrently, unlike the other stages
        self._fps_n = 0
        self._fps_t0 = time.perf_counter()   # real (non-generated) frames actually delivered - the throughput number pipelining is meant to improve
        # JPEG encoding (measured ~25-45ms/frame) runs in this separate pool, not on the fg thread: at framegen multiplier=2 that's 2 encodes per real
        # submission, ~50-90ms - if done inline on _fg_run, that directly delays it from picking up the NEXT frame, capping throughput below what
        # dlss5/vsr/framegen's own combined cost would otherwise allow. A 2-worker pool lets one submission's pair encode while the next is processed.
        self._encode_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="ns-yt-encode")
        self._threads = [threading.Thread(target=self._dlss_run, daemon=True), threading.Thread(target=self._vsr_run, daemon=True),
                          threading.Thread(target=self._fg_run, daemon=True)]
        for th in self._threads:
            th.start()

    def submit(self, seq, rgba):
        self._in_box.put((seq, rgba, time.perf_counter()))

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
            work_w = max(2, int(src_w * scale) & ~1)
            work_h = max(2, int(round(work_w * src_h / src_w)) & ~1)
        else:
            work_w, work_h = src_w, src_h   # no downscale - DLSS5 (if on) processes at the crop's native resolution

        d = cfg.get("dlss5") or {}
        with self._dlss_lock:
            self.work_w, self.work_h = work_w, work_h
            if d.get("enabled"):
                params = DlssParams(style=int(d.get("style", 1)), auto_mask=int(d.get("auto_mask", 0)),
                                    ui_correction=int(d.get("ui_correction", 0)), intensity=float(d.get("intensity", 1.0)),
                                    local_tone=float(d.get("local_tone", 1.0)), local_structure=float(d.get("local_structure", 1.0)),
                                    skin_structure=float(d.get("skin_structure", -1.0)))
                key = (work_w, work_h)
                if self.worker is None or self.worker_key != key:
                    if self.worker is not None:
                        self.worker.close()
                    self.worker = Worker(work_w, work_h, params=params, max_w=work_w, max_h=work_h)
                    self.worker_key = key
                else:
                    self.worker.reconfigure(work_w, work_h, params=params)
            elif self.worker is not None:
                self.worker.close()
                self.worker = self.worker_key = None

        with self._vsr_lock:
            if vsr_on and (work_w, work_h) != (self.disp_w, self.disp_h):
                if self.vsr is None:
                    self.vsr = UPSCALER_REGISTRY["rtx_vsr"]()
                quality = max(1, min(4, int(v.get("quality", 4))))
                self.vsr.configure(work_w, work_h, self.disp_w, self.disp_h, quality=quality)
                self.vsr_key = (work_w, work_h, self.disp_w, self.disp_h, quality)
            elif self.vsr is not None:
                self.vsr.close()
                self.vsr = self.vsr_key = None
            vsr_active = self.vsr is not None

        with self._fg_lock:
            f = cfg.get("framegen") or {}
            if f.get("enabled"):
                method = f.get("method", "dlssg")
                multiplier = max(2, min(4, int(f.get("multiplier", 2))))
                fg_w, fg_h = (self.disp_w, self.disp_h) if vsr_active else (work_w, work_h)
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

    def _record(self, stage, ms):
        with self._stats_lock:
            s = self._stats[stage]
            s[0] += 1
            s[1] += ms
            done = s[0] >= 60
            if done:
                avg = s[1] / s[0]
                s[0] = 0
                s[1] = 0.0
        if done:
            log(f"[{stage}] avg {avg:.1f}ms over the last 60 frames")

    def _dlss_run(self):
        while True:
            item = self._in_box.get(self._stop)
            if item is None:
                return
            try:
                self._dlss_step(item)
            except Exception:  # noqa: BLE001 - an uncaught exception here would silently kill this stage FOREVER (the thread just ends), breaking the
                log("dlss5 stage failed:\n" + traceback.format_exc())   # whole pipeline until reconnect - never let one bad frame do that.

    def _dlss_step(self, item):
        seq, rgba, t_submit = item
        with self._dlss_lock:
            work_w, work_h = self.work_w, self.work_h
            if not work_w or not work_h:
                return   # no config received yet
            t0 = time.perf_counter()
            frame = rgba
            if (frame.shape[1], frame.shape[0]) != (work_w, work_h):
                frame = np.asarray(Image.fromarray(frame, "RGBA").resize((work_w, work_h), Image.BILINEAR))
            if self.worker is not None:
                try:
                    out = self.worker.process(frame)
                    if out is not None:
                        frame = out   # None = still priming - keep feeding the pre-DLSS5 frame downstream rather than stall the pipeline
                except Exception:  # noqa: BLE001
                    log("dlss5 process() failed:\n" + traceback.format_exc())
            self._record("dlss5", (time.perf_counter() - t0) * 1000)
        if seq == 1 and os.environ.get("NS_YT_DUMP"):
            try:
                Image.fromarray(rgba, "RGBA").save("/home/sendem/логгг_source.png")
                log("dumped source frame to /home/sendem/логгг_source.png")
            except Exception as exc:  # noqa: BLE001
                log("source dump failed:", exc)
        self._vsr_box.put((seq, frame, t_submit))

    def _vsr_run(self):
        while True:
            item = self._vsr_box.get(self._stop)
            if item is None:
                return
            try:
                self._vsr_step(item)
            except Exception:  # noqa: BLE001 - see _dlss_run's comment
                log("vsr stage failed:\n" + traceback.format_exc())

    def _vsr_step(self, item):
        seq, frame, t_submit = item
        with self._vsr_lock:
            if self.vsr is not None:
                t0 = time.perf_counter()
                try:
                    frame = self.vsr.upscale(frame)
                except Exception:  # noqa: BLE001
                    log("vsr upscale() failed:\n" + traceback.format_exc())
                self._record("vsr", (time.perf_counter() - t0) * 1000)
        self._fg_box.put((seq, frame, t_submit))

    def _fg_run(self):
        while True:
            item = self._fg_box.get(self._stop)
            if item is None:
                return
            try:
                self._fg_step(item)
            except Exception:  # noqa: BLE001 - see _dlss_run's comment
                log("framegen stage failed:\n" + traceback.format_exc())

    def _fg_step(self, item):
        seq, frame, t_submit = item
        with self._fg_lock:
            fg = self.fg
            if fg is not None:
                t0 = time.perf_counter()
                try:
                    gens = fg.submit(frame, uniform_timestamps(fg.cfg.multiplier) if fg.supports_timestamps else None)
                except Exception:  # noqa: BLE001
                    log("framegen submit() failed:\n" + traceback.format_exc())
                    gens = []
                self._record("framegen", (time.perf_counter() - t0) * 1000)
                bgra = getattr(fg, "output_bgra", False)
                n = fg.cfg.multiplier
                interval_ms = 1000.0 / 30.0
                outs = [(g, bgra, -interval_ms * (1.0 - (i + 1) / n)) for i, g in enumerate(gens)]
            else:
                outs = []
        outs.append((frame, False, 0.0))
        if not self._dumped and os.environ.get("NS_YT_DUMP"):
            self._dumped = True
            try:
                real_frame, real_bgra, _ = outs[-1]
                arr = real_frame[..., [2, 1, 0, 3]] if real_bgra else real_frame
                Image.fromarray(arr, "RGBA").save("/home/sendem/логгг_output.png")
                log(f"dumped first output frame to /home/sendem/логгг_output.png {arr.shape}")
            except Exception as exc:  # noqa: BLE001
                log("output dump failed:", exc)
        count = len(outs)
        total_ms = (time.perf_counter() - t_submit) * 1000
        if seq == 1:
            log(f"first frame processed in {total_ms:.1f} ms -> {count} output(s)")
        self._fps_n += 1
        fps_elapsed = time.perf_counter() - self._fps_t0
        if fps_elapsed >= 5.0:
            log(f"delivered {self._fps_n} frames in {fps_elapsed:.1f}s = {self._fps_n / fps_elapsed:.1f} fps "
                f"(last real frame's end-to-end latency: {total_ms:.1f}ms)")
            self._fps_n = 0
            self._fps_t0 = time.perf_counter()
        for idx, (out_frame, is_bgra, pts_ms) in enumerate(outs):
            self._encode_pool.submit(self._encode_and_emit, seq, idx, count, out_frame, is_bgra, pts_ms)

    def _encode_and_emit(self, seq, idx, count, out_frame, is_bgra, pts_ms):
        fh, fw = out_frame.shape[:2]
        t0 = time.perf_counter()
        jpeg_bytes = _encode_jpeg(out_frame, is_bgra)
        self._record("jpeg", (time.perf_counter() - t0) * 1000)
        self._loop.call_soon_threadsafe(self._on_output, seq, idx, count, jpeg_bytes, fw, fh, pts_ms)

    def close(self):
        self._stop.set()
        self._encode_pool.shutdown(wait=False, cancel_futures=True)
        for th in self._threads:
            th.join(timeout=5)
        if self.worker is not None:
            self.worker.close()
        if self.vsr is not None:
            self.vsr.close()
        if self.fg is not None:
            self.fg.close()


async def handle(ws):
    peer = getattr(ws, "remote_address", "?")
    log("connected", peer)
    loop = asyncio.get_running_loop()
    out_q: "asyncio.Queue" = asyncio.Queue(maxsize=16)

    def on_output(seq, idx, count, jpeg_bytes, fw, fh, pts_ms):
        # Called via call_soon_threadsafe from a pipeline stage thread - never blocks (put_nowait; the queue is generously sized and drained continuously
        # by the sender below, so it only fills up if the WebSocket send itself is backed up, not from pipeline speed). jpeg_bytes is already fully
        # encoded (done in _fg_step, off the event loop) - sender() below just ships it, no CPU work on the event loop itself.
        try:
            out_q.put_nowait((seq, idx, count, jpeg_bytes, fw, fh, pts_ms))
        except asyncio.QueueFull:
            pass

    pipe = Pipeline(loop, on_output)

    async def sender():
        while True:
            seq, idx, count, jpeg_bytes, fw, fh, pts_ms = await out_q.get()
            header = OUT_HDR.pack(b"OUT1", seq, idx, count, fw, fh, 0, pts_ms)
            await ws.send(header + jpeg_bytes)

    sender_task = asyncio.create_task(sender())
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
            if seq == 1:
                log(f"received first frame ({w}x{h}), dispatching to pipeline - can take up to 90s if the worker is still loading its model")
            rgba = np.frombuffer(msg, dtype=np.uint8, count=w * h * 4, offset=SRC_HDR.size).reshape(h, w, 4).copy()
            pipe.submit(seq, rgba)   # non-blocking - the pipeline threads pick this up and may supersede it with a newer frame before finishing it
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        sender_task.cancel()
        await asyncio.to_thread(pipe.close)
        log("disconnected", peer)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    log(f"listening on ws://{args.host}:{args.port}")
    # compression=None: the `websockets` library defaults to permessage-deflate, which was silently eating ~1s+ per frame CPU-compressing multi-megabyte
    # photographic pixel data that barely compresses at all (confirmed: client-measured round-trip was ~1000-1500ms while the bridge's own per-frame
    # processing latency was ~35-110ms - the gap was compression, not the network or the pipeline). This is localhost; there's nothing to save bandwidth on.
    async with websockets.serve(handle, args.host, args.port, max_size=64 * 1024 * 1024, compression=None):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
