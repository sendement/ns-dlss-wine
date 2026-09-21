# SPDX-License-Identifier: MIT
"""Upscaler plugin registry. Add a new upscaler by writing a new module
implementing `Upscaler` (see base.py) and adding one line below - the
settings panel builds its UI generically from each entry's `Upscaler.settings`.
"""
from .base import Setting, Upscaler
from .none_upscaler import NoneUpscaler
from .nis_upscaler import NisUpscalerPlugin
from .rtx_vsr_upscaler import RtxVsrUpscaler
from .rtx_vsr_native import RtxVsrNativeUpscaler
import userfiles as _userfiles
from .fsr_upscaler import FsrUpscalerPlugin

REGISTRY: dict[str, type[Upscaler]] = {
    "none": NoneUpscaler,
    "nis": NisUpscalerPlugin,
    # native (nvidia-vfx) when its venv is installed, else the Wine bridge host
    "rtx_vsr": RtxVsrNativeUpscaler if not _userfiles.vsr_native_status() else RtxVsrUpscaler,
    "fsr": FsrUpscalerPlugin,
}

try:   # upscalers of optional modules (docs/module-protocol.md); a broken module must never take the core down
    import modules as _modules
    from .module_client import make_upscaler_class as _mk
    for _m in _modules.usable():
        for _e in _m.upscalers:
            REGISTRY[_e["key"]] = _mk(_m, _e)
except Exception as _exc:
    import sys as _sys
    print(f"[upscalers] modules unavailable: {_exc}", file=_sys.stderr)

__all__ = ["Setting", "Upscaler", "REGISTRY"]
