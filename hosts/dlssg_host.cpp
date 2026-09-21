// SPDX-License-Identifier: MIT
// Wine-side host for Merserk's MIT-licensed "neuroframe_engine_frame_interpolation.dll" (CUDA + D3D12 + NVOF +
// NVIDIA DLSS Frame Generation bridge). ABI read from dlss5-visual-enhancer's src/frame_interpolation/native.py.
//
// Args: width height generated_count in_path out_path ctrl_path runtime_dir
//   in_path : one RGBA8 frame (mmap'ed file) - the NEXT real frame, written by the Linux client
//   out_path: generated_count RGBA8 frames (mmap'ed file) - the frames between the previous and the current real frame
// Every call to the bridge processes one real frame; its history is kept inside the session.
// stdin must be /dev/null (code inside NGX reads it).
#include <windows.h>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <immintrin.h>
#include <cstdlib>
#include <string>
#include <vector>
#include <algorithm>

struct FrameDesc { uint32_t struct_size, abi, memory_type, pixel_format, width, height;
    uint64_t planes[4]; uint32_t strides[4]; uint32_t color_matrix, color_range, color_primaries, color_transfer, rotation, reserved[3]; };
struct SessionDesc { uint32_t struct_size, abi, width, height, generated_count, hdr, surface_pool_size, reserved[5]; };
struct FrameResult { uint32_t struct_size, abi, generated_count, reset, scene_cut, duplicate, interpolation_disabled, nvof_active;
    uint64_t upload_bytes, download_bytes; uint64_t surface_handles[4];
    double scene_score, input_ms, optical_flow_ms, ngx_ms, output_ms, total_ms; };
struct BridgeStatus { uint32_t struct_size, abi_version, flags; int32_t gpu_ordinal; uint32_t multi_frame_count_max, active_sessions;
    char bridge_version[32], runtime_version[32], gpu_name[128], last_error[512]; };

typedef uint32_t (*fi_abi_t)();
typedef int   (*fi_init_t)(int, const wchar_t*, char*, int);
typedef int   (*fi_status_t)(BridgeStatus*);
typedef void* (*fi_session_create_t)(SessionDesc*, char*, int);
typedef void  (*fi_session_release_t)(void*);
typedef int   (*fi_process_t)(void*, FrameDesc*, uint32_t, uint32_t, FrameResult*, char*, int);
typedef int   (*fi_surface_desc_t)(void*, FrameDesc*);
typedef void  (*fi_surface_release_t)(void*);
typedef int   (*fi_copy_t)(void*, void*, uint32_t, void*, uint32_t, char*, int);

