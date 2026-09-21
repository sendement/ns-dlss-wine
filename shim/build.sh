#!/bin/sh
# SPDX-License-Identifier: MIT
# Generates and builds the nvcuda shim (PE half + unix half) and installs it into runtime/wine_nvcuda/ (WINEDLLPATH) and runtime/artifacts/.
# Needs: the NVIDIA driver's /usr/lib/libcuda.so.1 (function list), mingw-w64 gcc, gcc, python3.
set -e
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
OUT="$ROOT/runtime/wine_nvcuda"
mkdir -p "$OUT/x86_64-windows" "$OUT/x86_64-unix" "$ROOT/runtime/artifacts"
python3 gen.py >/dev/null
x86_64-w64-mingw32-gcc -shared -O2 -o nvcudashim.dll pe_stubs.c nvcuda.def -static-libgcc -lkernel32 -luuid
gcc -shared -fPIC -O2 -o nvcudashim.so unix_side.c -ldl
# Wine loads the PE half as a "fake builtin" (and then finds nvcudashim.so next to it) only when the 32 bytes at offset 0x40 hold this signature.
python3 - <<'PY'
d = bytearray(open('nvcudashim.dll', 'rb').read())
d[0x40:0x60] = b'Wine builtin DLL'.ljust(32, b'\0')
open('nvcudashim.dll', 'wb').write(d)
PY
# the native forwarder that replaces Proton's builtin nvcuda.dll in the prefix: it re-exports everything to nvcudashim (installed by app/userfiles.py)
x86_64-w64-mingw32-gcc -shared -nostdlib -Wl,--entry=DllMain -o nvcuda.dll fwd.c nvcuda_fwd.def -lkernel32
cp nvcuda.dll "$OUT/nvcuda_forwarder.dll"
cp nvcudashim.dll "$OUT/x86_64-windows/"
cp nvcudashim.so "$OUT/x86_64-unix/"
cp nvcudashim.dll "$ROOT/runtime/artifacts/"
rm -f nvcudashim.dll nvcudashim.so nvcuda.dll pe_stubs.c unix_side.c nvcuda.def nvcuda_fwd.def
