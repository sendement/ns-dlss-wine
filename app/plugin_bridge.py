# SPDX-License-Identifier: MIT
"""Python side of the nsproxy Hyprland plugin link (protocol: hyprplug/src/proto.hpp).

PluginLink connects to the plugin's socket, maps the shared memory and offers:
  * wait_frame() / capture_raw()  - the newest exported window frame (RGBA8, top-down), as a ToplevelCapture-like source;
  * publish(bgra)                 - hand a processed frame (BGRA8 premultiplied = cairo ARGB32) back to be drawn over the window;
  * set_override(bool)            - whether the plugin should show the result.
If the pipeline stops publishing for 300 ms the plugin shows the real window again by itself.
"""
import array
import ctypes
import mmap
import os
import socket
import threading
import time

import numpy as np

SLOTS = 3


def _load_nsmem():
    """Cache-aware copy helpers (app/nsmem/nsmem.c) - compiled on first use. Without them the zero-copy slots would tear, so the plugin is told not to use them."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nsmem")
    so, src = os.path.join(here, "libnsmem.so"), os.path.join(here, "nsmem.c")
    if os.environ.get("NS_NSMEM") == "0":          # test switch: behave as if the helper could not be built
        return None
    try:
        if not os.path.exists(so) or os.path.getmtime(so) < os.path.getmtime(src):
            import subprocess
            subprocess.run(["cc", "-O2", "-shared", "-fPIC", "-o", so, src], check=True, capture_output=True)
        lib = ctypes.CDLL(so)
        for fn in (lib.nsmem_copy_rows_evict, lib.nsmem_nt_copy_rows):
            fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t]
            fn.restype = None
        return lib
    except Exception as exc:                      # no compiler / no helper: fall back to plain copies and keep the plugin on its copying paths
        print(f"[plugin_bridge] cache-aware copy helper unavailable ({exc}); zero-copy stays off", flush=True)
        return None


_NSMEM = _load_nsmem()
CLIENT_FLAG_COHERENT_COPIES = 1   # this client uses nsmem helpers on the slots: the plugin may use its zero-copy (dma-buf) paths


def pitch_for(w: int) -> int:
    """Row pitch of a slot holding w-pixel rows (must match hyprplug/src/proto.hpp pitchFor)."""
    return (w * 4 + 255) & ~255


class _Header(ctypes.Structure):
    _fields_ = [("magic", ctypes.c_uint32), ("version", ctypes.c_uint32), ("cap_w", ctypes.c_uint32), ("cap_h", ctypes.c_uint32),
                ("slot_bytes", ctypes.c_uint64), ("exp_off", ctypes.c_uint64), ("res_off", ctypes.c_uint64),
                ("exp_seq", ctypes.c_uint64), ("exp_w", ctypes.c_uint32 * SLOTS), ("exp_h", ctypes.c_uint32 * SLOTS),
                ("res_seq", ctypes.c_uint64), ("res_w", ctypes.c_uint32 * SLOTS), ("res_h", ctypes.c_uint32 * SLOTS),
                ("res_ns", ctypes.c_uint64), ("override_on", ctypes.c_uint32), ("pad", ctypes.c_uint32),
                ("want_w", ctypes.c_uint32), ("want_h", ctypes.c_uint32), ("client_flags", ctypes.c_uint32), ("pad2", ctypes.c_uint32)]


class PluginLinkError(RuntimeError):
    pass


class PluginLink:
    MAGIC = 0x5850534E

    def __init__(self, path: str = ""):
        path = path or os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "nsproxy.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(path)
        except OSError as exc:
            raise PluginLinkError(f"cannot connect to {path}: {exc} (plugin loaded and `hyprctl nsproxy attach ...` done?)")
        fds = array.array("i")
        msg, anc, _flags, _addr = self.sock.recvmsg(256, socket.CMSG_LEN(fds.itemsize))
        for level, typ, data in anc:
            if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                fds.frombytes(data[:fds.itemsize])
        if not msg.startswith(b"NSPX1") or not fds:
            raise PluginLinkError(f"bad handshake: {msg!r}")
        total = int(msg.split()[1])
        self._fd = fds[0]
        self._mm = mmap.mmap(self._fd, total)
        self.h = _Header.from_buffer(self._mm)
        if self.h.magic != self.MAGIC or self.h.version != 2:
            raise PluginLinkError("plugin protocol mismatch")
        self._buf = np.frombuffer(self._mm, dtype=np.uint8)
        self._base = self._buf.ctypes.data
        self.h.client_flags = CLIENT_FLAG_COHERENT_COPIES if _NSMEM is not None else 0
        self._last_seq = 0
        self.sock.settimeout(0.5)
        self.last_wait_ms = 0.0
        # Heartbeat: the plugin hides the override when `res_ns` is older than 300 ms (dead/hung pipeline). A STATIC window produces no new
        # frames and so no new results, which must not look like a hang - keep the stamp fresh unless a frame we received got no answer.
        self._t_export = self._t_publish = 0.0
        self._alive = True
        self._beat = threading.Thread(target=self._heartbeat, daemon=True)
        self._beat.start()

    def _heartbeat(self):
        while self._alive:
            h = self.h
            if h is None:
                return
            now = time.monotonic()
            stalled = self._t_export > self._t_publish and now - self._t_export > 0.4
            if not stalled and h.res_seq:
                h.res_ns = time.monotonic_ns()
            time.sleep(0.05)

    # --- window frames from the compositor ---
    def wait_frame(self, timeout: float = 1.0):
        """Block until a frame newer than the last returned one exists; returns (rgba view HxWx4, seq)."""
        deadline = time.monotonic() + timeout
        t0 = time.monotonic()
        while True:
            seq = self.h.exp_seq
            if seq != self._last_seq:
                break
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("no frame from the plugin (window not repainting?)")
            self.sock.settimeout(min(left, 0.5))
            try:
                if self.sock.recv(4096) == b"":
                    raise PluginLinkError("plugin closed the connection")
            except socket.timeout:
                pass
            except BlockingIOError:
                pass
        self.last_wait_ms = (time.monotonic() - t0) * 1000
        self._last_seq = seq
        self._t_export = time.monotonic()
        slot = seq % SLOTS
        w, h = self.h.exp_w[slot], self.h.exp_h[slot]
        pitch = pitch_for(w)
        off = self.h.exp_off + slot * self.h.slot_bytes
        if _NSMEM is not None:   # private contiguous copy; the slot's cache lines are dropped afterwards (the GPU rewrites it later without snooping them)
            out = np.empty((h, w, 4), np.uint8)
            _NSMEM.nsmem_copy_rows_evict(out.ctypes.data, w * 4, self._base + off, pitch, w * 4, h)
            return out, seq
        return self._buf[off:off + pitch * h].reshape(h, pitch)[:, :w * 4].reshape(h, w, 4), seq

    def capture_raw(self):
        """ToplevelCapture-compatible: (array view, is_bgr). The view is only valid until the plugin reuses the slot (2 frames)."""
        arr, _ = self.wait_frame()
        return arr, False

    # --- results back to the compositor ---
    def publish(self, bgra: np.ndarray):
        h, w = bgra.shape[:2]
        if w > self.h.cap_w or h > self.h.cap_h:
            raise ValueError("result larger than the shared capacity")
        seq = self.h.res_seq + 1
        slot = seq % SLOTS
        off = self.h.res_off + slot * self.h.slot_bytes
        pitch = pitch_for(w)
        if _NSMEM is not None:   # non-temporal stores straight to memory (the GPU reads the slot without snooping the CPU caches)
            src = np.ascontiguousarray(bgra)
            _NSMEM.nsmem_nt_copy_rows(self._base + off, pitch, src.ctypes.data, w * 4, w * 4, h)
        else:
            self._buf[off:off + pitch * h].reshape(h, pitch)[:, :w * 4].reshape(h, w, 4)[:] = bgra
        self.h.res_w[slot], self.h.res_h[slot] = w, h
        self.h.res_ns = time.monotonic_ns()
        self.h.res_seq = seq        # published last
        self._t_publish = time.monotonic()

    def set_export_size(self, w: int, h: int):
        """Ask the plugin to export at (w, h) - it downscales on the GPU (0, 0 = the window's native size)."""
        if (self.h.want_w, self.h.want_h) != (w, h):
            self.h.want_w, self.h.want_h = w, h

    def set_override(self, on: bool):
        self.h.override_on = 1 if on else 0

    def close(self):
        self._alive = False
        self._beat.join(0.3)
        try:
            self.set_override(False)
        except Exception:
            pass
        self._buf = None
        self.h = None
        try:
            self._mm.close()
        except BufferError:
            pass
        self.sock.close()


class SharedCapture:
    """ToplevelCapture-compatible facade over one PluginLink (capture threads may 'close' it without ending the link)."""
    def __init__(self, link: PluginLink):
        self._link = link
        self.last_wait_ms = 0.0

    def set_export_size(self, w: int, h: int):
        self._link.set_export_size(w, h)

    def capture_raw(self):
        arr, is_bgr = self._link.capture_raw()
        self.last_wait_ms = self._link.last_wait_ms
        # the slot is rewritten by the plugin two exports later: hand the pipeline its own contiguous copy
        return (arr if _NSMEM is not None else np.ascontiguousarray(arr).copy()), is_bgr

    def close(self):
        pass


class PluginOverlay:
    """Stands in for ScreenOverlay when the result is drawn by the compositor plugin: update_frame() publishes into shared memory.
    Runs on the caller's thread (no GTK involved); compare mode is composed here (raw on the left of the divider)."""
    def __init__(self, link: PluginLink, ww: int, wh: int):
        self._link = link
        self.ww, self.wh = ww, wh
        self._local_x = self._local_y = 0
        self._compare_mode = False
        self._divider_frac = 0.5
        self._destroy_cb = None

    def update_frame(self, processed, raw=None):
        h, w = processed.h, processed.w
        out = np.frombuffer(processed.buf, dtype=np.uint8).reshape(h, w, 4)
        if self._compare_mode and raw is not None and (raw.h, raw.w) == (h, w):
            out = out.copy()
            split = int(w * self._divider_frac)
            out[:, :split] = np.frombuffer(raw.buf, dtype=np.uint8).reshape(h, w, 4)[:, :split]
            out[:, max(0, split - 1):split + 1, :3] = 255      # divider line
        self._link.publish(out)
        self._link.set_override(True)
        return False

    def set_compare_mode(self, enabled: bool):
        self._compare_mode = enabled

    def reposition(self, *a, **k):
        pass

    def show_all(self):
        self._link.set_override(True)

    def hide(self):
        self._link.set_override(False)

    def connect(self, name, cb):
        pass
