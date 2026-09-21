// SPDX-License-Identifier: MIT
// Shared-memory protocol between the plugin (compositor) and the external pipeline process. Python side: app/plugin_bridge.py.
// Handshake: connect to $XDG_RUNTIME_DIR/nsproxy.sock; the plugin answers "NSPX1 <total_bytes>\n" with the memfd in SCM_RIGHTS.
// Then mmap it: [Header][export slots x3][result slots x3]; each slot is cap_w*cap_h*4 bytes, rows every pitchFor(w) bytes (w = the slot's width).
//   export  (plugin -> pipeline): RGBA8 top-down pixels of the window. The plugin fills slot exp_seq%3 then bumps exp_seq (release) and
//           writes one byte to the socket as a wake-up (drop if the socket is full).
//   result  (pipeline -> plugin): BGRA8 premultiplied (cairo ARGB32 / DRM ARGB8888) pixels. The pipeline fills slot res_seq%3 (after
//           bumping: slot (res_seq+1)%3), sets res_w/res_h/res_ns, then bumps res_seq (release). res_ns = CLOCK_MONOTONIC ns.
//   client_flags bit0: the pipeline copies with cache-aware helpers -> the plugin may use the zero-copy (dma-buf) result/export paths.
//   want_w/want_h: the size the pipeline wants exports at (GPU downscale in the plugin; 0 = native).
//   override_on: the pipeline sets 1 when the result should replace the window's picture.
// Watchdog: the plugin ignores a result older than kResultMaxAgeNs (the real window shows through) - a stalled pipeline never freezes it.
#pragma once
#include <cstdint>

namespace nsproxy {
constexpr uint32_t kMagic = 0x5850534E; // "NSPX"
constexpr uint32_t kVersion = 2;
constexpr uint32_t kPitchAlign = 256;
// Row pitch of a slot holding a w-pixel-wide frame: rows start every pitchFor(w) bytes (256-aligned, so slots can be imported as linear dma-bufs).
constexpr uint32_t pitchFor(uint32_t w) { return (w * 4 + kPitchAlign - 1) & ~(kPitchAlign - 1); }
constexpr int kSlots = 3;
constexpr uint32_t kCapW = 3840, kCapH = 2160;
constexpr uint64_t kResultMaxAgeNs = 300'000'000ULL;

struct Header {
    uint32_t magic, version;
    uint32_t cap_w, cap_h;
    uint64_t slot_bytes, exp_off, res_off;
    uint64_t exp_seq;
    uint32_t exp_w[kSlots], exp_h[kSlots];
    uint64_t res_seq;
    uint32_t res_w[kSlots], res_h[kSlots];
    uint64_t res_ns;
    uint32_t override_on, pad;
    // pipeline -> plugin: wanted export size (the plugin downscales on the GPU); 0 = the window's native buffer size
    uint32_t want_w, want_h;
    // pipeline -> plugin: bit 0 = the client copies through cache-aware helpers (non-temporal stores in / clflush after reads), so the plugin may use its
    // zero-copy dma-buf paths. Without it those paths tear (the GPU and the CPU caches are not coherent here): the plugin uses the copying paths.
    uint32_t client_flags, pad2;
};
} // namespace nsproxy
