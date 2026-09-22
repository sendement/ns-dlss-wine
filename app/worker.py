# SPDX-License-Identifier: MIT
"""Client of docs/worker-protocol.md (the open neural-render worker protocol): starts a worker process and exchanges frames with it through three mmap'ed
files. `hosts/worker_adapter.cpp` (built by tools/build_all.sh) is the reference adapter, translating this protocol to NeuralScreen's own DLSS5 worker wire
format; a worker that speaks the open protocol directly needs no adapter and no Wine, and would use this same client unchanged.

Public surface kept identical to the previous in-process pipe implementation, so live_filter.py's call sites don't change: `Worker(w, h, full_w, full_h,
params)`, `.process(rgba) -> frame | None`, `.reconfigure(w, h, full_w, full_h, params, flags)`, `.close()`, `.out_w/.out_h`, `.last_write_ms/.last_wait_ms/
.last_read_ms`.
"""
import mmap
import os
import struct
import subprocess
import tempfile
import time

import numpy as np

import paths as nspaths
import userfiles

# Control block (docs/worker-protocol.md): 18 u32 fields at 4-byte offsets 0..68, then 4 f32 at 72/76/80/84. 88 bytes total. Field -> byte offset:
_STATE, _REQ_SEQ, _REQ_MODE, _ACK_SEQ, _OK, _CODE, _QUIT, _OUT_W, _OUT_H = 0, 4, 8, 12, 16, 20, 24, 28, 32
_WORK_W, _WORK_H, _REQ_OUT_W, _REQ_OUT_H, _WARMUP, _FLAGS, _STYLE, _AUTO_MASK, _UI_CORRECTION = 36, 40, 44, 48, 52, 56, 60, 64, 68
_INTENSITY, _LOCAL_TONE, _LOCAL_STRUCTURE, _SKIN_STRUCTURE = 72, 76, 80, 84
_CTL_SIZE = 88
_STARTUP_TIMEOUT_SEC = 90.0
_CONFIGURE_TIMEOUT_SEC = 90.0   # the first reconfigure is where the worker actually loads its model - can take much longer than a steady-state frame
_FRAME_TIMEOUT_SEC = 10.0

EXE = "worker_adapter.exe"


class WorkerError(RuntimeError):
    pass


def _winpath(p: str) -> str:
    return "Z:" + p.replace("/", "\\")


