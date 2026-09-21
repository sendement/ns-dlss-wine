# SPDX-License-Identifier: MIT
"""Composition post-pass: how much of the DLSSNR result is blended back over the captured source.

Ports the ideas of the reference app's "composition" controls (Detail-Only, NR Color Strength, Tone Preservation,
Face/Skin Protection, Grain Preservation, Shimmer Suppression, Custom NR Mask + Feather) to a small numpy pass that
runs at the WORKING resolution (before the upscaler), where both the captured `src` and the model's `out` are at
hand. Everything is a no-op at its default value, and an all-default `Composition` costs nothing.

Definitions (all on BT.709 luma Y and chroma = rgb - Y):
  color_strength c  : 1 = the model's colour, 0 = the source's colour (chroma blended by c).
  tone_preservation t: moves the output's LOW-frequency luma toward the source's (t=1: source tone, model detail).
                       Detail-Only == color 0 + tone 1.
  grain g           : puts back source fine detail the model removed (only where the source has more of it).
  face_skin p       : blends the source back over skin-coloured regions (YCbCr box, softened).
  shimmer s         : temporal - static pixels (source barely changed) are blended with the previous result.
  mask              : luminance image, 1 = model applies, 0 = source shows through; `mask_feather` blurs it.
"""
import ctypes
import os
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter

_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
_SHIMMER_THRESHOLD = 14.0   # source luma delta (0-255) above which a pixel counts as "moving"
_LOWPASS_DIV = 4            # blurs run on a frame downscaled by this much - they are low-frequency by definition


@dataclass(frozen=True)
class Composition:
    color_strength: float = 1.0
    tone_preservation: float = 0.0
    face_skin_protection: float = 0.0
    grain_preservation: float = 0.0
    shimmer_suppression: float = 0.0
    mask_path: str = ""
    mask_feather: int = 0
    passes: int = 1

    def needs_post(self) -> bool:
        return (self.color_strength < 1.0 or self.tone_preservation > 0.0 or self.face_skin_protection > 0.0
                or self.grain_preservation > 0.0 or self.shimmer_suppression > 0.0 or bool(self.mask_path))


def _luma8(rgba: np.ndarray) -> np.ndarray:
    return (rgba[:, :, :3].astype(np.float32) @ _LUMA)


def _blur_lowres(plane: np.ndarray, radius: float) -> np.ndarray:
    """Gaussian blur of a float plane (0-255) with `radius` in full-res pixels, done on a downscaled copy."""
    h, w = plane.shape
    sw, sh = max(1, w // _LOWPASS_DIV), max(1, h // _LOWPASS_DIV)
    small = Image.fromarray(np.clip(plane, 0, 255).astype(np.uint8), "L").resize((sw, sh), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(max(0.5, radius / _LOWPASS_DIV)))
    return np.asarray(small.resize((w, h), Image.BILINEAR), dtype=np.float32)



_LIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "post", "libpost.so")


class _Params(ctypes.Structure):
    _fields_ = [("color", ctypes.c_float), ("tone", ctypes.c_float), ("grain", ctypes.c_float),
                ("skin", ctypes.c_float), ("shimmer", ctypes.c_float),
                ("use_mask", ctypes.c_int), ("prev_valid", ctypes.c_int), ("tone_radius", ctypes.c_int)]


_lib = None


def _load():
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(_LIB_PATH)
        lib.post_apply.restype = ctypes.c_int
        lib.post_apply.argtypes = [ctypes.c_void_p] * 3 + [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_Params),
                                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        _lib = lib
    return _lib


