#!/bin/sh
# SPDX-License-Identifier: MIT
# Run an exe from a runtime/worker_* folder under the project's Wine prefix with the DLSS-G host environment.  Usage: dev/run_wine_host.sh <worker_dir_name> <exe> [args...]
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
W="$1"; shift
export WINEPREFIX="$ROOT/runtime/prefix"
export __NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/nvidia_icd.json
export WINEDLLOVERRIDES="dxgi,d3d11,d3d12,d3d12core,nvcuda,d3dcompiler_47,nvofapi64=n"
export WINEDLLPATH="$ROOT/runtime/wine_nvcuda"
export WINEDEBUG=${WINEDEBUG:--all}
cd "$ROOT/runtime/$W" || exit 1
WINE="${NS_WINE:-$HOME/.local/share/Steam/steamapps/common/Proton - Experimental/files/bin/wine}"
exec "$WINE" "$@" < /dev/null
