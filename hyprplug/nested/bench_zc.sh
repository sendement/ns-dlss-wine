#!/bin/bash
# SPDX-License-Identifier: MIT
# Copy path vs zero-copy path under an animated 2560x1440 nested session; identity pipeline with a content integrity check.
cd "$(dirname "$0")/.."
sed -i 's/mode = "1280x720@60"/mode = "2560x1440@60"/' nested/hyprland.lua
run() {
  zc=$1
  before=$(ls "$XDG_RUNTIME_DIR/hypr" | sort)
  NSPROXY_ZEROCOPY=$zc NSPROXY_CLFLUSH=${CLF:-} Hyprland -c "$PWD/nested/hyprland.lua" >nested/nested.log 2>&1 &
  NP=$!; sleep 6
  N=$(comm -13 <(echo "$before") <(ls "$XDG_RUNTIME_DIR/hypr" | sort) | head -1)
  h(){ hyprctl -i "$N" "$@"; }
  WAYLAND_DISPLAY=wayland-2 python3 nested/animwin.py >/dev/null 2>&1 &
  AP=$!; sleep 3
  h plugin load "$PWD/build/libnsproxy.so" >/dev/null; h nsproxy attach animwin >/dev/null
  echo "== zero-copy=$zc"
  python3 nested/sink_check.py 8
  h nsproxy status | grep -o "tex: [0-9x]*\|avg export [0-9]* us, avg draw [0-9]* us\|zero-copy draws: [0-9]*, zero-copy exports: [0-9]*"
  kill -9 $AP 2>/dev/null; kill $NP 2>/dev/null; wait $NP 2>/dev/null; sleep 2
}
[ -n "$SKIP0" ] || run 0
run 1
