# SPDX-License-Identifier: MIT
"""RTX Video Super Resolution on NVIDIA's `nvidia-vfx` package, natively (no Wine): hosts/vsr_native_host.py runs in runtime/venv-vsr (cupy + nvidia-vfx,
installed with `python3 app/userfiles.py install vsr-native`) and speaks the same three-file protocol as the Wine host, so only process start-up differs
from RtxVsrUpscaler."""
import mmap
import os
import subprocess
import tempfile
import time

import paths as nspaths
import userfiles

from .rtx_vsr_upscaler import (RtxVsrUpscaler, VsrError, _STARTUP_TIMEOUT_SEC, _STATE)


class RtxVsrNativeUpscaler(RtxVsrUpscaler):
    name = "RTX VSR"

    def _start(self, iw, ih, ow, oh, quality):
        userfiles.require_vsr_native()
        self._dir = tempfile.mkdtemp(prefix="ns-vsr-")
        paths = [os.path.join(self._dir, n) for n in ("in.bin", "out.bin", "ctl.bin")]
        for p, size in zip(paths, (iw * ih * 4, ow * oh * 4, 64)):
            with open(p, "wb") as f:
                f.truncate(size)
        env = dict(os.environ)
        env.setdefault("__NV_PRIME_RENDER_OFFLOAD", "1")
        env.setdefault("__GLX_VENDOR_LIBRARY_NAME", "nvidia")
        self._proc = subprocess.Popen(
            [nspaths.VENV_VSR_PYTHON, os.path.join(nspaths.ROOT, "hosts", "vsr_native_host.py"), str(iw), str(ih), str(ow), str(oh), str(quality), *paths],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=None if os.environ.get("NS_VSR_LOG") else subprocess.DEVNULL)
        for p in paths:
            f = open(p, "r+b")
            self._files.append(f)
            self._maps.append(mmap.mmap(f.fileno(), 0))
        self._in, self._out, self._ctl = self._maps
        t0 = time.monotonic()
        while self._u32(_STATE) == 0:
            if self._proc.poll() is not None or time.monotonic() - t0 > _STARTUP_TIMEOUT_SEC:
                self._stop()
                raise VsrError("native VSR host failed to start (run with NS_VSR_LOG=1 to see why)")
            time.sleep(0.02)
        if self._u32(_STATE) != 1:
            self._stop()
            raise VsrError("native VSR session creation failed")
        self._src_shape = (ih, iw, 4)
        self._out_shape = (oh, ow, 4)
        self._seq = 0
