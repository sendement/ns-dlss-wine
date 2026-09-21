// SPDX-License-Identifier: MIT
// Cache-aware copies for the shared-memory link with the compositor plugin (the slots are dma-bufs the GPU reads/writes WITHOUT snooping the CPU caches on this
// machine, so plain memcpy/numpy leaves stale cache lines: horizontal tearing).
//   nsmem_copy_rows_evict: pitch-strided rows from a slot the GPU wrote -> a private buffer, then drop the slot's lines from the CPU caches so the NEXT read
//                          (after the GPU rewrites the slot) fetches from memory.
//   nsmem_nt_copy_rows:    private buffer -> pitch-strided rows of a slot the GPU will read, with non-temporal stores (they bypass the cache), then sfence.
#include <immintrin.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

__attribute__((target("clflushopt")))
static void evict_opt(const uint8_t* p, size_t n) { for (size_t o = 0; o < n; o += 64) _mm_clflushopt((void*)(p + o)); }
static void evict_plain(const uint8_t* p, size_t n) { for (size_t o = 0; o < n; o += 64) _mm_clflush(p + o); }

void nsmem_copy_rows_evict(uint8_t* dst, size_t dpitch, const uint8_t* src, size_t spitch, size_t rowbytes, size_t rows) {
    static int opt = -1;
    if (opt < 0) { __builtin_cpu_init(); opt = __builtin_cpu_supports("clflushopt") ? 1 : 0; }
    for (size_t r = 0; r < rows; r++) {
        memcpy(dst + r * dpitch, src + r * spitch, rowbytes);
        if (opt) evict_opt(src + r * spitch, rowbytes); else evict_plain(src + r * spitch, rowbytes);
    }
    _mm_mfence();
}

__attribute__((target("avx2")))
void nsmem_nt_copy_rows(uint8_t* dst, size_t dpitch, const uint8_t* src, size_t spitch, size_t rowbytes, size_t rows) {
    for (size_t r = 0; r < rows; r++) {
        uint8_t* d = dst + r * dpitch; const uint8_t* s = src + r * spitch; size_t o = 0;
        if (((uintptr_t)d & 31) == 0)
            for (; o + 32 <= rowbytes; o += 32) _mm256_stream_si256((__m256i*)(d + o), _mm256_loadu_si256((const __m256i*)(s + o)));
        if (o < rowbytes) memcpy(d + o, s + o, rowbytes - o);
    }
    _mm_sfence();
}
