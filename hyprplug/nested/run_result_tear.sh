#!/bin/bash
# SPDX-License-Identifier: MIT
# Result direction tearing check on screenshots of the nested compositor (zero-copy result upload on/off via ZC=0/1).
cd "$(dirname "$0")/.."
sed -i 's/mode = "2560x1440@60"/mode = "1600x900@60"/' nested/hyprland.lua
before=$(ls "$XDG_RUNTIME_DIR/hypr" | sort)
NSPROXY_ZEROCOPY=${ZC:-1} Hyprland -c "$PWD/nested/hyprland.lua" >nested/nested.log 2>&1 &
NP=$!; sleep 7
N=$(comm -13 <(echo "$before") <(ls "$XDG_RUNTIME_DIR/hypr" | sort) | head -1)
h(){ hyprctl -i "$N" "$@"; }
WAYLAND_DISPLAY=wayland-2 python3 nested/animwin.py >/dev/null 2>&1 &
AP=$!; sleep 2
h plugin load "$PWD/build/libnsproxy.so" >/dev/null; h nsproxy attach animwin >/dev/null
python3 nested/pub_move.py 14 &
PP=$!; sleep 3
for i in $(seq 1 25); do WAYLAND_DISPLAY=wayland-2 grim /tmp/rt_$i.png; done
wait $PP
python3 - <<'PY'
import numpy as np
from PIL import Image
torn = ok = 0
for i in range(1, 26):
    a = np.array(Image.open(f"/tmp/rt_{i}.png").convert("RGB"))[80:-80, 60:-60].astype(int)
    white = (a.min(-1) > 235)
    rows = np.where(white.any(1))[0]
    if len(rows) < 100: continue
    starts = np.array([np.argmax(white[r]) for r in rows])
    if starts.max() - starts.min() > 2: torn += 1
    else: ok += 1
print(f"screenshots with a rigid bar: {ok}, TORN: {torn}")
PY
kill $AP $NP 2>/dev/null; wait 2>/dev/null
