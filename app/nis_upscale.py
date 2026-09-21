# SPDX-License-Identifier: MIT
"""ctypes wrapper around nis/libnisupscale.so - a GLES fragment-shader port
of NVIDIA Image Scaling's NVScaler (edge-adaptive single-frame upscale +
sharpen, see nis/nis_lib.c for the full port notes). Used to upscale a
reduced-resolution DLSS5-cleaned frame back up to display resolution
instead of naive bilinear/nearest - see ../README.md.
"""
import ctypes
import os

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB_PATH = os.path.join(_HERE, "nis", "libnisupscale.so")


class NisError(RuntimeError):
    pass


class NisUpscaler:
    def __init__(self, src_w: int, src_h: int, dst_w: int, dst_h: int,
                 sharpness: float = 0.5, card_path: str = ""):
        from drm_card import detect_display_card
        card_path = card_path or detect_display_card()
        self._lib = ctypes.CDLL(_LIB_PATH)
        self._lib.nis_init.restype = ctypes.c_void_p
        self._lib.nis_init.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_float]
        self._lib.nis_upscale.restype = ctypes.c_int
        self._lib.nis_upscale.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self._lib.nis_last_error.restype = ctypes.c_char_p
        self._lib.nis_last_error.argtypes = [ctypes.c_void_p]
        self._lib.nis_close.argtypes = [ctypes.c_void_p]

        self.src_w, self.src_h = src_w, src_h
        self.dst_w, self.dst_h = dst_w, dst_h
        self._handle = self._lib.nis_init(card_path.encode(), src_w, src_h, dst_w, dst_h, sharpness)
        if not self._handle:
            raise NisError(f"nis_init failed ({src_w}x{src_h} -> {dst_w}x{dst_h})")

    def upscale(self, src_rgba: np.ndarray) -> np.ndarray:
        assert src_rgba.shape == (self.src_h, self.src_w, 4), src_rgba.shape
        src = np.ascontiguousarray(src_rgba, dtype=np.uint8)
        dst = np.empty((self.dst_h, self.dst_w, 4), dtype=np.uint8)
        ret = self._lib.nis_upscale(
            self._handle, src.ctypes.data_as(ctypes.c_void_p), dst.ctypes.data_as(ctypes.c_void_p)
        )
        if ret != 0:
            err = self._lib.nis_last_error(self._handle).decode(errors="replace")
            raise NisError(f"upscale failed: {err}")
        return dst

    def close(self):
        if self._handle:
            self._lib.nis_close(self._handle)
            self._handle = None


if __name__ == "__main__":
    import sys
    import time
    from PIL import Image

    src_path = sys.argv[1] if len(sys.argv) > 1 else "kms_capture_test.png"
    factor = int(sys.argv[2]) if len(sys.argv) > 2 else 4

    im = Image.open(src_path).convert("RGBA")
    full_w, full_h = im.size
    small_w, small_h = full_w // factor // 2 * 2, full_h // factor // 2 * 2
    small = im.resize((small_w, small_h), Image.BILINEAR)
    small_arr = np.array(small, dtype=np.uint8)
    print(f"source {full_w}x{full_h} -> shrunk {small_w}x{small_h} (bilinear) -> NIS upscale back to {full_w}x{full_h}")

    up = NisUpscaler(small_w, small_h, full_w, full_h, sharpness=0.5)
    out = up.upscale(small_arr)

    N = 30
    t0 = time.monotonic()
    for _ in range(N):
        out = up.upscale(small_arr)
    dt = time.monotonic() - t0
    print(f"{N} upscales in {dt:.3f}s -> {N/dt:.1f} fps ({dt/N*1000:.2f} ms/frame)")

    Image.fromarray(out, "RGBA").convert("RGB").save("nis_test_out.png")
    small.resize((full_w, full_h), Image.NEAREST).convert("RGB").save("nis_test_nearest_ref.png")
    small.resize((full_w, full_h), Image.BILINEAR).convert("RGB").save("nis_test_bilinear_ref.png")
    print("saved nis_test_out.png (NIS), nis_test_nearest_ref.png, nis_test_bilinear_ref.png for comparison")
    up.close()
