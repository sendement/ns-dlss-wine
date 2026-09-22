#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Live PoC: capture a window (via a persistent wlr-screencopy Wayland
client), run every frame through the real nvngx_dlssnr.dll DLSS5 worker
under Wine (via Proton-Experimental's own matched DXVK+vkd3d-proton - see
../README.md for why that specific combination matters), show the
processed result in a layer-shell preview window so it can be compared
side by side with the original, live.
"""
import json
import mmap
import concurrent.futures
import math
import os
import queue
import struct
import subprocess
import sys
import threading
import time

import numpy as np
from PIL import Image

from wl_capture import ScreencopyCapture
from drm_card import detect_display_card
from toplevel_capture import ToplevelCapture
from nis_upscale import NisUpscaler
from live_state import LiveState, DlssParams
from upscalers import REGISTRY as UPSCALER_REGISTRY
from framegen import REGISTRY as FRAMEGEN_REGISTRY, FrameGenSettings, uniform_timestamps
from worker import Worker

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, GLib, GdkPixbuf, GtkLayerShell

import paths
import userfiles

HERE = paths.APP_DIR



def preflight_or_exit():
    """The DLSS5 worker is the core of the app: check (and install from user_files/) what it needs before anything starts."""
    problems = userfiles.ensure("dlss5")
    if problems:
        print(userfiles.explain("dlss5", problems), file=sys.stderr)
        raise SystemExit(2)

# Optional CLI arg; without it the UI mode targets whatever window is active
# at launch and can be switched live from the settings panel (no hardcoding
# to one specific app any more). The headless main() still needs a concrete
# class, hence the "vivaldi-stable" fallback there.
CLI_WINDOW_CLASS = sys.argv[1] if len(sys.argv) > 1 else None
WINDOW_CLASS = CLI_WINDOW_CLASS or "vivaldi-stable"
MAX_DIM = int(os.environ.get("NS_MAX_DIM", "800"))
DISPLAY_CARD = os.environ.get("NS_DRM_CARD") or detect_display_card()
# Capture+DLSS run at 1/NIS_FACTOR linear resolution, NIS upscales the
# DLSS-cleaned result back up to the full NS_MAX_DIM-derived size - see
# ../README.md's "4x-shrink + NIS" section. Independent of NS_WORK_SCALE
# (DLSSNR's own internal upscale, which doesn't reduce capture/IPC cost -
# this does, since capture/IPC/NGX compute all happen at the small size).
NIS_FACTOR = float(os.environ.get("NS_NIS_FACTOR", "1"))
NIS_SHARPNESS = float(os.environ.get("NS_NIS_SHARPNESS", "0.5"))

def list_open_windows():
    """[(class, title)] of mapped, visible toplevels - for the target picker."""
    out = subprocess.check_output(["hyprctl", "clients", "-j"])
    seen, res = set(), []
    for c in json.loads(out):
        if not c.get("mapped") or c.get("hidden") or not c.get("class"):
            continue
        if c["class"] in seen:
            continue
        seen.add(c["class"])
        res.append((c["class"], c.get("title", "")))
    return res


def active_window_class():
    out = subprocess.check_output(["hyprctl", "activewindow", "-j"])
    return (json.loads(out) or {}).get("class") or ""


def resolve_window(win_class: str) -> dict:
    """The hyprctl client dict for a target: 'address:0x..' exactly, else windows whose class equals (else contains) the name; among them one on a VISIBLE
    workspace first (a hidden window is never rendered), then the most recently focused."""
    clients = [c for c in json.loads(subprocess.check_output(["hyprctl", "clients", "-j"])) if c.get("mapped", True)]
    if win_class.startswith("address:"):
        for c in clients:
            if c["address"] == win_class[8:]:
                return c
        raise RuntimeError(f"no window {win_class!r}")
    visible = {m["activeWorkspace"]["id"] for m in json.loads(subprocess.check_output(["hyprctl", "monitors", "-j"]))}
    def rank(c):   # smaller is better
        return (c["workspace"]["id"] not in visible, c.get("focusHistoryID", 999))
    exact = [c for c in clients if c["class"] == win_class]
    part = [c for c in clients if win_class.lower() in c["class"].lower()]
    for group in (exact, part):
        if group:
            return min(group, key=rank)
    raise RuntimeError(f"no window with class containing {win_class!r}")


def get_window_geometry(win_class: str):
    c = resolve_window(win_class)
    return (*c["at"], *c["size"])


def get_monitor_for_window(x, y):
    """Find the hyprctl monitor containing (x, y) and return (name, mon_w,
    mon_h, mon_x, mon_y) - the overlay's layer-shell margins are monitor-local, not Hyprland's global multi-monitor layout."""
    out = subprocess.check_output(["hyprctl", "monitors", "-j"])
    for m in json.loads(out):
        if m["x"] <= x < m["x"] + m["width"] and m["y"] <= y < m["y"] + m["height"]:
            return m["name"], m["width"], m["height"], m["x"], m["y"]
    raise RuntimeError(f"no monitor contains {x},{y}")


def capture_frame(cap: ScreencopyCapture, x, y, w, h, target_w, target_h) -> np.ndarray:
    # Resize is channel-order agnostic, so downscale the raw (possibly BGRA)
    # capture first and only pay for the B<->R channel swap on the much
    # smaller result - swapping at native window resolution first would
    # touch ~4x more bytes for the exact same outcome.
    arr, is_bgr = cap.capture_region_raw(x, y, w, h)
    if (w, h) != (target_w, target_h):
        # NEAREST: ~20x cheaper than BILINEAR and the DLSS pass
        # denoises/sharpens anyway, so the extra aliasing from skipping
        # interpolation is not worth the cost.
        im = Image.fromarray(arr, "RGBA").resize((target_w, target_h), Image.NEAREST)
        arr = np.array(im, dtype=np.uint8)
    if is_bgr:
        # Per-channel slice copies are ~3x faster than the fancy-index
        # `arr[:, :, [2, 1, 0, 3]]` (5 ms vs 16 ms at 1975x1398).
        rgba = np.empty(arr.shape, dtype=np.uint8)
        rgba[:, :, 0] = arr[:, :, 2]
        rgba[:, :, 1] = arr[:, :, 1]
        rgba[:, :, 2] = arr[:, :, 0]
        rgba[:, :, 3] = 255
    else:
        rgba = np.ascontiguousarray(arr)
    return rgba


def capture_frame_toplevel(cap: "ToplevelCapture", x, y, w, h, target_w, target_h) -> np.ndarray:
    # ToplevelCapture always captures the WHOLE window's own client surface
    # (no region/crop support, and none needed - x,y are ignored, kept only
    # so this matches the do_capture(cap, x, y, w, h, tw, th) call signature
    # shared with the other two capture backends). See toplevel_capture.py's
    # module docstring for why this backend exists: it bypasses compositing
    # entirely, so it never captures our own overlay sitting on top.
    arr, is_bgr = cap.capture_raw()
    if (arr.shape[1], arr.shape[0]) != (target_w, target_h):
        # NEAREST is enough (and fast) when shrinking; a target ABOVE native (slider > 100%) needs interpolation
        # or the model would see blocks.
        grow = target_w > arr.shape[1] or target_h > arr.shape[0]
        im = Image.fromarray(arr, "RGBA").resize((target_w, target_h), Image.BILINEAR if grow else Image.NEAREST)
        arr = np.array(im, dtype=np.uint8)
    if is_bgr:
        rgba = arr[:, :, [2, 1, 0, 3]].copy()
        rgba[:, :, 3] = 255
    else:
        rgba = np.ascontiguousarray(arr)
        rgba[:, :, 3] = 255   # plugin frames come from an X-format framebuffer: alpha is not meaningful
    return rgba


class _Profiler:
    """NS_PROFILE=1: average per-stage milliseconds, printed every 60 processed frames."""
    def __init__(self):
        self.on = bool(os.environ.get("NS_PROFILE"))
        self.acc, self.cnt, self.lock = {}, {}, threading.Lock()
    def add(self, name, sec):
        if not self.on:
            return
        with self.lock:
            self.acc[name] = self.acc.get(name, 0.0) + sec
            self.cnt[name] = self.cnt.get(name, 0) + 1
            if name == "queue->overlay" and self.cnt[name] % 60 == 0:
                print("[prof] " + "  ".join(f"{k}={self.acc[k] / max(self.cnt[k], 1) * 1000:.1f}ms" for k in self.acc), file=sys.stderr)
                self.acc.clear(); self.cnt.clear()


PROF = _Profiler()



class Presenter:
    """Paces frames onto the overlay. Without frame generation a frame is published the moment it is ready; with it,
    the frames for one real frame ([generated..., real]) are spread evenly over the measured real-frame interval, so
    the overlay updates at (multiplier x) the real rate. Costs a fraction of one real interval of latency."""

    def __init__(self, state, publish):
        self._state, self._publish = state, publish
        self._q: "queue.Queue" = queue.Queue()
        self._stop = False
        self._n, self._t0 = 0, time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def show_now(self, main, raw):
        self._q.put((0.0, main, raw))

    def schedule(self, frames, raws, offsets, base=None):
        """frames: CairoFrames in display order; offsets: seconds after `base` (default: now) at which each goes out."""
        base = time.monotonic() if base is None else base
        for f, r, off in zip(frames, raws, offsets):
            self._q.put((base + off, f, r))

    def close(self):
        self._stop = True
        self._q.put(None)
        self._thread.join(timeout=2)

    def _run(self):
        while not self._stop:
            item = self._q.get()
            if item is None:
                break
            due, frame, raw = item
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif self._q.qsize() > 12:      # far behind: skip frames instead of falling further back
                continue
            self._publish(frame, raw)
            self._n += 1
            now = time.monotonic()
            if now - self._t0 >= 1.0:
                self._state.fps = self._n / (now - self._t0)
                self._n, self._t0 = 0, now


class UpscalerHost:
    """Owns the active Upscaler on a thread of its own, so upscaling (plus the compare-frame prep and the hand-off
    to the overlay) overlaps with `Worker.process()` instead of adding to it - the two are independent (the
    worker is a Wine process, blocked on its pipe; the upscaler is GL / a second Wine process). Everything that
    touches an upscaler - including its EGL context, which is thread-affine - happens on this thread only.
    Frames are handed over through a single slot: if the upscaler is slower than the worker the older frame is
    dropped rather than queued (latency stays bounded). Commands (configure/close) run in order, before the
    next frame, and the caller waits for them."""

    def __init__(self, state, publish, get_divider=lambda: 1.0):
        self._state, self._publish, self._get_divider = state, publish, get_divider
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)  # compare-frame prep, overlaps the upscale
        self._cmds: "queue.Queue" = queue.Queue()
        self._slot = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = False
        self._up = None
        self._key = None
        self._presenter = Presenter(state, publish)
        self._fg = None
        self._fg_key = None
        self._fg_active = False          # a backend exists (owned by the frame-generation thread)
        self._fg_ema = 0.02              # smoothed generation time (s)
        self._fg_q: "queue.Queue" = queue.Queue(maxsize=1)
        self._fg_thread = threading.Thread(target=self._fg_run, daemon=True)
        self._fg_thread.start()
        self._last_real = None
        self._interval = 1 / 30
        self._frac = 0.0
        from postprocess import PostProcess
        self._post = PostProcess()
        self._n, self._t0 = 0, time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # -- caller side ---------------------------------------------------
    def _call(self, fn):
        done = threading.Event()
        box = {}
        self._cmds.put((fn, done, box))
        self._wake.set()
        done.wait()
        if "exc" in box:
            raise box["exc"]
        return box.get("result")

    def configure(self, key, res, disp, settings):
        self._call(lambda: self._configure(key, res, disp, settings))

    def submit(self, out, rgba, disp):
        with self._lock:
            self._slot = (out, rgba, disp)
        self._wake.set()

    def close(self):
        try:
            self._fg_q.put(None)
            self._fg_thread.join(timeout=5)
            self._call(self._close_up)
        finally:
            self._stopping = True
            self._wake.set()
            self._thread.join(timeout=3)
            self._presenter.close()

    # -- upscaler-thread side ------------------------------------------
    def _close_up(self):
        if self._up is not None:
            self._up.close()
            self._up, self._key = None, None

    def _close_fg(self):
        if self._fg is not None:
            self._fg.close()
            self._fg, self._fg_key = None, None

    def _fallback(self, exc, res, disp):
        """The chosen upscaler can't run (e.g. the RTX VSR host fails to start): use the plain resize so the
        pipeline keeps running instead of dying."""
        self._state.status = f"upscaler failed: {exc}"
        print(f"[live] {self._state.status}", file=sys.stderr)
        self._close_up()
        self._up = UPSCALER_REGISTRY["none"](card_path=DISPLAY_CARD)
        self._up.configure(*res, *disp)
        self._key = "none"

    def _configure(self, key, res, disp, settings):
        self._res, self._disp = res, disp
        if res != disp and (res[0] > disp[0] or res[1] > disp[1]):
            key, settings = "none", {}  # slider above 100%: the frame must be brought DOWN to the display size
        print(f"[live] upscaler={key} {res[0]}x{res[1]} -> {disp[0]}x{disp[1]} {settings}", file=sys.stderr)
        with self._lock:
            self._slot = None  # a frame from the previous geometry would not fit the new configuration
        try:
            if self._up is None or key != self._key:
                self._close_up()
                self._up = UPSCALER_REGISTRY[key](card_path=DISPLAY_CARD)
                self._key = key
            self._up.configure(*res, *disp, **settings)
        except Exception as exc:
            self._fallback(exc, res, disp)

    def _prep_compare(self, rgba, disp):
        """"Before" side of the divider: the captured pre-DLSS frame, plain-resized to the display canvas size so it
        lines up with "after" - a naive baseline, not run through the upscaler itself (that would defeat the point
        of comparing). Only the part LEFT of the divider is ever shown, so only that strip is resized/converted
        (at a 25% divider that is a quarter of the work); a margin covers the divider moving between frames."""
        from screen_overlay import CairoFrame
        dw, dh = disp
        sh, sw = rgba.shape[:2]
        keep_w = min(dw, int(dw * self._get_divider()) + 96)
        src_w = min(sw, max(1, -(-keep_w * sw // dw)))
        strip = rgba[:, :src_w]
        if (sh, src_w) != (dh, keep_w):
            strip = np.array(Image.fromarray(np.ascontiguousarray(strip), "RGBA").resize((keep_w, dh), Image.BILINEAR), dtype=np.uint8)
        return CairoFrame.from_rgba(strip)

    def _do_frame(self, out, rgba, disp):
        from screen_overlay import CairoFrame
        _t_frame = time.perf_counter()
        raw_future = self._pool.submit(self._prep_compare, rgba, disp) if self._state.compare_mode else None
        _tp = time.perf_counter()
        out = self._post.apply(rgba, out, self._state.composition)
        PROF.add("composition", time.perf_counter() - _tp)
        try:
            _tu = time.perf_counter()
            out = self._up.upscale(out)
            PROF.add("upscale", time.perf_counter() - _tu)
        except Exception as exc:
            self._fallback(exc, self._res, self._disp)
            out = self._up.upscale(out)
        main = CairoFrame.from_rgba(out)
        raw_frame = raw_future.result() if raw_future is not None else None
        PROF.add("upscale thread total", time.perf_counter() - _t_frame)
        PROF.add("queue->overlay", 0.0)

        # measured real-frame interval (EMA), used to pace generated frames
        now = time.monotonic()
        if self._last_real is not None:
            dt = min(max(now - self._last_real, 0.004), 0.5)
            self._interval = 0.85 * self._interval + 0.15 * dt
        self._last_real = now

        plan = self._plan_timestamps()
        if plan is None and not self._fg_active:
            self._presenter.show_now(main, raw_frame)      # frame generation off: straight to the overlay
        else:
            # Generation runs on its own thread so it overlaps with capture/DLSS/upscale of the next frame instead of
            # adding to them. If it is still busy, this frame is shown as is (no generated frames for it).
            # A blocking hand-over: when generation is the slowest stage the whole pipeline settles at ITS rate with a steady
            # cadence (dropping generation for some frames made the on-screen spacing irregular = visible judder).
            try:
                self._fg_q.put((out, main, raw_frame, plan, self._interval, time.monotonic()), timeout=0.25)
            except queue.Full:
                self._presenter.show_now(main, raw_frame)
        self._n += 1
        if now - self._t0 >= 1.0:  # sliding ~1 s window of REAL frames (the presenter counts displayed ones)
            self._state.real_fps = self._n / (now - self._t0)
            self._n, self._t0 = 0, now
            if self._state.framegen.method == "off":
                self._state.fps = self._state.real_fps

    def _fg_run(self):
        """Owns the frame-generation backend (its Vulkan/Wine objects are used from this thread only): generates the
        frames for each real frame and hands [generated..., real] to the presenter."""
        from screen_overlay import CairoFrame
        pend = None   # (job, token, arrived): a frame the pipelined backend has accepted but whose result is not collected yet
        while True:
            if pend is None:
                job = self._fg_q.get()
            else:
                try:
                    job = self._fg_q.get_nowait()   # the next frame is already waiting: hand it over first, then collect the previous one (they overlap)
                except queue.Empty:
                    self._fg_finish(pend, CairoFrame); pend = None
                    continue
            if job is None:
                break
            out, main, raw_frame, plan, interval, _enqueued = job
            arrived = time.monotonic()   # anchor = when generation STARTS on this frame (see the latency note below)
            if plan is None:                              # switched off: release the backend, show the frame
                if pend is not None:
                    self._fg_finish(pend, CairoFrame); pend = None
                self._close_fg()
                self._fg_active = False
                self._presenter.show_now(main, raw_frame)
                continue
            if pend is not None and self._fg_stale(out):   # settings or size changed: the backend is about to be replaced, so collect the queued frame first
                self._fg_finish(pend, CairoFrame); pend = None
            token = self._generate_begin(out, plan)
            if pend is not None:
                self._fg_finish(pend, CairoFrame)
            pend = (job, token, arrived)
        if pend is not None:
            self._fg_finish(pend, CairoFrame)
        self._close_fg()

    def _fg_finish(self, pend, CairoFrame):
        """Collect the generated frames of a queued job and hand [generated..., real] to the presenter."""
        (out, main, raw_frame, plan, interval, _enqueued), token, arrived = pend[0], pend[1], pend[2]
        try:
            generated = self._generate_end(token)
        except Exception as exc:
            print(f"[live] frame generation thread error: {exc}", file=sys.stderr)
            generated = []
        self._fg_ema = 0.9 * self._fg_ema + 0.1 * (time.monotonic() - arrived)
        self._fg_active = self._fg is not None
        if generated:
            # G(t) at (t - t_first) * interval after now, the real frame at (1 - t_first) * interval: the displayed
            # real-frame spacing stays one interval and nothing is scheduled before "now".
            ts = plan[:len(generated)]
            # Times are anchored to when generation started plus a constant latency (its smoothed duration), not to
            # whenever it happened to finish. (Anchoring to the enqueue time made everything 'late' whenever generation
            # was the slowest stage: frames were shown in bursts.)
            latency = 1.15 * self._fg_ema + 0.004
            offsets = [latency + (t - ts[0]) * interval for t in ts] + [latency + (1.0 - ts[0]) * interval]
            bgra = getattr(self._fg, "output_bgra", False)
            frames = [(CairoFrame.from_bgra(g) if bgra else CairoFrame.from_rgba(g)) for g in generated] + [main]
            self._presenter.schedule(frames, [None] * len(generated) + [raw_frame], offsets, base=arrived)
        else:
            self._presenter.show_now(main, raw_frame)

    def _generate_begin(self, out, plan):
        """Start generating for a real frame. Pipelined backends (begin/end) return a token; the others generate right here and the token carries the result."""
        gens = self._ensure_fg(out)
        if gens is not None:
            return ("done", gens)
        try:
            _tg = time.perf_counter()
            if getattr(self._fg, "pipelined", False):
                tok = self._fg.begin(out, plan)
                PROF.add("framegen", time.perf_counter() - _tg)
                return ("pipe", tok)
            gens = self._fg.submit(out, plan)
            PROF.add("framegen", time.perf_counter() - _tg)
            return ("done", gens)
        except Exception as exc:
            self._fg_failed(exc)
            return ("done", [])

    def _generate_end(self, token):
        kind, val = token
        if kind == "done":
            return val
        try:
            return self._fg.finish(val) if self._fg is not None else []
        except Exception as exc:
            self._fg_failed(exc)
            return []

    def _fg_failed(self, exc):
        self._state.status = f"frame generation failed: {exc}"
        print(f"[live] {self._state.status}", file=sys.stderr)
        self._close_fg()
        self._state.framegen = FrameGenSettings()

    def _fg_key_for(self, out):
        cfg = self._state.framegen
        return (cfg.method, cfg.multiplier, cfg.flow_scale, cfg.performance, out.shape[1], out.shape[0])  # adaptive/target apply live

    def _fg_stale(self, out):
        return self._fg is not None and self._fg_key_for(out) != self._fg_key

    def _ensure_fg(self, out):
        """(Re)create the backend when the settings or the frame size changed. Returns [] when creation failed, None when the backend is ready."""
        cfg = self._state.framegen
        key = self._fg_key_for(out)
        if self._fg is None or key != self._fg_key:
            self._close_fg()
            try:
                self._fg = FRAMEGEN_REGISTRY[cfg.method](out.shape[1], out.shape[0], cfg)
                self._fg_key = key
                print(f"[live] framegen={cfg.method} x{cfg.multiplier} {out.shape[1]}x{out.shape[0]}", file=sys.stderr)
            except Exception as exc:
                self._state.status = f"frame generation failed: {exc}"
                print(f"[live] {self._state.status}", file=sys.stderr)
                self._state.framegen = FrameGenSettings()  # back to off, or it would retry every frame
                return []
        return None

    def _plan_timestamps(self):
        """Interpolation positions wanted between the previous real frame and this one (None = frame generation off)."""
        cfg = self._state.framegen
        if cfg.method == "off" or cfg.method not in FRAMEGEN_REGISTRY:
            return None                                    # the generation thread closes an idle backend
        cls = FRAMEGEN_REGISTRY[cfg.method]
        if not (cfg.adaptive and cls.supports_timestamps):
            return uniform_timestamps(cfg.multiplier)
        # Adaptive: aim for `target_fps` displayed. Per real frame we want target*interval displayed frames, i.e. that
        # many minus one generated; the fractional part is carried over (error diffusion) so the average is right,
        # and the ceiling is multiplier-1 generated frames. 0 generated frames still advances the model's history.
        self._frac += cfg.target_fps * self._interval - 1.0
        k = max(0, min(cfg.multiplier - 1, int(math.floor(self._frac + 0.5))))
        self._frac = max(-1.0, min(1.0, self._frac - k))
        return [j / (k + 1) for j in range(1, k + 1)]

    def _run(self):
        while not self._stopping:
            self._wake.wait(0.5)
            self._wake.clear()
            while True:
                try:
                    fn, done, box = self._cmds.get_nowait()
                except queue.Empty:
                    break
                try:
                    box["result"] = fn()
                except Exception as exc:
                    box["exc"] = exc
                done.set()
            with self._lock:
                job, self._slot = self._slot, None
            if job is not None and self._up is not None:
                try:
                    self._do_frame(*job)
                except Exception as exc:
                    print(f"[live] upscale thread error: {exc}", file=sys.stderr)


def read_exact(stream, size):
    buf = bytearray()
    while len(buf) < size:
        chunk = stream.read(size - len(buf))
        if not chunk:
            raise EOFError("worker stopped")
        buf.extend(chunk)
    return bytes(buf)


class PreviewWindow(Gtk.Window):
    def __init__(self, w, h):
        super().__init__()
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.TOP)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, 20)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.RIGHT, 20)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        self.set_default_size(w, h)
        self.image = Gtk.Image()
        self.add(self.image)
        self.set_decorated(False)
        self.show_all()

    def update_frame(self, rgba: np.ndarray):
        h, w = rgba.shape[:2]
        data = np.ascontiguousarray(rgba).tobytes()
        pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(data), GdkPixbuf.Colorspace.RGB, True, 8, w, h, w * 4)
        self.image.set_from_pixbuf(pixbuf)
        return False


def main():
    preflight_or_exit()
    x, y, ww, wh = get_window_geometry(WINDOW_CLASS)
    print(f"[live] target window at {x},{y} {ww}x{wh}")
    k = MAX_DIM / max(ww, wh)
    full_w, full_h = int(ww * k) // 2 * 2, int(wh * k) // 2 * 2

    display_w, display_h = full_w, full_h

    if NIS_FACTOR > 1.0:
        # Capture, IPC AND NGX compute all run at 1/NIS_FACTOR linear
        # resolution (genuinely smaller data at every stage, unlike
        # NS_WORK_SCALE below) - DLSSNR runs plain 1:1 (no NGX upscale) at
        # that small size, and NisUpscaler (a GLES port of NVIDIA Image
        # Scaling, see ../README.md) restores the full display_w x
        # display_h size afterwards with real edge-adaptive scaling instead
        # of naive bilinear/nearest.
        tw = int(display_w / NIS_FACTOR) // 2 * 2
        th = int(display_h / NIS_FACTOR) // 2 * 2
        full_w = full_h = 0  # 1:1 into the worker, NIS does the upscale ourselves
        print(f"[live] NS_NIS_FACTOR={NIS_FACTOR}: working size {tw}x{th} -> "
              f"NIS upscale -> {display_w}x{display_h}")
    else:
        # NS_WORK_SCALE < 1.0: capture/send frames at a SMALLER resolution
        # than the displayed/output one and let NGX upscale on the GPU
        # (NS_NR_SMALL, wired in Worker.__init__) - shrinks pipe-write cost
        # a little, but NOT capture/IPC cost (the protocol always wants the
        # color frame at OUTPUT resolution regardless of this flag - see
        # the comment below). NS_NIS_FACTOR above is the real lever;
        # this is kept for comparison/reference.
        work_scale = float(os.environ.get("NS_WORK_SCALE", "1.0"))
        if work_scale < 1.0:
            tw, th = int(full_w * work_scale) // 2 * 2, int(full_h * work_scale) // 2 * 2
            print(f"[live] output size {full_w}x{full_h}, working size {tw}x{th} "
                  f"(scale={work_scale}, NGX upscales)")
        else:
            tw, th = full_w, full_h
            full_w = full_h = 0  # 1:1, no upscale header fields
            print(f"[live] working size {tw}x{th} (1:1)")

    worker = Worker(tw, th, full_w, full_h)
    win = PreviewWindow(display_w if NIS_FACTOR > 1.0 else worker.out_w,
                         display_h if NIS_FACTOR > 1.0 else worker.out_h)
    # The protocol always wants the COLOR frame at the OUTPUT resolution
    # (worker.out_w/out_h) even in NS_NR_SMALL mode - the worker downscales
    # on its own GPU after receiving it, not before we send it. Only the
    # motion field (unused here, always zero) is sized at the work
    # resolution. So capture/resize cost is unchanged by work_scale; only
    # the worker's own NGX compute cost drops. Kept for the fixed
    # PreviewWindow size below and to make that limitation explicit.
    cap_w, cap_h = worker.out_w, worker.out_h

    cap_x, cap_y = x, y
    do_capture = capture_frame

    stop = threading.Event()
    # maxsize=1: the consumer always wants the FRESHEST frame, not a queued
    # backlog - a stale frame is replaced, never enqueued behind another one.
    frame_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=1)

    def capture_loop():
        cap = ScreencopyCapture()
        try:
            while not stop.is_set():
                try:
                    rgba = do_capture(cap, cap_x, cap_y, ww, wh, cap_w, cap_h)
                except Exception as exc:
                    print(f"[live] capture error: {exc}", file=sys.stderr)
                    break
                try:
                    frame_q.get_nowait()
                except queue.Empty:
                    pass
                frame_q.put(rgba)
        finally:
            cap.close()

    def process_loop():
        # NisUpscaler holds its own EGL context (thread-affine): build and use it entirely on this thread.
        nis = NisUpscaler(tw, th, display_w, display_h, sharpness=NIS_SHARPNESS,
                           card_path=DISPLAY_CARD) if NIS_FACTOR > 1.0 else None
        n = 0
        t0 = time.monotonic()
        wait_total = proc_total = w_total = ngx_total = r_total = nis_total = 0.0
        try:
            while not stop.is_set():
                tw0 = time.monotonic()
                try:
                    _tq = time.perf_counter()
                    rgba = frame_q.get(timeout=2.0)
                    PROF.add("wait for frame", time.perf_counter() - _tq)
                except queue.Empty:
                    continue
                tw1 = time.monotonic()
                try:
                    out = worker.process(rgba)
                except Exception as exc:
                    print(f"[live] worker error: {exc}", file=sys.stderr)
                    break
                tw2 = time.monotonic()
                wait_total += tw1 - tw0
                proc_total += tw2 - tw1
                w_total += worker.last_write_ms
                ngx_total += worker.last_wait_ms
                r_total += worker.last_read_ms
                if out is not None:
                    if nis is not None:
                        tn0 = time.monotonic()
                        out = nis.upscale(out)
                        nis_total += (time.monotonic() - tn0) * 1000
                    GLib.idle_add(win.update_frame, out)
                n += 1
                if n % 10 == 0:
                    fps = n / (time.monotonic() - t0)
                    print(f"[live] {n} frames, {fps:.1f} fps  "
                          f"(queue-wait {wait_total/n*1000:.0f}ms/f, worker {proc_total/n*1000:.0f}ms/f "
                          f"= write {w_total/n:.1f} + ngx-wait {ngx_total/n:.1f} + read {r_total/n:.1f}"
                          f"{f' + nis {nis_total/n:.1f}' if nis is not None else ''})",
                          flush=True)
        finally:
            if nis is not None:
                nis.close()

    threading.Thread(target=capture_loop, daemon=True).start()
    threading.Thread(target=process_loop, daemon=True).start()

    def on_destroy(*_):
        stop.set()
        worker.close()
        Gtk.main_quit()

    win.connect("destroy", on_destroy)
    try:
        Gtk.main()
    except KeyboardInterrupt:
        on_destroy()


RECONFIGURE_DEBOUNCE_SEC = 0.25


def main_ui():
    preflight_or_exit()
    """NS_UI=1 entry point: live settings panel driving the worker via
    Worker.reconfigure() (RNSZ) and a swappable Upscaler (see upscalers/),
    instead of fixed env vars chosen before launch. See ../README.md's
    "Live settings overlay" section.
    """
    from settings_panel import SettingsPanel
    from screen_overlay import ScreenOverlay, gdk_monitor_for_geometry

    target0 = CLI_WINDOW_CLASS or active_window_class()
    if not target0:
        raise SystemExit("no window class given and no active window - pass one as argv[1]")
    x, y, ww, wh = get_window_geometry(target0)
    print(f"[live] target window {target0!r} at {x},{y} {ww}x{wh}")
    display_w, display_h = ww // 2 * 2, wh // 2 * 2  # native size - no NS_MAX_DIM cap in UI mode
    PLUGIN = os.environ.get("NS_PLUGIN", "0") == "1"   # compositor plugin (hyprplug/): frames in / result out via shared memory
    plugin_link = None
    if PLUGIN:
        from plugin_bridge import PluginLink, SharedCapture, PluginOverlay
        r = subprocess.run(["hyprctl"] + (["-i", os.environ["NS_PLUGIN_INSTANCE"]] if os.environ.get("NS_PLUGIN_INSTANCE") else [])
                           + ["nsproxy", "attach", "address:" + resolve_window(target0)["address"]], capture_output=True, text=True)
        print(f"[live] nsproxy attach: {(r.stdout + r.stderr).strip()}")
        if not r.stdout.strip().startswith("attached"):
            raise SystemExit("nsproxy plugin: attach failed (is it loaded? `hyprctl plugin load .../libnsproxy.so`)")
        plugin_link = PluginLink()
    PROXY = os.environ.get("NS_PROXY", "0") == "1"
    proxy = fwd = None
    if PROXY:
        # proxy-desktop mode (see proxy_desktop.py): the source moves to a headless output, we show the result in a window
        from proxy_desktop import ProxyDesktop, ProxyWindow, ProxyCapture, InputForwarder
        proxy = ProxyDesktop(target0)
        px, py, pw, ph = proxy.engage()
        display_w, display_h = pw, ph

    # Needed for the overlay's positioning regardless of capture backend
    # (GtkLayerShell.set_monitor() wants monitor-relative margins).
    mon_name, mon_w, mon_h, mon_x, mon_y = get_monitor_for_window(x, y)
    local_x, local_y = x - mon_x, y - mon_y
    print(f"[live] monitor {mon_name} {mon_w}x{mon_h} at {mon_x},{mon_y}")

    initial_params = DlssParams()
    # Construct with a ceiling covering the LARGEST size the resolution slider can ever reach (native/100%) - Worker.reconfigure() enforces it, since the
    # worker's mmap regions are sized once at start-up (see worker.py / docs/worker-protocol.md). The slider starts at 100% too, matching this - no extra
    # reconfigure needed at startup.
    worker = Worker(display_w, display_h, 0, 0, params=initial_params, max_w=display_w, max_h=display_h)
    import screen_overlay as _so
    _so.PROFILE_CB = PROF.add  # no-op unless profiling is on
    if PLUGIN:
        overlay = PluginOverlay(plugin_link, display_w, display_h)
    elif PROXY:
        fwd = InputForwarder(px, py, pw, ph)
        overlay = ProxyWindow(pw, ph, fwd)
    else:
        overlay = ScreenOverlay(gdk_monitor_for_geometry(mon_x, mon_y), local_x, local_y, display_w, display_h)

    # RTX VSR by default: 5-10 ms per frame at 3440x1440 vs 17 ms (NIS) / ~50 ms (the PIL bilinear "none").
    # NS_UPSCALER=none|nis|rtx_vsr overrides.
    initial_upscaler = os.environ.get("NS_UPSCALER", "rtx_vsr")
    if initial_upscaler not in UPSCALER_REGISTRY:
        initial_upscaler = "none"
    state = LiveState(display_w, display_h, initial_params, initial_upscaler, {
        s.key: s.default for s in UPSCALER_REGISTRY[initial_upscaler].settings
    })
    state.target_class = target0
    if PLUGIN:   # both sides must follow the very same window: pin the target to the resolved address
        state.target_class = target0 = "address:" + resolve_window(target0)["address"]

    # Always ToplevelCapture in UI mode, never wlr-screencopy: the
    # overlay sits exactly on top of the captured window, and both of the
    # other backends read the compositor's FINAL composited output - they'd
    # capture our own previous frame's output instead of the real window,
    # a feedback loop (each pass re-denoises/re-sharpens the last one's
    # result, compounding into visible ringing/noise). ToplevelCapture
    # exports the window's own client surface directly, bypassing
    # compositing - see toplevel_capture.py's module docstring.
    print(f"[live] capture: hyprland-toplevel-export (bypasses compositing, "
          f"avoids capturing our own overlay)")

    stop = threading.Event()
    frame_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=1)
    seq_lock = threading.Lock()
    seq_counter, last_put = [0], [0]
    N_CAPTURE = 1 if PLUGIN else max(1, int(os.environ.get("NS_CAPTURE_THREADS", "4")))

    def capture_loop():
        # Capture always runs at the CURRENT work resolution (state's
        # resolution, read fresh every iteration) - not display_w/h - since
        # that's what the worker actually wants; the active upscaler is
        # what restores display_w/h afterward. The target window can be
        # switched live from the panel: the ToplevelCapture is owned by
        # this thread, so it's rebuilt here when state.target_class changes.
        cap, cap_target = None, None
        errors = 0
        try:
            while not stop.is_set():
                if not state.active:
                    time.sleep(0.05)
                    continue
                want = state.target_class
                if cap is None or want != cap_target:
                    if cap is not None:
                        cap.close()
                        cap = None
                    try:
                        cap = SharedCapture(plugin_link) if PLUGIN else ProxyCapture(px, pw, ph) if PROXY else ToplevelCapture(want)
                        cap_target = want
                    except Exception as exc:
                        state.status = f"target {want!r}: {exc}"
                        time.sleep(0.5)
                        continue
                tw, th = state.resolution
                try:
                    with seq_lock:
                        seq_counter[0] += 1
                        my_seq = seq_counter[0]
                    _tp = time.perf_counter()
                    if PLUGIN:
                        cap.set_export_size(tw, th)   # the compositor downscales on the GPU
                    rgba = capture_frame_toplevel(cap, 0, 0, 0, 0, tw, th)
                    PROF.add("capture(total)", time.perf_counter() - _tp)
                    PROF.add("capture(wl wait)", cap.last_wait_ms / 1000)
                    errors = 0
                except Exception as exc:
                    # A single failed/timed-out export (e.g. mid-resize)
                    # must not kill capture for good - skip and retry; only
                    # rebuild the capture object if it keeps failing.
                    errors += 1
                    if errors == 1 or errors % 30 == 0:
                        print(f"[live] capture error ({errors}): {exc}", file=sys.stderr)
                    if errors >= 10:
                        cap.close()
                        cap = None
                    time.sleep(0.05)
                    continue
                # Several capture threads run in parallel (each on its own Wayland connection - the compositor
                # serves them concurrently, ~2.3x the throughput of one), so a slower one can finish after a
                # newer frame was already delivered: drop it instead of going back in time.
                with seq_lock:
                    if my_seq < last_put[0]:
                        continue
                    last_put[0] = my_seq
                    try:
                        frame_q.get_nowait()
                    except queue.Empty:
                        pass
                    frame_q.put(rgba)
        finally:
            if cap is not None:
                cap.close()

    def process_loop():
        # Everything that touches worker.proc's pipe or an upscaler's GL
        # context lives on THIS thread only (see live_state.py's docstring)
        # - the settings panel (GTK thread) only ever writes plain data to
        # `state`.
        applied_resolution = state.resolution
        applied_params = state.params
        applied_upscaler_key = state.upscaler_key
        applied_upscaler_settings = dict(state.upscaler_settings)
        applied_display_size = state.display_size
        host = UpscalerHost(state, (overlay.update_frame if PLUGIN else lambda out, raw: GLib.idle_add(overlay.update_frame, out, raw)),
                            get_divider=lambda: overlay._divider_frac)
        host.configure(applied_upscaler_key, applied_resolution, applied_display_size, applied_upscaler_settings)

        try:
            while not stop.is_set():
                (res, res_stable, params, params_stable,
                 up_key, up_settings, up_stable) = state.stable_snapshot(RECONFIGURE_DEBOUNCE_SEC)
                # Not debounced like the others - the window's own geometry
                # only changes when the user actually finishes a resize
                # (the compositor reports the new size, not a stream of
                # intermediate ones), so there's nothing to coalesce here.
                display_size = state.display_size

                if display_size != applied_display_size:
                    applied_display_size = display_size
                    host.configure(applied_upscaler_key, applied_resolution, applied_display_size, applied_upscaler_settings)

                if up_stable and (up_key != applied_upscaler_key or up_settings != applied_upscaler_settings):
                    host.configure(up_key, applied_resolution, applied_display_size, up_settings)
                    applied_upscaler_key, applied_upscaler_settings = up_key, dict(up_settings)

                if res_stable and res != applied_resolution:
                    try:
                        worker.reconfigure(*res, params=applied_params)
                        applied_resolution = res
                        host.configure(applied_upscaler_key, applied_resolution, applied_display_size, applied_upscaler_settings)
                        state.status = ""
                    except Exception as exc:
                        state.status = f"resize failed: {exc}"
                        print(f"[live] {state.status}", file=sys.stderr)
                elif params_stable and params != applied_params:
                    try:
                        worker.reconfigure(*applied_resolution, params=params)
                        applied_params = params
                        state.status = ""
                    except Exception as exc:
                        state.status = f"param update failed: {exc}"
                        print(f"[live] {state.status}", file=sys.stderr)

                try:
                    _tq = time.perf_counter()
                    rgba = frame_q.get(timeout=2.0)
                    PROF.add("wait for frame", time.perf_counter() - _tq)
                except queue.Empty:
                    continue
                # capture_loop reads state.resolution independently and on
                # its own timing - a frame captured just before a resize
                # (window resize -> panel recomputes the slider's
                # percentage -> state.resolution changes -> reconfigure()
                # above) can still be sitting in frame_q by the time this
                # runs. Feeding the worker a frame whose byte size doesn't
                # match what it was JUST reconfigured to expect leaves it
                # blocked waiting for bytes that will never come - and
                # Worker.process()'s read has no timeout, so that's a
                # silent, permanent hang (this is what "frames stopped,
                # one static frame showing" after a resize turned out to
                # be). Simplest correct fix: never feed it a mismatched
                # frame in the first place.
                if (rgba.shape[1], rgba.shape[0]) != applied_resolution:
                    continue
                try:
                    _tw = time.perf_counter()
                    out = worker.process(rgba)
                    for _extra_pass in range(state.composition.passes - 1):  # NR passes: feed the result back in
                        if out is None:
                            break
                        out = worker.process(out)
                    PROF.add("worker.process", time.perf_counter() - _tw)
                except Exception as exc:
                    print(f"[live] worker error: {exc}", file=sys.stderr)
                    break
                if out is not None:
                    host.submit(out, rgba, applied_display_size)
        finally:
            host.close()

    threads = {
        "capture": threading.Thread(target=capture_loop, daemon=True),
        "process": threading.Thread(target=process_loop, daemon=True),
    }
    threads["capture"].start()
    for _extra in range(N_CAPTURE - 1):
        threading.Thread(target=capture_loop, daemon=True).start()
    threads["process"].start()

    def restart_with_nr_preset(preset: int):
        # NS_NR_PRESET is read once by the worker process at launch
        # (NrPresetHint() caches it) - it can't be changed via RNSZ, so
        # this is the one control that restarts the whole Wine process
        # (and, with it, the capture/process threads that depend on it).
        #
        # A fixed sleep() here (the original approach) is a race: if a
        # thread happens to be blocked past that window (e.g. process_loop
        # in frame_q.get(timeout=2.0), or capture_loop mid Wayland
        # round-trip), `stop.clear()` below would un-set the flag before it
        # ever got to check it, so the OLD thread never exits and keeps
        # running alongside the NEW one - exactly the kind of thing that
        # can silently stall frame delivery after a restart. join() with a
        # generous timeout instead confirms both old threads are actually
        # gone before touching `stop` or spawning their replacements.
        nonlocal worker
        state.status = f"restarting worker with NS_NR_PRESET={preset}..."
        stop.set()
        threads["capture"].join(timeout=5)
        threads["process"].join(timeout=5)
        if threads["capture"].is_alive() or threads["process"].is_alive():
            state.status = "restart failed: old capture/process thread did not exit in time"
            print(f"[live] {state.status}", file=sys.stderr)
            return
        worker.close()
        os.environ["NS_NR_PRESET"] = str(preset)
        stop.clear()
        worker = Worker(*state.resolution, params=state.params, max_w=display_w, max_h=display_h)
        threads["capture"] = threading.Thread(target=capture_loop, daemon=True)
        threads["process"] = threading.Thread(target=process_loop, daemon=True)
        threads["capture"].start()
        threads["process"].start()
        state.status = f"worker restarted at NS_NR_PRESET={preset}"

    def shutdown(*_):
        stop.set()
        worker.close()
        if PLUGIN:
            overlay.hide()
            plugin_link.close()
            subprocess.run(["hyprctl"] + (["-i", os.environ["NS_PLUGIN_INSTANCE"]] if os.environ.get("NS_PLUGIN_INSTANCE") else [])
                           + ["nsproxy", "detach"], capture_output=True)
        if PROXY:
            fwd.close()
            proxy.release()
        Gtk.main_quit()

    panel = SettingsPanel(state, display_w, display_h, restart_with_nr_preset,
                           on_compare_toggled=overlay.set_compare_mode,
                           quit_cb=shutdown, list_windows_cb=list_open_windows)

    last_geometry = [(target0, x, y, ww, wh)]
    cur_mon = [mon_x, mon_y]

    plugin_attached = [state.target_class]

    def retarget_plugin():
        """Plugin mode: the panel's target picker (state.target_class) must re-attach the compositor plugin to the newly chosen window."""
        cls = state.target_class
        if not PLUGIN or not cls or cls == plugin_attached[0]:
            return
        r = subprocess.run(["hyprctl"] + (["-i", os.environ["NS_PLUGIN_INSTANCE"]] if os.environ.get("NS_PLUGIN_INSTANCE") else [])
                           + ["nsproxy", "attach", "address:" + resolve_window(cls)["address"]], capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        print(f"[live] nsproxy attach {cls!r}: {out}")
        if out.startswith("attached"):
            plugin_attached[0] = state.target_class = "address:" + resolve_window(cls)["address"]   # follow THIS window from now on
            state.status = ""
        else:
            state.status = f"cannot attach to {cls!r}: {out}"
            state.target_class = plugin_attached[0]   # stay on the previous one

    def poll_geometry():
        retarget_plugin()
        # Keeps the overlay pinned over the target window: it can be moved,
        # resized, moved to another monitor, or the target itself switched
        # from the panel. ToplevelCapture already follows the window's
        # current size on its own (it captures the live client surface) -
        # this keeps the overlay's own size/position/monitor and the
        # upscaler's target size in sync with it.
        if PROXY:
            return True   # the source is pinned fullscreen on the headless output
        cls = state.target_class
        try:
            nx, ny, nw, nh = get_window_geometry(cls)
        except Exception:
            return True  # window may be transiently gone (e.g. mid-move); keep polling
        key = (cls, nx, ny, nw, nh)
        if key != last_geometry[0]:
            last_geometry[0] = key
            new_dw, new_dh = nw // 2 * 2, nh // 2 * 2
            try:
                _n, _w, _h, nmx, nmy = get_monitor_for_window(nx, ny)
            except Exception:
                nmx, nmy = cur_mon
            gdk_mon = None
            if [nmx, nmy] != cur_mon:
                cur_mon[:] = [nmx, nmy]
                gdk_mon = gdk_monitor_for_geometry(nmx, nmy)
            overlay.reposition(nx - nmx, ny - nmy, new_dw, new_dh, gdk_mon)
            state.set_display_size(new_dw, new_dh)
            panel.update_native_size(new_dw, new_dh)
        return True

    GLib.timeout_add(300, poll_geometry)

    # --- hide/show hotkey + tray icon -------------------------------------
    # SIGUSR2 shows/hides the settings panel (the filter keeps running), SIGWINCH turns the filter overlay on/off
    # (hidden = capture/processing idle too, so it costs nothing); the tray menu has both. Hyprland binds:
    # `pkill -USR2 -f '^python3 live_filter.py'` / `pkill -WINCH -f ...`.
    import signal
    tray_items = {}

    def set_overlay_active(on: bool):
        if on == state.active:
            return
        state.active = on
        if on:
            if PROXY:
                proxy.engage((pw, ph))
                fwd._layout()
            overlay.show_all()
        else:
            overlay.hide()
            if PROXY:
                proxy.release()   # filter off = the app is back on the real desktop
        if "overlay" in tray_items:
            tray_items["overlay"].set_label("Скрыть оверлей" if on else "Показать оверлей")
        state.status = "" if on else "overlay hidden"

    def toggle_overlay(*_):
        set_overlay_active(not state.active)
        return True

    def toggle_panel(*_):
        if panel.get_visible():
            panel.hide()
        else:
            panel.show_all()
        if "panel" in tray_items:
            tray_items["panel"].set_label("Показать панель" if not panel.get_visible() else "Скрыть панель")
        return True

    for _sig in (signal.SIGTERM, signal.SIGINT):   # the source window must never be left on the headless output
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, _sig, lambda *_: (shutdown(), False)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, toggle_panel)   # hotkey: settings panel
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGWINCH, toggle_overlay)   # filter on/off
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, lambda: (setattr(PROF, "on", not PROF.on), True)[1])

    try:
        import gi
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator
        indicator = AppIndicator.Indicator.new("ns-dlss-live", "video-display",
                                               AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        indicator.set_title("DLSS5 live filter")
        menu = Gtk.Menu()
        for key, label, cb in (("overlay", "Скрыть оверлей", toggle_overlay),
                               ("panel", "Скрыть панель", toggle_panel),
                               ("quit", "Выключить", lambda *_: shutdown())):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            menu.append(item)
            tray_items[key] = item
        menu.show_all()
        indicator.set_menu(menu)
        tray_items["indicator"] = indicator  # keep a reference alive
    except Exception as exc:
        print(f"[live] tray icon unavailable: {exc}", file=sys.stderr)

    on_destroy = shutdown
    overlay.connect("destroy", on_destroy)
    panel.connect("destroy", on_destroy)
    try:
        Gtk.main()
    except KeyboardInterrupt:
        on_destroy()


if __name__ == "__main__":
    if os.environ.get("NS_UI", "0") == "1":
        main_ui()
    else:
        main()
