# SPDX-License-Identifier: MIT
"""NS_PROXY=1 - the "proxy desktop" mode.

The source application is moved to a virtual (headless) Hyprland output and made fullscreen there; the filtered picture is shown
in an ordinary window (ProxyWindow) on the real desktop, and the user's mouse is forwarded from that window to the real app.

Why: the fast capture (wlr-screencopy of an OUTPUT, ~5-8 ms, no dependence on the source's own frame pacing) only sees what is
scanned out, so the result must not be drawn over the source - and on a headless output the source is fully alive (frame
callbacks, input) yet invisible. Keyboard needs no forwarding: the proxy window is `no_focus`, so keyboard focus stays on the
source once it was clicked. The pointer is forwarded through a uinput absolute device: warp onto the source, press/move/scroll,
warp back (sub-millisecond, the pointer visibly does not leave the proxy).

Crash recovery: state is kept in ~/.cache/ns-proxy-state.json - `python3 proxy_desktop.py restore` puts the app back.
"""
import atexit
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

GLib.set_prgname("ns-dlss-proxy")   # Wayland app_id: the window rule below (no_focus) and the user's own rules match on it

OUT_NAME = "NSPROXY"
WS_NAME = "nsproxy"
STATE_FILE = os.path.expanduser("~/.cache/ns-proxy-state.json")


