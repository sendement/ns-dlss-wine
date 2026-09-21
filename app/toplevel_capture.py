# SPDX-License-Identifier: MIT
"""Per-window capture via Hyprland's own hyprland-toplevel-export-v1
protocol, instead of a screen/output region (wlr-screencopy,
wl_capture.ScreencopyCapture).

Why this one specifically for the live settings overlay: both of the
other capture paths read the COMPOSITOR's final, fully-composited output -
whatever is visually on screen, stacking-order and all. Track 1's overlay
(screen_overlay.py) is deliberately positioned exactly on top of the
window it's processing, which means with either of those capture paths,
each new capture would pick up the PREVIOUS frame's own DLSS+upscale
output instead of the real window - a feedback loop (each frame gets
re-denoised/re-sharpened on top of the last, compounding into visible
ringing/noise; not a rendering bug, an architectural one). This protocol
captures a specific toplevel's own CLIENT surface directly, bypassing
compositing (and therefore bypassing whatever is drawn on top of it,
including our own overlay) - exactly what "grimblast copy active"-style
tools use to screenshot one window without capturing overlays.

`capture_toplevel`'s v1 request takes a plain `uint` handle ("the address
of the window as seen in `hyprctl clients`") - but that address is a
truncated-to-32-bit pointer, and on this system (64-bit ASLR addresses)
`hyprctl clients`' address values overflow uint32 outright. So this uses
the v2 request instead, `capture_toplevel_with_wlr_toplevel_handle`, which
takes a real `zwlr_foreign_toplevel_handle_v1` object - obtained by
binding `zwlr_foreign_toplevel_manager_v1` (wlr-foreign-toplevel-
management-unstable-v1) and matching its `app_id`/`title` events against
the target window, the same way wl_capture.py's own window lookups work.
"""
import mmap
import os
import sys
import time

import numpy as np

from pywayland.client import Display

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wl_protocols.wayland import WlShm  # noqa: E402
from wl_protocols.hyprland_toplevel_export_v1 import HyprlandToplevelExportManagerV1  # noqa: E402
from wl_protocols.wlr_foreign_toplevel_management_unstable_v1 import (  # noqa: E402
    ZwlrForeignToplevelManagerV1,
)

WL_SHM_FORMAT_ARGB8888 = 0
WL_SHM_FORMAT_XRGB8888 = 1


