#!/bin/bash
# SPDX-License-Identifier: MIT
# End-to-end: nested Hyprland + plugin + animated window + live_filter with DLSS-G x2 (NS_FRAMEGEN=dlssg:2). Prints the pipeline profile.
cd "$(dirname "$0")/.."
LOG=${LOG:-/tmp/live_fg.log}
sed -i 's/mode = "2560x1440@60"/mode = "1600x900@60"/' nested/hyprland.lua
before=$(ls "$XDG_RUNTIME_DIR/hypr" | sort)
Hyprland -c "$PWD/nested/hyprland.lua" >nested/nested.log 2>&1 &
NP=$!; sleep 7
N=$(comm -13 <(echo "$before") <(ls "$XDG_RUNTIME_DIR/hypr" | sort) | head -1)
h(){ hyprctl -i "$N" "$@"; }
WAYLAND_DISPLAY=wayland-2 python3 nested/animwin.py >/dev/null 2>&1 &
AP=$!; sleep 3
h plugin load "$PWD/build/libnsproxy.so" >/dev/null
cd ../app
WAYLAND_DISPLAY=wayland-2 HYPRLAND_INSTANCE_SIGNATURE=$N NS_UI=1 NS_PLUGIN=1 NS_UPSCALER=${UP:-none} NS_FRAMEGEN=${FG:-dlssg:2} NS_PROFILE=1 PYTHONUNBUFFERED=1 \
  python3 live_filter.py animwin >"$LOG" 2>&1 &
LP=$!
sleep ${RUN_SECONDS:-60}
h nsproxy status | cut -c1-200
grep -a "prof\|framegen=\|Traceback\|Error\|error" "$LOG" | tail -6 | cut -c1-260
WAYLAND_DISPLAY=wayland-2 grim -s 0.4 /tmp/live_fg.png
kill $LP 2>/dev/null; sleep 6; kill $AP 2>/dev/null; kill $NP 2>/dev/null; wait 2>/dev/null
