# SPDX-License-Identifier: MIT
"""Floating, draggable DLSS5 settings panel - see ../README.md's "Live
settings overlay" section. Purely a view onto `LiveState`: every control
here only writes plain data to the shared `LiveState` (never touches the
Worker's pipe or an upscaler's GL context - those belong to
process_loop's thread, see live_state.py's module docstring).
"""
import json
import os
import socket
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, GLib, GtkLayerShell, Gdk

# Applying every single motion-notify-event's margin change as its own
# GtkLayerShell.set_margin() call means a full layer-shell reconfigure/
# commit round trip per event - GTK can emit dozens of these per second
# during a drag, and at that rate the round trips visibly stutter. A
# layer-shell surface has no cheap compositor-level "move" the way a
# normal xdg_toplevel does, so instead this throttles how often the
# margin actually gets committed while still tracking the true drag
# position every event (see _on_drag_motion).
DRAG_COMMIT_INTERVAL_SEC = 1 / 60

from live_state import DlssParams, NR_STYLES
from postprocess import Composition
from framegen import REGISTRY as FG_REGISTRY, FrameGenSettings
from upscalers import REGISTRY


class SettingsPanel(Gtk.Window):
    def __init__(self, state, native_w, native_h, nr_preset_restart_cb, on_compare_toggled=None,
                 quit_cb=None, list_windows_cb=None):
        super().__init__()
        self.state = state
        self.native_w, self.native_h = native_w, native_h
        self._nr_preset_restart_cb = nr_preset_restart_cb
        self._compare_cb = on_compare_toggled
        self._list_windows_cb = list_windows_cb
        self._dragging = False
        self._drag_start = (0, 0)
        self._margin = [60, 60]  # left, top
        self._last_drag_commit = 0.0

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "ns-dlss-panel")
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, self._margin[0])
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, self._margin[1])
        GtkLayerShell.set_exclusive_zone(self, -1)  # see screen_overlay.py's comment
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.ON_DEMAND)
        self.set_decorated(False)
        self.set_default_size(280, -1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_border_width(8)
        self.add(root)

        # --- drag handle ---
        handle = Gtk.EventBox()
        handle_label = Gtk.Label(label="⋮⋮  DLSS5 settings")
        handle_label.set_xalign(0.0)
        handle.add(handle_label)
        handle.connect("button-press-event", self._on_drag_start)
        handle.connect("button-release-event", self._on_drag_end)
        handle.connect("motion-notify-event", self._on_drag_motion)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head.pack_start(handle, True, True, 0)
        quit_btn = Gtk.Button(label="\u23fb Выключить")
        quit_btn.connect("clicked", lambda *_: quit_cb() if quit_cb else Gtk.main_quit())
        head.pack_start(quit_btn, False, False, 0)
        root.pack_start(head, False, False, 0)
        root.pack_start(Gtk.Separator(), False, False, 0)

        # --- target window ---
        root.pack_start(Gtk.Label(label="Target window", xalign=0), False, False, 0)
        trow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.target_combo = Gtk.ComboBoxText()
        self._target_classes = []
        self._target_updating = False
        trow.pack_start(self.target_combo, True, True, 0)
        refresh = Gtk.Button(label="\u21bb")
        refresh.connect("clicked", lambda *_: self._refresh_targets())
        trow.pack_start(refresh, False, False, 0)
        root.pack_start(trow, False, False, 0)
        self.target_combo.connect("changed", self._on_target_changed)
        self._refresh_targets()

        # --- resolution slider ---
        root.pack_start(Gtk.Label(label="Model input resolution", xalign=0), False, False, 0)
        self.res_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 10, 200, 1)
        self.res_scale.set_value(100)
        self.res_scale.set_draw_value(False)
        self.res_scale.connect("value-changed", self._on_res_changed)
        root.pack_start(self.res_scale, False, False, 0)
        self.res_label = Gtk.Label(label=f"100%  {native_w}x{native_h}", xalign=0)
        root.pack_start(self.res_label, False, False, 0)

        root.pack_start(Gtk.Separator(), False, False, 0)

        # --- upscaler picker ---
        root.pack_start(Gtk.Label(label="Upscaler", xalign=0), False, False, 0)
        self.upscaler_combo = Gtk.ComboBoxText()
        self._upscaler_keys = list(REGISTRY.keys())
        for key in self._upscaler_keys:
            self.upscaler_combo.append_text(REGISTRY[key].name)
        self.upscaler_combo.set_active(self._upscaler_keys.index(state.upscaler_key))
        self.upscaler_combo.connect("changed", self._on_upscaler_changed)
        root.pack_start(self.upscaler_combo, False, False, 0)

        self.upscaler_settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        root.pack_start(self.upscaler_settings_box, False, False, 0)
        self._build_upscaler_settings_ui(state.upscaler_key)

        root.pack_start(Gtk.Separator(), False, False, 0)

        # --- DLSS5 parameters (live via RNSZ - see live_filter.DlssParams) ---
        root.pack_start(Gtk.Label(label="DLSS5 parameters", xalign=0), False, False, 0)
        self._param_scales = {}
        for key, label, lo, hi in [
            ("intensity", "Intensity", 0.0, 2.0),
            ("local_tone", "Local tone strength", 0.0, 2.0),
            ("local_structure", "Local structure strength", 0.0, 2.0),
            ("skin_structure", "Skin structure strength", -1.0, 2.0),
        ]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.pack_start(Gtk.Label(label=label, xalign=0, width_chars=18), False, False, 0)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, 0.05)
            scale.set_value(getattr(state.params, key))
            scale.set_draw_value(True)
            scale.connect("value-changed", self._on_param_changed)
            row.pack_start(scale, True, True, 0)
            root.pack_start(row, False, False, 0)
            self._param_scales[key] = scale

        self.auto_mask_check = Gtk.CheckButton(label="Auto mask")
        self.auto_mask_check.set_active(bool(state.params.auto_mask))
        self.auto_mask_check.connect("toggled", self._on_param_changed)
        root.pack_start(self.auto_mask_check, False, False, 0)

        srow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        srow.pack_start(Gtk.Label(label="NR style", xalign=0, width_chars=18), False, False, 0)
        self._style_combo = Gtk.ComboBoxText()
        for name in NR_STYLES:
            self._style_combo.append_text(name)
        self._style_combo.set_active(state.params.style)
        self._style_combo.connect("changed", self._on_param_changed)
        srow.pack_start(self._style_combo, True, True, 0)
        root.pack_start(srow, False, False, 0)

        urow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        urow.pack_start(Gtk.Label(label="UI correction (undocumented, int)", xalign=0), False, False, 0)
        self._ui_correction_spin = Gtk.SpinButton.new_with_range(0, 8, 1)
        self._ui_correction_spin.set_value(state.params.ui_correction)
        self._ui_correction_spin.connect("value-changed", self._on_param_changed)
        urow.pack_start(self._ui_correction_spin, False, False, 0)
        root.pack_start(urow, False, False, 0)

        # --- composition (post-pass on the model's output, see postprocess.py) ---
        comp = state.composition
        expander = Gtk.Expander(label="Composition")
        cbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        expander.add(cbox)
        detail_btn = Gtk.Button(label="Detail-Only (colour 0, tone 1)")
        detail_btn.connect("clicked", self._on_detail_only)
        cbox.pack_start(detail_btn, False, False, 0)
        self._comp_scales = {}
        for key, label in [("color_strength", "NR colour strength"), ("tone_preservation", "Tone preservation"),
                           ("face_skin_protection", "Face/skin protection"), ("grain_preservation", "Grain preservation"),
                           ("shimmer_suppression", "Shimmer suppression")]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.pack_start(Gtk.Label(label=label, xalign=0, width_chars=18), False, False, 0)
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
            sc.set_value(getattr(comp, key))
            sc.set_draw_value(True)
            sc.connect("value-changed", self._on_comp_changed)
            row.pack_start(sc, True, True, 0)
            cbox.pack_start(row, False, False, 0)
            self._comp_scales[key] = sc
        prow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        prow.pack_start(Gtk.Label(label="NR passes (1-4)", xalign=0, width_chars=18), False, False, 0)
        self._passes_spin = Gtk.SpinButton.new_with_range(1, 4, 1)
        self._passes_spin.set_value(comp.passes)
        self._passes_spin.connect("value-changed", self._on_comp_changed)
        prow.pack_start(self._passes_spin, False, False, 0)
        cbox.pack_start(prow, False, False, 0)
        mrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        mrow.pack_start(Gtk.Label(label="NR mask image", xalign=0, width_chars=18), False, False, 0)
        self._mask_entry = Gtk.Entry()
        self._mask_entry.set_placeholder_text("path to a grayscale image (white = apply)")
        self._mask_entry.connect("activate", self._on_comp_changed)
        self._mask_entry.connect("focus-out-event", lambda *_: self._on_comp_changed())
        mrow.pack_start(self._mask_entry, True, True, 0)
        browse = Gtk.Button(label="...")
        browse.connect("clicked", self._on_browse_mask)
        mrow.pack_start(browse, False, False, 0)
        cbox.pack_start(mrow, False, False, 0)
        frow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        frow.pack_start(Gtk.Label(label="Mask feather (px)", xalign=0, width_chars=18), False, False, 0)
        self._feather_spin = Gtk.SpinButton.new_with_range(0, 128, 1)
        self._feather_spin.connect("value-changed", self._on_comp_changed)
        frow.pack_start(self._feather_spin, False, False, 0)
        cbox.pack_start(frow, False, False, 0)
        root.pack_start(expander, False, False, 0)

        # --- frame generation (see framegen/) ---
        fgs = state.framegen
        fg_exp = Gtk.Expander(label="Frame generation")
        fgbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        fg_exp.add(fgbox)
        mrow_ = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        mrow_.pack_start(Gtk.Label(label="Method", xalign=0, width_chars=14), False, False, 0)
        self._fg_keys = ["off"] + list(FG_REGISTRY.keys())
        self._fg_combo = Gtk.ComboBoxText()
        self._fg_combo.append_text("Off")
        for k in FG_REGISTRY:
            self._fg_combo.append_text(FG_REGISTRY[k].name)
        self._fg_combo.set_active(self._fg_keys.index(fgs.method) if fgs.method in self._fg_keys else 0)
        self._fg_combo.connect("changed", self._on_fg_changed)
        mrow_.pack_start(self._fg_combo, True, True, 0)
        fgbox.pack_start(mrow_, False, False, 0)
        xrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        xrow.pack_start(Gtk.Label(label="Multiplier (x)", xalign=0, width_chars=14), False, False, 0)
        self._fg_mult = Gtk.SpinButton.new_with_range(2, 4, 1)
        self._fg_mult.set_value(fgs.multiplier)
        self._fg_mult.connect("value-changed", self._on_fg_changed)
        xrow.pack_start(self._fg_mult, False, False, 0)
        fgbox.pack_start(xrow, False, False, 0)
        frow_ = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        frow_.pack_start(Gtk.Label(label="Flow scale", xalign=0, width_chars=14), False, False, 0)
        self._fg_flow = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.25, 1.0, 0.05)
        self._fg_flow.set_value(fgs.flow_scale)
        self._fg_flow.set_draw_value(True)
        self._fg_flow.connect("value-changed", self._on_fg_changed)
        frow_.pack_start(self._fg_flow, True, True, 0)
        fgbox.pack_start(frow_, False, False, 0)
        self._fg_adaptive = Gtk.CheckButton(label="Adaptive: aim for a target fps (multiplier = ceiling)")
        self._fg_adaptive.set_active(fgs.adaptive)
        self._fg_adaptive.set_tooltip_text("Backends that support arbitrary timestamps only (e.g. the MAKO module) - generates as many frames as needed to reach the target")
        self._fg_adaptive.connect("toggled", self._on_fg_changed)
        fgbox.pack_start(self._fg_adaptive, False, False, 0)
        trow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        trow.pack_start(Gtk.Label(label="Target fps", xalign=0, width_chars=14), False, False, 0)
        self._fg_target = Gtk.SpinButton.new_with_range(30, 240, 5)
        self._fg_target.set_value(fgs.target_fps)
        self._fg_target.connect("value-changed", self._on_fg_changed)
        trow.pack_start(self._fg_target, False, False, 0)
        fgbox.pack_start(trow, False, False, 0)
        self._fg_perf = Gtk.CheckButton(label="Performance mode")
        self._fg_perf.set_active(fgs.performance)
        self._fg_perf.connect("toggled", self._on_fg_changed)
        fgbox.pack_start(self._fg_perf, False, False, 0)
        root.pack_start(fg_exp, False, False, 0)

        root.pack_start(Gtk.Separator(), False, False, 0)

        # --- compare mode ---
        compare_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        compare_row.pack_start(Gtk.Label(label="Compare before/after", xalign=0), True, True, 0)
        self.compare_switch = Gtk.Switch()
        self.compare_switch.connect("state-set", self._on_compare_toggled)
        compare_row.pack_start(self.compare_switch, False, False, 0)
        root.pack_start(compare_row, False, False, 0)

        self.fps_label = Gtk.Label(label="-- fps", xalign=0)
        root.pack_start(self.fps_label, False, False, 0)
        self.status_label = Gtk.Label(label="", xalign=0)
        self.status_label.set_line_wrap(True)
        root.pack_start(self.status_label, False, False, 0)

        handle.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )

        self.show_all()
        GLib.timeout_add(300, self._tick)

    # --- drag handling (layer-shell surfaces have no compositor-level move) ---
    def _cursor_pos(self):
        """Global pointer position from Hyprland's IPC socket (~0.01 ms). A layer-shell surface gets pointer
        coordinates RELATIVE TO ITSELF, and it moves while being dragged, so deltas of event.x/x_root double-count
        (the surface lags the margin we set) - that was the jitter. The absolute position has no such feedback."""
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(os.path.join(os.environ["XDG_RUNTIME_DIR"], "hypr",
                                   os.environ["HYPRLAND_INSTANCE_SIGNATURE"], ".socket.sock"))
            s.sendall(b"j/cursorpos")
            data = s.recv(4096)
            s.close()
            pos = json.loads(data)
            return float(pos["x"]), float(pos["y"])
        except Exception:
            return None

    def _on_drag_start(self, widget, event):
        self._dragging = True
        cur = self._cursor_pos()
        if cur is not None:
            # where inside the panel the pointer grabbed it, expressed as (cursor - margin)
            self._grab = (cur[0] - self._margin[0], cur[1] - self._margin[1])
        else:
            self._grab = None
            self._drag_start = (event.x_root, event.y_root)
        return True

    def _on_drag_end(self, widget, event):
        self._dragging = False
        self._commit_margin()  # make sure the final position isn't lost to throttling
        return True

    def _on_drag_motion(self, widget, event):
        if not self._dragging:
            return False
        cur = self._cursor_pos() if self._grab is not None else None
        if cur is not None:
            self._margin[0] = max(0, int(cur[0] - self._grab[0]))
            self._margin[1] = max(0, int(cur[1] - self._grab[1]))
        else:  # no Hyprland IPC: relative deltas (jittery, but works)
            dx = event.x_root - self._drag_start[0]
            dy = event.y_root - self._drag_start[1]
            self._margin[0] = max(0, int(self._margin[0] + dx))
            self._margin[1] = max(0, int(self._margin[1] + dy))
            self._drag_start = (event.x_root, event.y_root)
        now = time.monotonic()
        if now - self._last_drag_commit >= DRAG_COMMIT_INTERVAL_SEC:
            self._commit_margin()
        return True

    def _commit_margin(self):
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, self._margin[0])
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, self._margin[1])
        self._last_drag_commit = time.monotonic()

    # --- target window picker ---
    def _refresh_targets(self):
        if self._list_windows_cb is None:
            return
        try:
            wins = self._list_windows_cb()
        except Exception:
            return
        self._target_updating = True
        self.target_combo.remove_all()
        self._target_classes = []
        for cls, title in wins:
            self.target_combo.append_text(f"{cls} - {title[:32]}")
            self._target_classes.append(cls)
        cur = self.state.target_class
        if cur in self._target_classes:
            self.target_combo.set_active(self._target_classes.index(cur))
        self._target_updating = False

    def _on_target_changed(self, combo):
        if self._target_updating:
            return
        idx = combo.get_active()
        if 0 <= idx < len(self._target_classes):
            self.state.set_target_class(self._target_classes[idx])

    # --- resolution slider ---
    def _on_res_changed(self, scale):
        pct = scale.get_value()
        tw = int(self.native_w * pct / 100) // 2 * 2
        th = int(self.native_h * pct / 100) // 2 * 2
        tw, th = min(max(tw, 64), 7680), min(max(th, 64), 4320)  # DLSSNR supports 64x64 .. 7680x4320
        self.res_label.set_text(f"{pct:.0f}%  {tw}x{th}")
        self.state.set_resolution(tw, th)

    def update_native_size(self, native_w, native_h):
        """Called when the captured window itself was resized (see
        live_filter.py's poll_geometry) - keeps the slider's percentage
        mapping and label in sync with the window's new actual size."""
        self.native_w, self.native_h = native_w, native_h
        self._on_res_changed(self.res_scale)

    # --- frame generation ---
    def _on_fg_changed(self, *_):
        idx = self._fg_combo.get_active()
        method = self._fg_keys[idx] if 0 <= idx < len(self._fg_keys) else "off"
        self.state.set_framegen(FrameGenSettings(
            method=method, multiplier=int(self._fg_mult.get_value()),
            flow_scale=round(float(self._fg_flow.get_value()), 2), performance=self._fg_perf.get_active(),
            adaptive=self._fg_adaptive.get_active(), target_fps=int(self._fg_target.get_value())))

    # --- composition ---
    def _on_comp_changed(self, *_):
        if getattr(self, "_comp_updating", False):
            return
        v = {k: sc.get_value() for k, sc in self._comp_scales.items()}
        self.state.set_composition(Composition(
            passes=int(self._passes_spin.get_value()),
            mask_path=self._mask_entry.get_text().strip(),
            mask_feather=int(self._feather_spin.get_value()), **v))

    def _on_detail_only(self, _btn):
        self._comp_updating = True
        self._comp_scales["color_strength"].set_value(0.0)
        self._comp_scales["tone_preservation"].set_value(1.0)
        self._comp_updating = False
        self._on_comp_changed()

    def _on_browse_mask(self, _btn):
        dlg = Gtk.FileChooserDialog(title="NR mask image", action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)
        if dlg.run() == Gtk.ResponseType.OK:
            self._mask_entry.set_text(dlg.get_filename() or "")
            self._on_comp_changed()
        dlg.destroy()

    # --- upscaler ---
    def _build_upscaler_settings_ui(self, key):
        for child in self.upscaler_settings_box.get_children():
            self.upscaler_settings_box.remove(child)
        cls = REGISTRY[key]
        self._upscaler_setting_widgets = {}
        for setting in cls.settings:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.pack_start(Gtk.Label(label=setting.label, xalign=0, width_chars=14), False, False, 0)
            if setting.kind == "float":
                w = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, setting.min, setting.max, setting.step)
                w.set_value(setting.default)
                w.connect("value-changed", lambda *_: self._on_upscaler_setting_changed())
            elif setting.kind == "bool":
                w = Gtk.CheckButton()
                w.set_active(bool(setting.default))
                w.connect("toggled", lambda *_: self._on_upscaler_setting_changed())
            else:
                w = Gtk.SpinButton.new_with_range(setting.min, setting.max, setting.step or 1)
                w.set_value(setting.default)
                w.connect("value-changed", lambda *_: self._on_upscaler_setting_changed())
            row.pack_start(w, True, True, 0)
            self.upscaler_settings_box.pack_start(row, False, False, 0)
            self._upscaler_setting_widgets[setting.key] = (setting.kind, w)
        self.upscaler_settings_box.show_all()

    def _collect_upscaler_settings(self):
        out = {}
        for key, (kind, w) in getattr(self, "_upscaler_setting_widgets", {}).items():
            if kind == "bool":
                out[key] = w.get_active()
            else:
                out[key] = w.get_value()
        return out

    def _on_upscaler_changed(self, combo):
        idx = combo.get_active()
        if idx < 0:
            return
        key = self._upscaler_keys[idx]
        self._build_upscaler_settings_ui(key)
        self.state.set_upscaler(key, self._collect_upscaler_settings())

    def _on_upscaler_setting_changed(self):
        key = self._upscaler_keys[self.upscaler_combo.get_active()]
        self.state.set_upscaler(key, self._collect_upscaler_settings())

    # --- DLSS params ---
    def _on_param_changed(self, *_widgets):
        # Same rule as the reference app: a skin-structure value above -1
        # only takes effect with Automatic Mask on, so turn it on for the user.
        if (self._param_scales["skin_structure"].get_value() > -1.0
                and not self.auto_mask_check.get_active()):
            self.auto_mask_check.set_active(True)
            return  # set_active re-enters this handler with the mask on
        params = DlssParams(
            style=max(0, self._style_combo.get_active()),
            auto_mask=int(self.auto_mask_check.get_active()),
            ui_correction=int(self._ui_correction_spin.get_value()),
            intensity=self._param_scales["intensity"].get_value(),
            local_tone=self._param_scales["local_tone"].get_value(),
            local_structure=self._param_scales["local_structure"].get_value(),
            skin_structure=self._param_scales["skin_structure"].get_value(),
        )
        self.state.set_params(params)

    def _on_compare_toggled(self, switch, active):
        self.state.compare_mode = active
        if self._compare_cb is not None:
            self._compare_cb(active)
        return False

    def _tick(self):
        real = self.state.real_fps
        shown = self.state.fps
        self.fps_label.set_text(f"{shown:.1f} fps" if self.state.framegen.method == "off" or abs(shown - real) < 0.5
                                else f"{shown:.1f} fps shown ({real:.1f} real)")
        if self.state.status:
            self.status_label.set_text(self.state.status)
        return True
