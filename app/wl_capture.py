# SPDX-License-Identifier: MIT
"""A persistent wlr-screencopy client: one Wayland connection, one reused
wl_shm buffer, repeated capture_output_region calls - replaces spawning
`grim` fresh every frame.
"""
import mmap
import os
import sys
import time

import numpy as np

from pywayland.client import Display

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wl_protocols.wayland import WlOutput, WlShm  # noqa: E402
from wl_protocols.wlr_screencopy_unstable_v1 import ZwlrScreencopyManagerV1  # noqa: E402

WL_SHM_FORMAT_ARGB8888 = 0
WL_SHM_FORMAT_XRGB8888 = 1


class ScreencopyCapture:
    def __init__(self, output_x=None):
        """output_x: pick the wl_output whose layout x equals it (e.g. the headless proxy output); default = first output."""
        self.display = Display()
        self.display.connect()
        registry = self.display.get_registry()
        self._shm = None
        self._manager = None
        self._output = None
        self._want_x = output_x
        self._outs = []
        registry.dispatcher["global"] = self._on_global
        self.display.roundtrip()
        self.display.roundtrip()   # wl_output.geometry events
        if output_x is not None:
            match = [o for o, geo in self._outs if geo.get("x") == output_x]
            if not match:
                raise RuntimeError(f"no wl_output at x={output_x}")
            self._output = match[0]
        if self._shm is None:
            raise RuntimeError("compositor has no wl_shm")
        if self._manager is None:
            raise RuntimeError("compositor has no zwlr_screencopy_manager_v1 "
                                "(not a wlroots compositor?)")
        if self._output is None:
            raise RuntimeError("compositor advertised no wl_output")

        self._pool = None
        self._pool_fd = None
        self._mm = None
        self._buffer = None
        self._buf_key = None  # (w, h, stride, format)

    def _on_global(self, registry, name, interface, version):
        if interface == "wl_shm":
            self._shm = registry.bind(name, WlShm, 1)
        elif interface == "zwlr_screencopy_manager_v1":
            self._manager = registry.bind(name, ZwlrScreencopyManagerV1, min(version, 3))
        elif interface == "wl_output":
            o = registry.bind(name, WlOutput, min(version, 2))
            geo = {}
            o.dispatcher["geometry"] = lambda _o, x, y, *a, geo=geo: geo.update(x=x, y=y)
            self._outs.append((o, geo))
            if self._output is None and self._want_x is None:
                self._output = o

    def _ensure_buffer(self, w, h, stride, fmt):
        key = (w, h, stride, fmt)
        if self._buf_key == key:
            return
        if self._pool is not None:
            self._pool.destroy()
        if self._mm is not None:
            self._mm.close()
        if self._pool_fd is not None:
            os.close(self._pool_fd)
        size = stride * h
        fd = os.memfd_create("ns-screencopy", 0)
        os.ftruncate(fd, size)
        mm = mmap.mmap(fd, size)
        pool = self._shm.create_pool(fd, size)
        buf = pool.create_buffer(0, w, h, stride, fmt)
        self._pool_fd, self._mm, self._pool, self._buffer = fd, mm, pool, buf
        self._buf_key = key

    def capture_region_raw(self, x: int, y: int, w: int, h: int, timeout: float = 2.0):
        """Capture one frame, return (array, is_bgr_order) with NO channel swap.

        The array is a zero-copy view straight onto the reused mmap buffer -
        it is only valid until the next capture_region/capture_region_raw
        call, and the caller must be done with it (e.g. resized it into a
        fresh array) before calling again. Channel order is whatever the
        compositor handed back (B,G,R,A/X for the two mandatory wl_shm
        formats) - swap it yourself once you no longer need every pixel,
        e.g. after downscaling, which is a lot fewer bytes to touch.
        """
        state = {"phase": None, "format": None, "width": None, "height": None, "stride": None}

        frame = self._manager.capture_output_region(0, self._output, x, y, w, h)

        def on_buffer(frame_proxy, fmt, bw, bh, stride):
            state["format"], state["width"], state["height"], state["stride"] = fmt, bw, bh, stride
            self._ensure_buffer(bw, bh, stride, fmt)
            frame_proxy.copy(self._buffer)

        def on_ready(frame_proxy, tv_sec_hi, tv_sec_lo, tv_nsec):
            state["phase"] = "ready"

        def on_failed(frame_proxy):
            state["phase"] = "failed"

        frame.dispatcher["buffer"] = on_buffer
        frame.dispatcher["ready"] = on_ready
        frame.dispatcher["failed"] = on_failed

        t_req = time.monotonic()
        deadline = t_req + timeout
        while state["phase"] is None:
            self.display.dispatch(block=True)
            if time.monotonic() > deadline:
                frame.destroy()
                raise TimeoutError("screencopy frame timed out")
        t_ready = time.monotonic()
        frame.destroy()
        if state["phase"] == "failed":
            raise RuntimeError("screencopy capture failed")
        self.last_wait_ms = (t_ready - t_req) * 1000

        bw, bh, stride, fmt = state["width"], state["height"], state["stride"], state["format"]
        # zero-copy view straight onto the mmap - no separate .read() copy
        arr = np.frombuffer(self._mm, dtype=np.uint8, count=stride * bh)
        arr = arr.reshape(bh, stride)[:, : bw * 4].reshape(bh, bw, 4)
        is_bgr = fmt in (WL_SHM_FORMAT_ARGB8888, WL_SHM_FORMAT_XRGB8888)
        return arr, is_bgr

    def capture_region(self, x: int, y: int, w: int, h: int, timeout: float = 2.0) -> np.ndarray:
        """Capture one frame of the region, return it as an (h, w, 4) RGBA array."""
        arr, is_bgr = self.capture_region_raw(x, y, w, h, timeout)
        if is_bgr:
            # native-endian 32bpp word in memory -> bytes are B,G,R,A/X
            rgba = arr[:, :, [2, 1, 0, 3]].copy()
            rgba[:, :, 3] = 255
        else:
            rgba = arr.copy()
        return rgba

    def close(self):
        if self._pool is not None:
            self._pool.destroy()
        if self._mm is not None:
            self._mm.close()
        if self._pool_fd is not None:
            os.close(self._pool_fd)
        self.display.disconnect()


if __name__ == "__main__":
    # standalone smoke test: capture a few frames of a given region and
    # save the last one, for a visual/byte comparison against grim's output.
    import json
    import subprocess
    from PIL import Image

    win_class = sys.argv[1] if len(sys.argv) > 1 else "vivaldi-stable"
    out = subprocess.check_output(["hyprctl", "clients", "-j"])
    x = y = w = h = None
    for c in json.loads(out):
        if win_class.lower() in c["class"].lower():
            x, y = c["at"]
            w, h = c["size"]
            break
    if x is None:
        raise SystemExit(f"no window with class containing {win_class!r}")
    print(f"capturing {x},{y} {w}x{h}")

    cap = ScreencopyCapture()
    t0 = time.monotonic()
    N = 30
    wait_total = 0.0
    for i in range(N):
        frame = cap.capture_region(x, y, w, h)
        wait_total += cap.last_wait_ms
    dt = time.monotonic() - t0
    print(f"{N} frames in {dt:.3f}s -> {N/dt:.1f} fps, per-frame {dt/N*1000:.1f}ms "
          f"(compositor wait {wait_total/N:.1f}ms/f, python overhead {dt*1000/N - wait_total/N:.1f}ms/f)")
    Image.fromarray(frame, "RGBA").convert("RGB").save("wl_capture_test.png")
    print("saved wl_capture_test.png")
    cap.close()
