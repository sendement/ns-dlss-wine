# SPDX-License-Identifier: MIT
"""NVIDIA DLSS Frame Generation through the PUBLIC NGX API (hosts/ngxdlssg_host.cpp, D3D12 on vkd3d-proton, NVOF on dxvk-nvapi).

Same interface and file protocol as nsdlssg.DlssgFrameGen, but no third-party bridge DLL: the host talks to NVIDIA's own nvngx_dlssg.dll. Like the FSR 3 host it
feeds a constant depth plane and either zero motion vectors or the global-motion hint from nsfsr3._GlobalMotion, so the interpolation rests on NVIDIA's own
optical flow of the colour frames.

The host is a two-slot pipeline: `begin(rgba)` queues a frame (blocking only while both slots are busy) and `end(token)` collects its generated frames, so the
caller can hand the next frame over while the previous one is still on the GPU. `submit()` is begin+end.
"""
import os
import struct

import paths as nspaths
from .nsdlssg import DlssgFrameGen, DlssgError, _REQ
from .nsfsr3 import _GlobalMotion

NgxDlssgError = DlssgError

# Per-slot block of the control file (see ngxdlssg_host.cpp: Ext): flags[2] u32, mvx[2] f32, mvy[2] f32, gen[4] u32, info[4] u32, ok[4] u32
_EXT = 64
_E_FLAGS, _E_MVX, _E_MVY, _E_GEN, _E_INFO, _E_OK = _EXT, _EXT + 8, _EXT + 16, _EXT + 24, _EXT + 40, _EXT + 56


class NgxDlssgFrameGen(DlssgFrameGen):
    EXE = "ngxdlssg_host.exe"
    WORKER = nspaths.WORKER_DLSSG
    FEATURE = "dlssg"
    ENV_PREFIX = "NGXG"
    MAX_COUNT = 3
    IN_SLOTS = 2
    pipelined = True

    def begin(self, rgba):
        """Queue the next real frame; returns a token for end(). Blocks only while the host still has two frames in flight."""
        assert rgba.shape == (self.h, self.w, 4), rgba.shape
        seq = self._seq + 1
        if seq > 2:
            self._wait_ack(seq - 2)
        slot = seq % 2
        self._write_in(rgba, slot)
        if not hasattr(self, "_motion"):
            self._motion = _GlobalMotion()
            self._motion_on = os.environ.get("NS_DLSSG_MOTION", "1") != "0"   # global-motion hint (validated: 40 px pans go from MAD 22 to 0.04)
        dx, dy = self._motion.update(rgba) if self._motion_on else (0.0, 0.0)
        self.last_motion = (dx, dy)
        struct.pack_into("<I", self._ctl, _E_FLAGS + 4 * slot, 1 if self._first else 0)
        struct.pack_into("<f", self._ctl, _E_MVX + 4 * slot, dx)
        struct.pack_into("<f", self._ctl, _E_MVY + 4 * slot, dy)
        self._first = False
        self._seq = seq
        struct.pack_into("<I", self._ctl, _REQ, seq)
        return seq

    def end(self, seq):
        """Wait for the frame `seq` and return its generated frames (BGRA views into the shared ring, valid for RING-2 further frames)."""
        import numpy as np
        self._wait_ack(seq)
        k = seq % 4
        if not struct.unpack_from("<I", self._ctl, _E_OK + 4 * k)[0]:
            raise DlssgError("DLSS-G frame failed")
        n = min(struct.unpack_from("<I", self._ctl, _E_GEN + 4 * k)[0], self.count)
        out = np.frombuffer(self._out, dtype=np.uint8).reshape(self.RING, self.count, self.h, self.w, 4)
        region = out[seq % self.RING]
        return [region[i] for i in range(n)]

    def submit(self, rgba):
        return self.end(self.begin(rgba))


class NgxVkDlssgFrameGen(NgxDlssgFrameGen):
    """The same host protocol, but the host is a NATIVE Linux binary (hosts/ngxdlssg_vk_host.cpp: NGX on Vulkan with NVIDIA's libnvidia-ngx-dlssg.so): no Wine."""
    EXE = "ngxdlssg_vk_host"
    WORKER = nspaths.WORKER_DLSSG_VK
    FEATURE = "dlssg-vk"

    def _runner(self):
        return None

    def _spawn(self, wine, width, height, paths, env):
        import subprocess
        return subprocess.Popen(
            [os.path.join(self.WORKER, self.EXE), str(width), str(height), str(self.count), *paths, self.WORKER],
            cwd=self.WORKER, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=None if env.get(self.ENV_PREFIX + "_PROF") or env.get(self.ENV_PREFIX + "_LOG") else subprocess.DEVNULL)


def native_ready() -> bool:
    import userfiles
    return not userfiles.ensure("dlssg-vk")
