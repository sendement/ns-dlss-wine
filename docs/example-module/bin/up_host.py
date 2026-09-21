#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reference upscaler host for docs/module-protocol.md: nearest-neighbour resize with a brightness setting (key=value argument)."""
import mmap, struct, sys, time
import numpy as np

iw, ih, ow, oh = (int(a) for a in sys.argv[1:5])
files = [open(p, "r+b") for p in sys.argv[5:8]]
opts = dict(a.split("=", 1) for a in sys.argv[8:])
bright = float(opts.get("brightness", 1.0))
m_in, m_out, ctl = (mmap.mmap(f.fileno(), 0) for f in files)
u32 = lambda off: struct.unpack_from("<I", ctl, off)[0]
src = np.frombuffer(m_in, dtype=np.uint8).reshape(ih, iw, 4)
dst = np.frombuffer(m_out, dtype=np.uint8).reshape(oh, ow, 4)
ys = (np.arange(oh) * ih // oh)[:, None]
xs = (np.arange(ow) * iw // ow)[None, :]
struct.pack_into("<I", ctl, 0, 1)
while not u32(16):
    if u32(4) == u32(8):
        time.sleep(0.0005)
        continue
    frame = src[ys, xs].astype(np.float32)
    frame[..., :3] *= bright
    dst[:] = np.clip(frame, 0, 255).astype(np.uint8)
    dst[..., 3] = 255
    struct.pack_into("<I", ctl, 12, 1)
    struct.pack_into("<I", ctl, 8, u32(4))
