"""Frame-generation module smoke test on a scrolling texture. Usage: module_fg_test.py KEY [W H count step]"""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import framegen

key = sys.argv[1]
W, H = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (1280, 720)
count = int(sys.argv[4]) if len(sys.argv) > 4 else 1
step = int(sys.argv[5]) if len(sys.argv) > 5 else 8
rng = np.random.default_rng(1)
n = rng.random((H, W + step * 40 + 8, 3), dtype=np.float32)
k = np.ones(7, np.float32) / 7
for _ in range(2):
    n = np.apply_along_axis(lambda r: np.convolve(r, k, "same"), 1, n)
tex = np.clip(n * 255 * 1.6, 0, 255).astype(np.uint8)
def frame(off):
    f = np.empty((H, W, 4), np.uint8); f[..., :3] = tex[:, off:off + W]; f[..., 3] = 255; return f
frames = [frame(step * i) for i in range(30)]
cfg = framegen.FrameGenSettings(method=key, multiplier=count + 1)
b = framegen.REGISTRY[key](W, H, cfg)
bgra = getattr(b, "output_bgra", False)
ts = [j / (count + 1) for j in range(1, count + 1)]
outs = {}; t0 = None; pend = []
for i, f in enumerate(frames):
    if i == 8: t0 = time.perf_counter()
    if b.pipelined:
        pend.append((i, b.begin(f, ts)))
        if len(pend) == 2:
            j, tok = pend.pop(0); o = b.finish(tok)
            if o and j in (12, 20): outs[j] = [x.copy() for x in o]
    else:
        o = b.submit(f, ts)
        if o and i in (12, 20): outs[i] = [x.copy() for x in o]
while pend:
    j, tok = pend.pop(0); o = b.finish(tok)
    if o and j in (12, 20): outs[j] = [x.copy() for x in o]
dt = (time.perf_counter() - t0) / (len(frames) - 8) * 1000
mad = []
for j, os_ in outs.items():
    for i, o in enumerate(os_):
        a = (o[..., :3][..., ::-1] if bgra else o[..., :3]).astype(np.int16)
        mad.append(np.abs(a - frame(step * j - step + int(step * (i + 1) / (count + 1)))[..., :3]).mean())
print(f"{key} {W}x{H} x{count} step {step}: {dt:.1f} ms/frame, MAD vs true frame {np.mean(mad):.2f}")
b.close()
