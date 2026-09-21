#!/bin/bash
# SPDX-License-Identifier: MIT
# Re-attach test: live_filter runs on window A, then the plugin is re-attached to window B (as the panel's target picker does).
cd "$(dirname "$0")/.."
sed -i 's/mode = "2560x1440@60"/mode = "1600x900@60"/' nested/hyprland.lua
before=$(ls "$XDG_RUNTIME_DIR/hypr" | sort)
Hyprland -c "$PWD/nested/hyprland.lua" >nested/nested.log 2>&1 &
NP=$!; sleep 7
N=$(comm -13 <(echo "$before") <(ls "$XDG_RUNTIME_DIR/hypr" | sort) | head -1)
h(){ hyprctl -i "$N" "$@"; }
WAYLAND_DISPLAY=wayland-2 python3 nested/animwin.py >/dev/null 2>&1 &
AP=$!; sleep 2
h plugin load "$PWD/build/libnsproxy.so" >/dev/null
cd ../app
WAYLAND_DISPLAY=wayland-2 HYPRLAND_INSTANCE_SIGNATURE=$N NS_UI=1 NS_PLUGIN=1 NS_UPSCALER=none PYTHONUNBUFFERED=1 python3 live_filter.py animwin >/tmp/retarget.log 2>&1 &
LP=$!
sleep 30
echo "-- attached to A:"; h nsproxy status | grep -o "attached: [a-z]*, client: [a-z]*, .*post_window" | cut -c1-80
WAYLAND_DISPLAY=wayland-2 python3 ../hyprplug/nested/subwin.py >/dev/null 2>&1 &
BP=$!; sleep 3
echo "-- exact/substring: attach 'subwin':"; h nsproxy attach subwin
sleep 5
h nsproxy status | grep -o "attached: [a-z]*, client: [a-z]*" ; h nsproxy status | grep -o "zero-copy exports: [0-9]*"
echo "-- back to A:"; h nsproxy attach animwin; sleep 4; h nsproxy status | grep -o "attached: [a-z]*, client: [a-z]*, "
h nsproxy attach nonexistent-window
h nsproxy status | cut -c1-40
kill $LP 2>/dev/null; sleep 6; kill $AP $BP $NP 2>/dev/null; wait 2>/dev/null
