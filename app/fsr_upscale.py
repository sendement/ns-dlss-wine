# SPDX-License-Identifier: MIT
"""ctypes wrapper around fsr/libfsrupscale.so - AMD FidelityFX FSR 1.0 (EASU + RCAS) on GLES 3.1; same shape as
nis_upscale.py. FSR 1 is spatial (single frame); FSR 2/3/4 are temporal and need motion vectors + depth."""
import ctypes
import os

import numpy as np

_LIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fsr", "libfsrupscale.so")


class FsrError(RuntimeError):
    pass


class FsrUpscaler:
    def __init__(self, src_w: int, src_h: int, dst_w: int, dst_h: int, sharpness: float = 0.85, card_path: str = ""):
        from drm_card import detect_display_card
        card_path = card_path or detect_display_card()
        lib = self._lib = ctypes.CDLL(_LIB_PATH)
        lib.fsr_init.restype = ctypes.c_void_p
        lib.fsr_init.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_float]
        lib.fsr_upscale.restype = ctypes.c_int
        lib.fsr_upscale.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.fsr_last_error.restype = ctypes.c_char_p
        lib.fsr_last_error.argtypes = [ctypes.c_void_p]
        lib.fsr_close.argtypes = [ctypes.c_void_p]
        self.src_w, self.src_h, self.dst_w, self.dst_h = src_w, src_h, dst_w, dst_h
        self._handle = lib.fsr_init(card_path.encode(), src_w, src_h, dst_w, dst_h, sharpness)
        if not self._handle:
            raise FsrError(f"fsr_init failed ({src_w}x{src_h} -> {dst_w}x{dst_h}) - see stderr / shader log")

    def upscale(self, src_rgba: np.ndarray) -> np.ndarray:
        assert src_rgba.shape == (self.src_h, self.src_w, 4), src_rgba.shape
        src = np.ascontiguousarray(src_rgba, dtype=np.uint8)
        dst = np.empty((self.dst_h, self.dst_w, 4), dtype=np.uint8)
        if self._lib.fsr_upscale(self._handle, src.ctypes.data_as(ctypes.c_void_p), dst.ctypes.data_as(ctypes.c_void_p)) != 0:
            raise FsrError("upscale failed: " + self._lib.fsr_last_error(self._handle).decode(errors="replace"))
        return dst

    def close(self):
        if self._handle:
            self._lib.fsr_close(self._handle)
            self._handle = None
