# SPDX-License-Identifier: MIT
"""Native (no Wine) RTX Video Super Resolution host on NVIDIA's `nvidia-vfx` Python package + cupy. Runs in its own venv (runtime/venv-vsr) so the GPU stack stays
out of the GUI process. Same file protocol as vsr_host.exe:

    vsr_native_host.py iw ih ow oh quality in_path out_path ctrl_path
      in : RGBA8 frame (ih x iw)   out: RGBA8 frame (oh x ow, alpha 255)   ctrl: state 0 / req_seq 4 / ack_seq 8 / ok 12 / quit 16 (uint32)
"""
import mmap
import struct
import sys
import time

import cupy as cp   # must come BEFORE nvvfx: the reverse order segfaults (both load NVIDIA CUDA libraries)
import numpy as np
from nvvfx import VideoSuperRes


# Fused conversion kernels (the plain cupy expressions cost ~5 ms per 720p->1440p frame in temporaries).
_TO_CHW = cp.RawKernel(r'''
extern "C" __global__ void to_chw(const unsigned char* __restrict__ src, float* __restrict__ dst, int iw, int ih, int piw) {
    int x = blockIdx.x * blockDim.x + threadIdx.x, y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= piw || y >= ih) return;
    int sx = x < iw ? x : iw - 1;                       // replicate padding on the right
    const uchar4 p = *reinterpret_cast<const uchar4*>(src + ((size_t)y * iw + sx) * 4);
    size_t plane = (size_t)ih * piw, o = (size_t)y * piw + x;
    const float k = 1.0f / 255.0f;
    dst[o] = p.x * k; dst[plane + o] = p.y * k; dst[2 * plane + o] = p.z * k;
}''', "to_chw")
_TO_RGBA = cp.RawKernel(r'''
extern "C" __global__ void to_rgba(const float* __restrict__ src, unsigned char* __restrict__ dst, int ow, int oh, int pow_) {
    int x = blockIdx.x * blockDim.x + threadIdx.x, y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= ow || y >= oh) return;
    size_t plane = (size_t)oh * pow_, i = (size_t)y * pow_ + x;
    auto q = [](float v) { v = v < 0.f ? 0.f : (v > 1.f ? 1.f : v); return (unsigned char)(v * 255.0f + 0.5f); };
    uchar4 o = make_uchar4(q(src[i]), q(src[plane + i]), q(src[2 * plane + i]), 255);
    *reinterpret_cast<uchar4*>(dst + ((size_t)y * ow + x) * 4) = o;
}''', "to_rgba")


def aligned(iw, ow):
    """The DLPack output path needs 32-byte aligned float rows: pad each width up to the next multiple of 8. The two paddings need not share iw/ow's
    exact ratio - _TO_CHW edge-replicates the extra input columns and _TO_RGBA crops the network's output back down to the real `ow` columns, so this
    only has to satisfy the alignment requirement, not preserve the scale factor.
    A previous version searched for a piw/pow_ pair that also preserved iw/ow's ratio exactly, padding only the input side; for most real-world sizes
    (e.g. iw=304, ow=1222: 1222 % 8 == 6) no such pair exists within any reasonable search window, silently falling back to the UNALIGNED raw iw/ow -
    which is exactly the case this function exists to avoid, and produced a corrupted (sheared, noisy) output, confirmed by dumping the raw frame."""
    return (iw + 7) // 8 * 8, (ow + 7) // 8 * 8


def main():
    iw, ih, ow, oh, quality = (int(a) for a in sys.argv[1:6])
    files = [open(p, "r+b") for p in sys.argv[6:9]]
    m_in, m_out, ctl = (mmap.mmap(f.fileno(), 0) for f in files)
    u32 = lambda off: struct.unpack_from("<I", ctl, off)[0]
    piw, pow_ = aligned(iw, ow)
    q = {1: VideoSuperRes.QualityLevel.LOW, 2: VideoSuperRes.QualityLevel.MEDIUM, 3: VideoSuperRes.QualityLevel.HIGH, 4: VideoSuperRes.QualityLevel.ULTRA}[max(1, min(4, quality))]
    src = np.frombuffer(m_in, dtype=np.uint8).reshape(ih, iw, 4)
    dst = np.frombuffer(m_out, dtype=np.uint8).reshape(oh, ow, 4)
    stream = cp.cuda.get_current_stream()
    for arr in (src, dst):                                     # page-lock the shared files: DMA straight from/to them (about 4x faster than pageable copies)
        try:
            cp.cuda.runtime.hostRegister(arr.ctypes.data, arr.nbytes, 0)
        except Exception:  # noqa: BLE001
            pass
    try:
        with VideoSuperRes(quality=q) as sr:
            sr.output_width, sr.output_height = pow_, oh
            sr.load()
            g_in = cp.empty((ih, iw, 4), dtype=cp.uint8)
            frame = cp.zeros((3, ih, piw), dtype=cp.float32)
            g_out = cp.empty((oh, ow, 4), dtype=cp.uint8)
            blk = (32, 8)
            grid_in, grid_out = ((piw + 31) // 32, (ih + 7) // 8), ((ow + 31) // 32, (oh + 7) // 8)
            struct.pack_into("<I", ctl, 0, 1)
            spins = 0
            while not u32(16):
                if u32(4) == u32(8):
                    spins += 1
                    time.sleep(0 if spins < 2000 else 0.001)
                    continue
                spins = 0
                ok = 1
                try:
                    g_in.set(src, stream=stream)                                   # H2D (RGBA8)
                    _TO_CHW(grid_in, blk, (g_in, frame, np.int32(iw), np.int32(ih), np.int32(piw)))
                    out = cp.from_dlpack(sr.run(frame, stream_ptr=stream.ptr).image)   # (3, oh, pow_) float32
                    _TO_RGBA(grid_out, blk, (out, g_out, np.int32(ow), np.int32(oh), np.int32(pow_)))
                    g_out.get(out=dst, stream=stream)                              # D2H straight into the shared file
                    stream.synchronize()
                except Exception as exc:  # noqa: BLE001
                    print(f"[vsr-native] frame failed: {exc}", file=sys.stderr)
                    ok = 0
                struct.pack_into("<I", ctl, 12, ok)
                struct.pack_into("<I", ctl, 8, u32(4))
    except Exception as exc:  # noqa: BLE001
        print(f"[vsr-native] session failed: {exc}", file=sys.stderr)
        struct.pack_into("<I", ctl, 0, 2)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
