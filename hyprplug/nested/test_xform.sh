#!/bin/bash
# SPDX-License-Identifier: MIT
# Buffer transform / viewporter test. For each wl_output_transform (and a viewport crop) a FRESH nested Hyprland runs one client with a known
# 4-colour buffer; the plugin's export is compared with what the compositor itself shows (grim) at the four quadrant centres of the surface.
cd "$(dirname "$0")/.."
(cd build && ninja >/dev/null)
fail=0
for spec in "0" "1" "2" "3" "4" "5" "6" "7" "0 crop"; do
  [ -n "$ONLY" ] && [ "$spec" != "$ONLY" ] && continue
  before=$(ls "$XDG_RUNTIME_DIR/hypr" | sort)
  Hyprland -c "$PWD/nested/hyprland.lua" >nested/nested.log 2>&1 &
  NP=$!; sleep 6
  N=$(comm -13 <(echo "$before") <(ls "$XDG_RUNTIME_DIR/hypr" | sort) | head -1)
  h(){ hyprctl -i "$N" "$@"; }
  h plugin load "$PWD/build/libnsproxy.so" >/dev/null
  WAYLAND_DISPLAY=wayland-2 python3 nested/xformwin.py $spec >/dev/null 2>&1 &
  CP=$!; sleep 3
  h nsproxy attach xform >/dev/null
  WAYLAND_DISPLAY=wayland-2 grim /tmp/xf_screen.png
  python3 - "$N" "$spec" <<'PY' || fail=1
import sys, json, subprocess, numpy as np
import os; sys.path.insert(0, os.path.abspath("../app"))   # cwd is hyprplug/
from plugin_bridge import PluginLink
from PIL import Image
N, spec = sys.argv[1], sys.argv[2]
cl = [c for c in json.loads(subprocess.check_output(["hyprctl", "-i", N, "clients", "-j"])) if c["class"] == "xform"][0]
L = PluginLink(); a, _ = L.wait_frame(3.0); exp = np.array(a)[..., :3]; L.close()
H, W = exp.shape[:2]
scr = np.array(Image.open("/tmp/xf_screen.png").convert("RGB"))
x0, y0 = cl["at"]
def quad(img, ox=0, oy=0):
    h, w = (H, W)
    return [tuple(int(v) for v in img[oy + int(h * fy), ox + int(w * fx)]) for fy in (.25, .75) for fx in (.25, .75)]
T = int(spec.split()[0]); crop = "crop" in spec
buf = np.zeros((200, 300, 3), np.uint8)
buf[:100, :150] = (255, 0, 0); buf[:100, 150:] = (0, 255, 0); buf[100:, :150] = (0, 0, 255); buf[100:, 150:] = (255, 255, 255)
if T % 2 == 0 or crop:
    # even transforms and the viewport case: the compositor's own rendering is the reference
    want, got = quad(scr, x0, y0), quad(exp)
else:
    # Hyprland stretches odd-transform buffers into an unswapped box (distorted), so use the wl_surface.set_buffer_transform definition:
    # surface = inverse(transform)(buffer)
    ref = {1: np.rot90(buf, -1), 3: np.rot90(buf, 1), 5: buf.transpose(1, 0, 2), 7: np.rot90(buf.transpose(1, 0, 2), 2)}[T]
    assert ref.shape[:2] == (H, W), (ref.shape, H, W)
    want, got = quad(ref), quad(exp)
ok = want == got
print(f"transform {spec:8s} export {W}x{H} at {x0},{y0}  {'OK ' if ok else 'MISMATCH'} compositor={want} plugin={got}")
sys.exit(0 if ok else 1)
PY
  kill -9 $CP 2>/dev/null; kill $NP 2>/dev/null; wait $NP 2>/dev/null; sleep 1
done
exit $fail
