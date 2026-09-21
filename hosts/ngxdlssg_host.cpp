// SPDX-License-Identifier: MIT
// DLSS Frame Generation host built on NVIDIA's PUBLIC NGX API (Windows exe, runs under Wine + vkd3d-proton + dxvk-nvapi). Same file protocol as
// dlssg_host.exe / fsr3fg_host.exe: the Python side writes RGBA8 frames into an "in" file and bumps req_seq in a control block; we generate the frames
// between the previous and the current real frame with nvngx_dlssg.dll and write them (BGRA8, cairo order) into a ring of regions of the "out" file.
//
// No engine data exists for a screen filter: depth is a constant plane, motion vectors are zero (or a global vector hint from the client) and the
// feature derives its own optical flow (NVOF through dxvk-nvapi) from the colour frames.
//
// Needs, next to the exe: _nvngx.dll (NGX core) and nvngx_dlssg.dll (the DLSS-G snippet; NVIDIA publishes it in the NVIDIA/DLSS repository).
// The NGX runtime is built by MSVC, whose layout of overloaded virtuals differs from g++'s, so parameters are set through explicit vtable slots
// (indices read off NVIDIA's own static wrappers in nvsdk_ngx_s.lib).
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <immintrin.h>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <string>
#include "nvsdk_ngx.h"
#include "nvsdk_ngx_defs_dlssg.h"
#include "nvsdk_ngx_params.h"

