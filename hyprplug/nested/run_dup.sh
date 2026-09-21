#!/bin/bash
# SPDX-License-Identifier: MIT
# Two windows of the SAME class, the first one on a hidden workspace: attaching by class (plugin and live_filter) must pick the visible one.
cd "$(dirname "$0")/.."
sed -i 's/mode = "2560x1440@60"/mode = "1600x900@60"/' nested/hyprland.lua
before=$(ls "$XDG_RUNTIME_DIR/hypr" | sort)
Hyprland -c "$PWD/nested/hyprland.lua" >nested/nested.log 2>&1 &
NP=$!; sleep 7
N=$(comm -13 <(echo "$before") <(ls "$XDG_RUNTIME_DIR/hypr" | sort) | head -1)
h(){ hyprctl -i "$N" "$@"; }
WAYLAND_DISPLAY=wayland-2 python3 nested/animwin.py >/dev/null 2>&1 &
A1=$!; sleep 3
h eval 'hl.dispatch(hl.dsp.window.move({ workspace = "2", follow = false }))' >/dev/null
sleep 1
WAYLAND_DISPLAY=wayland-2 python3 nested/animwin.py >/dev/null 2>&1 &
A2=$!; sleep 3
h clients -j | python3 -c "
import json,sys
for c in json.load(sys.stdin): print('client', c['address'], 'ws', c['workspace']['name'], c['size'], 'focusHist', c['focusHistoryID'])"
h plugin load "$PWD/build/libnsproxy.so" >/dev/null
echo "-- plugin attach by class:"; h nsproxy attach animwin; h nsproxy detach >/dev/null
cd ../app
WAYLAND_DISPLAY=wayland-2 HYPRLAND_INSTANCE_SIGNATURE=$N NS_UI=1 NS_PLUGIN=1 NS_UPSCALER=none PYTHONUNBUFFERED=1 python3 live_filter.py animwin >/tmp/dup.log 2>&1 &
LP=$!; sleep 30
echo "-- live_filter:"; grep -a "target window\|nsproxy attach" /tmp/dup.log | cut -c1-200
h nsproxy status | grep -o "exported: [0-9]*"
kill $LP 2>/dev/null; sleep 6; kill $A1 $A2 $NP 2>/dev/null; wait 2>/dev/null
