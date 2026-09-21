# SPDX-License-Identifier: MIT
"""The "off" baseline - plain PIL resize, no GPU/EGL dependency at all.
Always available even if every GPU-based upscaler fails to load, and the
honest baseline to compare any other upscaler's quality against (see the
NIS-vs-bilinear comparison in ../../README.md).
"""
import numpy as np
from PIL import Image

from .base import Setting, Upscaler


class NoneUpscaler(Upscaler):
    name = "none (bilinear)"
    settings: list[Setting] = []

    def configure(self, src_w, src_h, dst_w, dst_h, **settings):
        self.src_w, self.src_h = src_w, src_h
        self.dst_w, self.dst_h = dst_w, dst_h

    def upscale(self, frame: np.ndarray) -> np.ndarray:
        if (self.dst_w, self.dst_h) == (self.src_w, self.src_h):
            return frame
        im = Image.fromarray(frame, "RGBA").resize((self.dst_w, self.dst_h), Image.BILINEAR)
        return np.array(im, dtype=np.uint8)
