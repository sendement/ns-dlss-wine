"""Sync vs pipelined throughput of the NGX DLSS-G host + correctness of pipelined outputs. Usage: ngxg_pipe_test.py [W H step]"""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from framegen.nsngxg import NgxDlssgFrameGen as _W, NgxVkDlssgFrameGen as _N
NgxDlssgFrameGen = _N if os.environ.get("NS_DLSSG_IMPL", "native") == "native" else _W

W, H = int(sys.argv[1]) if len(sys.argv) > 1 else 1920, int(sys.argv[2]) if len(sys.argv) > 2 else 1080
step = int(sys.argv[3]) if len(sys.argv) > 3 else 8
rng = np.random.default_rng(1)
n = rng.random((H, W + step * 80 + 8, 3), dtype=np.float32)
k = np.ones(7, np.float32) / 7
for _ in range(2):
    n = np.apply_along_axis(lambda r: np.convolve(r, k, "same"), 1, n)
tex = np.clip(n * 255 * 1.6, 0, 255).astype(np.uint8)
def frame(off):
    f = np.empty((H, W, 4), np.uint8); f[..., :3] = tex[:, off:off + W]; f[..., 3] = 255; return f
frames = [frame(step * i) for i in range(60)]
for mode in ("sync", "pipelined"):
    g = NgxDlssgFrameGen(W, H, 1)
    outs = {}; t0 = None; pend = []
    for i, f in enumerate(frames):
        if i == 10: t0 = time.perf_counter()
        pend.append((i, g.begin(f)))
        if mode == "sync" or len(pend) == 2:
            j, tok = pend.pop(0); out = g.end(tok)
            if out and j >= 3 and j in (20, 30, 40):
                outs[j] = out[0].copy()          # keep a few results for the accuracy check (outside the timed loop it would be overwritten by the ring)
    while pend:
        j, tok = pend.pop(0); g.end(tok)
    dt = (time.perf_counter() - t0) / 50 * 1000
    mad = [np.abs(o[..., :3][..., ::-1].astype(np.int16) - frame(step * j - step // 2)[..., :3]).mean() for j, o in outs.items()]
    print(f"{W}x{H} {mode:9s}: {dt:5.1f} ms/frame ({1000/dt:4.0f} fps)  MAD vs true midpoint {np.mean(mad):.2f}")
    g.close()
