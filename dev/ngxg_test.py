"""NGX DLSS-G host test: scrolling noise texture, x-shift per frame; compares the generated frame with the true midpoint. Usage: ngxg_test.py [W H count step]"""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from framegen.nsngxg import NgxDlssgFrameGen as _W, NgxVkDlssgFrameGen as _N
NgxDlssgFrameGen = _N if os.environ.get("NS_DLSSG_IMPL", "native") == "native" else _W

W, H = int(sys.argv[1]) if len(sys.argv) > 1 else 1280, int(sys.argv[2]) if len(sys.argv) > 2 else 720
count = int(sys.argv[3]) if len(sys.argv) > 3 else 1
step = int(sys.argv[4]) if len(sys.argv) > 4 else 8
rng = np.random.default_rng(1)
n = rng.random((H, W + step * 40 + 8, 3), dtype=np.float32)
k = np.ones(7, np.float32) / 7
for _ in range(2):
    n = np.apply_along_axis(lambda r: np.convolve(r, k, "same"), 1, n)
tex = np.clip(n * 255 * 1.6, 0, 255).astype(np.uint8)
def frame(off, ch=4):
    f = np.empty((H, W, 4), np.uint8); f[..., :3] = tex[:, off:off + W]; f[..., 3] = 255; return f
g = NgxDlssgFrameGen(W, H, count=count)
mad = {"cur": [], "prev": [], "mid": []}; times = []
for i in range(30):
    cur = frame(step * i)
    t0 = time.perf_counter(); out = g.submit(cur); times.append((time.perf_counter() - t0) * 1000)
    if not out or i < 3:
        continue
    for j, o in enumerate(out):
        ref = frame(step * i - step + int(step * (j + 1) / (count + 1)))
        a = o[..., :3][..., ::-1].astype(np.int16)   # BGRA -> RGB
        mad["mid"].append(np.abs(a - ref[..., :3]).mean()); mad["cur"].append(np.abs(a - cur[..., :3]).mean())
        mad["prev"].append(np.abs(a - frame(step * i - step)[..., :3]).mean())
print(f"{W}x{H} x{count} step {step}: submit {np.mean(times[3:]):.1f} ms | MAD vs true frame {np.mean(mad['mid']):.2f}, vs current {np.mean(mad['cur']):.2f}, vs previous {np.mean(mad['prev']):.2f}")
g.close()
