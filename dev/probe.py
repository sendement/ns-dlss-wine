#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Minimal litmus test for the NeuralScreen DLSS5 worker under Wine.

Launches the worker (native/nvngx.dll, disguised as a Windows DLL - it's
actually dlss5-feed-host64.cpp, a console EXE) in --live mode, sends a
synthetic 512x512 frame over its stdin/stdout wire protocol, and checks
that NVSDK_NGX_D3D12_Init, Init_Ext and CreateFeature(18) all succeed and
that real (non-echoed) pixel data comes back.

Requires setup_prefix.sh to have been run first (or WINEPREFIX to point at
some other already-Proton-initialized prefix with a matched DXVK+vkd3d-proton
pair - see README.md for why this matters).
"""
import os
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNTIME = os.path.join(ROOT, "runtime")   # see app/paths.py

HEADER_FMT = "<10I4f2I"
FRAME_FMT = "<4Iq"
OUT_FMT = "<5Iq"
VIDEO_MAGIC = 0x33563544
FRAME_MAGIC = 0x314D5246
OUT_MAGIC = 0x3154554F

W, H = 512, 512
FRAMES_TO_SEND = 8

PROTON_WINE = os.environ.get(
    "NS_WINE",
    os.path.expanduser(
        "~/.local/share/Steam/steamapps/common/Proton - Experimental/files/bin/wine"
    ),
)
WINEPREFIX = os.environ.get("NS_WINEPREFIX", os.path.join(RUNTIME, "prefix"))
WORKER_DIR = os.path.join(RUNTIME, "worker")


def main():
    if not os.path.isdir(WINEPREFIX):
        sys.exit(f"no prefix at {WINEPREFIX} - run ./setup_prefix.sh first")

    env = dict(os.environ)
    env["WINEPREFIX"] = WINEPREFIX
    env.setdefault("__NV_PRIME_RENDER_OFFLOAD", "1")
    env.setdefault("__GLX_VENDOR_LIBRARY_NAME", "nvidia")
    env.setdefault("VK_ICD_FILENAMES", "/usr/share/vulkan/icd.d/nvidia_icd.json")
    env["WINEDLLOVERRIDES"] = "dxgi,d3d12,d3d12core=n"

    proc = subprocess.Popen(
        [PROTON_WINE, "nvngx.dll", "--live"],
        cwd=WORKER_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    def drain_stderr():
        for line in iter(proc.stderr.readline, b""):
            sys.stderr.buffer.write(b"[worker] " + line)
            sys.stderr.flush()

    threading.Thread(target=drain_stderr, daemon=True).start()

    header = struct.pack(
        HEADER_FMT, VIDEO_MAGIC, W, H, 0, 0, 0, 0, 1, 1, 0, 1.0, 0.5, 1.0, -1.0, 0, 0
    )
    proc.stdin.write(header)
    proc.stdin.flush()

    rgba = bytes([128, 64, 32, 255]) * (W * H)
    motion = bytes(W * H * 2 * 2)  # float16 x2 channels, all zero

    def read_exact(n):
        buf = b""
        while len(buf) < n:
            chunk = proc.stdout.read(n - len(buf))
            if not chunk:
                raise EOFError(f"worker closed stdout after {len(buf)}/{n} bytes")
            buf += chunk
        return buf

    replies = {"ok": 0, "fail": 0}

    def reader_loop():
        n = 0
        while True:
            try:
                magic_raw = read_exact(4)
            except EOFError:
                print("[reader] stdout closed")
                return
            magic = struct.unpack("<I", magic_raw)[0]
            if magic == OUT_MAGIC:
                rest = read_exact(struct.calcsize(OUT_FMT) - 4)
                _m, idx, ok, byte_count, ngx_result, pts = struct.unpack(
                    OUT_FMT, magic_raw + rest
                )
                print(
                    f"OUT1: index={idx} ok={ok} byte_count={byte_count} "
                    f"ngx_result=0x{ngx_result:08X}",
                    flush=True,
                )
                replies["ok" if ok and ngx_result == 1 else "fail"] += 1
                if byte_count and byte_count != 0xFFFFFFFF:
                    data = read_exact(byte_count)
                    print(
                        f"  {len(data)} bytes back, first pixel: "
                        f"{tuple(data[0:4])}",
                        flush=True,
                    )
            else:
                rest = read_exact(20)
                vals = struct.unpack("<4Iq", magic_raw + rest)
                print(
                    f"other reply magic=0x{magic:08X} "
                    f"({magic.to_bytes(4, 'little')}) fields={vals}",
                    flush=True,
                )
            n += 1
            if n > FRAMES_TO_SEND * 2:
                return

    threading.Thread(target=reader_loop, daemon=True).start()

    for i in range(FRAMES_TO_SEND):
        proc.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, i, int(i == 0), 0, i))
        proc.stdin.write(rgba)
        proc.stdin.write(motion)
        proc.stdin.flush()
        time.sleep(0.3)

    time.sleep(1)
    proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    print(f"worker exit code: {proc.returncode}")
    print(f"frames ok={replies['ok']} fail={replies['fail']} of {FRAMES_TO_SEND}")
    sys.exit(0 if replies["ok"] == FRAMES_TO_SEND else 1)


if __name__ == "__main__":
    main()