static void* MapFile(const char* path, size_t bytes) {
    HANDLE f = CreateFileA(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (f == INVALID_HANDLE_VALUE) { fprintf(stderr, "[dlssg] open %s failed %lu\n", path, GetLastError()); return nullptr; }
    HANDLE m = CreateFileMappingA(f, nullptr, PAGE_READWRITE, 0, (DWORD)bytes, nullptr);
    if (!m) { fprintf(stderr, "[dlssg] mapping failed %lu\n", GetLastError()); return nullptr; }
    return MapViewOfFile(m, FILE_MAP_ALL_ACCESS, 0, 0, bytes);
}

struct Ctrl { volatile uint32_t state;   // 0 starting, 1 ready, 2 error
              volatile uint32_t req_seq, ack_seq, ok, quit;
              volatile float total_ms, ngx_ms;
              volatile uint32_t generated;   // frames written for the last request
              volatile uint32_t info;        // bit0 reset, bit1 scene cut, bit2 duplicate, bit3 interpolation disabled, bit4 nvof active
              volatile uint32_t frame_flags; // set by the client: 1 = force reset
              volatile float flow_ms; };

// NV12 -> RGBA8 (BT.709). range: 0 = limited (16-235), 1 = full. Integer maths, split over worker threads by rows
// (the single-threaded float version took ~15-20 ms per 2.3 MP frame and dominated the bridge's cost).
struct ConvJob { const uint8_t *y, *uv; uint32_t ypitch, uvpitch, w, r0, r1; int range; uint8_t* out; int bgra; };
static inline uint8_t clamp8(int v) { return (uint8_t)(v < 0 ? 0 : (v > 255 ? 255 : v)); }
// One chroma sample serves two horizontally adjacent pixels: compute its U/V terms once per pair.
static void conv_rows(const ConvJob* j) {
    const int range = j->range, swapRB = j->bgra;
    for (uint32_t r = j->r0; r < j->r1; r++) {
        const uint8_t* yr = j->y + (size_t)r * j->ypitch; const uint8_t* uvr = j->uv + (size_t)(r / 2) * j->uvpitch; uint8_t* o = j->out + (size_t)r * j->w * 4;
        for (uint32_t c = 0; c + 1 < j->w + 1; c += 2) {
            const int U = uvr[c] - 128, V = uvr[c + 1] - 128;
            int us, vs;
            if (range) { us = U << 8; vs = V << 8; } else { us = U * 292; vs = V * 292; }   // 256*255/224 = 291.4
            const int rAdd = (403 * vs) >> 8, gAdd = -((48 * us) >> 8) - ((120 * vs) >> 8), bAdd = (475 * us) >> 8;
            for (int k = 0; k < 2 && c + k < j->w; k++) {
                const int Y = yr[c + k];
                const int ys = range ? (Y << 8) : (Y - 16) * 298;                                 // 256*255/219 = 298.0
                const uint8_t r8 = clamp8((ys + rAdd + 128) >> 8), g8 = clamp8((ys + gAdd + 128) >> 8), b8 = clamp8((ys + bAdd + 128) >> 8);
                uint8_t* px = o + (size_t)(c + k) * 4;
                px[0] = swapRB ? b8 : r8; px[1] = g8; px[2] = swapRB ? r8 : b8; px[3] = 255;
            }
        }
    }
}
// AVX2 version: 8 pixels per iteration in 32-bit lanes with exactly the scalar formulas (bit-identical output).
__attribute__((target("avx2"))) static void conv_rows_avx2(const ConvJob* j) {
    const int range = j->range;
    const __m256i c128 = _mm256_set1_epi32(128), c16 = _mm256_set1_epi32(16), k298 = _mm256_set1_epi32(298), k292 = _mm256_set1_epi32(292);
    const __m256i k403 = _mm256_set1_epi32(403), k48 = _mm256_set1_epi32(48), k120 = _mm256_set1_epi32(120), k475 = _mm256_set1_epi32(475);
    const __m256i zero = _mm256_setzero_si256(), v255 = _mm256_set1_epi32(255), alpha = _mm256_set1_epi32((int)0xFF000000u);
    const __m128i uSel = _mm_setr_epi8(0, 0, 2, 2, 4, 4, 6, 6, -1, -1, -1, -1, -1, -1, -1, -1);   // U of pixel pair -> both pixels
    const __m128i vSel = _mm_setr_epi8(1, 1, 3, 3, 5, 5, 7, 7, -1, -1, -1, -1, -1, -1, -1, -1);
    const int bgra = j->bgra;
    for (uint32_t r = j->r0; r < j->r1; r++) {
        const uint8_t* yr = j->y + (size_t)r * j->ypitch; const uint8_t* uvr = j->uv + (size_t)(r / 2) * j->uvpitch; uint8_t* o = j->out + (size_t)r * j->w * 4;
        uint32_t c = 0;
        for (; c + 8 <= j->w; c += 8) {
            __m256i Y = _mm256_cvtepu8_epi32(_mm_loadl_epi64((const __m128i*)(yr + c)));
            __m128i uvb = _mm_loadl_epi64((const __m128i*)(uvr + c));
            __m256i U = _mm256_sub_epi32(_mm256_cvtepu8_epi32(_mm_shuffle_epi8(uvb, uSel)), c128);
            __m256i V = _mm256_sub_epi32(_mm256_cvtepu8_epi32(_mm_shuffle_epi8(uvb, vSel)), c128);
            __m256i ys, us, vs;
            if (range) { ys = _mm256_slli_epi32(Y, 8); us = _mm256_slli_epi32(U, 8); vs = _mm256_slli_epi32(V, 8); }
            else { ys = _mm256_mullo_epi32(_mm256_sub_epi32(Y, c16), k298); us = _mm256_mullo_epi32(U, k292); vs = _mm256_mullo_epi32(V, k292); }
            __m256i R = _mm256_add_epi32(ys, _mm256_srai_epi32(_mm256_mullo_epi32(vs, k403), 8));
            __m256i G = _mm256_sub_epi32(_mm256_sub_epi32(ys, _mm256_srai_epi32(_mm256_mullo_epi32(us, k48), 8)), _mm256_srai_epi32(_mm256_mullo_epi32(vs, k120), 8));
            __m256i B = _mm256_add_epi32(ys, _mm256_srai_epi32(_mm256_mullo_epi32(us, k475), 8));
            R = _mm256_min_epi32(_mm256_max_epi32(_mm256_srai_epi32(_mm256_add_epi32(R, c128), 8), zero), v255);
            G = _mm256_min_epi32(_mm256_max_epi32(_mm256_srai_epi32(_mm256_add_epi32(G, c128), 8), zero), v255);
            B = _mm256_min_epi32(_mm256_max_epi32(_mm256_srai_epi32(_mm256_add_epi32(B, c128), 8), zero), v255);
            __m256i lo = bgra ? B : R, hi = bgra ? R : B;   // byte 0 / byte 2 of each output pixel
            __m256i px = _mm256_or_si256(_mm256_or_si256(lo, _mm256_slli_epi32(G, 8)), _mm256_or_si256(_mm256_slli_epi32(hi, 16), alpha));
            _mm256_storeu_si256((__m256i*)(o + (size_t)c * 4), px);
        }
        // tail (< 8 pixels): scalar, same formulas
        for (; c < j->w; c++) {
            const int Yv = yr[c], Uv = uvr[c & ~1u] - 128, Vv = uvr[(c & ~1u) + 1] - 128;
            int ysv, usv, vsv;
            if (range) { ysv = Yv << 8; usv = Uv << 8; vsv = Vv << 8; } else { ysv = (Yv - 16) * 298; usv = Uv * 292; vsv = Vv * 292; }
            const uint8_t r8 = clamp8((ysv + ((403 * vsv) >> 8) + 128) >> 8), g8 = clamp8((ysv - ((48 * usv) >> 8) - ((120 * vsv) >> 8) + 128) >> 8), b8 = clamp8((ysv + ((475 * usv) >> 8) + 128) >> 8);
            uint8_t* pp = o + (size_t)c * 4;
            pp[0] = bgra ? b8 : r8; pp[1] = g8; pp[2] = bgra ? r8 : b8; pp[3] = 255;
        }
    }
}
static void conv_run(const ConvJob* j) {
    static const bool avx2 = __builtin_cpu_supports("avx2") && !getenv("DLSSG_NO_AVX2");
    if (avx2) conv_rows_avx2(j); else conv_rows(j);
}

// Persistent worker pool (Windows thread pool): no thread creation per frame.
static ConvJob g_jobs[16]; static volatile LONG g_next = 0;
static VOID CALLBACK conv_work(PTP_CALLBACK_INSTANCE, PVOID, PTP_WORK) { LONG i = InterlockedIncrement(&g_next) - 1; conv_run(&g_jobs[i]); }
static PTP_WORK g_work = nullptr;
static void nv12_to_rgba(const uint8_t* y, uint32_t ypitch, const uint8_t* uv, uint32_t uvpitch, uint32_t w, uint32_t h, int range, uint8_t* out, int bgra = 0) {
    if (!g_work) g_work = CreateThreadpoolWork(conv_work, nullptr, nullptr);
    SYSTEM_INFO si; GetSystemInfo(&si);
    uint32_t n = std::min<uint32_t>(std::max<DWORD>(si.dwNumberOfProcessors, 1), 12);
    if (getenv("DLSSG_CONV_THREADS")) n = std::max(1, std::min(12, atoi(getenv("DLSSG_CONV_THREADS"))));
    uint32_t per = ((h + n - 1) / n + 1) & ~1u;
    uint32_t used = 0;
    for (uint32_t i = 0; i < n; i++) {
        uint32_t r0 = i * per, r1 = std::min(h, r0 + per);
        if (r0 >= h) break;
        g_jobs[i] = { y, uv, ypitch, uvpitch, w, r0, r1, range, out, bgra }; used++;
    }
    g_next = 1;   // job 0 is the calling thread's; the pool workers take 1..used-1
    for (uint32_t i = 1; i < used; i++) SubmitThreadpoolWork(g_work);
    conv_run(&g_jobs[0]);
    if (used > 1) WaitForThreadpoolWorkCallbacks(g_work, FALSE);
}

int main(int argc, char** argv) {
    if (argc < 8) { fprintf(stderr, "[dlssg] usage: width height count in out ctrl dir\n"); return 2; }
    uint32_t w = atoi(argv[1]), h = atoi(argv[2]), count = atoi(argv[3]);
    const char *in_path = argv[4], *out_path = argv[5], *ctrl_path = argv[6], *dir = argv[7];
    Ctrl* ctrl = (Ctrl*)MapFile(ctrl_path, sizeof(Ctrl));
    if (!ctrl) return 2;
    auto fail = [&](int code) { ctrl->state = 2; return code; };
    char err[4096] = {};
    wchar_t wdir[520]; MultiByteToWideChar(CP_ACP, 0, dir, -1, wdir, 520);
    std::wstring bridge = std::wstring(wdir) + L"\\neuroframe_engine_frame_interpolation.dll";
    HMODULE lib = LoadLibraryW(bridge.c_str());
    if (!lib) { fprintf(stderr, "[dlssg] LoadLibrary bridge failed %lu\n", GetLastError()); return fail(3); }
    auto abi = (fi_abi_t)GetProcAddress(lib, "fi_abi_version");
    auto init = (fi_init_t)GetProcAddress(lib, "fi_init");
    auto status = (fi_status_t)GetProcAddress(lib, "fi_get_status_v1");
    auto screate = (fi_session_create_t)GetProcAddress(lib, "fi_session_create");
    auto srelease = (fi_session_release_t)GetProcAddress(lib, "fi_session_release");
    auto process = (fi_process_t)GetProcAddress(lib, "fi_process_frame_v1");
    auto sdesc = (fi_surface_desc_t)GetProcAddress(lib, "fi_surface_frame_desc");
    auto surel = (fi_surface_release_t)GetProcAddress(lib, "fi_surface_release");
    auto scopy = (fi_copy_t)GetProcAddress(lib, "fi_surface_copy_to_host");
    if (!abi || !init || !status || !screate || !process || !sdesc || !surel || !scopy) { fprintf(stderr, "[dlssg] missing exports\n"); return fail(4); }
    fprintf(stderr, "[dlssg] bridge abi=%u\n", abi());
    if (!init(0, wdir, err, sizeof(err))) { fprintf(stderr, "[dlssg] fi_init failed: %s\n", err); return fail(5); }
    BridgeStatus st = {}; st.struct_size = sizeof(st); st.abi_version = 1; status(&st);
    fprintf(stderr, "[dlssg] bridge %s runtime %s gpu=%s flags=0x%x (init=%d poisoned=%d available=%d cuda_interop=%d) max_multi=%u last_error=%s\n",
        st.bridge_version, st.runtime_version, st.gpu_name, st.flags, st.flags & 1, (st.flags >> 1) & 1, (st.flags >> 2) & 1, (st.flags >> 3) & 1,
        st.multi_frame_count_max, st.last_error);
    if (!(st.flags & 4)) { fprintf(stderr, "[dlssg] DLSS-G not available\n"); return fail(6); }
    SessionDesc sd = {}; sd.struct_size = sizeof(sd); sd.abi = 1; sd.width = w; sd.height = h; sd.generated_count = count; sd.hdr = 0; sd.surface_pool_size = 8;
    void* sess = screate(&sd, err, sizeof(err));
    if (!sess) { fprintf(stderr, "[dlssg] session_create failed: %s\n", err); return fail(7); }
    size_t bytes = (size_t)w * h * 4;
    const uint32_t ring = getenv("DLSSG_RING") ? std::max(1, atoi(getenv("DLSSG_RING"))) : 1;   // output regions, one per request (seq % ring): the consumer reads them in place
    const int bgra = getenv("DLSSG_BGRA") && atoi(getenv("DLSSG_BGRA")) ? 1 : 0;              // 1 = write B,G,R,A (cairo ARGB32 order)
    void* in_map = MapFile(in_path, bytes); uint8_t* out_map = (uint8_t*)MapFile(out_path, bytes * count * ring);
    if (!in_map || !out_map) return fail(8);
    int range = getenv("DLSSG_RANGE") && !strcmp(getenv("DLSSG_RANGE"), "full") ? 1 : 0;
    fprintf(stderr, "[dlssg] ready %ux%u x%u (range=%s, out ring %u, %s)\n", w, h, count, range ? "full" : "limited", ring, bgra ? "BGRA" : "RGBA");
    MemoryBarrier(); ctrl->state = 1;
    uint32_t spins = 0;
    std::vector<uint8_t> yp, uvp;
    while (!ctrl->quit) {
        if (ctrl->req_seq == ctrl->ack_seq) { if (++spins > 2000) Sleep(1); else Sleep(0); continue; }
        spins = 0;
        FrameDesc s = {}; s.struct_size = sizeof(FrameDesc); s.abi = 1; s.memory_type = 1; s.pixel_format = 1; s.width = w; s.height = h;
        s.planes[0] = (uint64_t)(uintptr_t)in_map; s.strides[0] = w * 4; s.color_matrix = 1; s.color_range = 0; s.color_primaries = 1; s.color_transfer = 0;
        FrameResult r = {}; r.struct_size = sizeof(r); r.abi = 1;
        uint32_t flags = 2 | ((ctrl->frame_flags & 1) ? 1 : 0);   // FLAG_DETECT_CUT | FLAG_FORCE_RESET
        LARGE_INTEGER q0, q1, q2, q3, qf; QueryPerformanceFrequency(&qf); QueryPerformanceCounter(&q0);
        int ok = process(sess, &s, 4 /* NV12 */, flags, &r, err, sizeof(err));
        QueryPerformanceCounter(&q1); double t_copy = 0, t_conv = 0;
        uint32_t made = 0;
        if (!ok) fprintf(stderr, "[dlssg] frame failed: %s\n", err);
        else for (uint32_t i = 0; i < r.generated_count && i < count; i++) {
            void* hnd = (void*)(uintptr_t)r.surface_handles[i];
            FrameDesc d = {}; d.struct_size = sizeof(d); d.abi = 1;
            if (!hnd || !sdesc(hnd, &d)) { fprintf(stderr, "[dlssg] invalid output surface\n"); continue; }
            yp.resize((size_t)w * h); uvp.resize((size_t)w * ((h + 1) / 2));   // (kept between frames)
            QueryPerformanceCounter(&q2);
            if (!scopy(hnd, yp.data(), w, uvp.data(), w, err, sizeof(err))) { fprintf(stderr, "[dlssg] copy_to_host failed: %s\n", err); surel(hnd); continue; }
            QueryPerformanceCounter(&q3); t_copy += (double)(q3.QuadPart - q2.QuadPart) * 1000.0 / qf.QuadPart;
            nv12_to_rgba(yp.data(), w, uvp.data(), w, w, h, range, out_map + ((size_t)(ctrl->req_seq % ring) * count + made) * bytes, bgra);
            QueryPerformanceCounter(&q2); t_conv += (double)(q2.QuadPart - q3.QuadPart) * 1000.0 / qf.QuadPart;
            surel(hnd); made++;
        }
        for (uint32_t i = made; i < r.generated_count && i < 4; i++) {}   // (handles beyond `count` are not expected)
        if (getenv("DLSSG_PROF")) { static int n = 0; if (++n % 20 == 0) fprintf(stderr, "[dlssg] host: process %.1f ms, copy_to_host %.1f ms, nv12->rgba %.1f ms\n", (double)(q1.QuadPart - q0.QuadPart) * 1000.0 / qf.QuadPart, t_copy, t_conv); }
        ctrl->generated = made; ctrl->total_ms = (float)r.total_ms; ctrl->ngx_ms = (float)r.ngx_ms; ctrl->flow_ms = (float)r.optical_flow_ms;
        ctrl->info = (r.reset ? 1 : 0) | (r.scene_cut ? 2 : 0) | (r.duplicate ? 4 : 0) | (r.interpolation_disabled ? 8 : 0) | (r.nvof_active ? 16 : 0);
        ctrl->ok = ok ? 1 : 0;
        MemoryBarrier(); ctrl->ack_seq = ctrl->req_seq;
    }
    srelease(sess);
    return 0;
}
