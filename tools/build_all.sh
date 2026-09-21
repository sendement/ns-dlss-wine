#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Builds everything that can be built from this repository. Each step is independent and is skipped with a message when its toolchain / sources are missing.
#   native libraries of the app (GLES/EGL, OpenMP)      : cc, libEGL/libGLESv2/libdrm headers
#   Hyprland plugin                                      : hyprland headers (pkg-config hyprland), cmake, ninja
#   Windows hosts (run under Wine) and the nvcuda shim   : x86_64-w64-mingw32-g++ / -gcc   -> runtime/artifacts/, runtime/wine_nvcuda/
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ART="$ROOT/runtime/artifacts"; mkdir -p "$ART"
ok=0; skipped=0; failed=0
step() { # step "name" command...
  local name="$1" log; shift
  log="$ROOT/runtime/build-$(echo "$name" | tr -c 'A-Za-z0-9\n' '_').log"
  if "$@" >"$log" 2>&1; then echo "[ ok ] $name"; ok=$((ok+1)); else echo "[FAIL] $name  (log: ${log#$ROOT/})"; failed=$((failed+1)); fi
}
skip() { echo "[skip] $1 - $2"; skipped=$((skipped+1)); }
have() { command -v "$1" >/dev/null 2>&1; }

A="$ROOT/app"
if have cc; then
  step "app/nis"          cc -O2 -shared -fPIC -o "$A/nis/libnisupscale.so"        "$A/nis/nis_lib.c"          -lEGL -lGLESv2 -lm
  step "app/fsr (FSR1)"   cc -O2 -shared -fPIC -o "$A/fsr/libfsrupscale.so"        "$A/fsr/fsr_lib.c"          -lEGL -lGLESv2 -lm
  step "app/post"         cc -O3 -shared -fPIC -fopenmp -o "$A/post/libpost.so"    "$A/post/post_lib.c"        -lm
  step "app/nsmem"        cc -O2 -shared -fPIC -o "$A/nsmem/libnsmem.so"           "$A/nsmem/nsmem.c"
else skip "native libraries" "no C compiler (cc)"; fi


if have pkg-config && pkg-config --exists hyprland && have cmake; then
  step "hyprplug (Hyprland plugin)" bash -c "mkdir -p '$ROOT/hyprplug/build' && cd '$ROOT/hyprplug/build' && cmake .. -G Ninja -DCMAKE_BUILD_TYPE=Release && ninja"
else skip "Hyprland plugin" "hyprland development headers / cmake not found"; fi

if have x86_64-w64-mingw32-g++; then
  CXX="x86_64-w64-mingw32-g++"
  step "hosts/dlssg_host.exe"   bash -c "$CXX -O2 -std=c++17 -o '$ART/dlssg_host.exe' '$ROOT/hosts/dlssg_host.cpp' -static"
  NGX="$ROOT/third_party/nvidia-dlss/include"
  if [ -d "$NGX" ]; then
    step "hosts/ngxdlssg_host.exe" bash -c "$CXX -O2 -std=c++17 -mavx2 -mf16c -I'$NGX' -o '$ART/ngxdlssg_host.exe' '$ROOT/hosts/ngxdlssg_host.cpp' -static -ldxguid"
  else skip "hosts/ngxdlssg_host.exe (DLSS-G on the public NGX API)" "run tools/fetch_third_party.sh (fetches NVIDIA/DLSS headers)"; fi
  NGXL="$ROOT/third_party/nvidia-dlss/lib/Linux_x86_64"
  if [ -d "$NGX" ] && [ -f "$NGXL/libnvsdk_ngx.a" ] && have g++; then
    step "hosts/ngxdlssg_vk_host (native Linux DLSS-G)" bash -c "g++ -O2 -std=c++17 -mavx2 -mf16c -pthread -I'$NGX' -o '$ART/ngxdlssg_vk_host' '$ROOT/hosts/ngxdlssg_vk_host.cpp' '$NGXL/libnvsdk_ngx.a' -ldl -lvulkan && cp -f '$NGXL/rel/'libnvidia-ngx-dlssg.so.* '$ART/'"
  else skip "hosts/ngxdlssg_vk_host (native Linux DLSS-G)" "run tools/fetch_third_party.sh (NVIDIA/DLSS Linux libraries) and install g++ + the Vulkan headers"; fi
  step "hosts/vsr_host.exe"     bash -c "$CXX -O2 -std=c++17 -o '$ART/vsr_host.exe' '$ROOT/hosts/vsr_host.cpp' -static"
  step "hosts/shm_bridge.exe"   bash -c "$CXX -O2 -o '$ART/shm_bridge.exe' '$ROOT/hosts/shm_bridge.cpp' -static"
  SDK="$ROOT/third_party/fidelityfx-sdk/Kits/FidelityFX"
  if [ -d "$SDK/api/include" ]; then
    step "hosts/fsr3fg_host.exe" bash -c "$CXX -O2 -std=c++17 -mavx2 -mf16c -I'$SDK/api/include' -I'$SDK/framegeneration/include' -o '$ART/fsr3fg_host.exe' '$ROOT/hosts/fsr3fg_host.cpp' -static -ldxguid"
  else skip "hosts/fsr3fg_host.exe (FSR 3.1 frame generation, optional)" "opt-in: run tools/fetch_third_party.sh --with-fsr3"; fi
  step "shim (nvcuda shim for Wine)" sh "$ROOT/shim/build.sh"
else skip "Windows hosts and the nvcuda shim" "mingw-w64 (x86_64-w64-mingw32-g++) not found"; fi

echo; echo "built: $ok, skipped: $skipped, failed: $failed"
echo "next: tools/build_patched_wine_libs.sh (patched vkd3d-proton / dxvk-nvapi), then put your own files into user_files/ and run: python3 app/userfiles.py check"
[ "$failed" -eq 0 ]
