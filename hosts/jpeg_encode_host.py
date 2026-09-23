# SPDX-License-Identifier: MIT
"""GPU JPEG encode host on torch/torchvision's nvjpeg binding. Runs in its own venv (runtime/venv-jpeg, `python3 app/userfiles.py install jpeg-gpu` - or
just `uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128` into that venv) so the CUDA/torch stack stays out of the main
bridge process, same reasoning as hosts/vsr_native_host.py. Benchmarked ~2.8x faster than PIL/libjpeg-turbo (already the fast CPU codec, confirmed present
on this system) for a 2154x1152 frame, INCLUDING the host<->GPU transfer - nvjpeg's own encode is near-free (~0.3ms), the transfer dominates.

Same three-file mmap protocol shape as vsr_native_host.py, extended for a variable-length output and per-request size/quality (encoding doesn't need a
GPU feature rebuild on a size change the way DLSS5/VSR do, so there's no reason to restart the process for that - just size the buffers for a ceiling):

  jpeg_encode_host.py max_w max_h in_path out_path ctl_path
    in  : RGB8 frame, row-major, up to max_w*max_h*3 bytes (only the first w*h*3 bytes of a request are read)
    out : JPEG bytes, up to max_w*max_h*3 bytes (a real ceiling - compressed output is always far smaller at any sane quality)
    ctl : state(0) req_seq(4) ack_seq(8) ok(12) quit(16) out_len(20) w(24) h(28) quality(32) - uint32 each, 64 bytes total (reserved padding after)
"""
import mmap
import struct
import sys
import time

import numpy as np
import torch
from torchvision.io import encode_jpeg

_STATE, _REQ_SEQ, _ACK_SEQ, _OK, _QUIT, _OUT_LEN, _REQ_W, _REQ_H, _QUALITY = 0, 4, 8, 12, 16, 20, 24, 28, 32
_CTL_SIZE = 64


def main():
    if len(sys.argv) < 6:
        print("usage: jpeg_encode_host.py max_w max_h in_path out_path ctl_path", file=sys.stderr)
        return 2
    max_w, max_h = int(sys.argv[1]), int(sys.argv[2])
    files = [open(p, "r+b") for p in sys.argv[3:6]]
    m_in, m_out, ctl = (mmap.mmap(f.fileno(), 0) for f in files)
    u32 = lambda off: struct.unpack_from("<I", ctl, off)[0]  # noqa: E731

    if not torch.cuda.is_available():
        print("[jpeg-host] CUDA not available", file=sys.stderr)
        struct.pack_into("<I", ctl, _STATE, 2)
        return 3
    device = torch.device("cuda")
    torch.cuda.init()

    struct.pack_into("<I", ctl, _STATE, 1)
    spins = 0
    while not u32(_QUIT):
        if u32(_REQ_SEQ) == u32(_ACK_SEQ):
            spins += 1
            time.sleep(0 if spins < 2000 else 0.001)
            continue
        spins = 0
        seq = u32(_REQ_SEQ)
        w, h, quality = u32(_REQ_W), u32(_REQ_H), u32(_QUALITY)
        ok, out_len = 1, 0
        try:
            n = w * h * 3
            frame = np.frombuffer(m_in, dtype=np.uint8, count=n).reshape(h, w, 3)
            t = torch.from_numpy(frame).permute(2, 0, 1).contiguous().to(device, non_blocking=True)
            jpeg = encode_jpeg(t, quality=quality).cpu().numpy()
            out_len = jpeg.nbytes
            np.frombuffer(m_out, dtype=np.uint8, count=out_len)[:] = jpeg
        except Exception as exc:  # noqa: BLE001
            print(f"[jpeg-host] encode failed: {exc}", file=sys.stderr)
            ok = 0
        struct.pack_into("<I", ctl, _OK, ok)
        struct.pack_into("<I", ctl, _OUT_LEN, out_len)
        struct.pack_into("<I", ctl, _ACK_SEQ, seq)
    return 0


if __name__ == "__main__":
    sys.exit(main())
