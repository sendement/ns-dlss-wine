# SPDX-License-Identifier: MIT
"""Upscaler plugin interface - see ../../README.md's "Upscaling" section.

Adding a new upscaler is: one new file implementing `Upscaler`, plus one
line in `upscalers/__init__.py`'s `REGISTRY`. The settings panel builds its
controls generically from `Upscaler.settings`, so a new upscaler never
needs its own UI code.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

import numpy as np

SettingKind = Literal["float", "bool", "int"]


@dataclass
class Setting:
    key: str
    label: str
    kind: SettingKind
    default: float
    min: float = 0.0
    max: float = 1.0
    step: float = 0.01


class Upscaler(ABC):
    """One instance is configured for a fixed (src_w, src_h) -> (dst_w,
    dst_h) at a time. Changing sizes goes through `configure()` again -
    implementations that need to rebuild GPU resources on resize (e.g.
    NIS, which allocates fixed-size textures) do that inside `configure()`,
    not on every `upscale()` call.

    Every subclass takes the same `(card_path=...)` constructor, even if
    it ignores it (e.g. NoneUpscaler) - lets callers build any registry
    entry uniformly (`REGISTRY[key](card_path=KMS_CARD)`) without knowing
    which ones actually need a GPU device.
    """
    name: str = "base"
    settings: list[Setting] = []

    def __init__(self, card_path: str = "/dev/dri/card0"):
        self.card_path = card_path

    @abstractmethod
    def configure(self, src_w: int, src_h: int, dst_w: int, dst_h: int, **settings) -> None:
        ...

    @abstractmethod
    def upscale(self, frame: np.ndarray) -> np.ndarray:
        ...

    def close(self) -> None:
        pass
