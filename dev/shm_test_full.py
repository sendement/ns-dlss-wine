# SPDX-License-Identifier: MIT
"""Full SHM round-trip test (bridge launched via subprocess.Popen, matching
how live_filter.py would actually do it) using the fixed shm_bridge.exe
(quit-file based idle, not stdin-EOF based). Run this multiple times to
check reliability now that the real bug (premature bridge exit) is fixed.
"""
import struct, subprocess, sys, os, time, threading, mmap

HEADER_FMT = "<10I4f2I"
FRAME_FMT = "<4Iq"
OUT_FMT = "<5Iq"
SHM_MAGIC = 0x494D4853
SHM_FMT = "<4Iq64s"
OUTS_MAGIC = 0x5354554F
OUTS_FMT = "<4Iq64s"
VIDEO_MAGIC = 0x33563544
FRAME_MAGIC = 0x314D5246
OUT_MAGIC = 0x3154554F
FRAME_FLAG_SHM = 0x1
OUT_BYTES_IN_SHM = 0xFFFFFFFF

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNTIME = os.path.join(ROOT, "runtime")   # see app/paths.py
W, H = 256, 256
COLOR_BYTES = W * H * 4
MOTION_BYTES = W * H * 4
OUT_BYTES = W * H * 4

env = dict(os.environ)
env["WINEPREFIX"] = os.path.join(RUNTIME, "prefix")
env["__NV_PRIME_RENDER_OFFLOAD"] = "1"
env["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
env["VK_ICD_FILENAMES"] = "/usr/share/vulkan/icd.d/nvidia_icd.json"
env["WINEDLLOVERRIDES"] = "dxgi,d3d12,d3d12core=n"
wine = os.path.expanduser(
    "~/.local/share/Steam/steamapps/common/Proton - Experimental/files/bin/wine"
)

os.makedirs("/tmp/ns-shm-test", exist_ok=True)
IN_PATH = "/tmp/ns-shm-test/in.bin"
OUT_PATH = "/tmp/ns-shm-test/out.bin"
for p in (IN_PATH, IN_PATH + ".quit", OUT_PATH):
    try:
        os.remove(p)
    except FileNotFoundError:
        pass

bridge = subprocess.Popen(
    [wine, "shm_bridge.exe",
     r"Z:\tmp\ns-shm-test\in.bin", str(COLOR_BYTES + MOTION_BYTES), "NS_TEST_IN",
     r"Z:\tmp\ns-shm-test\out.bin", str(OUT_BYTES + 8), "NS_TEST_OUT"],
    cwd=os.path.join(RUNTIME, "artifacts"),
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
)

def drain(proc, tag, keywords):
    for line in iter(proc.stderr.readline, b""):
        s = line.decode(errors="replace")
        if any(k in s for k in keywords):
            sys.stderr.write(f"[{tag}] " + s)

threading.Thread(target=drain, args=(bridge, "bridge", ("section", "fail")), daemon=True).start()
ready = bridge.stdout.readline()
print("bridge:", ready.strip())

proc = subprocess.Popen(
    [wine, "nvngx.dll", "--live"],
    cwd=os.path.join(RUNTIME, "worker"),
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
)
threading.Thread(target=drain, args=(proc, "worker",
                  ("host]", "pure]", "SHM", "shm", "video]"))).start()

header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, 0, 0, 0, 0, 1, 1, 0,
                      1.0, 0.5, 1.0, -1.0, 0, 0)
proc.stdin.write(header)
proc.stdin.flush()
time.sleep(2.0)

proc.stdin.write(struct.pack(SHM_FMT, SHM_MAGIC, COLOR_BYTES, MOTION_BYTES, 0, 0, b"NS_TEST_IN"))
proc.stdin.flush()
time.sleep(0.2)
proc.stdin.write(struct.pack(OUTS_FMT, OUTS_MAGIC, W, H, 0, 0, b"NS_TEST_OUT"))
proc.stdin.flush()
time.sleep(0.2)

with open(IN_PATH, "r+b") as f:
    mm = mmap.mmap(f.fileno(), 0)
    px = bytes([200, 150, 100, 255]) * (W * H)
    mm[0:len(px)] = px
    mm[COLOR_BYTES:COLOR_BYTES + MOTION_BYTES] = bytes(MOTION_BYTES)
    mm.flush()
    mm.close()
print("wrote test pattern via native Linux mmap")

out_f = open(OUT_PATH, "rb")
out_mm = mmap.mmap(out_f.fileno(), 0, prot=mmap.PROT_READ)

def read_exact(n):
    buf = b""
    while len(buf) < n:
        chunk = proc.stdout.read(n - len(buf))
        if not chunk:
            raise EOFError("closed")
        buf += chunk
    return buf

results = []

def reader_loop():
    n = 0
    while n < 12:
        try:
            magic_raw = read_exact(4)
        except EOFError:
            return
        magic = struct.unpack("<I", magic_raw)[0]
        if magic == OUT_MAGIC:
            rest = read_exact(struct.calcsize(OUT_FMT) - 4)
            _m, idx, ok, byte_count, ngx_result, pts = struct.unpack(OUT_FMT, magic_raw + rest)
            print(f"OUT1: index={idx} ok={ok} byte_count={byte_count} ngx_result=0x{ngx_result:08X}", flush=True)
            if byte_count == OUT_BYTES_IN_SHM:
                seq1 = int.from_bytes(out_mm[0:8], "little")
                data = bytes(out_mm[8:8 + 16])
                print(f"  SHM out seq={seq1} first pixel={tuple(data[0:4])}", flush=True)
                results.append(tuple(data[0:4]))
            elif byte_count:
                data = read_exact(byte_count)
                print(f"  pipe out first pixel={tuple(data[0:4])}", flush=True)
                results.append(tuple(data[0:4]))
        else:
            rest = read_exact(20)
            vals = struct.unpack("<4Iq", magic_raw + rest)
            print(f"other reply magic=0x{magic:08X} ({magic.to_bytes(4,'little')}) fields={vals}", flush=True)
        n += 1

threading.Thread(target=reader_loop, daemon=True).start()

for i in range(5):
    proc.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, i, int(i == 0), FRAME_FLAG_SHM, i))
    proc.stdin.flush()
    time.sleep(0.3)

time.sleep(1)
out_mm.close()
out_f.close()
try:
    proc.stdin.close()
except Exception:
    pass
open(IN_PATH + ".quit", "w").close()  # tell the bridge to exit
for p in (proc, bridge):
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()

print("results:", results)
sys.exit(0 if results else 1)
