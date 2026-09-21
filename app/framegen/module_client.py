# SPDX-License-Identifier: MIT
"""Frame-generation client of the module protocol (docs/module-protocol.md): starts a module's host process and exchanges frames with it through three mmap'ed files.
Two requests may be in flight (`begin()` / `end()`); `submit()` is both."""
import mmap
import os
import struct
import subprocess
import tempfile
import time

import numpy as np

_STATE, _REQ, _ACK, _QUIT = 0, 4, 8, 16
_SLOT, _RES = 64, 104
_STARTUP_TIMEOUT_SEC = 90.0
_FRAME_TIMEOUT_SEC = 10.0
RING = 4


class ModuleError(RuntimeError):
    pass


class ModuleFrameGen:
    def __init__(self, module, entry: dict, width: int, height: int, count: int, options: dict = None):
        self.w, self.h = width, height
        self.max_count = max(1, int(entry.get("max_count", 3)))
        self.count = max(1, min(self.max_count, int(count)))
        self.bgra = entry.get("output", "rgba") == "bgra"
        self.supports_timestamps = bool(entry.get("supports_timestamps", False))
        frame_bytes = width * height * 4
        self._dir = tempfile.mkdtemp(prefix="ns-mod-fg-")
        paths = [os.path.join(self._dir, n) for n in ("in.bin", "out.bin", "ctl.bin")]
        for p, size in zip(paths, (frame_bytes * 2, frame_bytes * self.max_count * RING, 192)):
            with open(p, "wb") as f:
                f.truncate(size)
        args = [str(width), str(height), str(self.max_count), *paths] + [f"{k}={v}" for k, v in (options or {}).items()]
        env = dict(os.environ)
        env.setdefault("__NV_PRIME_RENDER_OFFLOAD", "1")
        env.setdefault("__GLX_VENDOR_LIBRARY_NAME", "nvidia")
        self._proc = subprocess.Popen(module.command(entry) + args, cwd=module.root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=None if os.environ.get("NS_MODULE_LOG") else subprocess.DEVNULL)
        self._files, self._maps = [], []
        for p in paths:
            f = open(p, "r+b")
            self._files.append(f)
            self._maps.append(mmap.mmap(f.fileno(), 0))
        self._in, self._out, self._ctl = self._maps
        t0 = time.monotonic()
        while self._u32(_STATE) == 0:
            if self._proc.poll() is not None or time.monotonic() - t0 > _STARTUP_TIMEOUT_SEC:
                self.close()
                raise ModuleError(f"module host {entry['key']} failed to start (run with NS_MODULE_LOG=1 to see why)")
            time.sleep(0.02)
        if self._u32(_STATE) != 1:
            self.close()
            raise ModuleError(f"module host {entry['key']} reported an error while starting (NS_MODULE_LOG=1)")
        self._seq = 0
        self._first = True

    def _u32(self, off):
        return struct.unpack_from("<I", self._ctl, off)[0]

    def _wait_ack(self, seq):
        t0 = time.monotonic()
        while self._u32(_ACK) < seq:
            if self._proc.poll() is not None or time.monotonic() - t0 > _FRAME_TIMEOUT_SEC:
                raise ModuleError("module host stopped answering")
            time.sleep(0.0002)

    def begin(self, rgba: np.ndarray, timestamps=None):
        """Queue the next real frame (RGBA8, height x width x 4). Blocks only while two frames are already in flight. Returns a token for end()."""
        assert rgba.shape == (self.h, self.w, 4), rgba.shape
        seq = self._seq + 1
        if seq > 2:
            self._wait_ack(seq - 2)
        slot = seq % 2
        n = self.h * self.w * 4
        np.frombuffer(self._in, dtype=np.uint8, count=n, offset=slot * n).reshape(self.h, self.w, 4)[:] = rgba
        ts = list(timestamps) if timestamps is not None else [j / (self.count + 1) for j in range(1, self.count + 1)]
        ts = ts[:self.max_count]
        struct.pack_into("<II3f", self._ctl, _SLOT + 20 * slot, 1 if self._first else 0, len(ts), *(ts + [0.0] * (3 - len(ts))))
        self._first = False
        self._seq = seq
        struct.pack_into("<I", self._ctl, _REQ, seq)
        return seq

    def end(self, seq) -> list:
        """The frames generated for request `seq` (views into the shared ring, valid for three more requests)."""
        self._wait_ack(seq)
        gen, ok = struct.unpack_from("<II", self._ctl, _RES + 8 * (seq % 4))
        if not ok:
            raise ModuleError("module host failed a frame")
        out = np.frombuffer(self._out, dtype=np.uint8).reshape(RING, self.max_count, self.h, self.w, 4)
        return [out[seq % RING][i] for i in range(min(gen, self.max_count))]

    def submit(self, rgba, timestamps=None):
        return self.end(self.begin(rgba, timestamps))

    def close(self):
        proc = getattr(self, "_proc", None)
        if proc is not None:
            try:
                if self._maps:
                    struct.pack_into("<I", self._ctl, _QUIT, 1)
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
                proc.wait()
            self._proc = None
        for m in getattr(self, "_maps", []):
            try:
                m.close()
            except Exception:
                pass
        for f in getattr(self, "_files", []):
            f.close()
        self._maps, self._files = [], []
        d = getattr(self, "_dir", None)
        if d:
            for name in os.listdir(d):
                try:
                    os.unlink(os.path.join(d, name))
                except OSError:
                    pass
            try:
                os.rmdir(d)
            except OSError:
                pass
            self._dir = None