static void* MapFile(const char* path, size_t bytes) {
    HANDLE f = CreateFileA(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (f == INVALID_HANDLE_VALUE) { fprintf(stderr, "[ngxg] open %s failed %lu\n", path, GetLastError()); return nullptr; }
    HANDLE m = CreateFileMappingA(f, nullptr, PAGE_READWRITE, 0, (DWORD)bytes, nullptr);
    if (!m) { fprintf(stderr, "[ngxg] mapping failed %lu\n", GetLastError()); return nullptr; }
    return MapViewOfFile(m, FILE_MAP_ALL_ACCESS, 0, 0, bytes);
}

struct Ctrl { volatile uint32_t state;   // 0 starting, 1 ready, 2 error
              volatile uint32_t req_seq, ack_seq, ok, quit;
              volatile float total_ms, ngx_ms;
              volatile uint32_t generated, info, frame_flags;
              volatile float flow_ms;
              volatile float mv_x, mv_y; };   // global content motion prev->cur in DISPLAY pixels, estimated by the Python side (0,0 = none)

// Per-slot request data of the two-slot pipeline (at byte 64 of the control file): the Python side fills slot seq%2 before bumping req_seq, the host fills
// gen/info/ok[seq%4] before it acknowledges.
struct Ext { volatile uint32_t flags[2]; volatile float mvx[2], mvy[2]; volatile uint32_t gen[4], info[4], ok[4]; };

#define CHK(x) do { HRESULT _h = (x); if (FAILED(_h)) { fprintf(stderr, "[ngxg] %s failed: 0x%08lx (line %d)\n", #x, (unsigned long)_h, __LINE__); return fail(20); } } while (0)

static D3D12_RESOURCE_BARRIER Trans(ID3D12Resource* r, D3D12_RESOURCE_STATES a, D3D12_RESOURCE_STATES b) {
    D3D12_RESOURCE_BARRIER x = {}; x.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION; x.Transition.pResource = r;
    x.Transition.StateBefore = a; x.Transition.StateAfter = b; x.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES; return x;
}


enum { S_VOID = 0, S_D3D12 = 1, S_I = 3, S_UI = 4, S_F = 6, G_I = 11, G_UI = 12 };
template <class T> static void slotSet(NVSDK_NGX_Parameter* p, int slot, const char* n, T v) { (*(void (**)(void*, const char*, T))(*(void***)p + slot))(p, n, v); }
template <class T> static NVSDK_NGX_Result slotGet(NVSDK_NGX_Parameter* p, int slot, const char* n, T* v) { return (*(NVSDK_NGX_Result (**)(void*, const char*, T*))(*(void***)p + slot))(p, n, v); }
static void setUI(NVSDK_NGX_Parameter* p, const char* n, unsigned v) { slotSet<unsigned>(p, S_UI, n, v); }
static void setF(NVSDK_NGX_Parameter* p, const char* n, float v) { slotSet<float>(p, S_F, n, v); }
static void setRes(NVSDK_NGX_Parameter* p, const char* n, ID3D12Resource* v) { slotSet<ID3D12Resource*>(p, S_D3D12, n, v); }
static void setPtr(NVSDK_NGX_Parameter* p, const char* n, void* v) { slotSet<void*>(p, S_VOID, n, v); }

typedef NVSDK_NGX_Result (NVSDK_CONV *PFN_Init)(const char*, NVSDK_NGX_EngineType, const char*, const wchar_t*, ID3D12Device*, NVSDK_NGX_Version, const NVSDK_NGX_FeatureCommonInfo*);
typedef NVSDK_NGX_Result (NVSDK_CONV *PFN_Caps)(NVSDK_NGX_Parameter**);
typedef NVSDK_NGX_Result (NVSDK_CONV *PFN_Alloc)(NVSDK_NGX_Parameter**);
typedef NVSDK_NGX_Result (NVSDK_CONV *PFN_Create)(ID3D12GraphicsCommandList*, NVSDK_NGX_Feature, NVSDK_NGX_Parameter*, NVSDK_NGX_Handle**);
typedef NVSDK_NGX_Result (NVSDK_CONV *PFN_Eval)(ID3D12GraphicsCommandList*, const NVSDK_NGX_Handle*, const NVSDK_NGX_Parameter*, PFN_NVSDK_NGX_ProgressCallback);
typedef NVSDK_NGX_Result (NVSDK_CONV *PFN_Release)(NVSDK_NGX_Handle*);
typedef NVSDK_NGX_Result (NVSDK_CONV *PFN_Shutdown)(ID3D12Device*);

static void NVSDK_CONV ngxLog(const char* msg, NVSDK_NGX_Logging_Level, NVSDK_NGX_Feature) { if (getenv("NGXG_LOG")) fputs(msg, stderr); }


// Row-band parallel-for on the Win32 thread pool: the two big CPU copies (input -> upload heap, readback -> BGRA ring) are memory-bound and single-thread
// bandwidth is well below what the machine can do.
struct ParJob { void (*fn)(void*, uint32_t, uint32_t); void* ctx; uint32_t r0, r1; };
static VOID CALLBACK parWork(PTP_CALLBACK_INSTANCE, PVOID v, PTP_WORK) { ParJob* j = (ParJob*)v; j->fn(j->ctx, j->r0, j->r1); }
static void parallelRows(uint32_t rows, void (*fn)(void*, uint32_t, uint32_t), void* ctx) {
    const uint32_t bands = 4; ParJob jobs[bands]; PTP_WORK works[bands]; uint32_t nb = 0;
    for (uint32_t b = 0; b < bands; b++) {
        jobs[b] = {fn, ctx, rows * b / bands, rows * (b + 1) / bands};
        if (b + 1 < bands) { works[nb] = CreateThreadpoolWork(parWork, &jobs[b], nullptr); if (works[nb]) SubmitThreadpoolWork(works[nb++]); else fn(ctx, jobs[b].r0, jobs[b].r1); }
    }
    fn(ctx, jobs[bands - 1].r0, jobs[bands - 1].r1);   // the last band on this thread
    for (uint32_t i = 0; i < nb; i++) { WaitForThreadpoolWorkCallbacks(works[i], FALSE); CloseThreadpoolWork(works[i]); }
}

int main(int argc, char** argv) {
    if (argc < 8) { fprintf(stderr, "[ngxg] usage: width height count in out ctrl dir\n"); return 2; }
    const uint32_t w = atoi(argv[1]), h = atoi(argv[2]), count = std::max(1, std::min(3, atoi(argv[3])));
    Ctrl* ctrl = (Ctrl*)MapFile(argv[6], 64 + sizeof(Ext));
    if (!ctrl) return 2;
    Ext* ext = (Ext*)((uint8_t*)ctrl + 64);
    auto fail = [&](int code) { ctrl->state = 2; return code; };
    const uint32_t ring = getenv("NGXG_RING") ? std::max(1, atoi(getenv("NGXG_RING"))) : (getenv("DLSSG_RING") ? std::max(1, atoi(getenv("DLSSG_RING"))) : 1);
    const size_t bytes = (size_t)w * h * 4;
    uint8_t* in_map = (uint8_t*)MapFile(argv[4], bytes * 2);   // two input slots
    uint8_t* out_map = (uint8_t*)MapFile(argv[5], bytes * count * ring);
    if (!in_map || !out_map) return fail(8);

    char dir[520]; snprintf(dir, sizeof dir, "%s", argv[7]);
    wchar_t wdir[520]; MultiByteToWideChar(CP_ACP, 0, dir, -1, wdir, 520);
    std::wstring core = std::wstring(wdir) + L"\\_nvngx.dll";
    HMODULE ngx = LoadLibraryW(core.c_str());
    if (!ngx) ngx = LoadLibraryA("_nvngx.dll");   // Proton/Wine ship the NGX core in the prefix's system32
    if (!ngx) { fprintf(stderr, "[ngxg] LoadLibrary _nvngx.dll failed %lu\n", GetLastError()); return fail(3); }
    auto pInit = (PFN_Init)GetProcAddress(ngx, "NVSDK_NGX_D3D12_Init_ProjectID");
    auto pCaps = (PFN_Caps)GetProcAddress(ngx, "NVSDK_NGX_D3D12_GetCapabilityParameters");
    auto pAlloc = (PFN_Alloc)GetProcAddress(ngx, "NVSDK_NGX_D3D12_AllocateParameters");
    auto pCreate = (PFN_Create)GetProcAddress(ngx, "NVSDK_NGX_D3D12_CreateFeature");
    auto pEval = (PFN_Eval)GetProcAddress(ngx, "NVSDK_NGX_D3D12_EvaluateFeature");
    auto pRelease = (PFN_Release)GetProcAddress(ngx, "NVSDK_NGX_D3D12_ReleaseFeature");
    auto pShutdown = (PFN_Shutdown)GetProcAddress(ngx, "NVSDK_NGX_D3D12_Shutdown1");
    if (!pInit || !pCaps || !pAlloc || !pCreate || !pEval || !pRelease || !pShutdown) { fprintf(stderr, "[ngxg] missing NGX exports\n"); return fail(4); }

    auto mkDev = (PFN_D3D12_CREATE_DEVICE)GetProcAddress(LoadLibraryA("d3d12.dll"), "D3D12CreateDevice");
    ID3D12Device* dev = nullptr;
    CHK(mkDev(nullptr, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), (void**)&dev));

    const wchar_t* paths[1] = {wdir};
    NVSDK_NGX_FeatureCommonInfo info = {};
    info.PathListInfo.Path = paths; info.PathListInfo.Length = 1;
    info.LoggingInfo.LoggingCallback = ngxLog; info.LoggingInfo.MinimumLoggingLevel = NVSDK_NGX_LOGGING_LEVEL_ON; info.LoggingInfo.DisableOtherLoggingSinks = true;
    // ProjectId: GUID-like, no more than 4 identical digits in a row (NGX rejects it otherwise: 0xBAD00005)
    NVSDK_NGX_Result r = pInit("3f2a9c1e-7b4d-4e8a-9f61-c5d02a8b7e14", NVSDK_NGX_ENGINE_TYPE_CUSTOM, "1.0", L"C:\\windows\\temp", dev, NVSDK_NGX_Version_API, &info);
    if (NVSDK_NGX_FAILED(r)) { fprintf(stderr, "[ngxg] NGX init failed 0x%08x\n", (unsigned)r); return fail(5); }
    NVSDK_NGX_Parameter* caps = nullptr;
    int avail = 0, initRes = 0;
    if (NVSDK_NGX_FAILED(pCaps(&caps)) || !caps) { fprintf(stderr, "[ngxg] no capability parameters\n"); return fail(5); }
    slotGet<int>(caps, G_I, NVSDK_NGX_Parameter_FrameGeneration_Available, &avail);
    slotGet<int>(caps, G_I, NVSDK_NGX_Parameter_FrameGeneration_FeatureInitResult, &initRes);
    if (!avail) { fprintf(stderr, "[ngxg] DLSS-G not available (init result 0x%x)\n", (unsigned)initRes); return fail(6); }

    D3D12_COMMAND_QUEUE_DESC qd = {}; qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    ID3D12CommandQueue* q; CHK(dev->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void**)&q));
    ID3D12CommandAllocator* al; CHK(dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), (void**)&al));
    ID3D12GraphicsCommandList* cl; CHK(dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, al, nullptr, __uuidof(ID3D12GraphicsCommandList), (void**)&cl));
    cl->Close();
    ID3D12Fence* fence; CHK(dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), (void**)&fence));
    HANDLE fev = CreateEventA(nullptr, FALSE, FALSE, nullptr); uint64_t fval = 0;

    auto heap = [](D3D12_HEAP_TYPE t) { D3D12_HEAP_PROPERTIES p = {}; p.Type = t; return p; };
    auto tex = [&](DXGI_FORMAT f, D3D12_RESOURCE_FLAGS fl, D3D12_RESOURCE_STATES st, ID3D12Resource** out) {
        D3D12_RESOURCE_DESC d = {}; d.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D; d.Width = w; d.Height = h; d.DepthOrArraySize = 1; d.MipLevels = 1;
        d.Format = f; d.SampleDesc.Count = 1; d.Flags = fl; D3D12_HEAP_PROPERTIES hp = heap(D3D12_HEAP_TYPE_DEFAULT);
        return dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, st, nullptr, __uuidof(ID3D12Resource), (void**)out);
    };
    auto buf = [&](D3D12_HEAP_TYPE t, uint64_t size, D3D12_RESOURCE_STATES st, ID3D12Resource** out) {
        D3D12_RESOURCE_DESC d = {}; d.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; d.Width = size; d.Height = 1; d.DepthOrArraySize = 1; d.MipLevels = 1;
        d.SampleDesc.Count = 1; d.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR; D3D12_HEAP_PROPERTIES hp = heap(t);
        return dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, st, nullptr, __uuidof(ID3D12Resource), (void**)out);
    };
    const uint32_t pitch = (w * 4 + 255) & ~255u;   // D3D12_TEXTURE_DATA_PITCH_ALIGNMENT
    ID3D12Resource *back, *depth, *mv, *outs[3], *upload, *readback[3], *initUp, *mvUp;
    CHK(tex(DXGI_FORMAT_R8G8B8A8_UNORM, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST, &back));
    CHK(tex(DXGI_FORMAT_R32_FLOAT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST, &depth));
    CHK(tex(DXGI_FORMAT_R16G16_FLOAT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST, &mv));
    for (uint32_t i = 0; i < count; i++) {
        CHK(tex(DXGI_FORMAT_R8G8B8A8_UNORM, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, &outs[i]));
        CHK(buf(D3D12_HEAP_TYPE_READBACK, (uint64_t)pitch * h, D3D12_RESOURCE_STATE_COPY_DEST, &readback[i]));
    }
    CHK(buf(D3D12_HEAP_TYPE_UPLOAD, (uint64_t)pitch * h, D3D12_RESOURCE_STATE_GENERIC_READ, &upload));
    CHK(buf(D3D12_HEAP_TYPE_UPLOAD, (uint64_t)pitch * h, D3D12_RESOURCE_STATE_GENERIC_READ, &initUp));
    CHK(buf(D3D12_HEAP_TYPE_UPLOAD, (uint64_t)pitch * h, D3D12_RESOURCE_STATE_GENERIC_READ, &mvUp));
    uint8_t *up_ptr, *mv_ptr; { D3D12_RANGE r0 = {0, 0}; CHK(upload->Map(0, &r0, (void**)&up_ptr)); CHK(mvUp->Map(0, &r0, (void**)&mv_ptr)); }
    auto wait = [&]() { q->Signal(fence, ++fval); if (fence->GetCompletedValue() < fval) { fence->SetEventOnCompletion(fval, fev); WaitForSingleObject(fev, 10000); } };
    auto copyTexFromBuf = [&](ID3D12Resource* t, ID3D12Resource* b, DXGI_FORMAT f) {
        D3D12_TEXTURE_COPY_LOCATION dst = {}; dst.pResource = t; dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        D3D12_TEXTURE_COPY_LOCATION src = {}; src.pResource = b; src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        src.PlacedFootprint.Footprint = {f, w, h, 1, pitch}; cl->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    };
    auto fillMv = [&](uint8_t* mv_ptr, float mx, float my) {   // the same half-float vector in every texel
        const __m128i hh = _mm_cvtps_ph(_mm_setr_ps(mx, my, mx, my), 0);
        uint64_t two; _mm_storel_epi64((__m128i*)&two, hh);
        for (uint32_t y = 0; y < h; y++) { uint64_t* row = (uint64_t*)(mv_ptr + (size_t)y * pitch); for (uint32_t x = 0; x < w / 2; x++) row[x] = two; }
    };
    { // one-time init of the constant depth plane (0.5) and a zero motion field
        uint8_t* ip; D3D12_RANGE r0 = {0, 0}; CHK(initUp->Map(0, &r0, (void**)&ip));
        for (uint32_t y = 0; y < h; y++) { float* row = (float*)(ip + (size_t)y * pitch); for (uint32_t x = 0; x < w; x++) row[x] = 0.5f; }
        initUp->Unmap(0, nullptr);
        fillMv(mv_ptr, 0.f, 0.f);
        al->Reset(); cl->Reset(al, nullptr);
        copyTexFromBuf(depth, initUp, DXGI_FORMAT_R32_FLOAT);
        copyTexFromBuf(mv, mvUp, DXGI_FORMAT_R16G16_FLOAT);
        D3D12_RESOURCE_BARRIER bs[2] = {Trans(depth, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE), Trans(mv, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE)};
        cl->ResourceBarrier(2, bs);
        cl->Close(); ID3D12CommandList* ls[] = {cl}; q->ExecuteCommandLists(1, ls); wait();
    }
    D3D12_RESOURCE_STATES mvState = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, backState = D3D12_RESOURCE_STATE_COPY_DEST;
    const float mvSign = getenv("NGXG_MV_SIGN") ? (float)atof(getenv("NGXG_MV_SIGN")) : -1.f;

    // ---- feature ----
    NVSDK_NGX_Parameter* params = nullptr;
    if (NVSDK_NGX_FAILED(pAlloc(&params)) || !params) { fprintf(stderr, "[ngxg] AllocateParameters failed\n"); return fail(5); }
    setUI(params, NVSDK_NGX_Parameter_CreationNodeMask, 1); setUI(params, NVSDK_NGX_Parameter_VisibilityNodeMask, 1);
    setUI(params, NVSDK_NGX_Parameter_Width, w); setUI(params, NVSDK_NGX_Parameter_Height, h);
    setUI(params, NVSDK_NGX_DLSSG_Parameter_BackbufferFormat, (unsigned)DXGI_FORMAT_R8G8B8A8_UNORM);
    setUI(params, NVSDK_NGX_DLSSG_Parameter_InternalWidth, w); setUI(params, NVSDK_NGX_DLSSG_Parameter_InternalHeight, h);
    setUI(params, NVSDK_NGX_DLSSG_Parameter_DynamicResolution, 0);
    NVSDK_NGX_Handle* handle = nullptr;
    al->Reset(); cl->Reset(al, nullptr);
    r = pCreate(cl, NVSDK_NGX_Feature_FrameGeneration, params, &handle);
    cl->Close(); { ID3D12CommandList* ls[] = {cl}; q->ExecuteCommandLists(1, ls); wait(); }
    if (NVSDK_NGX_FAILED(r) || !handle) { fprintf(stderr, "[ngxg] CreateFeature failed 0x%08x\n", (unsigned)r); return fail(7); }
    static float ident[16] = {1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1};
    fprintf(stderr, "[ngxg] ready %ux%u x%u (ring %u)\n", w, h, count, ring);
    MemoryBarrier(); ctrl->state = 1;


    // ---- two-slot pipeline ----
    // Slot s = seq % 2 owns: input region, upload heap, command allocator/list, readback buffers, motion-vector staging. The GPU queue runs the slots in order (so
    // NGX's history stays sequential); the CPU work of the next frame (copy-in, recording) overlaps the GPU work of the current one, and a completion thread
    // converts the finished frame's readback into the output ring and acknowledges it while the main thread is already on the next frame.
    struct Slot { ID3D12CommandAllocator* al; ID3D12GraphicsCommandList* cl; ID3D12Resource *upload, *mvUp, *readback[3]; uint8_t *up_ptr, *mv_ptr; HANDLE done; };
    Slot slots[2] = {};
    slots[0] = {al, cl, upload, mvUp, {readback[0], readback[1], readback[2]}, up_ptr, mv_ptr, CreateEventA(nullptr, TRUE, TRUE, nullptr)};
    { Slot& t = slots[1]; t.done = CreateEventA(nullptr, TRUE, TRUE, nullptr);
      CHK(dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), (void**)&t.al));
      CHK(dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, t.al, nullptr, __uuidof(ID3D12GraphicsCommandList), (void**)&t.cl)); t.cl->Close();
      CHK(buf(D3D12_HEAP_TYPE_UPLOAD, (uint64_t)pitch * h, D3D12_RESOURCE_STATE_GENERIC_READ, &t.upload));
      CHK(buf(D3D12_HEAP_TYPE_UPLOAD, (uint64_t)pitch * h, D3D12_RESOURCE_STATE_GENERIC_READ, &t.mvUp));
      for (uint32_t i = 0; i < count; i++) CHK(buf(D3D12_HEAP_TYPE_READBACK, (uint64_t)pitch * h, D3D12_RESOURCE_STATE_COPY_DEST, &t.readback[i]));
      D3D12_RANGE r0 = {0, 0}; CHK(t.upload->Map(0, &r0, (void**)&t.up_ptr)); CHK(t.mvUp->Map(0, &r0, (void**)&t.mv_ptr)); }

    struct Done { int slot; uint64_t seq, fence; uint32_t made; bool first, ok; double t_in, t_rec; LARGE_INTEGER t0; };
    struct Q { CRITICAL_SECTION cs; HANDLE ev; std::vector<Done> items; } dq; InitializeCriticalSection(&dq.cs); dq.ev = CreateEventA(nullptr, FALSE, FALSE, nullptr);
    volatile bool stopping = false;
    LARGE_INTEGER qf; QueryPerformanceFrequency(&qf);
    auto msBetween = [&](LARGE_INTEGER a, LARGE_INTEGER b) { return (double)(b.QuadPart - a.QuadPart) * 1000.0 / qf.QuadPart; };
    struct Ctx { Ctrl* ctrl; Ext* ext; Slot* slots; Q* dq; ID3D12Fence* fence; uint8_t* out_map; uint32_t w, h, pitch, count, ring; size_t bytes; volatile bool* stopping; LARGE_INTEGER qf; } cx =
        {ctrl, ext, slots, &dq, fence, out_map, w, h, pitch, count, ring, bytes, &stopping, qf};
    HANDLE cev = CreateEventA(nullptr, FALSE, FALSE, nullptr);
    struct CompleteArgs { Ctx* cx; HANDLE cev; } ca = {&cx, cev};
    HANDLE completion = CreateThread(nullptr, 0, [](LPVOID v) -> DWORD {
        CompleteArgs* a = (CompleteArgs*)v; Ctx& c = *a->cx; const __m256i sh = _mm256_setr_epi8(2,1,0,3, 6,5,4,7, 10,9,8,11, 14,13,12,15, 2,1,0,3, 6,5,4,7, 10,9,8,11, 14,13,12,15);
        for (;;) {
            Done d; bool have = false;
            EnterCriticalSection(&c.dq->cs); if (!c.dq->items.empty()) { d = c.dq->items.front(); c.dq->items.erase(c.dq->items.begin()); have = true; } LeaveCriticalSection(&c.dq->cs);
            if (!have) { if (*c.stopping) return 0; WaitForSingleObject(c.dq->ev, 50); continue; }
            if (c.fence->GetCompletedValue() < d.fence) { c.fence->SetEventOnCompletion(d.fence, a->cev); WaitForSingleObject(a->cev, 10000); }
            LARGE_INTEGER t1, t2; QueryPerformanceCounter(&t1);
            Slot& sl = c.slots[d.slot]; const uint32_t made = d.ok ? d.made : 0;
            for (uint32_t i = 0; i < made; i++) {
                uint8_t* rp; D3D12_RANGE rr = {0, (SIZE_T)c.pitch * c.h}; sl.readback[i]->Map(0, &rr, (void**)&rp);
                uint8_t* dstp = c.out_map + ((size_t)(d.seq % c.ring) * c.count + i) * c.bytes;
                struct V { const uint8_t* rp; uint8_t* dst; uint32_t pitch, w; } vv{rp, dstp, c.pitch, c.w};
                parallelRows(c.h, [](void* p, uint32_t r0, uint32_t r1) {
                    V* v = (V*)p; const __m256i sh = _mm256_setr_epi8(2,1,0,3, 6,5,4,7, 10,9,8,11, 14,13,12,15, 2,1,0,3, 6,5,4,7, 10,9,8,11, 14,13,12,15);
                    for (uint32_t y = r0; y < r1; y++) {   // RGBA -> BGRA: swap bytes 0 and 2 of every pixel (AVX2 shuffle), alpha forced opaque
                        const uint8_t* s = v->rp + (size_t)y * v->pitch; uint8_t* dd = v->dst + (size_t)y * v->w * 4; uint32_t x = 0;
                        for (; x + 8 <= v->w; x += 8) {
                            __m256i px = _mm256_loadu_si256((const __m256i*)(s + x * 4));
                            px = _mm256_or_si256(_mm256_shuffle_epi8(px, sh), _mm256_set1_epi32((int)0xFF000000u));
                            _mm256_storeu_si256((__m256i*)(dd + x * 4), px);
                        }
                        for (; x < v->w; x++) { dd[x*4] = s[x*4+2]; dd[x*4+1] = s[x*4+1]; dd[x*4+2] = s[x*4]; dd[x*4+3] = 255; }
                    }
                }, &vv);
                D3D12_RANGE wr = {0, 0}; sl.readback[i]->Unmap(0, &wr);
            }
            QueryPerformanceCounter(&t2);
            const uint32_t k = (uint32_t)(d.seq % 4);
            c.ext->gen[k] = made; c.ext->info[k] = d.first ? 1 : 0; c.ext->ok[k] = d.ok ? 1 : 0;
            if (getenv("NGXG_PROF")) { static int n = 0; if (++n % 20 == 0) fprintf(stderr, "[ngxg] host: copy-in %.1f ms, record %.1f ms, gpu+wait %.1f ms, readback->out %.1f ms, in flight %.1f ms\n", d.t_in, d.t_rec,
                (double)(t1.QuadPart - d.t0.QuadPart) * 1000.0 / c.qf.QuadPart - d.t_in - d.t_rec, (double)(t2.QuadPart - t1.QuadPart) * 1000.0 / c.qf.QuadPart, (double)(t2.QuadPart - d.t0.QuadPart) * 1000.0 / c.qf.QuadPart); }
            MemoryBarrier(); c.ctrl->ok = d.ok ? 1 : 0; c.ctrl->ack_seq = (uint32_t)d.seq;
            SetEvent(sl.done);
        }
    }, &ca, 0, nullptr);

    uint64_t next = 1, frameId = 0;
    uint32_t spins = 0; float lastGx = 0.f, lastGy = 0.f;
    while (!ctrl->quit) {
        if (next > ctrl->req_seq) { if (++spins > 2000) Sleep(1); else Sleep(0); continue; }
        spins = 0;
        const int si = (int)(next % 2); Slot& S = slots[si];
        WaitForSingleObject(S.done, INFINITE);   // the completion thread has finished with this slot's previous frame (seq - 2)
        ResetEvent(S.done);
        LARGE_INTEGER q0, qc, qa; QueryPerformanceCounter(&q0);
        const bool first = frameId == 0 || (ext->flags[si] & 1);
        { struct C { uint8_t* dst; const uint8_t* src; uint32_t pitch, w; } c{S.up_ptr, in_map + (size_t)si * bytes, pitch, w};
          parallelRows(h, [](void* v, uint32_t r0, uint32_t r1) { C* c = (C*)v; for (uint32_t y = r0; y < r1; y++) memcpy(c->dst + (size_t)y * c->pitch, c->src + (size_t)y * c->w * 4, (size_t)c->w * 4); }, &c); }
        QueryPerformanceCounter(&qc);
        S.al->Reset(); S.cl->Reset(S.al, nullptr); ID3D12GraphicsCommandList* L = S.cl;
        auto copyTex = [&](ID3D12Resource* t, ID3D12Resource* b, DXGI_FORMAT f) {
            D3D12_TEXTURE_COPY_LOCATION dst = {}; dst.pResource = t; dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
            D3D12_TEXTURE_COPY_LOCATION src = {}; src.pResource = b; src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
            src.PlacedFootprint.Footprint = {f, w, h, 1, pitch}; L->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr); };
        if (backState != D3D12_RESOURCE_STATE_COPY_DEST) { auto b = Trans(back, backState, D3D12_RESOURCE_STATE_COPY_DEST); L->ResourceBarrier(1, &b); }
        copyTex(back, S.upload, DXGI_FORMAT_R8G8B8A8_UNORM);
        { auto b = Trans(back, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE); L->ResourceBarrier(1, &b); backState = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE; }
        const float gx = ext->mvx[si], gy = ext->mvy[si];
        if (gx != lastGx || gy != lastGy) {   // global content motion hint (display px) -> uniform vector field (uploaded only when it changed)
            lastGx = gx; lastGy = gy;
            fillMv(S.mv_ptr, gx * mvSign, gy * mvSign);
            auto b0 = Trans(mv, mvState, D3D12_RESOURCE_STATE_COPY_DEST); L->ResourceBarrier(1, &b0);
            copyTex(mv, S.mvUp, DXGI_FORMAT_R16G16_FLOAT);
            auto b1 = Trans(mv, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE); L->ResourceBarrier(1, &b1);
        }
        ++frameId;
        uint32_t made = 0; bool evalOk = true;
        for (uint32_t i = 0; i < count; i++) {
            setRes(params, NVSDK_NGX_DLSSG_Parameter_Backbuffer, back); setRes(params, NVSDK_NGX_DLSSG_Parameter_MVecs, mv);
            setRes(params, NVSDK_NGX_DLSSG_Parameter_Depth, depth); setRes(params, NVSDK_NGX_DLSSG_Parameter_OutputInterpolated, outs[i]);
            setUI(params, NVSDK_NGX_DLSSG_Parameter_MultiFrameCount, count); setUI(params, NVSDK_NGX_DLSSG_Parameter_MultiFrameIndex, i + 1);
            setF(params, NVSDK_NGX_DLSSG_Parameter_MvecScaleX, 1.f); setF(params, NVSDK_NGX_DLSSG_Parameter_MvecScaleY, 1.f);
            setF(params, NVSDK_NGX_DLSSG_Parameter_JitterOffsetX, 0.f); setF(params, NVSDK_NGX_DLSSG_Parameter_JitterOffsetY, 0.f);
            setPtr(params, NVSDK_NGX_DLSSG_Parameter_CameraViewToClip, ident); setPtr(params, NVSDK_NGX_DLSSG_Parameter_ClipToCameraView, ident);
            setPtr(params, NVSDK_NGX_DLSSG_Parameter_ClipToLensClip, ident); setPtr(params, NVSDK_NGX_DLSSG_Parameter_ClipToPrevClip, ident);
            setPtr(params, NVSDK_NGX_DLSSG_Parameter_PrevClipToClip, ident);
            setF(params, NVSDK_NGX_DLSSG_Parameter_CameraNear, 0.1f); setF(params, NVSDK_NGX_DLSSG_Parameter_CameraFar, 1000.f);
            setF(params, NVSDK_NGX_DLSSG_Parameter_CameraFOV, 1.0f); setF(params, NVSDK_NGX_DLSSG_Parameter_CameraAspectRatio, (float)w / h);
            setF(params, NVSDK_NGX_DLSSG_Parameter_CameraUpY, 1.f); setF(params, NVSDK_NGX_DLSSG_Parameter_CameraRightX, 1.f); setF(params, NVSDK_NGX_DLSSG_Parameter_CameraFwdZ, 1.f);
            setUI(params, NVSDK_NGX_DLSSG_Parameter_ColorBuffersHDR, 0); setUI(params, NVSDK_NGX_DLSSG_Parameter_DepthInverted, 0);
            setUI(params, NVSDK_NGX_DLSSG_Parameter_CameraMotionIncluded, 0); setUI(params, NVSDK_NGX_DLSSG_Parameter_MvecDilated, 0);
            setUI(params, NVSDK_NGX_DLSSG_Parameter_Reset, first ? 1 : 0);
            r = pEval(L, handle, params, nullptr);
            if (NVSDK_NGX_FAILED(r)) { fprintf(stderr, "[ngxg] EvaluateFeature failed 0x%08x\n", (unsigned)r); evalOk = false; break; }
            if (first) break;
            auto b1 = Trans(outs[i], D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE); L->ResourceBarrier(1, &b1);
            D3D12_TEXTURE_COPY_LOCATION src = {}; src.pResource = outs[i]; src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
            D3D12_TEXTURE_COPY_LOCATION dst = {}; dst.pResource = S.readback[i]; dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
            dst.PlacedFootprint.Footprint = {DXGI_FORMAT_R8G8B8A8_UNORM, w, h, 1, pitch}; L->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
            auto b2 = Trans(outs[i], D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS); L->ResourceBarrier(1, &b2);
            made++;
        }
        QueryPerformanceCounter(&qa);
        L->Close(); ID3D12CommandList* ls[] = {L}; q->ExecuteCommandLists(1, ls);
        q->Signal(fence, ++fval);
        Done d = {si, next, fval, made, first, evalOk, msBetween(q0, qc), msBetween(qc, qa), q0};
        EnterCriticalSection(&dq.cs); dq.items.push_back(d); LeaveCriticalSection(&dq.cs); SetEvent(dq.ev);
        ++next;
    }
    stopping = true; WaitForSingleObject(completion, 5000);
    pRelease(handle); pShutdown(dev);
    return 0;
}
