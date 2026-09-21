# SPDX-License-Identifier: MIT
"""Shared state between the GTK main thread (settings_panel.py's widgets)
and the worker thread (live_filter.py's process_loop). GTK callbacks only
ever write plain data here (never touch Worker.proc's pipe or any
upscaler's GL context directly - those are thread-affine, see
../README.md's "EGL contexts are current-on-a-THREAD" section). The
process_loop thread reads this, debounces, and is the only thread that
ever calls worker.reconfigure() or constructs/closes an upscaler.
"""
import threading
import os
import time
from postprocess import Composition
from framegen import FrameGenSettings
from dataclasses import dataclass


NR_STYLES = {"Default": 0, "Natural": 1, "Cinematic": 2}  # from the reference app's NR_STYLES


@dataclass
class DlssParams:
    """The DLSSNR knobs the worker actually reads every frame (see
    EvaluateVideo() in dlss5-feed-host64.cpp - h.params->Set("DLSSNR.*", ...)).
    `profile`/`preset` header fields exist in the wire protocol but are
    never consumed anywhere in the worker - left out here on purpose, not
    an oversight. Defaults match what live_filter.py sent before these
    became live-adjustable.
    """
    style: int = 1          # NR_STYLES: 0 Default, 1 Natural, 2 Cinematic
    auto_mask: int = 0
    ui_correction: int = 0
    intensity: float = 1.0
    local_tone: float = 1.0
    local_structure: float = 1.0
    skin_structure: float = -1.0


class LiveState:
    def __init__(self, tw, th, params, upscaler_key, upscaler_settings):
        self._lock = threading.Lock()
        self.resolution = (tw, th)
        self._resolution_ts = time.monotonic()
        self.params = params
        self._params_ts = time.monotonic()
        self.upscaler_key = upscaler_key
        self.upscaler_settings = dict(upscaler_settings)
        self._upscaler_ts = time.monotonic()
        self.framegen = FrameGenSettings()  # replaced as a whole; UpscalerHost rebuilds its backend when it changes
        _fg = os.environ.get("NS_FRAMEGEN", "")   # startup preset, e.g. NS_FRAMEGEN=dlssg:2 (method[:multiplier]); the panel still works
        if _fg:
            _m, _, _x = _fg.partition(":")
            self.framegen = FrameGenSettings(method=_m, multiplier=int(_x) if _x else 2)
        self.real_fps = 0.0
        self.composition = Composition()  # replaced as a whole (GIL-atomic); read fresh by the upscaler thread
        self.compare_mode = False
        self.active = True  # False = overlay hidden: capture/processing idle (hotkey / tray toggle)
        self.divider_frac = 0.5  # 0..1, left portion shows the pre-DLSS frame
        self.fps = 0.0
        self.status = ""
        # The captured window's own on-screen size, tracked live (a plain
        # tuple assignment is GIL-atomic, no lock needed) - the GTK thread's
        # geometry poll updates this when the window moves/resizes;
        # process_loop reads it fresh each frame rather than trusting a
        # value captured once at startup, which otherwise goes stale the
        # moment the user resizes the window (the overlay's content would
        # keep targeting the old size while the window itself changed).
        self.display_size = (tw, th)
        self.target_class = ""  # window class being processed, switchable live from the panel

    def set_framegen(self, fg: FrameGenSettings):
        self.framegen = fg

    def set_composition(self, comp: Composition):
        self.composition = comp

    def set_target_class(self, cls):
        self.target_class = cls

    def set_display_size(self, w, h):
        self.display_size = (w, h)

    def set_resolution(self, tw, th):
        with self._lock:
            if (tw, th) != self.resolution:
                self.resolution = (tw, th)
                self._resolution_ts = time.monotonic()

    def set_params(self, params):
        with self._lock:
            self.params = params
            self._params_ts = time.monotonic()

    def set_upscaler(self, key, settings):
        with self._lock:
            if key != self.upscaler_key or settings != self.upscaler_settings:
                self.upscaler_key = key
                self.upscaler_settings = dict(settings)
                self._upscaler_ts = time.monotonic()

    def stable_snapshot(self, debounce_sec):
        """Called from the worker thread: returns (resolution, params,
        upscaler_key, upscaler_settings, stable_flags) where each `_stable`
        flag is True only once that field has been unchanged for
        `debounce_sec` - so a slider mid-drag never triggers a reconfigure."""
        with self._lock:
            now = time.monotonic()
            return (
                self.resolution, now - self._resolution_ts >= debounce_sec,
                self.params, now - self._params_ts >= debounce_sec,
                self.upscaler_key, dict(self.upscaler_settings),
                now - self._upscaler_ts >= debounce_sec,
            )
