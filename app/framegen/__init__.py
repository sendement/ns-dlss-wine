# SPDX-License-Identifier: MIT
"""Frame generation backends. A backend takes each REAL frame (RGBA8, display size) and returns the frames to show
BETWEEN the previous real frame and this one (temporal order). Add a method = one class + one REGISTRY line; the
panel builds its controls from `settings` like the upscalers do.

  (modules) frame generators contributed by optional modules (docs/module-protocol.md), e.g. the GPL-3 `ns-mako` module (Lossless Scaling FG through MAKO).
  dlssg  NVIDIA DLSS Frame Generation through a Wine host on the public NGX API (framegen/nsngxg.py; needs NVIDIA's nvngx_dlssg.dll; NS_DLSSG_IMPL=bridge = legacy Visual Enhancer bridge, framegen/nsdlssg.py).
  fsr3   AMD FSR 3.1 frame generation through a Wine host (framegen/nsfsr3.py; x2 only, optical flow + a global-motion hint).
  blend  plain 50/50 crossfade - only a stand-in to exercise the pacing/presentation path (it ghosts).
"""
from dataclasses import dataclass

import numpy as np



@dataclass(frozen=True)
class FrameGenSettings:
    method: str = "off"           # "off" or a REGISTRY key
    multiplier: int = 2           # m-1 generated frames between two real ones
    flow_scale: float = 0.5       # module backends (e.g. mako): motion-estimation resolution (lower = faster)
    performance: bool = False     # module backends: performance mode
    adaptive: bool = False        # target a displayed fps instead of a fixed multiplier (`multiplier` = ceiling)
    target_fps: int = 90          # adaptive: displayed frames per second to aim for


class FrameGenBackend:
    name = "base"
    settings: list = []

    def __init__(self, width: int, height: int, cfg: FrameGenSettings):
        self.w, self.h, self.cfg = width, height, cfg

    supports_timestamps = False   # True: `timestamps` may be arbitrary positions in (0,1); False: uniform only
    pipelined = False             # True: begin()/finish() overlap consecutive frames (submit() is both)

    def begin(self, rgba: np.ndarray, timestamps=None):
        raise NotImplementedError

    def finish(self, token) -> list:
        raise NotImplementedError

    def submit(self, rgba: np.ndarray, timestamps=None) -> list:
        """`timestamps`: positions between the previous real frame and this one (None = uniform from the settings)."""
        raise NotImplementedError

    def close(self):
        pass


class BlendBackend(FrameGenBackend):
    name = "Simple blend (test only)"

    def __init__(self, width, height, cfg):
        super().__init__(width, height, cfg)
        self._prev = None

    supports_timestamps = True

    def submit(self, rgba, timestamps=None):
        prev, self._prev = self._prev, rgba.copy()
        if prev is None:
            return []
        out = []
        for a in (timestamps if timestamps is not None else uniform_timestamps(self.cfg.multiplier)):
            out.append(((prev.astype(np.uint16) * int((1 - a) * 256) + rgba.astype(np.uint16) * int(a * 256)) >> 8).astype(np.uint8))
        return out


def uniform_timestamps(multiplier: int) -> list:
    return [j / multiplier for j in range(1, multiplier)]


class DlssgBackend(FrameGenBackend):
    name = "NVIDIA DLSS-G (NGX, Wine)"
    output_bgra = True   # submit() returns cairo-ordered (B,G,R,A) views - the caller skips the RGBA->BGRA shuffle and the copy

    def __init__(self, width, height, cfg):
        super().__init__(width, height, cfg)
        import os
        w, h = width - width % 2, height - height % 2
        self._crop = (w, h) != (width, height)
        if os.environ.get("NS_DLSSG_IMPL", "ngx") == "bridge":   # legacy: Visual Enhancer's bridge DLL (needs the user's own copy)
            from .nsdlssg import DlssgFrameGen
            self._impl = DlssgFrameGen(w, h, count=max(1, min(4, cfg.multiplier - 1)))
        else:
            from .nsngxg import NgxDlssgFrameGen, NgxVkDlssgFrameGen, native_ready
            # NS_DLSSG_IMPL: "native" = Linux/Vulkan host, "ngx" = the same NGX code under Wine, unset = native when it is built and its NVIDIA library is staged
            impl = os.environ.get("NS_DLSSG_IMPL", "auto")
            cls = NgxVkDlssgFrameGen if impl == "native" or (impl == "auto" and native_ready()) else NgxDlssgFrameGen
            self._impl = cls(w, h, count=max(1, min(3, cfg.multiplier - 1)))

    def submit(self, rgba, timestamps=None):
        if self._crop:
            rgba = rgba[:self._impl.h, :self._impl.w]
        return self._impl.submit(rgba)

    @property
    def pipelined(self):
        return getattr(self._impl, "pipelined", False)

    def begin(self, rgba, timestamps=None):
        if self._crop:
            rgba = rgba[:self._impl.h, :self._impl.w]
        return self._impl.begin(rgba)

    def finish(self, token):
        return self._impl.end(token)

    def close(self):
        self._impl.close()


class Fsr3Backend(FrameGenBackend):
    name = "AMD FSR 3.1 frame generation (x2 only, Wine)"
    output_bgra = True   # views in cairo order, like the DLSS-G backend

    def __init__(self, width, height, cfg):
        super().__init__(width, height, cfg)
        from .nsfsr3 import Fsr3FrameGen
        self._impl = Fsr3FrameGen(width, height, count=1)

    def submit(self, rgba, timestamps=None):
        return self._impl.submit(rgba)   # one generated frame at the midpoint

    def close(self):
        self._impl.close()


def _module_backend_class(module, entry):
    """A registry-ready backend for one frame generator of an optional module (docs/module-protocol.md)."""
    class ModuleBackend(FrameGenBackend):
        name = entry["title"]
        supports_timestamps = bool(entry.get("supports_timestamps", False))
        pipelined = True
        output_bgra = entry.get("output", "rgba") == "bgra"

        def __init__(self, width, height, cfg):
            super().__init__(width, height, cfg)
            from .module_client import ModuleFrameGen
            w, h = width - width % 2, height - height % 2
            self._crop = (w, h) != (width, height)
            self._impl = ModuleFrameGen(module, entry, w, h, count=max(1, cfg.multiplier - 1),
                                        options={"flow_scale": cfg.flow_scale, "performance": int(cfg.performance)})

        def _cut(self, rgba):
            return rgba[:self._impl.h, :self._impl.w] if self._crop else rgba

        def begin(self, rgba, timestamps=None):
            return self._impl.begin(self._cut(rgba), timestamps if timestamps is not None else uniform_timestamps(self.cfg.multiplier))

        def finish(self, token):
            return self._impl.end(token)

        def submit(self, rgba, timestamps=None):
            return self.finish(self.begin(rgba, timestamps))

        def close(self):
            self._impl.close()

    ModuleBackend.__name__ = "Module_" + entry["key"]
    return ModuleBackend


REGISTRY = {"dlssg": DlssgBackend, "fsr3": Fsr3Backend, "blend": BlendBackend}
try:
    import modules as _modules
    for _m in _modules.usable():
        for _e in _m.framegen:
            REGISTRY[_e["key"]] = _module_backend_class(_m, _e)
except Exception as _exc:   # a broken module must never take the core down
    import sys as _sys
    print(f"[framegen] modules unavailable: {_exc}", file=_sys.stderr)
