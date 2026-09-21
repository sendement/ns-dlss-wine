#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Builds the two patched Wine-side libraries from upstream sources + the patches in patches/ (they are derivative works of LGPL / MIT code, so we ship
# the patches and this recipe instead of binaries):
#   vkd3d-proton (LGPL-2.1-or-later)  + vkd3d-proton-shared-heaps.patch  -> d3d12.dll, d3d12core.dll   (D3D12 shared heaps that CUDA can import)
#   dxvk-nvapi   (MIT)                + dxvk-nvapi-nvof-bgra.patch       -> nvofapi64.dll              (NV Optical Flow BGRA input format)
# Results go to runtime/artifacts/. Needs: git, meson, ninja, mingw-w64 (gcc + g++, posix threads), glslang, and whatever the upstream projects list.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/runtime/build-src"; ART="$ROOT/runtime/artifacts"; mkdir -p "$SRC" "$ART"

build_one() { # name url patchfile basefile
  local name="$1" url="$2" patch="$3" base="$4" rev
  rev="$(tr -d '[:space:]' < "$ROOT/patches/$base")"
  [ -d "$SRC/$name/.git" ] || git clone "$url" "$SRC/$name"
  git -C "$SRC/$name" fetch --quiet origin
  git -C "$SRC/$name" checkout --quiet -f "$rev"
  git -C "$SRC/$name" submodule update --init --recursive --quiet
  git -C "$SRC/$name" reset --hard --quiet "$rev"
  git -C "$SRC/$name" apply "$ROOT/patches/$patch"
  echo "$name @ $rev + $patch"
}

build_one vkd3d-proton https://github.com/HansKristian-Work/vkd3d-proton vkd3d-proton-shared-heaps.patch vkd3d-proton.base
( cd "$SRC/vkd3d-proton" && ./package-release.sh nsproxy "$SRC/out" --no-package )
cp "$SRC/out/vkd3d-proton-nsproxy/x64/d3d12.dll" "$SRC/out/vkd3d-proton-nsproxy/x64/d3d12core.dll" "$ART/"

build_one dxvk-nvapi https://github.com/jp7677/dxvk-nvapi dxvk-nvapi-nvof-bgra.patch dxvk-nvapi.base
( cd "$SRC/dxvk-nvapi" && ./package-release.sh nsproxy "$SRC/out" --no-package )
cp "$SRC/out/dxvk-nvapi-nsproxy/x64/nvofapi64.dll" "$ART/"
echo "done: $(ls "$ART" | tr '\n' ' ')"