class ToplevelCapture:
    def __init__(self, window_class: str):
        """|window_class| is matched case-insensitively as a substring
        against each open toplevel's app_id - same matching convention as
        get_window_geometry() elsewhere in this project. The first match
        wins if more than one window shares the class."""
        self.display = Display()
        self.display.connect()
        registry = self.display.get_registry()
        self._shm = None
        self._manager = None
        self._toplevel_manager = None
        registry.dispatcher["global"] = self._on_global
        self.display.roundtrip()
        if self._shm is None:
            raise RuntimeError("compositor has no wl_shm")
        if self._manager is None:
            raise RuntimeError("compositor has no hyprland_toplevel_export_manager_v1 "
                                "(not Hyprland?)")
        if self._toplevel_manager is None:
            raise RuntimeError("compositor has no zwlr_foreign_toplevel_manager_v1")

        self.wlr_handle = self._find_toplevel_handle(window_class)
        if self.wlr_handle is None:
            raise RuntimeError(f"no open toplevel matching class {window_class!r}")

        self._pool = None
        self._pool_fd = None
        self._mm = None
        self._buffer = None
        self._buf_key = None  # (w, h, stride, format)
        self.last_wait_ms = 0.0

    def _on_global(self, registry, name, interface, version):
        if interface == "wl_shm":
            self._shm = registry.bind(name, WlShm, 1)
        elif interface == "hyprland_toplevel_export_manager_v1":
            self._manager = registry.bind(name, HyprlandToplevelExportManagerV1, min(version, 2))
        elif interface == "zwlr_foreign_toplevel_manager_v1":
            self._toplevel_manager = registry.bind(name, ZwlrForeignToplevelManagerV1, min(version, 3))

    def _find_toplevel_handle(self, window_class: str):
        want = window_class.lower()
        candidates = []  # list of {"handle": obj, "app_id": str|None}

        def on_toplevel(manager, handle):
            info = {"handle": handle, "app_id": None}
            candidates.append(info)

            def on_app_id(h, app_id, _info=info):
                _info["app_id"] = app_id

            handle.dispatcher["app_id"] = on_app_id
            handle.dispatcher["title"] = lambda h, title: None
            handle.dispatcher["output_enter"] = lambda h, output: None
            handle.dispatcher["output_leave"] = lambda h, output: None
            handle.dispatcher["state"] = lambda h, state: None
            handle.dispatcher["done"] = lambda h: None
            handle.dispatcher["closed"] = lambda h: None

        self._toplevel_manager.dispatcher["toplevel"] = on_toplevel
        # Two roundtrips: one to receive all "toplevel" events (which create
        # the handle objects), a second to let each handle's own app_id/done
        # events (queued as a result of the first roundtrip's binds) arrive.
        self.display.roundtrip()
        self.display.roundtrip()

        for info in candidates:
            if info["app_id"] and want in info["app_id"].lower():
                return info["handle"]
        return None

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
        fd = os.memfd_create("ns-toplevel-export", 0)
        os.ftruncate(fd, size)
        mm = mmap.mmap(fd, size)
        pool = self._shm.create_pool(fd, size)
        buf = pool.create_buffer(0, w, h, stride, fmt)
        self._pool_fd, self._mm, self._pool, self._buffer = fd, mm, pool, buf
        self._buf_key = key

    def capture_raw(self, timeout: float = 2.0):
        """Capture one frame of the whole window, return (array,
        is_bgr_order) - zero-copy view onto the reused mmap buffer, valid
        only until the next capture call. No region/crop support - this
        protocol always captures the entire toplevel."""
        state = {"phase": None, "format": None, "width": None, "height": None, "stride": None}

        frame = self._manager.capture_toplevel_with_wlr_toplevel_handle(0, self.wlr_handle)

        def on_buffer(frame_proxy, fmt, bw, bh, stride):
            state["format"], state["width"], state["height"], state["stride"] = fmt, bw, bh, stride

        def on_buffer_done(frame_proxy):
            self._ensure_buffer(state["width"], state["height"], state["stride"], state["format"])
            # ignore_damage=1: always copy immediately rather than waiting
            # for a damage event - a static/unchanged frame would otherwise
            # never fire "ready" and we'd hit the timeout for no reason.
            frame_proxy.copy(self._buffer, 1)

        def on_ready(frame_proxy, tv_sec_hi, tv_sec_lo, tv_nsec):
            state["phase"] = "ready"

        def on_failed(frame_proxy):
            state["phase"] = "failed"

        frame.dispatcher["buffer"] = on_buffer
        frame.dispatcher["buffer_done"] = on_buffer_done
        frame.dispatcher["ready"] = on_ready
        frame.dispatcher["failed"] = on_failed

        t_req = time.monotonic()
        deadline = t_req + timeout
        while state["phase"] is None:
            self.display.dispatch(block=True)
            if time.monotonic() > deadline:
                frame.destroy()
                raise TimeoutError("toplevel export frame timed out")
        self.last_wait_ms = (time.monotonic() - t_req) * 1000
        frame.destroy()
        if state["phase"] == "failed":
            raise RuntimeError("toplevel export capture failed")

        bw, bh, stride, fmt = state["width"], state["height"], state["stride"], state["format"]
        arr = np.frombuffer(self._mm, dtype=np.uint8, count=stride * bh)
        arr = arr.reshape(bh, stride)[:, : bw * 4].reshape(bh, bw, 4)
        is_bgr = fmt in (WL_SHM_FORMAT_ARGB8888, WL_SHM_FORMAT_XRGB8888)
        return arr, is_bgr

    def close(self):
        if self._pool is not None:
            self._pool.destroy()
        if self._mm is not None:
            self._mm.close()
        if self._pool_fd is not None:
            os.close(self._pool_fd)
        self.display.disconnect()


if __name__ == "__main__":
    from PIL import Image

    win_class = sys.argv[1] if len(sys.argv) > 1 else "vivaldi-stable"
    print(f"capturing window matching class {win_class!r}")

    cap = ToplevelCapture(win_class)
    arr, is_bgr = cap.capture_raw()
    print("first frame:", arr.shape, "is_bgr=", is_bgr)

    N = 30
    t0 = time.monotonic()
    for _ in range(N):
        arr, is_bgr = cap.capture_raw()
    dt = time.monotonic() - t0
    print(f"{N} frames in {dt:.3f}s -> {N/dt:.1f} fps ({dt/N*1000:.2f} ms/frame)")

    rgba = arr[:, :, [2, 1, 0, 3]].copy() if is_bgr else arr.copy()
    rgba[:, :, 3] = 255
    Image.fromarray(rgba, "RGBA").convert("RGB").save("toplevel_capture_test.png")
    print("saved toplevel_capture_test.png")
    del arr, rgba
    cap.close()