class Worker:
    def __init__(self, w, h, full_w=0, full_h=0, params: "DlssParams | None" = None, max_w=0, max_h=0, max_out_w=0, max_out_h=0):
        """`max_*` bound every later reconfigure() (default: the construction size - pass the largest size you will ever request if you plan to
        reconfigure to something bigger, e.g. a resolution slider's ceiling)."""
        from live_state import DlssParams
        self.params = params or DlssParams()
        userfiles.require("dlss5")
        self._max_w, self._max_h = max_w or w, max_h or h
        out_w0, out_h0 = (full_w, full_h) if (full_w and full_h) else (w, h)
        self._max_out_w, self._max_out_h = max_out_w or out_w0, max_out_h or out_h0
        wine = nspaths.wine_binary()
        self._dir = tempfile.mkdtemp(prefix="ns-worker-")
        self._paths = [os.path.join(self._dir, n) for n in ("in.bin", "out.bin", "ctl.bin")]
        for p, size in zip(self._paths, (self._max_w * self._max_h * 4, self._max_out_w * self._max_out_h * 4, _CTL_SIZE)):
            with open(p, "wb") as f:
                f.truncate(size)
        env = dict(os.environ)
        env["WINEPREFIX"] = nspaths.PREFIX
        env.setdefault("__NV_PRIME_RENDER_OFFLOAD", "1")
        env.setdefault("__GLX_VENDOR_LIBRARY_NAME", "nvidia")
        env.setdefault("VK_ICD_FILENAMES", "/usr/share/vulkan/icd.d/nvidia_icd.json")
        env["WINEDLLOVERRIDES"] = "dxgi,d3d12,d3d12core=n"
        env["WINEDEBUG"] = "-all"
        args = [str(self._max_w), str(self._max_h), str(self._max_out_w), str(self._max_out_h), *[_winpath(p) for p in self._paths]]
        nr_preset = os.environ.get("NS_NR_PRESET")
        if nr_preset:
            args.append(f"nr_preset={nr_preset}")
        self.proc = subprocess.Popen(
            [wine, EXE, *args], cwd=nspaths.WORKER_DLSS5, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=None if os.environ.get("NS_WORKER_LOG") else subprocess.DEVNULL)
        self._files, self._maps = [], []
        for p in self._paths:
            f = open(p, "r+b")
            self._files.append(f)
            self._maps.append(mmap.mmap(f.fileno(), 0))
        self._in, self._out, self._ctl = self._maps
        t0 = time.monotonic()
        while self._u32(_STATE) == 0:
            if self.proc.poll() is not None or time.monotonic() - t0 > _STARTUP_TIMEOUT_SEC:
                self.close()
                raise WorkerError("worker_adapter failed to start (NS_WORKER_LOG=1 shows why)")
            time.sleep(0.02)
        if self._u32(_STATE) != 1:
            self.close()
            raise WorkerError("worker_adapter reported an error while starting (NS_WORKER_LOG=1)")
        self._seq = 0
        self.w, self.h, self.out_w, self.out_h = w, h, out_w0, out_h0
        self.last_write_ms = self.last_wait_ms = self.last_read_ms = 0.0
        self._reconfigure(w, h, full_w, full_h, self.params, 0)

    def _u32(self, off):
        return struct.unpack_from("<I", self._ctl, off)[0]

    def _wait_ack(self, seq, timeout=_FRAME_TIMEOUT_SEC):
        t0 = time.monotonic()
        while self._u32(_ACK_SEQ) != seq:
            if self.proc.poll() is not None or time.monotonic() - t0 > timeout:
                raise WorkerError("worker_adapter stopped answering")
            time.sleep(0.0002)

    def _reconfigure(self, w, h, full_w, full_h, params, flags):
        upscale = full_w > 0 and full_h > 0 and (full_w != w or full_h != h)
        out_w, out_h = (full_w, full_h) if upscale else (w, h)
        if w > self._max_w or h > self._max_h or out_w > self._max_out_w or out_h > self._max_out_h:
            raise ValueError(f"reconfigure({w}x{h} -> {out_w}x{out_h}) exceeds the ceiling this Worker was constructed with "
                              f"({self._max_w}x{self._max_h} -> {self._max_out_w}x{self._max_out_h})")
        # Every field of the reconfigure request except req_seq itself (bumped last, to publish the request atomically) - out_w/out_h (offset 28/32) are
        # WORKER-owned per docs/worker-protocol.md and must never be written here.
        struct.pack_into("<7I", self._ctl, _WORK_W, w, h, out_w, out_h, 0, flags, params.style)
        struct.pack_into("<2I", self._ctl, _AUTO_MASK, params.auto_mask, params.ui_correction)
        struct.pack_into("<4f", self._ctl, _INTENSITY, params.intensity, params.local_tone, params.local_structure, params.skin_structure)
        struct.pack_into("<I", self._ctl, _REQ_MODE, 1)
        seq = self._seq + 1
        struct.pack_into("<I", self._ctl, _REQ_SEQ, seq)
        self._wait_ack(seq, timeout=_CONFIGURE_TIMEOUT_SEC if seq == 1 else _FRAME_TIMEOUT_SEC)
        self._seq = seq
        if not self._u32(_OK):
            raise WorkerError(f"reconfigure({w}x{h} -> {out_w}x{out_h}) rejected by the worker (code={self._u32(_CODE)})")
        self.w, self.h, self.out_w, self.out_h = w, h, self._u32(_OUT_W), self._u32(_OUT_H)
        self.params = params

    def reconfigure(self, w, h, full_w=0, full_h=0, params: "DlssParams | None" = None, flags=0):
        """Live in-place reconfigure - no worker process restart. Debounce continuous controls (a slider): the underlying worker typically tears down and
        recreates its internal feature on every call, which is far cheaper than a restart but not free (expect a visible hitch)."""
        self._reconfigure(w, h, full_w, full_h, params if params is not None else self.params, flags)

    def process(self, rgba: np.ndarray) -> np.ndarray | None:
        assert rgba.shape == (self.h, self.w, 4), rgba.shape
        t0 = time.monotonic()
        n = self.h * self.w * 4
        np.frombuffer(self._in, dtype=np.uint8, count=n).reshape(self.h, self.w, 4)[:] = rgba
        struct.pack_into("<I", self._ctl, _REQ_MODE, 0)
        seq = self._seq + 1
        struct.pack_into("<I", self._ctl, _REQ_SEQ, seq)
        t1 = time.monotonic()
        self._wait_ack(seq)
        self._seq = seq
        t2 = time.monotonic()
        self.last_write_ms = (t1 - t0) * 1000
        self.last_wait_ms = (t2 - t1) * 1000
        if not self._u32(_OK):
            self.last_read_ms = 0.0
            return None
        out = np.frombuffer(self._out, dtype=np.uint8, count=self.out_h * self.out_w * 4).reshape(self.out_h, self.out_w, 4).copy()
        self.last_read_ms = (time.monotonic() - t2) * 1000
        return out

    def close(self):
        proc = getattr(self, "proc", None)
        if proc is not None:
            try:
                if self._maps:
                    struct.pack_into("<I", self._ctl, _QUIT, 1)
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
                proc.wait()
            self.proc = None
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
