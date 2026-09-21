# SPDX-License-Identifier: MIT
"""The real screen overlay - replaces the old corner PreviewWindow for
NS_UI=1 runs. Positioned exactly over the captured window (same monitor,
same on-screen rect) and click-through everywhere except an optional
before/after comparison-divider handle. See ../README.md's "Live settings
overlay" section.
"""
import sys
import cairo
import time

import numpy as np

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GtkLayerShell

HANDLE_W = 18
HANDLE_H = 90


def gdk_monitor_for_geometry(mon_x: int, mon_y: int) -> Gdk.Monitor:
    """Match a hyprctl monitor (by its x,y - confirmed identical to
    Gdk.Monitor's own geometry on this system) to the Gdk.Monitor
    GtkLayerShell.set_monitor() needs."""
    display = Gdk.Display.get_default()
    for i in range(display.get_n_monitors()):
        m = display.get_monitor(i)
        geo = m.get_geometry()
        if geo.x == mon_x and geo.y == mon_y:
            return m
    raise RuntimeError(f"no Gdk.Monitor at {mon_x},{mon_y}")


PROFILE_CB = None  # set by live_filter when NS_PROFILE=1: fn(stage_name, seconds)


class CairoFrame:
    """A frame already converted to cairo's ARGB32 byte order (B,G,R,A on little-endian), backed by a bytearray
    cairo can wrap without another copy. Build it off the GTK thread (`from_rgba`) so the draw handler only
    has to paint - the conversion is ~5 ms at 1975x1398 and used to run on the GTK thread every frame."""
    __slots__ = ("buf", "w", "h")

    def __init__(self, buf: bytearray, w: int, h: int):
        self.buf, self.w, self.h = buf, w, h

    @classmethod
    def from_rgba(cls, frame: np.ndarray) -> "CairoFrame":
        h, w = frame.shape[:2]
        buf = bytearray(h * w * 4)
        v = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        v[:, :, 0] = frame[:, :, 2]
        v[:, :, 1] = frame[:, :, 1]
        v[:, :, 2] = frame[:, :, 0]
        v[:, :, 3] = 255  # opaque, so "premultiplied" is a no-op
        return cls(buf, w, h)

    @classmethod
    def from_bgra(cls, frame: np.ndarray) -> "CairoFrame":
        """Wrap an ALREADY cairo-ordered (B,G,R,A, opaque) (h, w, 4) uint8 array without copying; the caller keeps it alive and unchanged."""
        h, w = frame.shape[:2]
        return cls(frame, w, h)

    def surface(self) -> "cairo.ImageSurface":
        stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32, self.w)  # == w * 4
        return cairo.ImageSurface.create_for_data(self.buf, cairo.FORMAT_ARGB32, self.w, self.h, stride)