class PostProcess:
    def __init__(self):
        self._prev_ys = None
        self._prev_out = None
        self._mask_key = None
        self._mask = None

    def reset(self):
        self._prev_ys = self._prev_out = None

    # -- custom mask ---------------------------------------------------
    def _get_mask(self, path: str, feather: int, w: int, h: int):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        key = (path, mtime, feather, w, h)
        if key != self._mask_key:
            try:
                im = Image.open(path).convert("RGBA")
            except Exception:
                self._mask_key, self._mask = key, None
                return None
            rgba = np.asarray(im, dtype=np.float32) / 255.0
            lum = (rgba[..., :3] @ _LUMA) * rgba[..., 3]
            m = Image.fromarray((lum * 255).astype(np.uint8), "L").resize((w, h), Image.BILINEAR)
            if feather > 0:
                m = m.filter(ImageFilter.GaussianBlur(feather))
            self._mask_key, self._mask = key, np.asarray(m, dtype=np.float32)[:, :, None] / 255.0
        return self._mask

    # -- main entry ----------------------------------------------------
    def apply(self, src: np.ndarray, out: np.ndarray, c: Composition) -> np.ndarray:
        """Composition post-pass (C, OpenMP). Returns `out` untouched when everything is at its default."""
        if not c.needs_post() or src.shape != out.shape:
            self.reset()
            return out
        h, w = out.shape[:2]
        src = np.ascontiguousarray(src)
        out = np.ascontiguousarray(out)
        dst = np.empty_like(out)
        mask = self._get_mask8(c.mask_path, c.mask_feather, w, h) if c.mask_path else None
        shim = c.shimmer_suppression > 0.0
        if shim and (self._prev_out is None or self._prev_out.shape != out.shape):
            self._prev_out = np.zeros_like(out)
            self._prev_luma = np.zeros((h, w), dtype=np.uint8)
            self._prev_ys = None
        params = _Params(c.color_strength, c.tone_preservation, c.grain_preservation, c.face_skin_protection,
                         c.shimmer_suppression, int(mask is not None), int(shim and self._prev_ys is not None),
                         int(max(3, min(h, w) // 40)))
        rc = _load().post_apply(src.ctypes.data, out.ctypes.data, dst.ctypes.data, w, h, ctypes.byref(params),
                                mask.ctypes.data if mask is not None else None,
                                self._prev_out.ctypes.data if shim else None,
                                self._prev_luma.ctypes.data if shim else None)
        if rc:
            return out
        if shim:
            self._prev_ys = True  # marks the temporal state as valid
        else:
            self._prev_ys = None
        return dst

    def _get_mask8(self, path, feather, w, h):
        m = self._get_mask(path, feather, w, h)
        return None if m is None else np.ascontiguousarray((m[:, :, 0] * 255.0).astype(np.uint8))

    def apply_numpy(self, src: np.ndarray, out: np.ndarray, c: Composition) -> np.ndarray:
        """Reference implementation (slow: ~25-70 ms at 1720x720) - kept to check the C library against."""
        if not c.needs_post() or src.shape != out.shape:
            self.reset()
            return out
        h, w = out.shape[:2]
        s = src[:, :, :3].astype(np.float32)
        o = out[:, :, :3].astype(np.float32)
        ys, yo = s @ _LUMA, o @ _LUMA
        rgb = o.copy()

        # tone: output's low-frequency luma -> source's (keeps the model's detail)
        if c.tone_preservation > 0.0:
            radius = max(3.0, min(h, w) / 40.0)
            rgb += (c.tone_preservation * (_blur_lowres(ys, radius) - _blur_lowres(yo, radius)))[:, :, None]
        # colour: chroma of the source vs the model's
        if c.color_strength < 1.0:
            cs = s - ys[:, :, None]
            co = o - yo[:, :, None]
            rgb += (1.0 - c.color_strength) * (cs - co)
        # grain: restore source fine detail the model smoothed away
        if c.grain_preservation > 0.0:
            hf_s = ys - _blur_lowres(ys, 2.0 * _LOWPASS_DIV)
            hf_o = yo - _blur_lowres(yo, 2.0 * _LOWPASS_DIV)
            lost = np.maximum(np.abs(hf_s) - np.abs(hf_o), 0.0) * np.sign(hf_s)
            rgb += (c.grain_preservation * lost)[:, :, None]
        # face/skin: source shows through over skin-coloured pixels
        if c.face_skin_protection > 0.0:
            cb = -0.1687 * s[..., 0] - 0.3313 * s[..., 1] + 0.5 * s[..., 2] + 128.0
            cr = 0.5 * s[..., 0] - 0.4187 * s[..., 1] - 0.0813 * s[..., 2] + 128.0
            skin = ((cb >= 77) & (cb <= 127) & (cr >= 133) & (cr <= 173) & (ys > 40)).astype(np.float32) * 255.0
            m = _blur_lowres(skin, 12.0)[:, :, None] / 255.0
            rgb += c.face_skin_protection * m * (s - rgb)
        # custom mask: 1 = model, 0 = source
        if c.mask_path:
            m = self._get_mask(c.mask_path, c.mask_feather, w, h)
            if m is not None:
                rgb = s + m * (rgb - s)
        # shimmer: blend static pixels with the previous result
        if c.shimmer_suppression > 0.0:
            if self._prev_ys is not None and self._prev_ys.shape == ys.shape:
                still = np.clip(1.0 - np.abs(ys - self._prev_ys) / _SHIMMER_THRESHOLD, 0.0, 1.0)
                wgt = (c.shimmer_suppression * still * still)[:, :, None]
                rgb += wgt * (self._prev_out - rgb)
            self._prev_ys = ys
        else:
            self._prev_ys = None

        res = np.empty_like(out)
        np.clip(rgb, 0.0, 255.0, out=rgb)
        res[:, :, :3] = rgb  # float -> uint8 truncation is fine here; rounding would cost another pass
        res[:, :, 3] = 255
        if c.shimmer_suppression > 0.0:
            self._prev_out = rgb
        return res
