# SPDX-License-Identifier: MIT
"""AMD FSR 3.1 frame generation (FidelityFX SDK, MIT) on the NVIDIA GPU, through a Wine host (fsr3/fsr3fg_host.cpp, D3D12 on vkd3d-proton).

Same interface and file protocol as nsdlssg.DlssgFrameGen: `submit(rgba)` takes the next real frame (RGBA8) and returns the generated frames between the
previous real frame and this one as BGRA8 (cairo order) views into a shared ring. There are no engine motion vectors or depth for a screen filter, so the
host feeds a constant depth plane and zero motion vectors and FSR interpolates from its own optical flow. The first frame returns [].
"""
import os

from .nsdlssg import DlssgFrameGen, DlssgError
import paths as nspaths

FsrError = DlssgError


class Fsr3FrameGen(DlssgFrameGen):
    EXE = "fsr3fg_host.exe"
    WORKER = nspaths.WORKER_FSR3
    FEATURE = "fsr3"
    DLL_OVERRIDES = "dxgi,d3d11,d3d12,d3d12core,d3dcompiler_47=n"
    DLL_PATH = ""
    ENV_PREFIX = "FSR3"
    MAX_COUNT = 1   # the FSR 3.1 provider interpolates ONE frame (the midpoint) per dispatch: x2 only


_MV_X, _MV_Y = 44, 48   # global motion hint floats in the control block (see fsr3fg_host.cpp: Ctrl)


class _GlobalMotion:
    """Global translation between consecutive frames by phase correlation on a 1/4-scale green channel (~2-4 ms). There are no engine motion vectors for a screen
    filter; a uniform vector field from this covers camera pans and scrolling (FSR then trusts its game-vector path where it matches, and its own optical flow
    elsewhere). Returns (dx, dy) in FULL-resolution pixels: how far the content moved from the previous frame to this one; (0, 0) when the peak is not convincing."""
    SCALE = 4
    DEADZONE = 1.0   # full-resolution pixels
    MIN_CONFIDENCE = float(os.environ.get("NS_FSR3_MOTION_MIN_CONF", "0.08"))

    def __init__(self):
        self._prev = None
        self._win = None

    def update(self, rgba):
        import numpy as np
        sc = self.SCALE if rgba.shape[1] < 1600 else (5 if rgba.shape[1] < 2400 else 8)   # cost is dominated by the FFT size: ~9 ms at 1/4 of 1440p, ~2 ms at 1/8
        g = rgba[::sc, ::sc, 1].astype(np.float32)
        g -= g.mean()
        if self._win is None or self._win.shape != g.shape:
            self._win = np.outer(np.hanning(g.shape[0]), np.hanning(g.shape[1])).astype(np.float32)
        g *= self._win
        prev, self._prev = self._prev, g
        if prev is None or prev.shape != g.shape:
            return 0.0, 0.0
        cross = np.fft.rfft2(g) * np.conj(np.fft.rfft2(prev))
        cross /= np.abs(cross) + 1e-6
        r = np.fft.irfft2(cross, s=g.shape)
        iy, ix = np.unravel_index(int(np.argmax(r)), r.shape)
        conf = float(r[iy, ix])
        if conf < self.MIN_CONFIDENCE:
            return 0.0, 0.0
        if (iy, ix) != (0, 0) and float(r[0, 0]) >= 0.6 * conf:   # a big part of the picture is static (moving object / HUD): the 'global' peak is a local one
            return 0.0, 0.0
        def sub(m1, c, p1):            # parabolic peak refinement
            den = m1 - 2 * c + p1
            return 0.0 if abs(den) < 1e-9 else 0.5 * (m1 - p1) / den
        H, W = r.shape
        dy = sub(r[(iy - 1) % H, ix], r[iy, ix], r[(iy + 1) % H, ix])
        dx = sub(r[iy, (ix - 1) % W], r[iy, ix], r[iy, (ix + 1) % W])
        sy = (iy + dy) if iy + dy < H / 2 else (iy + dy) - H
        sx = (ix + dx) if ix + dx < W / 2 else (ix + dx) - W
        mx, my = float(sx * sc), float(sy * sc)
        if abs(mx) < self.DEADZONE and abs(my) < self.DEADZONE:   # sub-pixel jitter around a static picture: no hint
            return 0.0, 0.0
        return mx, my


def _fsr3_before_request(self, rgba):
    import struct
    if not hasattr(self, "_motion"):
        self._motion = _GlobalMotion()
        self._motion_on = os.environ.get("NS_FSR3_MOTION", "1") != "0"
    dx, dy = self._motion.update(rgba) if self._motion_on else (0.0, 0.0)
    self.last_motion = (dx, dy)
    struct.pack_into("<ff", self._ctl, _MV_X, dx, dy)


Fsr3FrameGen._before_request = _fsr3_before_request
