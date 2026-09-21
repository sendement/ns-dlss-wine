# SPDX-License-Identifier: MIT
"""Plugin adapter around fsr_upscale.FsrUpscaler - AMD FidelityFX FSR 1.0 (EASU + RCAS), GLES 3.1 shaders taken
verbatim from AMD's headers (MIT). Like NIS the GL objects have fixed sizes, so `configure()` rebuilds on a size or
sharpness change (rare, user-driven). See ../../README.md ("FSR")."""
import numpy as np

from fsr_upscale import FsrUpscaler as _FsrImpl
from .base import Setting, Upscaler


class FsrUpscalerPlugin(Upscaler):
    name = "FSR 1 (EASU+RCAS)"
    settings = [
        Setting(key="sharpness", label="Sharpness", kind="float", default=0.85, min=0.0, max=1.0, step=0.05),
    ]

    def __init__(self, card_path: str = "/dev/dri/card0"):
        self._card_path = card_path
        self._impl = None
        self._key = None

    def configure(self, src_w, src_h, dst_w, dst_h, sharpness=0.85, **_ignored):
        key = (src_w, src_h, dst_w, dst_h, round(float(sharpness), 3))
        if key == self._key:
            return
        if self._impl is not None:
            self._impl.close()
            self._impl = None
        self._impl = _FsrImpl(src_w, src_h, dst_w, dst_h, sharpness=float(sharpness), card_path=self._card_path)
        self._key = key

    def upscale(self, frame: np.ndarray) -> np.ndarray:
        return self._impl.upscale(frame)

    def close(self):
        if self._impl is not None:
            self._impl.close()
            self._impl = None
