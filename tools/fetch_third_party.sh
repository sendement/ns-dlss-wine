#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Clones the third-party SOURCES this project builds against into third_party/ (git-ignored), at the exact revisions it was developed with.
# Nothing proprietary is downloaded. (Optional GPL-licensed modules such as ns-mako are fetched by tools/fetch_modules.sh and fetch what they need themselves.)
# Optional: --with-fsr3  also fetches the header part of AMD's FidelityFX SDK (per-file MIT licences), needed only to build the FSR 3.1 frame-generation host.
WITH_FSR3=0; for a in "$@"; do [ "$a" = "--with-fsr3" ] && WITH_FSR3=1; done
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; TP="$ROOT/third_party"; mkdir -p "$TP"


FFX_TAG=v2.3.0
if [ "$WITH_FSR3" = 1 ]; then
if [ ! -d "$TP/fidelityfx-sdk/.git" ]; then
  git clone --depth 1 --branch "$FFX_TAG" --filter=blob:none --sparse https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK "$TP/fidelityfx-sdk"
fi
git -C "$TP/fidelityfx-sdk" sparse-checkout set Kits/FidelityFX/api/include Kits/FidelityFX/framegeneration/include
echo "FidelityFX SDK headers @ $FFX_TAG (the signed DLL is NOT fetched - see user_files/README.md)"
else echo "FSR 3.1 frame generation not requested (add --with-fsr3 to fetch the FidelityFX SDK headers)"; fi

# NVIDIA DLSS repo (NVIDIA RTX SDK licence: build against the headers and use the DLLs; do not redistribute them - they are fetched here, not shipped).
# `include/` = the public NGX headers the DLSS-G host (hosts/ngxdlssg_host.cpp) is built against; `lib/Windows_x86_64/rel/nvngx_dlssg.dll` = the DLSS-G snippet.
DLSS_REV=374959484e79
if [ ! -d "$TP/nvidia-dlss/.git" ]; then
  git clone --filter=blob:none --sparse https://github.com/NVIDIA/DLSS "$TP/nvidia-dlss"
fi
git -C "$TP/nvidia-dlss" sparse-checkout set include lib/Windows_x86_64/rel lib/Linux_x86_64
git -C "$TP/nvidia-dlss" checkout --quiet "$DLSS_REV" 2>/dev/null || true
echo "NVIDIA DLSS headers + nvngx_dlssg.dll @ $DLSS_REV (copy lib/Windows_x86_64/rel/nvngx_dlssg.dll into user_files/ - see user_files/README.md)"
