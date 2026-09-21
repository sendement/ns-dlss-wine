#!/bin/bash
# SPDX-License-Identifier: MIT
# Fault-shield regression test. Runs ONLY against a NESTED Hyprland it starts itself (never the real session).
set -u
cd "$(dirname "$0")"
(cd build && ninja >/dev/null)
before=$(ls "$XDG_RUNTIME_DIR/hypr" | sort)
Hyprland -c "$PWD/nested/hyprland.lua" >nested/nested.log 2>&1 &
NP=$!; sleep 6
N=$(comm -13 <(echo "$before") <(ls "$XDG_RUNTIME_DIR/hypr" | sort) | head -1)
[ -n "$N" ] || { echo "nested instance not found"; kill $NP; exit 1; }
h(){ hyprctl -i "$N" "$@"; }
h plugin load "$PWD/build/libnsproxy.so" >/dev/null; sleep 1
fail=0
for t in throw throw_int segv trap abort render_throw render_segv render_abort; do
  h nsproxy reset >/dev/null; h nsproxy selftest $t >/dev/null; sleep 0.5
  s=$(h nsproxy status)
  kill -0 $NP 2>/dev/null || { echo "FAIL $t: compositor died"; fail=1; break; }
  echo "$t -> $s" | head -1
done
kill $NP; exit $fail
