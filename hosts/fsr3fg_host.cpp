// SPDX-License-Identifier: MIT
// FSR 3.1 frame generation host (Windows exe, runs under Wine + vkd3d-proton). Same file protocol as dlssg_host.exe: the Python side writes RGBA8 frames into
// an "in" file and bumps req_seq in a control block; we run the AMD FidelityFX frame-interpolation DLL (signed DX12 build from the FSR SDK, MIT) on the
// NVIDIA GPU and write the generated frames (BGRA8, cairo order) into a ring of regions of the "out" file.
//
// No engine data exists for a screen filter, so the depth is a constant plane and the motion vectors are zero: the interpolation is driven by FSR's own
// optical flow on the colour frames.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <immintrin.h>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include "ffx_api.h"
#include "ffx_api_types.h"
#include "dx12/ffx_api_dx12.h"
#include "ffx_framegeneration.h"

static void* MapFile(const char* path, size_t bytes) {
    HANDLE f = CreateFileA(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (f == INVALID_HANDLE_VALUE) { fprintf(stderr, "[fsr3] open %s failed %lu\n", path, GetLastError()); return nullptr; }
    HANDLE m = CreateFileMappingA(f, nullptr, PAGE_READWRITE, 0, (DWORD)bytes, nullptr);
    if (!m) { fprintf(stderr, "[fsr3] mapping failed %lu\n", GetLastError()); return nullptr; }
    return MapViewOfFile(m, FILE_MAP_ALL_ACCESS, 0, 0, bytes);
}

struct Ctrl { volatile uint32_t state;   // 0 starting, 1 ready, 2 error
              volatile uint32_t req_seq, ack_seq, ok, quit;
              volatile float total_ms, ngx_ms;
              volatile uint32_t generated, info, frame_flags;
              volatile float flow_ms;
              volatile float mv_x, mv_y; };   // global content motion prev->cur in DISPLAY pixels, estimated by the Python side (0,0 = none)

#define CHK(x) do { HRESULT _h = (x); if (FAILED(_h)) { fprintf(stderr, "[fsr3] %s failed: 0x%08lx (line %d)\n", #x, (unsigned long)_h, __LINE__); return fail(20); } } while (0)

static D3D12_RESOURCE_BARRIER Trans(ID3D12Resource* r, D3D12_RESOURCE_STATES a, D3D12_RESOURCE_STATES b) {
    D3D12_RESOURCE_BARRIER x = {}; x.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION; x.Transition.pResource = r;
    x.Transition.StateBefore = a; x.Transition.StateAfter = b; x.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES; return x;
}

int main(int argc, char** argv) {
    if (argc < 8) { fprintf(stderr, "[fsr3] usage: width height count in out ctrl dir\n"); return 2; }
    const uint32_t w = atoi(argv[1]), h = atoi(argv[2]), count = std::max(1, std::min(3, atoi(argv[3])));
    Ctrl* ctrl = (Ctrl*)MapFile(argv[6], sizeof(Ctrl));
    if (!ctrl) return 2;
    auto fail = [&](int code) { ctrl->state = 2; return code; };
    const uint32_t ring = getenv("FSR3_RING") ? std::max(1, atoi(getenv("FSR3_RING"))) : 1;
    const size_t bytes = (size_t)w * h * 4;
    uint8_t* in_map = (uint8_t*)MapFile(argv[4], bytes);
    uint8_t* out_map = (uint8_t*)MapFile(argv[5], bytes * count * ring);
    if (!in_map || !out_map) return fail(8);

    HMODULE fg = LoadLibraryA("amd_fidelityfx_framegeneration_dx12.dll");
    if (!fg) { fprintf(stderr, "[fsr3] LoadLibrary FG dll failed %lu\n", GetLastError()); return fail(3); }
    auto ffxCreate = (PfnFfxCreateContext)GetProcAddress(fg, "ffxCreateContext");
    auto ffxDestroy = (PfnFfxDestroyContext)GetProcAddress(fg, "ffxDestroyContext");
    auto ffxConf = (PfnFfxConfigure)GetProcAddress(fg, "ffxConfigure");
    auto ffxDisp = (PfnFfxDispatch)GetProcAddress(fg, "ffxDispatch");
    if (!ffxCreate || !ffxDestroy || !ffxConf || !ffxDisp) { fprintf(stderr, "[fsr3] missing ffx exports\n"); return fail(4); }

    auto mkDev = (PFN_D3D12_CREATE_DEVICE)GetProcAddress(LoadLibraryA("d3d12.dll"), "D3D12CreateDevice");
    ID3D12Device* dev = nullptr;
    CHK(mkDev(nullptr, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), (void**)&dev));
    D3D12_COMMAND_QUEUE_DESC qd = {}; qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    ID3D12CommandQueue* q; CHK(dev->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void**)&q));
    ID3D12CommandAllocator* al; CHK(dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), (void**)&al));
    ID3D12GraphicsCommandList* cl; CHK(dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, al, nullptr, __uuidof(ID3D12GraphicsCommandList), (void**)&cl));
    cl->Close();
    ID3D12Fence* fence; CHK(dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), (void**)&fence));
    HANDLE fev = CreateEventA(nullptr, FALSE, FALSE, nullptr); uint64_t fval = 0;

    auto heap = [](D3D12_HEAP_TYPE t) { D3D12_HEAP_PROPERTIES p = {}; p.Type = t; return p; };
    auto tex = [&](DXGI_FORMAT f, uint32_t tw, uint32_t th, D3D12_RESOURCE_FLAGS fl, D3D12_RESOURCE_STATES st, ID3D12Resource** out) {
        D3D12_RESOURCE_DESC d = {}; d.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D; d.Width = tw; d.Height = th; d.DepthOrArraySize = 1; d.MipLevels = 1;
        d.Format = f; d.SampleDesc.Count = 1; d.Flags = fl; D3D12_HEAP_PROPERTIES hp = heap(D3D12_HEAP_TYPE_DEFAULT);
        return dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, st, nullptr, __uuidof(ID3D12Resource), (void**)out);
    };
    auto buf = [&](D3D12_HEAP_TYPE t, uint64_t size, D3D12_RESOURCE_STATES st, ID3D12Resource** out) {
        D3D12_RESOURCE_DESC d = {}; d.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; d.Width = size; d.Height = 1; d.DepthOrArraySize = 1; d.MipLevels = 1;
        d.SampleDesc.Count = 1; d.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR; D3D12_HEAP_PROPERTIES hp = heap(t);
        return dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, st, nullptr, __uuidof(ID3D12Resource), (void**)out);
    };
    const uint32_t pitch = (w * 4 + 255) & ~255u;   // D3D12_TEXTURE_DATA_PITCH_ALIGNMENT
    // Depth and motion vectors live at a reduced 'render size': they are constants / a single global vector, so a small plane is enough (and cheap to upload each frame).
    const uint32_t rdiv = getenv("FSR3_RENDER_DIV") ? std::max(1, atoi(getenv("FSR3_RENDER_DIV"))) : 4;
    const uint32_t rw = std::max(16u, (w / rdiv) & ~1u), rh = std::max(16u, (h / rdiv) & ~1u);
    const uint32_t rpitch = (rw * 4 + 255) & ~255u;
    ID3D12Resource *colors[2], *depth, *mv, *outs[3], *upload, *readback[3], *initUp;
    for (int i = 0; i < 2; i++) CHK(tex(DXGI_FORMAT_R8G8B8A8_UNORM, w, h, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST, &colors[i]));
    CHK(tex(DXGI_FORMAT_R32_FLOAT, rw, rh, D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_COPY_DEST, &depth));
    CHK(tex(DXGI_FORMAT_R16G16_FLOAT, rw, rh, D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_COPY_DEST, &mv));
    for (uint32_t i = 0; i < count; i++) {
        CHK(tex(DXGI_FORMAT_R8G8B8A8_UNORM, w, h, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, &outs[i]));
        CHK(buf(D3D12_HEAP_TYPE_READBACK, (uint64_t)pitch * h, D3D12_RESOURCE_STATE_COPY_DEST, &readback[i]));
    }
    CHK(buf(D3D12_HEAP_TYPE_UPLOAD, (uint64_t)pitch * h, D3D12_RESOURCE_STATE_GENERIC_READ, &upload));
    CHK(buf(D3D12_HEAP_TYPE_UPLOAD, (uint64_t)rpitch * rh, D3D12_RESOURCE_STATE_GENERIC_READ, &initUp));   // constants for the depth / motion-vector planes
    uint8_t* up_ptr; { D3D12_RANGE r = {0, 0}; CHK(upload->Map(0, &r, (void**)&up_ptr)); }
    auto wait = [&]() { q->Signal(fence, ++fval); if (fence->GetCompletedValue() < fval) { fence->SetEventOnCompletion(fval, fev); WaitForSingleObject(fev, 10000); } };
    auto copyTexFromBuf = [&](ID3D12Resource* t, ID3D12Resource* b, DXGI_FORMAT f, uint32_t p, uint32_t cw = 0, uint32_t ch = 0) {
        D3D12_TEXTURE_COPY_LOCATION dst = {}; dst.pResource = t; dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        D3D12_TEXTURE_COPY_LOCATION src = {}; src.pResource = b; src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        src.PlacedFootprint.Footprint = {f, cw ? cw : w, ch ? ch : h, 1, p}; cl->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    };
    ID3D12Resource* mvUp; CHK(buf(D3D12_HEAP_TYPE_UPLOAD, (uint64_t)rpitch * rh, D3D12_RESOURCE_STATE_GENERIC_READ, &mvUp));
    uint8_t* mv_ptr; { D3D12_RANGE r = {0, 0}; CHK(mvUp->Map(0, &r, (void**)&mv_ptr)); }
    auto fillMv = [&](float mx, float my) {   // the same half-float vector in every texel
        const __m128i hh = _mm_cvtps_ph(_mm_setr_ps(mx, my, mx, my), 0);
        uint64_t two; _mm_storel_epi64((__m128i*)&two, hh);   // 4 halves = 2 texels (x,y,x,y)
        for (uint32_t y = 0; y < rh; y++) { uint64_t* row = (uint64_t*)(mv_ptr + (size_t)y * rpitch); for (uint32_t x = 0; x < rw / 2; x++) row[x] = two; }
    };
    { // one-time init of the constant depth plane (0.5) and a zero motion field
        uint8_t* ip; D3D12_RANGE r = {0, 0}; CHK(initUp->Map(0, &r, (void**)&ip));
        const float half = 0.5f; for (uint32_t y = 0; y < rh; y++) { float* row = (float*)(ip + (size_t)y * rpitch); for (uint32_t x = 0; x < rw; x++) row[x] = half; }
        initUp->Unmap(0, nullptr);
        fillMv(0.f, 0.f);
        al->Reset(); cl->Reset(al, nullptr);
        copyTexFromBuf(depth, initUp, DXGI_FORMAT_R32_FLOAT, rpitch, rw, rh);
        copyTexFromBuf(mv, mvUp, DXGI_FORMAT_R16G16_FLOAT, rpitch, rw, rh);
        D3D12_RESOURCE_BARRIER bs[2] = {Trans(depth, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE), Trans(mv, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE)};
        cl->ResourceBarrier(2, bs);
        cl->Close(); ID3D12CommandList* ls[] = {cl}; q->ExecuteCommandLists(1, ls); wait();
    }
    D3D12_RESOURCE_STATES mvState = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
    const float mvSign = getenv("FSR3_MV_SIGN") ? (float)atof(getenv("FSR3_MV_SIGN")) : -1.f;   // FSR wants the vector from the current pixel back to its previous position
    const float mvScale = getenv("FSR3_MV_SCALE") ? (float)atof(getenv("FSR3_MV_SCALE")) : 1.f;

    // ---- FFX context ----
    ffxCreateBackendDX12Desc backend{}; backend.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12; backend.device = dev;
    ffxCreateContextDescFrameGenerationVersion ver{}; ver.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION_VERSION; ver.version = FFX_FRAMEGENERATION_VERSION;
    ffxCreateContextDescFrameGeneration cd{}; cd.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION;
    cd.flags = 0; cd.displaySize = {w, h}; cd.maxRenderSize = {rw, rh}; cd.backBufferFormat = FFX_API_SURFACE_FORMAT_R8G8B8A8_UNORM;
    cd.header.pNext = &backend.header; backend.header.pNext = &ver.header;
    ffxContext ctx = nullptr;
    ffxReturnCode_t rc = ffxCreate(&ctx, &cd.header, nullptr);
    if (rc != 0) { fprintf(stderr, "[fsr3] ffxCreateContext failed rc=%u\n", (unsigned)rc); return fail(5); }
    ffxConfigureDescFrameGeneration conf{}; conf.header.type = FFX_API_CONFIGURE_DESC_TYPE_FRAMEGENERATION;
    conf.frameGenerationEnabled = true; conf.allowAsyncWorkloads = false; conf.flags = FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY;
    conf.generationRect = {0, 0, (int32_t)w, (int32_t)h};
    rc = ffxConf(&ctx, &conf.header);
    fprintf(stderr, "[fsr3] context created, configure rc=%u, %ux%u x%u (ring %u)\n", (unsigned)rc, w, h, count, ring);
    MemoryBarrier(); ctrl->state = 1;

    std::vector<uint8_t> dummy;
    uint64_t frameId = 0; LARGE_INTEGER qf, tprev = {}; QueryPerformanceFrequency(&qf);
    uint32_t spins = 0;
    D3D12_RESOURCE_STATES colorState[2] = {D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COPY_DEST};
    while (!ctrl->quit) {
        if (ctrl->req_seq == ctrl->ack_seq) { if (++spins > 2000) Sleep(1); else Sleep(0); continue; }
        spins = 0;
        LARGE_INTEGER q0, q1; QueryPerformanceCounter(&q0);
        const bool first = frameId == 0 || (ctrl->frame_flags & 1);
        double dt_ms = tprev.QuadPart ? (double)(q0.QuadPart - tprev.QuadPart) * 1000.0 / qf.QuadPart : 16.6; tprev = q0;
        // input frame -> upload buffer (rows padded to the copy pitch)
        for (uint32_t y = 0; y < h; y++) memcpy(up_ptr + (size_t)y * pitch, in_map + (size_t)y * w * 4, (size_t)w * 4);
        al->Reset(); cl->Reset(al, nullptr);
        ID3D12Resource* color = colors[(frameId + 1) & 1];   // ping-pong (frameId is incremented below)
        D3D12_RESOURCE_STATES& cst = colorState[(frameId + 1) & 1];
        if (cst != D3D12_RESOURCE_STATE_COPY_DEST) { auto b = Trans(color, cst, D3D12_RESOURCE_STATE_COPY_DEST); cl->ResourceBarrier(1, &b); }
        copyTexFromBuf(color, upload, DXGI_FORMAT_R8G8B8A8_UNORM, pitch);
        static float lastGx = 0.f, lastGy = 0.f;
        if (ctrl->mv_x != lastGx || ctrl->mv_y != lastGy)
        {   // global content motion hint (display px) -> a uniform vector field at render size, in render-size pixels (uploaded only when it changed)
            const float gx = ctrl->mv_x, gy = ctrl->mv_y; lastGx = gx; lastGy = gy;
            fillMv(gx * mvSign * (float)rw / (float)w * mvScale, gy * mvSign * (float)rh / (float)h * mvScale);
            auto b0 = Trans(mv, mvState, D3D12_RESOURCE_STATE_COPY_DEST); cl->ResourceBarrier(1, &b0);
            copyTexFromBuf(mv, mvUp, DXGI_FORMAT_R16G16_FLOAT, rpitch, rw, rh);
            auto b1 = Trans(mv, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE); cl->ResourceBarrier(1, &b1);
        }
        { auto b = Trans(color, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE); cl->ResourceBarrier(1, &b); cst = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE; }

        ++frameId;
        conf.frameID = frameId;   // the provider treats a dispatch whose frameID differs from the last configured one as a discontinuity and resets
        rc = ffxConf(&ctx, &conf.header);
        if (rc != 0) fprintf(stderr, "[fsr3] configure rc=%u\n", (unsigned)rc);
        ffxDispatchDescFrameGenerationPrepareV2 prep{}; prep.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION_PREPARE_V2;
        prep.frameID = frameId; prep.flags = FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY; prep.commandList = cl; prep.renderSize = {rw, rh};
        prep.jitterOffset = {0, 0}; prep.motionVectorScale = {1.f, 1.f}; prep.frameTimeDelta = (float)dt_ms; prep.reset = first;
        prep.cameraNear = 0.1f; prep.cameraFar = 1000.f; prep.cameraFovAngleVertical = 1.0f; prep.viewSpaceToMetersFactor = 1.f;
        prep.depth = ffxApiGetResourceDX12(depth, FFX_API_RESOURCE_STATE_COMPUTE_READ); prep.motionVectors = ffxApiGetResourceDX12(mv, FFX_API_RESOURCE_STATE_COMPUTE_READ);
        prep.cameraUp[1] = 1.f; prep.cameraRight[0] = 1.f; prep.cameraForward[2] = 1.f;
        rc = ffxDisp(&ctx, &prep.header);
        if (rc != 0) fprintf(stderr, "[fsr3] prepare rc=%u\n", (unsigned)rc);

        ffxDispatchDescFrameGeneration gen{}; gen.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION;
        gen.commandList = cl; gen.presentColor = ffxApiGetResourceDX12(color, FFX_API_RESOURCE_STATE_COMPUTE_READ);
        for (uint32_t i = 0; i < count; i++) gen.outputs[i] = ffxApiGetResourceDX12(outs[i], FFX_API_RESOURCE_STATE_UNORDERED_ACCESS);
        gen.numGeneratedFrames = first ? 0 : count; gen.reset = first; gen.backbufferTransferFunction = FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB;
        gen.minMaxLuminance[0] = 0.f; gen.minMaxLuminance[1] = 1.f; gen.generationRect = {0, 0, (int32_t)w, (int32_t)h}; gen.frameID = frameId;
        uint32_t made = 0;
        rc = ffxDisp(&ctx, &gen.header);
        if (rc != 0) fprintf(stderr, "[fsr3] generation rc=%u\n", (unsigned)rc);
        if (rc == 0 && !first) {
            for (uint32_t i = 0; i < count; i++) {
                auto b1 = Trans(outs[i], D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE); cl->ResourceBarrier(1, &b1);
                D3D12_TEXTURE_COPY_LOCATION src = {}; src.pResource = outs[i]; src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
                D3D12_TEXTURE_COPY_LOCATION dst = {}; dst.pResource = readback[i]; dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
                dst.PlacedFootprint.Footprint = {DXGI_FORMAT_R8G8B8A8_UNORM, w, h, 1, pitch}; cl->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
                auto b2 = Trans(outs[i], D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS); cl->ResourceBarrier(1, &b2);
            }
            made = count;
        }
        LARGE_INTEGER qa; QueryPerformanceCounter(&qa);
        cl->Close(); ID3D12CommandList* ls[] = {cl}; q->ExecuteCommandLists(1, ls); wait();
        QueryPerformanceCounter(&q1);
        const double t_in = (double)(qa.QuadPart - q0.QuadPart) * 1000.0 / qf.QuadPart, t_gpu = (double)(q1.QuadPart - qa.QuadPart) * 1000.0 / qf.QuadPart;
        for (uint32_t i = 0; i < made; i++) {   // readback (RGBA, padded rows) -> out ring region, BGRA
            uint8_t* rp; D3D12_RANGE rr = {0, (SIZE_T)pitch * h}; readback[i]->Map(0, &rr, (void**)&rp);
            uint8_t* dstp = out_map + ((size_t)(ctrl->req_seq % ring) * count + i) * bytes;
            for (uint32_t y = 0; y < h; y++) {
                const uint8_t* s = rp + (size_t)y * pitch; uint8_t* d = dstp + (size_t)y * w * 4; uint32_t x = 0;
                for (; x + 8 <= w; x += 8) {   // RGBA -> BGRA: swap bytes 0 and 2 of every pixel (AVX2 shuffle), alpha forced opaque
                    __m256i v = _mm256_loadu_si256((const __m256i*)(s + x * 4));
                    const __m256i sh = _mm256_setr_epi8(2,1,0,3, 6,5,4,7, 10,9,8,11, 14,13,12,15, 2,1,0,3, 6,5,4,7, 10,9,8,11, 14,13,12,15);
                    v = _mm256_or_si256(_mm256_shuffle_epi8(v, sh), _mm256_set1_epi32((int)0xFF000000u));
                    _mm256_storeu_si256((__m256i*)(d + x * 4), v);
                }
                for (; x < w; x++) { d[x*4] = s[x*4+2]; d[x*4+1] = s[x*4+1]; d[x*4+2] = s[x*4]; d[x*4+3] = 255; }
            }
            D3D12_RANGE wr = {0, 0}; readback[i]->Unmap(0, &wr);
        }
        if (getenv("FSR3_PROF")) { static int n = 0; if (++n % 20 == 0) { LARGE_INTEGER qe; QueryPerformanceCounter(&qe); fprintf(stderr, "[fsr3] host: cpu-in+record %.1f ms, gpu+wait %.1f ms, readback/convert %.1f ms\n", t_in, t_gpu, (double)(qe.QuadPart - q1.QuadPart) * 1000.0 / qf.QuadPart); } }
        ctrl->generated = made; ctrl->total_ms = 0; ctrl->ngx_ms = 0; ctrl->flow_ms = 0; ctrl->info = first ? 1 : 0; ctrl->ok = 1;
        MemoryBarrier(); ctrl->ack_seq = ctrl->req_seq;
    }
    ffxDestroy(&ctx, nullptr);
    return 0;
}
