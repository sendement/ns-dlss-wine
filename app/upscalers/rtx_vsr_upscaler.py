# SPDX-License-Identifier: MIT
"""NVIDIA RTX Video Super Resolution (nvngx_vsr.dll, driven through the
reference app's MIT bridge) as an Upscaler plugin - see ../../README.md's
"RTX Video Super Resolution under Wine" section.

It runs in a separate Wine process (`worker_vsr/vsr_host.exe`) that gets its
CUDA from the nvcuda shim (`../../nvcuda_shim`, `WINEDLLPATH=wine_nvcuda`).
Frames travel through three mmap'ed files (in / out / control block); the
control block layout must match `Ctrl` in `vsr_host.cpp`. The session is
fixed-size, so `configure()` restarts the host when the sizes or quality
change (a couple of seconds - resizes are rare, user-driven).
"""
import mmap
import os
import struct
import subprocess
import tempfile
import time

import numpy as np

from .base import Setting, Upscaler

_HERE = os.path.dirname(os.path.abspath(__file__))
import paths as nspaths  # noqa: E402
import userfiles  # noqa: E402

_WORKER_DIR = nspaths.WORKER_VSR

# Ctrl offsets (uint32 unless noted): state 0 (0 starting/1 ready/2 error), req_seq 4, ack_seq 8, ok 12, quit 16
_STATE, _REQ, _ACK, _OK, _QUIT = 0, 4, 8, 12, 16
_STARTUP_TIMEOUT_SEC = 60.0
_FRAME_TIMEOUT_SEC = 5.0


class VsrError(RuntimeError):
    pass


def _winpath(p: str) -> str:
    return "Z:" + p.replace("/", "\\")


class RtxVsrUpscaler(Upscaler):
    name = "RTX VSR"
    settings = [
        Setting(key="quality", label="Quality (1-4)", kind="int", default=4, min=1, max=4, step=1),
    ]

    def __init__(self, card_path: str = ""):
        self._proc = None
        self._key = None
        self._dir = None
        self._maps = []
        self._files = []
        self._out_shape = None
        self._seq = 0
        self.passthrough = False

    # -- lifecycle ---------------------------------------------------
    def configure(self, src_w, src_h, dst_w, dst_h, quality=4, **_ignored):
        key = (src_w, src_h, dst_w, dst_h, int(quality))
        if key == self._key:
            return
        self._stop()
        self._key = key
        # VSR only upsamples; equal sizes = nothing to do.
        self.passthrough = dst_w <= src_w and dst_h <= src_h
        if self.passthrough:
            return
        self._start(src_w, src_h, dst_w, dst_h, int(quality))

    def _start(self, iw, ih, ow, oh, quality):
        userfiles.require("vsr")
        wine = nspaths.wine_binary()
        self._dir = tempfile.mkdtemp(prefix="ns-vsr-")
        paths = [os.path.join(self._dir, n) for n in ("in.bin", "out.bin", "ctl.bin")]
        for p, size in zip(paths, (iw * ih * 4, ow * oh * 4, 64)):
            with open(p, "wb") as f:
                f.truncate(size)
        env = dict(os.environ)
        env["WINEPREFIX"] = nspaths.PREFIX
        env.setdefault("__NV_PRIME_RENDER_OFFLOAD", "1")
        env.setdefault("__GLX_VENDOR_LIBRARY_NAME", "nvidia")
        env.setdefault("VK_ICD_FILENAMES", "/usr/share/vulkan/icd.d/nvidia_icd.json")
        env["WINEDLLOVERRIDES"] = "dxgi,d3d11,d3d12,d3d12core,nvcuda=n"
        env["WINEDLLPATH"] = nspaths.WINE_NVCUDA
        env["WINEDEBUG"] = "-all"
        # stdin MUST be /dev/null: code inside nvngx_vsr reads it (see vsr_host.cpp).
        self._proc = subprocess.Popen(
            [wine, "vsr_host.exe", str(iw), str(ih), str(ow), str(oh), str(quality),
             _winpath(paths[0]), _winpath(paths[1]), _winpath(paths[2]),
             _winpath(_WORKER_DIR)],
            cwd=_WORKER_DIR, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for p in paths:
            f = open(p, "r+b")
            self._files.append(f)
            self._maps.append(mmap.mmap(f.fileno(), 0))
        self._in, self._out, self._ctl = self._maps
        t0 = time.monotonic()
        while self._u32(_STATE) == 0:
            if self._proc.poll() is not None or time.monotonic() - t0 > _STARTUP_TIMEOUT_SEC:
                self._stop()
                raise VsrError("vsr_host failed to start")
            time.sleep(0.02)
        if self._u32(_STATE) != 1:
            self._stop()
            raise VsrError("vsr_host session creation failed")
        self._src_shape = (ih, iw, 4)
        self._out_shape = (oh, ow, 4)
        self._seq = 0

    def _u32(self, off):
        return struct.unpack_from("<I", self._ctl, off)[0]

    def _stop(self):
        self._key = None if self._proc is None and not self._maps else self._key
        if self._proc is not None:
            try:
                if self._maps:
                    struct.pack_into("<I", self._ctl, _QUIT, 1)
                self._proc.wait(timeout=2)
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

    # -- per frame ---------------------------------------------------
    def upscale(self, frame: np.ndarray) -> np.ndarray:
        if self.passthrough:
            return frame
        assert frame.shape == self._src_shape, (frame.shape, self._src_shape)
        np.frombuffer(self._in, dtype=np.uint8).reshape(self._src_shape)[:] = frame  # straight into the mmap, no temp copy
        self._seq += 1
        struct.pack_into("<I", self._ctl, _REQ, self._seq)
        t0 = time.monotonic()
        while self._u32(_ACK) != self._seq:
            if self._proc.poll() is not None or time.monotonic() - t0 > _FRAME_TIMEOUT_SEC:
                raise VsrError("vsr_host stopped answering")
        if not self._u32(_OK):
            raise VsrError("vsr frame failed")
        return np.frombuffer(self._out, dtype=np.uint8).reshape(self._out_shape).copy()

    def close(self):
        self._stop()
