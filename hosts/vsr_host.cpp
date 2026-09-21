// SPDX-License-Identifier: MIT
// Wine-side host for Merserk's MIT-licensed "neuroframe_engine_upscaling.dll"
// (an RTX Video Super Resolution bridge over NVIDIA's nvngx_vsr.dll, CUDA path).
// ABI reverse-read from dlss5-visual-enhancer's src/upscale/video/native.py.
//
// Protocol (stdin/stdout, little-endian):
//   header: u32 magic 'VSR1', u32 in_w,in_h,out_w,out_h,quality(1-4), char in_path[260], char out_path[260]
//   reply : u32 status (0 = ready, else error), then per frame:
//   client writes RGBA8 into in_path (mmap'ed file), sends u32 'GO..' ; host replies u32 ok, float ngx_ms, float total_ms
// in_path/out_path are Windows paths (Z:\tmp\...) to files the Linux side mmaps too.
#include <windows.h>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <string>
#include <io.h>
#include <fcntl.h>

struct FrameDesc { uint32_t struct_size, abi, memory_type, pixel_format, width, height;
    uint64_t planes[4]; uint32_t strides[4]; uint32_t color_matrix, color_range, color_primaries, color_transfer, rotation, reserved[3]; };
struct SessionDesc { uint32_t struct_size, abi, in_w, in_h, out_w, out_h, in_fmt, out_fmt, vsr_enabled, vsr_quality,
    hdr_enabled, hdr_contrast, hdr_saturation, hdr_middle_gray, hdr_peak, reserved[5]; };
struct FrameResult { uint32_t struct_size, abi, vsr_result, hdr_result, cuda_result, reserved0; uint64_t upload, download;
    double input_ms, ngx_ms, output_ms, total_ms; };
struct BridgeStatus { uint32_t struct_size, abi, flags; int32_t gpu_ordinal; uint32_t vsr_init, hdr_init, vmaj, vmin, hmaj, hmin;
    char version[32], gpu_name[128], last_error[512]; };

typedef int   (*rtxv_init_t)(int, const wchar_t*, char*, int);
typedef int   (*rtxv_status_t)(BridgeStatus*);
typedef void* (*rtxv_session_create_t)(SessionDesc*, char*, int);
typedef int   (*rtxv_process_t)(void*, FrameDesc*, FrameDesc*, FrameResult*, char*, int);

static bool ReadAll(HANDLE h, void* p, DWORD n) { BYTE* b = (BYTE*)p; while (n) { DWORD g = 0; if (!ReadFile(h, b, n, &g, nullptr) || !g) return false; b += g; n -= g; } return true; }
static bool WriteAll(HANDLE h, const void* p, DWORD n) { const BYTE* b = (const BYTE*)p; while (n) { DWORD g = 0; if (!WriteFile(h, b, n, &g, nullptr) || !g) return false; b += g; n -= g; } return true; }

