# SPDX-License-Identifier: MIT
"""NVIDIA DLSS Frame Generation under Wine: drives `worker_dlssg/dlssg_host.exe` (Merserk's
neuroframe_engine_frame_interpolation.dll bridge: CUDA + D3D12 + NVOF optical flow + NGX DLSSG) through three mmap'ed
files (in frame / out frames / control block), same design as the RTX VSR upscaler.

What made it run under Proton (see ../../README.md): patched vkd3d-proton (D3D12 shared HEAPS, dropped next to the host as
d3d12.dll/d3d12core.dll), the nvcuda shim's NT-handle -> fd translation for cuImportExternalMemory, a real Microsoft
d3dcompiler_47.dll, and a dxvk-nvapi nvofapi64.dll that advertises B8G8R8A8 as an NVOF input format.

`DlssgFrameGen.submit(frame)` takes the next real RGBA8 frame (even width/height) and returns the `count` frames generated
between the previous real frame and this one, uniformly spaced ([] for the first frame). `count` is fixed per session.
"""
import mmap
import os
import struct
import subprocess
import tempfile
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
import paths as nspaths  # noqa: E402
import userfiles  # noqa: E402

_ROOT = nspaths.ROOT
_WORKER_DIR = nspaths.WORKER_DLSSG

# Ctrl block (see dlssg_host.cpp): state 0, req 4, ack 8, ok 12, quit 16, total_ms 20 (f32), ngx_ms 24 (f32),
# generated 28, info 32, frame_flags 36, flow_ms 40 (f32)
_STATE, _REQ, _ACK, _OK, _QUIT, _TOTAL, _NGX, _GEN, _INFO, _FLAGS = 0, 4, 8, 12, 16, 20, 24, 28, 32, 36
_STARTUP_TIMEOUT_SEC = 90.0
_FRAME_TIMEOUT_SEC = 10.0


class DlssgError(RuntimeError):
    pass


def _winpath(p: str) -> str:
    return "Z:" + p.replace("/", "\\")


