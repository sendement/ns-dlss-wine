#!/bin/bash
# SPDX-License-Identifier: MIT
# Hot-reload stress test: NESTED Hyprland under gdb, animated window, N x (load, attach, pipeline client running, unload).
# The unload happens while the client is connected and frames are flowing. Prints the number of crashes seen by gdb.
set -u
cd "$(dirname "$0")"
N_CYCLES=${1:-10}
(cd build && ninja >/dev/null)
before=$(ls "$XDG_RUNTIME_DIR/hypr" | sort)
gdb -batch -ex "handle SIGUSR1 SIGUSR2 nostop noprint pass" -ex run -ex bt --args Hyprland -c "$PWD/nested/hyprland.lua" > nested/gdb.log 2>&1 &
GP=$!
sleep 12
N=$(comm -13 <(echo "$before") <(ls "$XDG_RUNTIME_DIR/hypr" | sort) | head -1)
[ -n "$N" ] || { echo "nested instance not found"; exit 1; }
h(){ hyprctl -i "$N" "$@"; }
WAYLAND_DISPLAY=wayland-2 python3 nested/animwin.py >/dev/null 2>&1 &
AP=$!; sleep 3
for i in $(seq 1 "$N_CYCLES"); do
  h plugin load "$PWD/build/libnsproxy.so" >/dev/null
  h nsproxy attach animwin >/dev/null
  python3 nested/sink.py 3 >/dev/null 2>&1 &
  SP=$!; sleep 1.5
  h plugin unload "$PWD/build/libnsproxy.so" >/dev/null      # client still connected, frames flowing
  wait $SP 2>/dev/null
  sleep 0.3
done
sleep 1
crashes=$(grep -a -c "SIGSEGV\|SIGABRT" nested/gdb.log)
alive=$(pgrep -f "[H]yprland -c" | head -1)
echo "cycles: $N_CYCLES, gdb signals: $crashes, compositor alive: ${alive:+yes}"
kill $AP 2>/dev/null; kill $alive 2>/dev/null; sleep 1; kill $GP 2>/dev/null
[ "$crashes" = 0 ] && [ -n "$alive" ]