class ScreenOverlay(Gtk.Window):
    def __init__(self, gdk_monitor: Gdk.Monitor, local_x: int, local_y: int, ww: int, wh: int):
        super().__init__()
        self._gdk_mon = gdk_monitor
        self.ww, self.wh = ww, wh
        self._local_x, self._local_y = local_x, local_y
        self._compare_mode = False
        self._divider_frac = 0.5
        self._dragging_divider = False
        self._processed = None  # CairoFrame, ww x wh
        self._raw = None        # CairoFrame (only needed in compare mode)

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "ns-dlss-overlay")
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_monitor(self, gdk_monitor)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
        px, py = self._display_pos(local_x, local_y, ww, wh)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, px)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, py)
        # -1 = ignore other surfaces' exclusive zones (e.g. the Caelestia
        # panel's reserved strip) when positioning us - we want to cover
        # exactly the target window's rect, not get pushed/resized around
        # a bar elsewhere on the screen. Without this, the shift the panel's
        # reserved zone caused matched its own size almost exactly.
        GtkLayerShell.set_exclusive_zone(self, -1)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        self.set_decorated(False)
        self.set_default_size(ww, wh)
        self.set_app_paintable(True)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)

        self.area = Gtk.DrawingArea()
        self.area.set_size_request(ww, wh)
        self.area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.area.connect("draw", self._on_draw)
        self.area.connect("button-press-event", self._on_button_press)
        self.area.connect("button-release-event", self._on_button_release)
        self.area.connect("motion-notify-event", self._on_motion)
        self.add(self.area)

        self.connect("realize", lambda *_: self._apply_input_shape())
        self.connect("map-event", lambda *_: self._apply_input_shape())
        self.show_all()

    def _display_pos(self, lx: int, ly: int, ww: int, wh: int):
        """Where the overlay surface goes: exactly over the source window."""
        return lx, ly

    # --- public API, called from process_loop via GLib.idle_add ---
    def update_frame(self, processed: "CairoFrame", raw: "CairoFrame | None" = None):
        self._processed = processed
        self._raw = raw
        # GDK can silently reset a window's input-shape region on its own
        # (e.g. around resizes/reconfigures) without telling the app - so
        # this doesn't trust the one-time realize/map-event application to
        # stick, and reasserts it on every frame instead. Cheap and
        # idempotent when nothing changed.
        self._apply_input_shape()
        self.area.queue_draw()
        return False

    def reposition(self, local_x: int, local_y: int, ww: int, wh: int, gdk_monitor=None):
        """Called when the captured window's own geometry changes (moved
        or resized) - keeps the overlay pinned exactly over it instead of
        silently drifting out of sync (stale size here previously showed
        as the picture "shrinking" relative to a resized source window)."""
        if gdk_monitor is not None:
            GtkLayerShell.set_monitor(self, gdk_monitor)  # window moved to another output
        elif (local_x, local_y, ww, wh) == (self._local_x, self._local_y, self.ww, self.wh):
            return
        self._local_x, self._local_y = local_x, local_y
        self.ww, self.wh = ww, wh
        if gdk_monitor is not None:
            self._gdk_mon = gdk_monitor
        px, py = self._display_pos(local_x, local_y, ww, wh)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, px)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, py)
        self.area.set_size_request(ww, wh)
        self.resize(ww, wh)
        self._apply_input_shape()

    def set_compare_mode(self, enabled: bool):
        self._compare_mode = enabled
        self._apply_input_shape()
        self.area.queue_draw()
        return False

    # --- click-through: pass-through everywhere except the divider handle ---
    def _handle_rect(self):
        x = int(self.ww * self._divider_frac) - HANDLE_W // 2
        y = self.wh // 2 - HANDLE_H // 2
        return max(0, x), max(0, y), HANDLE_W, HANDLE_H

    def _apply_input_shape(self):
        win = self.get_window()
        if win is None:
            return
        if not self._compare_mode:
            win.input_shape_combine_region(cairo.Region(), 0, 0)
        else:
            x, y, w, h = self._handle_rect()
            region = cairo.Region(cairo.RectangleInt(x, y, w, h))
            win.input_shape_combine_region(region, 0, 0)

    # --- divider drag (only reachable through the handle's input-shape rect) ---
    def _on_button_press(self, widget, event):
        x, y, w, h = self._handle_rect()
        if x <= event.x <= x + w and y <= event.y <= y + h:
            self._dragging_divider = True
            return True
        return False

    def _on_button_release(self, widget, event):
        self._dragging_divider = False
        return True

    def _on_motion(self, widget, event):
        if not self._dragging_divider:
            return False
        self._divider_frac = min(0.95, max(0.05, event.x / self.ww))
        self._apply_input_shape()
        self.area.queue_draw()
        return True

    # --- rendering ---
    def _on_draw(self, area, cr):
        _t = time.perf_counter()
        try:
            self._draw(area, cr)
        finally:
            if PROFILE_CB:
                PROFILE_CB("gtk draw", time.perf_counter() - _t)

    def _draw(self, area, cr):
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        if self._processed is None:
            return
        surface = self._processed.surface()
        cr.set_source_surface(surface, 0, 0)
        cr.paint()

        if self._compare_mode and self._raw is not None:
            divider_x = self.ww * self._divider_frac
            rsurface = self._raw.surface()
            cr.save()
            cr.rectangle(0, 0, divider_x, self.wh)
            cr.clip()
            cr.set_source_surface(rsurface, 0, 0)
            cr.paint()
            cr.restore()

            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.set_line_width(2)
            cr.move_to(divider_x, 0)
            cr.line_to(divider_x, self.wh)
            cr.stroke()

            x, y, hw, hh = self._handle_rect()
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.rectangle(x, y, hw, hh)
            cr.fill()