static void* MapFile(const char* path, size_t bytes) {
    HANDLE f = CreateFileA(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (f == INVALID_HANDLE_VALUE) { fprintf(stderr, "[vsr] open %s failed %lu\n", path, GetLastError()); return nullptr; }
    HANDLE m = CreateFileMappingA(f, nullptr, PAGE_READWRITE, 0, (DWORD)bytes, nullptr);
    if (!m) { fprintf(stderr, "[vsr] mapping failed %lu\n", GetLastError()); return nullptr; }
    return MapViewOfFile(m, FILE_MAP_ALL_ACCESS, 0, 0, bytes);
}

// Control block, mmap'ed from a third file shared with the Linux client. stdin/stdout are
// NOT used for the protocol: code inside nvngx_vsr/the bridge reads stdin (GetConsoleMode +
// ReadFile), which would eat protocol bytes - so the client gives us stdin=/dev/null.
struct Ctrl { volatile uint32_t state;   // 0 starting, 1 ready, 2 error
              volatile uint32_t req_seq; // client increments after writing a frame
              volatile uint32_t ack_seq; // host sets = req_seq after processing
              volatile uint32_t ok; volatile uint32_t quit; volatile float ngx_ms, total_ms; };


// Debug aid (VSR_DEBUG_EXIT=1): intercept nvngx_vsr's ExitProcess IAT slot and print who called it.
static char* g_vsr_base = nullptr;
static void ErrThunk(int err) { fprintf(stderr, "[vsr] cudaGetDevice failed in GetLUIDFromDevice_Pure: cudart err=%d\n", err); fflush(stderr); TerminateProcess(GetCurrentProcess(), 77); }
static void WINAPI HookExit(UINT code) {
    void* bt[24]; USHORT n = CaptureStackBackTrace(0, 24, bt, nullptr);
    fprintf(stderr, "[vsr] nvngx_vsr ExitProcess(%u), stack:\n", code);
    for (USHORT i = 0; i < n; i++) {
        HMODULE m = nullptr; GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT, (LPCWSTR)bt[i], &m);
        wchar_t nm[260] = L"?"; if (m) GetModuleFileNameW(m, nm, 260);
        fprintf(stderr, "  #%u %ls+0x%llx\n", i, nm, (unsigned long long)((char*)bt[i] - (char*)m)); fflush(stderr);
    }
    TerminateProcess(GetCurrentProcess(), code);
}
static void HookVsrExit(const std::wstring& dir) {
    HMODULE m = LoadLibraryW((dir + L"\\nvngx_vsr.dll").c_str()); if (!m) return; g_vsr_base = (char*)m;
    void** slot = (void**)((char*)m + 0xc7398); DWORD old;
    { unsigned char* p = (unsigned char*)m + 0x7f48d; DWORD o2;
      if (VirtualProtect(p, 16, PAGE_EXECUTE_READWRITE, &o2)) { p[0] = 0x48; p[1] = 0xb8; *(void**)(p + 2) = (void*)ErrThunk; p[10] = 0xff; p[11] = 0xe0; VirtualProtect(p, 16, o2, &o2); } }
    if (VirtualProtect(slot, 8, PAGE_READWRITE, &old)) { *slot = (void*)HookExit; VirtualProtect(slot, 8, old, &old); }
}
int main(int argc, char** argv) {
    // args: in_w in_h out_w out_h quality in_path out_path ctrl_path runtime_dir
    if (argc < 10) { fprintf(stderr, "[vsr] usage: in_w in_h out_w out_h quality in out ctrl dir\n"); return 2; }
    uint32_t in_w = atoi(argv[1]), in_h = atoi(argv[2]), out_w = atoi(argv[3]), out_h = atoi(argv[4]), quality = atoi(argv[5]);
    const char *in_path = argv[6], *out_path = argv[7], *ctrl_path = argv[8], *dir = argv[9];
    Ctrl* ctrl = (Ctrl*)MapFile(ctrl_path, sizeof(Ctrl));
    if (!ctrl) return 2;
    auto fail = [&](int code) { ctrl->state = 2; return code; };
    char err[4096] = {};
    wchar_t wdir[520]; MultiByteToWideChar(CP_ACP, 0, dir, -1, wdir, 520);
    std::wstring bridge = std::wstring(wdir) + L"\\neuroframe_engine_upscaling.dll";
    HMODULE lib = LoadLibraryW(bridge.c_str());
    if (!lib) { fprintf(stderr, "[vsr] LoadLibrary bridge failed %lu\n", GetLastError()); return fail(3); }
    auto rinit = (rtxv_init_t)GetProcAddress(lib, "rtxv_init");
    auto rstat = (rtxv_status_t)GetProcAddress(lib, "rtxv_get_status_v1");
    auto rsess = (rtxv_session_create_t)GetProcAddress(lib, "rtxv_session_create");
    auto rproc = (rtxv_process_t)GetProcAddress(lib, "rtxv_process_frame_v1");
    if (!rinit || !rstat || !rsess || !rproc) { fprintf(stderr, "[vsr] missing exports\n"); return fail(4); }
    if (!rinit(0, wdir, err, sizeof(err))) { fprintf(stderr, "[vsr] rtxv_init failed: %s\n", err); return fail(5); }
    if (getenv("VSR_DEBUG_EXIT")) HookVsrExit(std::wstring(wdir));
    BridgeStatus st = {}; st.struct_size = sizeof(st); st.abi = 1; rstat(&st);
    fprintf(stderr, "[vsr] bridge %s gpu=%s flags=0x%x vsr_init=0x%08x last_error=%s\n", st.version, st.gpu_name, st.flags, st.vsr_init, st.last_error);
    if (!(st.flags & 4)) { fprintf(stderr, "[vsr] VSR not available\n"); return fail(6); }
    SessionDesc sd = {}; sd.struct_size = sizeof(sd); sd.abi = 1; sd.in_w = in_w; sd.in_h = in_h; sd.out_w = out_w; sd.out_h = out_h;
    sd.in_fmt = 1; sd.out_fmt = 1; sd.vsr_enabled = 1; sd.vsr_quality = quality;
    sd.hdr_enabled = 0; sd.hdr_contrast = 100; sd.hdr_saturation = 100; sd.hdr_middle_gray = 50; sd.hdr_peak = 1000;
    void* sess = rsess(&sd, err, sizeof(err));
    if (!sess) { fprintf(stderr, "[vsr] session_create failed: %s\n", err); return fail(7); }
    size_t in_bytes = (size_t)in_w * in_h * 4, out_bytes = (size_t)out_w * out_h * 4;
    void* in_map = MapFile(in_path, in_bytes); void* out_map = MapFile(out_path, out_bytes);
    if (!in_map || !out_map) return fail(8);
    fprintf(stderr, "[vsr] ready %ux%u -> %ux%u quality %u\n", in_w, in_h, out_w, out_h, quality);
    MemoryBarrier(); ctrl->state = 1;
    uint32_t spins = 0;
    while (!ctrl->quit) {
        if (ctrl->req_seq == ctrl->ack_seq) { if (++spins > 2000) Sleep(1); else Sleep(0); continue; }
        spins = 0;
        FrameDesc s = {}, d = {};
        s.struct_size = d.struct_size = sizeof(FrameDesc); s.abi = d.abi = 1; s.memory_type = d.memory_type = 1; s.pixel_format = d.pixel_format = 1;
        s.width = in_w; s.height = in_h; s.planes[0] = (uint64_t)(uintptr_t)in_map; s.strides[0] = in_w * 4;
        d.width = out_w; d.height = out_h; d.planes[0] = (uint64_t)(uintptr_t)out_map; d.strides[0] = out_w * 4;
        FrameResult r = {}; r.struct_size = sizeof(r); r.abi = 1;
        int ok = rproc(sess, &s, &d, &r, err, sizeof(err));
        if (!ok) fprintf(stderr, "[vsr] frame failed: %s\n", err);
        ctrl->ngx_ms = (float)r.ngx_ms; ctrl->total_ms = (float)r.total_ms; ctrl->ok = ok ? 1 : 0;
        MemoryBarrier(); ctrl->ack_seq = ctrl->req_seq;
    }
    return 0;
}
