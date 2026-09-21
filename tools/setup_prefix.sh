#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Creates the isolated Wine prefix (runtime/prefix) the workers run in, as a COPY of an already-initialised Proton prefix (the source is never modified).
#
# Why a copy of a Proton prefix: NGX's internal D3D12/DXGI interop checks only pass with ONE Proton build's own matched DXVK + vkd3d-proton pair; a
# hand-mixed pair (a separately downloaded DXVK next to a distro vkd3d-proton) breaks them even though D3D12CreateDevice itself works.
#
# Usage: tools/setup_prefix.sh [source_pfx] [dest_dir]
#   source_pfx: a Proton prefix that Steam already initialised (steamapps/compatdata/<appid>/pfx) - any Proton game you have run once.
#               Default: the most recently used one found in your Steam library.
#   dest_dir:   default runtime/prefix
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STEAM="$HOME/.local/share/Steam"
SRC="${1:-}"
DST="${2:-$ROOT/runtime/prefix}"

if [ -z "$SRC" ]; then
  # newest initialised prefix that has Proton's own d3d12 in it
  SRC="$(ls -dt "$STEAM"/steamapps/compatdata/*/pfx 2>/dev/null | while read -r p; do
           [ -f "$p/drive_c/windows/system32/d3d12.dll" ] && { echo "$p"; break; }; done)"
fi
if [ -z "$SRC" ] || [ ! -d "$SRC/drive_c" ]; then
  echo "no initialised Proton prefix found. Run any Proton game from Steam once, or pass one: tools/setup_prefix.sh <path-to-pfx>" >&2
  exit 1
fi
echo "copying $SRC -> $DST"
rm -rf "$DST"; mkdir -p "$DST"
cp -a "$SRC/." "$DST/"
echo "done: $DST"
