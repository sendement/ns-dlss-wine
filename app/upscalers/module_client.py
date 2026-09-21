# SPDX-License-Identifier: MIT
"""Upscaler client of the module protocol (docs/module-protocol.md): one host process per (size, settings), one frame at a time through three mmap'ed files."""
import mmap
import os
import struct
import subprocess
import tempfile
import time

import numpy as np

from .base import Setting, Upscaler

_STATE, _REQ, _ACK, _OK, _QUIT = 0, 4, 8, 12, 16
_STARTUP_TIMEOUT_SEC = 90.0
_FRAME_TIMEOUT_SEC = 10.0


class ModuleUpscalerError(RuntimeError):
    pass


def make_upscaler_class(module, entry: dict):
    """A registry-ready Upscaler subclass for one manifest entry."""
    settings = [Setting(key=s["key"], label=s.get("label", s["key"]), kind=s.get("kind", "float"), default=s.get("default", 0), min=s.get("min", 0),
                        max=s.get("max", 1), step=s.get("step", 0.05)) for s in entry.get("settings", [])]

    class ModuleUpscaler(Upscaler):
        name = entry["title"]
        _settings_defs = settings

        def __init__(self, card_path: str = ""):
            self._proc = None
            self._key = None
            self._maps, self._files, self._dir = [], [], None
            self.passthrough = False

        def configure(self, src_w, src_h, dst_w, dst_h, **values):
            opts = {s.key: values.get(s.key, s.default) for s in settings}
            key = (src_w, src_h, dst_w, dst_h, tuple(sorted(opts.items())))
            if key == self._key:
                return
            self._stop()
            self._key = key
            self._start(src_w, src_h, dst_w, dst_h, opts)

        def _start(self, iw, ih, ow, oh, opts):
            self._dir = tempfile.mkdtemp(prefix="ns-mod-up-")
            paths = [os.path.join(self._dir, n) for n in ("in.bin", "out.bin", "ctl.bin")]
            for p, size in zip(paths, (iw * ih * 4, ow * oh * 4, 192)):
                with open(p, "wb") as f:
                    f.truncate(size)
            env = dict(os.environ)
            env.setdefault("__NV_PRIME_RENDER_OFFLOAD", "1")
            env.setdefault("__GLX_VENDOR_LIBRARY_NAME", "nvidia")
            self._proc = subprocess.Popen(module.command(entry) + [str(iw), str(ih), str(ow), str(oh), *paths, *[f"{k}={v}" for k, v in opts.items()]],
                                          cwd=module.root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=None if os.environ.get("NS_MODULE_LOG") else subprocess.DEVNULL)
            for p in paths:
                f = open(p, "r+b")
                self._files.append(f)
                self._maps.append(mmap.mmap(f.fileno(), 0))
            self._in, self._out, self._ctl = self._maps
            t0 = time.monotonic()
            while self._u32(_STATE) == 0:
                if self._proc.poll() is not None or time.monotonic() - t0 > _STARTUP_TIMEOUT_SEC:
                    self._stop()
                    raise ModuleUpscalerError(f"module host {entry['key']} failed to start (NS_MODULE_LOG=1 shows why)")
                time.sleep(0.02)
            if self._u32(_STATE) != 1:
                self._stop()
                raise ModuleUpscalerError(f"module host {entry['key']} reported an error while starting (NS_MODULE_LOG=1)")
            self._src_shape, self._out_shape, self._seq = (ih, iw, 4), (oh, ow, 4), 0

        def _u32(self, off):
            return struct.unpack_from("<I", self._ctl, off)[0]

        def _stop(self):
            if self._proc is not None:
                try:
                    if self._maps:
                        struct.pack_into("<I", self._ctl, _QUIT, 1)
                    self._proc.wait(timeout=3)
                except Exception:
                    self._proc.kill()
                    self._proc.wait()
                self._proc = None
            for m in self._maps:
                try:
                    m.close()
                except Exception:
                    pass
            for f in self._files:
                f.close()
            self._maps, self._files = [], []
            if self._dir:
                for n in os.listdir(self._dir):
                    try:
                        os.unlink(os.path.join(self._dir, n))
                    except OSError:
                        pass
                try:
                    os.rmdir(self._dir)
                except OSError:
                    pass
                self._dir = None

        def upscale(self, frame: np.ndarray) -> np.ndarray:
            assert frame.shape == self._src_shape, (frame.shape, self._src_shape)
            np.frombuffer(self._in, dtype=np.uint8).reshape(self._src_shape)[:] = frame
            self._seq += 1
            struct.pack_into("<I", self._ctl, _REQ, self._seq)
            t0 = time.monotonic()
            while self._u32(_ACK) != self._seq:
                if self._proc.poll() is not None or time.monotonic() - t0 > _FRAME_TIMEOUT_SEC:
                    raise ModuleUpscalerError("module host stopped answering")
            if not self._u32(_OK):
                raise ModuleUpscalerError("module host failed a frame")
            return np.frombuffer(self._out, dtype=np.uint8).reshape(self._out_shape).copy()

        def close(self):
            self._stop()

    ModuleUpscaler.settings = settings
    ModuleUpscaler.__name__ = "Module_" + entry["key"]
    return ModuleUpscaler