class DlssgFrameGen:
    # host variants: the FSR3 frame-generation host (nsfsr3.py) speaks the same file protocol
    EXE = "dlssg_host.exe"
    WORKER = _WORKER_DIR
    DLL_OVERRIDES = "dxgi,d3d11,d3d12,d3d12core,nvcuda,d3dcompiler_47,nvofapi64=n"
    DLL_PATH = nspaths.WINE_NVCUDA
    FEATURE = "dlssg"
    ENV_PREFIX = "DLSSG"
    MAX_COUNT = 4
    IN_SLOTS = 1   # input frames in the shared file (the NGX host pipelines two)
    RING = 4   # output regions in the shared file: generated frames are handed out as views into them (no copy)

    def __init__(self, width: int, height: int, count: int = 1):
        userfiles.require(self.FEATURE)
        if width % 2 or height % 2:
            raise DlssgError("DLSS-G needs even frame dimensions")
        self.w, self.h, self.count = width, height, max(1, min(self.MAX_COUNT, int(count)))
        wine = self._runner()
        self._dir = tempfile.mkdtemp(prefix="ns-dlssg-")
        paths = [os.path.join(self._dir, n) for n in ("in.bin", "out.bin", "ctl.bin")]
        frame_bytes = width * height * 4
        for p, size in zip(paths, (frame_bytes * self.IN_SLOTS, frame_bytes * self.count * self.RING, 192)):
            with open(p, "wb") as f:
                f.truncate(size)
        env = dict(os.environ)
        env["WINEPREFIX"] = nspaths.PREFIX
        env.setdefault("__NV_PRIME_RENDER_OFFLOAD", "1")
        env.setdefault("__GLX_VENDOR_LIBRARY_NAME", "nvidia")
        env.setdefault("VK_ICD_FILENAMES", "/usr/share/vulkan/icd.d/nvidia_icd.json")
        env["WINEDLLOVERRIDES"] = self.DLL_OVERRIDES
        if self.DLL_PATH:
            env["WINEDLLPATH"] = self.DLL_PATH
        env["WINEDEBUG"] = "-all"
        env[self.ENV_PREFIX + "_BGRA"] = "1"          # the host converts NV12 straight to cairo's B,G,R,A order
        env[self.ENV_PREFIX + "_RING"] = str(self.RING)
        self._proc = self._spawn(wine, width, height, paths, env)
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
                raise DlssgError("dlssg_host failed to start")
            time.sleep(0.02)
        if self._u32(_STATE) != 1:
            self.close()
            raise DlssgError("DLSS-G session creation failed (see README: shared-heap / NVOF prerequisites)")
        self._seq = 0
        self._first = True

    def _runner(self):
        return nspaths.wine_binary()

    def _spawn(self, wine, width, height, paths, env):
        return subprocess.Popen(
            [wine, self.EXE, str(width), str(height), str(self.count),
             _winpath(paths[0]), _winpath(paths[1]), _winpath(paths[2]), _winpath(self.WORKER)],
            cwd=self.WORKER, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=None if env.get(self.ENV_PREFIX + "_PROF") or env.get(self.ENV_PREFIX + "_LOG") else subprocess.DEVNULL)   # NGXG_PROF=1 / NGXG_LOG=1: show the host's messages

    def _wait_ack(self, seq):
        t0 = time.monotonic()
        while self._u32(_ACK) < seq:
            if self._proc.poll() is not None or time.monotonic() - t0 > _FRAME_TIMEOUT_SEC:
                raise DlssgError("dlssg_host stopped answering")
            time.sleep(0.0002)

    _COPY_THREADS = 4

    def _write_in(self, rgba, slot=0):
        """Frame -> shared input file. numpy releases the GIL while copying, so row bands go through a few threads (a 20 MB copy is memory-bound and
        a single thread reaches only a fraction of the bandwidth)."""
        n_px = self.h * self.w * 4
        dst = np.frombuffer(self._in, dtype=np.uint8, count=n_px, offset=slot * n_px).reshape(self.h, self.w, 4)
        if self.h * self.w < 500_000:
            dst[:] = rgba
            return
        pool = getattr(self, "_copy_pool", None)
        if pool is None:
            from concurrent.futures import ThreadPoolExecutor
            pool = self._copy_pool = ThreadPoolExecutor(self._COPY_THREADS - 1)
        n = self._COPY_THREADS
        cuts = [self.h * i // n for i in range(n + 1)]
        futs = [pool.submit(dst.__setitem__, slice(cuts[i], cuts[i + 1]), rgba[cuts[i]:cuts[i + 1]]) for i in range(n - 1)]
        dst[cuts[n - 1]:] = rgba[cuts[n - 1]:]
        for f in futs:
            f.result()

    def _before_request(self, rgba):
        """Hook for subclasses: runs after the frame is in the shared file, before the request is signalled."""

    def _u32(self, off):
        return struct.unpack_from("<I", self._ctl, off)[0]

    def _f32(self, off):
        return struct.unpack_from("<f", self._ctl, off)[0]

    def submit(self, rgba: np.ndarray) -> list:
        """Next real frame (RGBA8) -> the generated frames as BGRA8 (cairo ARGB32 order, opaque) VIEWS into the shared output ring: they stay
        valid for RING-1 further submits, which is far longer than the presenter holds them."""
        assert rgba.shape == (self.h, self.w, 4), rgba.shape
        self._write_in(rgba)
        self._before_request(rgba)
        struct.pack_into("<I", self._ctl, _FLAGS, 1 if self._first else 0)
        self._seq += 1
        struct.pack_into("<I", self._ctl, _REQ, self._seq)
        self._wait_ack(self._seq)
        self._first = False
        if not self._u32(_OK):
            raise DlssgError("DLSS-G frame failed")
        n = min(self._u32(_GEN), self.count)
        out = np.frombuffer(self._out, dtype=np.uint8).reshape(self.RING, self.count, self.h, self.w, 4)
        region = out[self._seq % self.RING]
        return [region[i] for i in range(n)]

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
            for n in os.listdir(d):
                try:
                    os.unlink(os.path.join(d, n))
                except OSError:
                    pass
            try:
                os.rmdir(d)
            except OSError:
                pass
            self._dir = None
