# SPDX-License-Identifier: MIT
"""Plugin adapter around the existing nis_upscale.NisUpscaler (a GLES
fragment-shader port of NVIDIA Image Scaling's NVScaler - see nis/nis_lib.c
and ../../README.md's "Upscaling" section for the full story). The
underlying library needs fixed src/dst sizes at construction, so
`configure()` just closes and rebuilds it on a size change - resizes are
rare/user-driven (a slider release, not per-frame), so this is cheap
enough not to be worth extending the C library for in-place resize.
"""
import numpy as np

from nis_upscale import NisUpscaler as _NisUpscalerImpl
from .base import Setting, Upscaler


class NisUpscalerPlugin(Upscaler):
    name = "NIS"
    settings = [
        Setting(key="sharpness", label="Sharpness", kind="float", default=0.5, min=0.0, max=1.0, step=0.05),
    ]

    def __init__(self, card_path: str = "/dev/dri/card0"):
        self._card_path = card_path
        self._impl = None
        self._key = None  # (src_w, src_h, dst_w, dst_h, sharpness)

    def configure(self, src_w, src_h, dst_w, dst_h, sharpness=0.5, **_ignored):
        key = (src_w, src_h, dst_w, dst_h, sharpness)
        if key == self._key:
            return
        if self._impl is not None:
            self._impl.close()
        self._impl = _NisUpscalerImpl(src_w, src_h, dst_w, dst_h, sharpness=sharpness, card_path=self._card_path)
        self._key = key

    def upscale(self, frame: np.ndarray) -> np.ndarray:
        return self._impl.upscale(frame)

    def close(self):
        if self._impl is not None:
            self._impl.close()
            self._impl = None
