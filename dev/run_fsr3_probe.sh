#!/bin/sh
# SPDX-License-Identifier: MIT
# run an exe from worker_fsr3 under the project's Wine prefix with the same GPU/DLL environment as the DLSS-G host
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export WINEPREFIX="$ROOT/runtime/prefix"
export __NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/nvidia_icd.json
export WINEDLLOVERRIDES="dxgi,d3d11,d3d12,d3d12core,d3dcompiler_47=n"
export WINEDEBUG=${WINEDEBUG:--all}
cd "$ROOT/runtime/worker_fsr3" || exit 1
exec "$HOME/.local/share/Steam/steamapps/common/Proton - Experimental/files/bin/wine" "$@"
