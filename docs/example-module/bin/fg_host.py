#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reference frame-generation host for docs/module-protocol.md: a 50/50-style crossfade at the requested timestamps. Copy it as a starting point."""
import mmap, struct, sys, time
import numpy as np

w, h, max_count = (int(a) for a in sys.argv[1:4])
files = [open(p, "r+b") for p in sys.argv[4:7]]
m_in, m_out, ctl = (mmap.mmap(f.fileno(), 0) for f in files)
u32 = lambda off: struct.unpack_from("<I", ctl, off)[0]
n = w * h * 4
frames_in = np.frombuffer(m_in, dtype=np.uint8).reshape(2, h, w, 4)
out = np.frombuffer(m_out, dtype=np.uint8).reshape(4, max_count, h, w, 4)
prev = None
struct.pack_into("<I", ctl, 0, 1)                      # ready
next_seq = 1
while not u32(16):
    if next_seq > u32(4):
        time.sleep(0.0005)
        continue
    slot = next_seq % 2
    flags, nts, *ts = struct.unpack_from("<II3f", ctl, 64 + 20 * slot)
    cur = frames_in[slot].astype(np.uint16)
    gen = 0
    if prev is not None and not (flags & 1):
        for i in range(min(nts, max_count)):
            a = ts[i]
            mix = ((prev * int((1 - a) * 256) + cur * int(a * 256)) >> 8).astype(np.uint8)
            out[next_seq % 4, i, :, :, 0] = mix[..., 2]   # this host answers in BGRA (manifest: "output": "bgra")
            out[next_seq % 4, i, :, :, 1] = mix[..., 1]
            out[next_seq % 4, i, :, :, 2] = mix[..., 0]
            out[next_seq % 4, i, :, :, 3] = 255
            gen += 1
    prev = cur
    struct.pack_into("<II", ctl, 104 + 8 * (next_seq % 4), gen, 1)   # result slot first ...
    struct.pack_into("<I", ctl, 8, next_seq)                          # ... then acknowledge
    next_seq += 1