def _eval(lua: str) -> str:
    r = subprocess.run(["hyprctl", "eval", lua], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def _hl_json(what: str):
    return json.loads(subprocess.check_output(["hyprctl", what, "-j"]))


def _hypr_socket_request(cmd: str) -> str:
    """Raw request on Hyprland's command socket - ~0.1 ms, vs ~5 ms for spawning hyprctl."""
    path = os.path.join(os.environ["XDG_RUNTIME_DIR"], "hypr", os.environ["HYPRLAND_INSTANCE_SIGNATURE"], ".socket.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(path)
        s.sendall(cmd.encode())
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    return b"".join(chunks).decode()


def _monitor_lua(m: dict) -> str:
    return ('hl.monitor({ output = "%s", mode = "%dx%d@%.3f", position = "%dx%d", scale = %s })'
            % (m["name"], m["width"], m["height"], m["refreshRate"], m["x"], m["y"], m["scale"]))


class ProxyDesktop:
    def __init__(self, win_class: str):
        self.win_class = win_class
        self.addr = None
        self.state = None
        self.out_x = self.out_y = self.out_w = self.out_h = 0
        self.engaged = False
        atexit.register(self.release)

    def _client(self):
        for c in _hl_json("clients"):
            if self.win_class.lower() in c["class"].lower() and c.get("mapped"):
                return c
        raise RuntimeError(f"no window with class containing {self.win_class!r}")

    def engage(self, size=None):
        """Move the source window to the headless output. Returns (x, y, w, h) of that output in the Hyprland layout."""
        if self.engaged:
            return self.out_x, self.out_y, self.out_w, self.out_h
        c = self._client()
        self.addr = c["address"]
        ws = c["workspace"]
        mons = _hl_json("monitors")
        phys = [m for m in mons if m["name"] != OUT_NAME]
        orig_mon = next((m["name"] for m in mons if m["id"] == c["monitor"]), phys[0]["name"])
        self.state = {"address": self.addr, "workspace": str(ws["id"]) if ws["id"] > 0 else ws["name"],
                      "monitor": orig_mon, "fullscreen": c["fullscreen"], "floating": c["floating"]}
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        json.dump(self.state, open(STATE_FILE, "w"))

        w, h = size or (c["size"][0], c["size"][1])
        w, h = max(640, w // 2 * 2), max(480, h // 2 * 2)
        if not any(m["name"] == OUT_NAME for m in mons):
            subprocess.run(["hyprctl", "output", "create", "headless", OUT_NAME], check=True, capture_output=True)
        x = max(m["x"] + int(m["width"] / m["scale"]) for m in phys)
        _eval('hl.monitor({ output = "%s", mode = "%dx%d@120", position = "%dx0", scale = 1 })' % (OUT_NAME, w, h, x))
        time.sleep(0.3)
        # (re)applying a monitor rule can make the layout auto-shift the physical outputs - put them back where they were
        for m in phys:
            now = next((q for q in _hl_json("monitors") if q["name"] == m["name"]), None)
            if now and (now["x"], now["y"]) != (m["x"], m["y"]):
                _eval(_monitor_lua(m))
                time.sleep(0.2)
        self.out_x, self.out_y, self.out_w, self.out_h = x, 0, w, h

        # the proxy window never takes keyboard focus - typing goes straight to the source (once it was clicked)
        _eval('hl.window_rule({ name = "ns-proxy-nofocus", match = { class = "ns-dlss-proxy" }, no_focus = true })')
        _eval('hl.dispatch(hl.dsp.focus({ monitor = "%s" }))' % OUT_NAME)
        _eval('hl.dispatch(hl.dsp.focus({ workspace = "name:%s" }))' % WS_NAME)
        _eval('hl.dispatch(hl.dsp.window.move({ window = "address:%s", workspace = "name:%s", follow = false }))'
              % (self.addr, WS_NAME))
        time.sleep(0.2)
        if c["fullscreen"] == 0:
            _eval('hl.dispatch(hl.dsp.window.fullscreen({ mode = "fullscreen", window = "address:%s" }))' % self.addr)
        _eval('hl.dispatch(hl.dsp.focus({ monitor = "%s" }))' % orig_mon)
        _eval('hl.dispatch(hl.dsp.focus({ workspace = "%s" }))' % self.state["workspace"])
        for _ in range(30):
            cc = next((q for q in _hl_json("clients") if q["address"] == self.addr), None)
            if cc and cc["at"] == [x, 0] and cc["size"] == [w, h]:
                break
            time.sleep(0.1)
        else:
            print(f"[proxy] warning: source is at {cc and cc['at']} {cc and cc['size']}, expected {x},0 {w}x{h}",
                  file=sys.stderr)
        self.engaged = True
        print(f"[proxy] {self.win_class!r} moved to {OUT_NAME} ({w}x{h} at {x},0)")
        return x, 0, w, h

    def release(self):
        """Put the source window back where it was and drop the headless output."""
        if not self.engaged:
            return
        self.engaged = False
        st = self.state
        try:
            if st["fullscreen"] == 0:
                _eval('hl.dispatch(hl.dsp.window.fullscreen({ mode = "fullscreen", window = "address:%s" }))' % st["address"])
            _eval('hl.dispatch(hl.dsp.window.move({ window = "address:%s", workspace = "%s", follow = false }))'
                  % (st["address"], st["workspace"]))
            time.sleep(0.2)
            _eval('hl.dispatch(hl.dsp.focus({ window = "address:%s" }))' % st["address"])
            subprocess.run(["hyprctl", "output", "remove", OUT_NAME], capture_output=True)
        finally:
            try:
                os.remove(STATE_FILE)
            except OSError:
                pass
        print("[proxy] source window restored")


def restore_from_state_file():
    """Crash recovery: `python3 proxy_desktop.py restore`."""
    if not os.path.exists(STATE_FILE):
        print("no state file - nothing to restore")
        return
    st = json.load(open(STATE_FILE))
    d = ProxyDesktop("")
    d.state, d.engaged = st, True
    d.release()


class ProxyCapture:
    """Screencopy of the whole headless output, with the ToplevelCapture interface capture_frame_toplevel() expects."""
    def __init__(self, out_x: int, out_w: int, out_h: int):
        from wl_capture import ScreencopyCapture
        self._sc = ScreencopyCapture(output_x=out_x)
        self._w, self._h = out_w, out_h
        self.last_wait_ms = 0.0

    def capture_raw(self):
        arr, is_bgr = self._sc.capture_region_raw(0, 0, self._w, self._h)
        self.last_wait_ms = self._sc.last_wait_ms
        return arr, is_bgr

    def close(self):
        self._sc.close()


class InputForwarder:
    """Proxy-window pointer -> the source window on the headless output, through a uinput absolute pointer."""
    MAXV = 65535
    HOVER_MIN_INTERVAL = 1 / 90.0

    def __init__(self, out_x: int, out_y: int, out_w: int, out_h: int):
        from evdev import UInput, ecodes as e, AbsInfo
        self._e = e
        self.out_x, self.out_y, self.out_w, self.out_h = out_x, out_y, out_w, out_h
        cap = {e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_SIDE, e.BTN_EXTRA],
               e.EV_REL: [e.REL_WHEEL, e.REL_HWHEEL],
               e.EV_ABS: [(e.ABS_X, AbsInfo(0, 0, self.MAXV, 0, 0, 0)), (e.ABS_Y, AbsInfo(0, 0, self.MAXV, 0, 0, 0))]}
        self._ui = UInput(cap, name="ns-proxy-pointer")
        self._q: "queue.Queue" = queue.Queue()
        self._held = set()
        self._home = (0, 0)
        self.busy_until = 0.0   # the proxy window ignores its own pointer events until then (our warps cause them)
        self._layout()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _layout(self):
        mons = _hl_json("monitors")
        self.lx0 = min(m["x"] for m in mons)
        self.ly0 = min(m["y"] for m in mons)
        self.lw = max(m["x"] + int(m["width"] / m["scale"]) for m in mons) - self.lx0
        self.lh = max(m["y"] + int(m["height"] / m["scale"]) for m in mons) - self.ly0

    def _warp(self, x, y):
        e = self._e
        self._ui.write(e.EV_ABS, e.ABS_X, round((x - self.lx0) * self.MAXV / (self.lw - 1)))
        self._ui.write(e.EV_ABS, e.ABS_Y, round((y - self.ly0) * self.MAXV / (self.lh - 1)))

    def _cursor(self):
        j = json.loads(_hypr_socket_request("j/cursorpos"))
        return j["x"], j["y"]

    # public, called from the GTK thread; sx, sy in source-window pixels
    def motion(self, sx, sy):
        self._q.put(("move", sx, sy))

    def button(self, code, down, sx, sy):
        self._q.put(("btn", code, down, sx, sy))

    def scroll(self, dx, dy, sx, sy):
        self._q.put(("scroll", dx, dy, sx, sy))

    def _run(self):
        e = self._e
        last_hover = 0.0
        while True:
            ev = self._q.get()
            if ev is None:
                return
            # coalesce a backlog of motions: only the newest position matters
            while ev[0] == "move":
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    return
                ev = nxt
            if ev[0] == "move" and not self._held and time.monotonic() - last_hover < self.HOVER_MIN_INTERVAL:
                continue
            try:
                if not self._held:
                    self._home = self._cursor()   # where the user's pointer really is (over the proxy window)
                back = self._home
                sx, sy = ev[-2], ev[-1]
                tx = self.out_x + min(max(sx, 0), self.out_w - 1)
                ty = self.out_y + min(max(sy, 0), self.out_h - 1)
                self.busy_until = time.monotonic() + 0.5   # generous while we work; tightened at the end
                self._warp(tx, ty)
                self._ui.syn()
                if ev[0] == "btn":
                    _, code, down, _, _ = ev
                    (self._held.add if down else self._held.discard)(code)
                    self._ui.write(e.EV_KEY, code, 1 if down else 0)
                    self._ui.syn()
                elif ev[0] == "scroll":
                    _, dx, dy, _, _ = ev
                    if dy:
                        self._ui.write(e.EV_REL, e.REL_WHEEL, -int(dy))
                    if dx:
                        self._ui.write(e.EV_REL, e.REL_HWHEEL, int(dx))
                    self._ui.syn()
                last_hover = time.monotonic()
                if self._held:
                    continue   # a drag is in progress: stay on the source so its implicit grab sees consistent coordinates
                time.sleep(0.002)   # let the compositor deliver enter/motion/button to the source first
                self._warp(*back)
                self._ui.syn()
                self.busy_until = time.monotonic() + 0.03
            except Exception as exc:
                print(f"[proxy] input forward error: {exc}", file=sys.stderr)
                time.sleep(0.05)

    def close(self):
        self._q.put(None)
        try:
            self._ui.close()
        except Exception:
            pass


class ProxyWindow(Gtk.Window):
    """Ordinary toplevel showing the filtered picture and forwarding the pointer. Same surface API as ScreenOverlay
    (update_frame / set_compare_mode / reposition / show_all / hide / connect), so live_filter treats them alike."""
    def __init__(self, src_w: int, src_h: int, forwarder: "InputForwarder | None"):
        super().__init__(title="DLSS5 proxy")
        self._cairo, self._Gdk = cairo, Gdk
        self.ww, self.wh = src_w, src_h
        self._local_x = self._local_y = 0
        self._fwd = forwarder
        self._compare_mode = False
        self._divider_frac = 0.5
        self._processed = self._raw = None
        self._scroll_acc = [0.0, 0.0]
        self.set_wmclass("ns-dlss-proxy", "ns-dlss-proxy")
        self.set_default_size(min(src_w, 1600), min(src_h, 900))
        self.area = Gtk.DrawingArea()
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                             | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK
                             | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.area.connect("draw", self._on_draw)
        self.area.connect("button-press-event", self._on_button, True)
        self.area.connect("button-release-event", self._on_button, False)
        self.area.connect("motion-notify-event", self._on_motion)
        self.area.connect("scroll-event", self._on_scroll)
        self.add(self.area)
        self.show_all()

    # --- API shared with ScreenOverlay ---
    def update_frame(self, processed, raw=None):
        self._processed, self._raw = processed, raw
        self.area.queue_draw()
        return False

    def set_compare_mode(self, enabled: bool):
        self._compare_mode = enabled
        self.area.queue_draw()

    def reposition(self, *a, **k):
        pass   # the source is fixed on the headless output; this window is placed by the user / compositor

    # --- geometry ---
    def _fit(self):
        aw, ah = self.area.get_allocated_width(), self.area.get_allocated_height()
        sc = min(aw / self.ww, ah / self.wh)
        return sc, (aw - self.ww * sc) / 2, (ah - self.wh * sc) / 2

    def _src_xy(self, x, y):
        sc, ox, oy = self._fit()
        return (x - ox) / sc, (y - oy) / sc

    def _quiet(self):
        return self._fwd is None or time.monotonic() < self._fwd.busy_until

    # --- input ---
    def _on_motion(self, w, ev):
        if not self._quiet():
            self._fwd.motion(*self._src_xy(ev.x, ev.y))
        return True

    def _on_button(self, w, ev, down):
        if self._fwd is None or ev.type not in (self._Gdk.EventType.BUTTON_PRESS, self._Gdk.EventType.BUTTON_RELEASE):
            return True
        from evdev import ecodes as e
        code = {1: e.BTN_LEFT, 2: e.BTN_MIDDLE, 3: e.BTN_RIGHT, 8: e.BTN_SIDE, 9: e.BTN_EXTRA}.get(ev.button)
        if code is not None:
            self._fwd.button(code, down, *self._src_xy(ev.x, ev.y))
        return True

    def _on_scroll(self, w, ev):
        if self._fwd is None:
            return True
        D = self._Gdk.ScrollDirection
        dx = dy = 0
        if ev.direction == D.SMOOTH:
            self._scroll_acc[0] += ev.delta_x
            self._scroll_acc[1] += ev.delta_y
            dx, dy = int(self._scroll_acc[0]), int(self._scroll_acc[1])
            self._scroll_acc[0] -= dx
            self._scroll_acc[1] -= dy
        elif ev.direction == D.UP:
            dy = -1
        elif ev.direction == D.DOWN:
            dy = 1
        elif ev.direction == D.LEFT:
            dx = -1
        elif ev.direction == D.RIGHT:
            dx = 1
        if dx or dy:
            self._fwd.scroll(dx, dy, *self._src_xy(ev.x, ev.y))
        return True

    # --- drawing ---
    def _on_draw(self, area, cr):
        cairo = self._cairo
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        if self._processed is None:
            return
        sc, ox, oy = self._fit()
        cr.translate(ox, oy)
        cr.scale(sc, sc)
        cr.set_source_surface(self._processed.surface(), 0, 0)
        cr.get_source().set_filter(cairo.FILTER_BILINEAR)
        cr.paint()
        if self._compare_mode and self._raw is not None:
            dx = self.ww * self._divider_frac
            cr.save()
            cr.rectangle(0, 0, dx, self.wh)
            cr.clip()
            cr.set_source_surface(self._raw.surface(), 0, 0)
            cr.paint()
            cr.restore()
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.set_line_width(2 / sc)
            cr.move_to(dx, 0)
            cr.line_to(dx, self.wh)
            cr.stroke()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        restore_from_state_file()
    else:
        print(__doc__)
