// SPDX-License-Identifier: MIT
// Feasibility probe: drive NVIDIA's DLSS frame generation through the PUBLIC NGX API (headers from github.com/NVIDIA/DLSS) under Wine,
// without any third-party bridge: load _nvngx.dll, create a D3D12 device (vkd3d-proton), init NGX, ask whether FrameGeneration is available, create the feature.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <vector>
#include <algorithm>
#include <cstdint>
#include "nvsdk_ngx.h"
#include "nvsdk_ngx_defs_dlssg.h"
#include "nvsdk_ngx_params_dlssg.h"
#include "nvsdk_ngx_params.h"

static void NVSDK_CONV ngxLog(const char* msg, NVSDK_NGX_Logging_Level lvl, NVSDK_NGX_Feature f) { printf("[ngx %d/%d] %s", (int)lvl, (int)f, msg); size_t n = strlen(msg); if (!n || msg[n-1] != '\n') printf("\n"); }

#define GETP(T, name) auto p_##name = (decltype(&name))GetProcAddress(ngx, #name); if (!p_##name) { printf("missing export %s\n", #name); return 3; }

// The NGX runtime is built by MSVC, which lays out overloaded virtuals differently from g++, so the header's
// C++ methods hit the wrong slots. Slot indices below were read off NVIDIA's own static wrappers (nvsdk_ngx_s.lib).
enum { S_VOID = 0, S_D3D12 = 1, S_D3D11 = 2, S_I = 3, S_UI = 4, S_D = 5, S_F = 6, S_ULL = 7, G_VOID = 8, G_D3D12 = 9, G_D3D11 = 10, G_I = 11, G_UI = 12, G_D = 13, G_F = 14, G_ULL = 15 };
template <class T> static void slotSet(NVSDK_NGX_Parameter* p, int slot, const char* n, T v) { (*(void (**)(void*, const char*, T))(*(void***)p + slot))(p, n, v); }
template <class T> static NVSDK_NGX_Result slotGet(NVSDK_NGX_Parameter* p, int slot, const char* n, T* v) { return (*(NVSDK_NGX_Result (**)(void*, const char*, T*))(*(void***)p + slot))(p, n, v); }
static void NVSDK_NGX_Parameter_SetUI_(NVSDK_NGX_Parameter* p, const char* n, unsigned v) { slotSet<unsigned>(p, S_UI, n, v); }
static void NVSDK_NGX_Parameter_SetF_(NVSDK_NGX_Parameter* p, const char* n, float v) { slotSet<float>(p, S_F, n, v); }
static void NVSDK_NGX_Parameter_SetD3d12Resource_(NVSDK_NGX_Parameter* p, const char* n, ID3D12Resource* v) { slotSet<ID3D12Resource*>(p, S_D3D12, n, v); }
static void NVSDK_NGX_Parameter_SetVoidPointer_(NVSDK_NGX_Parameter* p, const char* n, void* v) { slotSet<void*>(p, S_VOID, n, v); }

int main(int argc, char** argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    const char* dir = argc > 1 ? argv[1] : ".";   // folder that holds nvngx_dlssg.dll
    HMODULE ngx = LoadLibraryA(argc > 2 ? argv[2] : "_nvngx.dll");
    printf("LoadLibrary NGX core: %p (err %lu)\n", (void*)ngx, ngx ? 0 : GetLastError());
    if (!ngx) return 1;
    // The core library's parameter order (the standalone CUDA header shows it: ..., path, [device,] SDKVersion, FeatureInfo) - not the static wrapper's order.
    typedef NVSDK_NGX_Result (NVSDK_CONV *PFN_Init_ProjectID)(const char*, NVSDK_NGX_EngineType, const char*, const wchar_t*, ID3D12Device*, NVSDK_NGX_Version, const NVSDK_NGX_FeatureCommonInfo*);
    auto p_NVSDK_NGX_D3D12_Init_ProjectID = (PFN_Init_ProjectID)GetProcAddress(ngx, "NVSDK_NGX_D3D12_Init_ProjectID");
    if (!p_NVSDK_NGX_D3D12_Init_ProjectID) { printf("missing export Init_ProjectID\n"); return 3; }
    GETP(ngx, NVSDK_NGX_D3D12_GetCapabilityParameters);
    GETP(ngx, NVSDK_NGX_D3D12_AllocateParameters);
    GETP(ngx, NVSDK_NGX_D3D12_Shutdown1);
    auto p_NVSDK_NGX_D3D12_AllocateParametersX = p_NVSDK_NGX_D3D12_AllocateParameters; (void)p_NVSDK_NGX_D3D12_AllocateParametersX;

    auto mk = (PFN_D3D12_CREATE_DEVICE)GetProcAddress(LoadLibraryA("d3d12.dll"), "D3D12CreateDevice");
    ID3D12Device* dev = nullptr;
    HRESULT hr = mk(nullptr, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), (void**)&dev);
    printf("D3D12CreateDevice hr=0x%08lx\n", (unsigned long)hr);
    if (FAILED(hr)) return 2;

    wchar_t wdir[512]; MultiByteToWideChar(CP_ACP, 0, dir, -1, wdir, 512);
    const wchar_t* paths[1] = {wdir};
    NVSDK_NGX_FeatureCommonInfo info = {};
    info.PathListInfo.Path = paths; info.PathListInfo.Length = 1;
    info.LoggingInfo.LoggingCallback = ngxLog; info.LoggingInfo.MinimumLoggingLevel = NVSDK_NGX_LOGGING_LEVEL_ON; info.LoggingInfo.DisableOtherLoggingSinks = false;
    NVSDK_NGX_Result r = p_NVSDK_NGX_D3D12_Init_ProjectID("3f2a9c1e-7b4d-4e8a-9f61-c5d02a8b7e14", NVSDK_NGX_ENGINE_TYPE_CUSTOM, "1.0", L"C:\\windows\\temp", dev, NVSDK_NGX_Version_API, &info);
    printf("NVSDK_NGX_D3D12_Init_ProjectID -> 0x%08x\n", (unsigned)r);
    if (NVSDK_NGX_FAILED(r)) return 4;

    NVSDK_NGX_Parameter* caps = nullptr;
    r = p_NVSDK_NGX_D3D12_GetCapabilityParameters(&caps);
    printf("GetCapabilityParameters -> 0x%08x caps=%p\n", (unsigned)r, (void*)caps);
    if (caps) {
        int avail = -1, initRes = -1, needsDrv = -1; unsigned mj = 0, mn = 0;
        slotGet<int>(caps, G_I, NVSDK_NGX_Parameter_FrameGeneration_Available, &avail);
        slotGet<int>(caps, G_I, NVSDK_NGX_Parameter_FrameGeneration_FeatureInitResult, &initRes);
        slotGet<int>(caps, G_I, NVSDK_NGX_Parameter_FrameGeneration_NeedsUpdatedDriver, &needsDrv);
        slotGet<unsigned>(caps, G_UI, NVSDK_NGX_Parameter_FrameGeneration_MinDriverVersionMajor, &mj);
        slotGet<unsigned>(caps, G_UI, NVSDK_NGX_Parameter_FrameGeneration_MinDriverVersionMinor, &mn);
        printf("FrameGeneration: Available=%d FeatureInitResult=0x%x NeedsUpdatedDriver=%d MinDriver=%u.%u\n", avail, (unsigned)initRes, needsDrv, mj, mn);
    }

    // ---------------- create the feature and interpolate a few frames of a scrolling texture ----------------
    typedef NVSDK_NGX_Result (NVSDK_CONV *PFN_Create)(ID3D12GraphicsCommandList*, NVSDK_NGX_Feature, NVSDK_NGX_Parameter*, NVSDK_NGX_Handle**);
    typedef NVSDK_NGX_Result (NVSDK_CONV *PFN_Eval)(ID3D12GraphicsCommandList*, const NVSDK_NGX_Handle*, const NVSDK_NGX_Parameter*, PFN_NVSDK_NGX_ProgressCallback);
    auto p_Create = (PFN_Create)GetProcAddress(ngx, "NVSDK_NGX_D3D12_CreateFeature");
    auto p_Eval = (PFN_Eval)GetProcAddress(ngx, "NVSDK_NGX_D3D12_EvaluateFeature");
    NVSDK_NGX_Parameter* params = nullptr;
    r = p_NVSDK_NGX_D3D12_AllocateParameters(&params);
    printf("AllocateParameters -> 0x%08x\n", (unsigned)r);

    const UINT W = 1280, H = 720;
    D3D12_COMMAND_QUEUE_DESC qd = {}; qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    ID3D12CommandQueue* q; dev->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void**)&q);
    ID3D12CommandAllocator* al; dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), (void**)&al);
    ID3D12GraphicsCommandList* cl; dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, al, nullptr, __uuidof(ID3D12GraphicsCommandList), (void**)&cl);
    ID3D12Fence* fence; dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), (void**)&fence); HANDLE fev = CreateEventA(nullptr, FALSE, FALSE, nullptr); UINT64 fv = 0;
    auto wait = [&]() { q->Signal(fence, ++fv); if (fence->GetCompletedValue() < fv) { fence->SetEventOnCompletion(fv, fev); WaitForSingleObject(fev, 20000); } };
    auto tex = [&](DXGI_FORMAT f, D3D12_RESOURCE_FLAGS fl, D3D12_RESOURCE_STATES st) { D3D12_RESOURCE_DESC d = {}; d.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D; d.Width = W; d.Height = H; d.DepthOrArraySize = 1; d.MipLevels = 1; d.Format = f; d.SampleDesc.Count = 1; d.Flags = fl;
        D3D12_HEAP_PROPERTIES hp = {D3D12_HEAP_TYPE_DEFAULT}; ID3D12Resource* r; HRESULT h = dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, st, nullptr, __uuidof(ID3D12Resource), (void**)&r); if (FAILED(h)) printf("CreateCommittedResource fmt %d failed 0x%lx\n", (int)f, (unsigned long)h); return r; };
    auto buf = [&](D3D12_HEAP_TYPE t, UINT64 size, D3D12_RESOURCE_STATES st) { D3D12_RESOURCE_DESC d = {}; d.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; d.Width = size; d.Height = 1; d.DepthOrArraySize = 1; d.MipLevels = 1; d.SampleDesc.Count = 1; d.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        D3D12_HEAP_PROPERTIES hp = {t}; ID3D12Resource* r; dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, st, nullptr, __uuidof(ID3D12Resource), (void**)&r); return r; };
    const UINT pitch = (W * 4 + 255) & ~255u;
    ID3D12Resource* back = tex(DXGI_FORMAT_R8G8B8A8_UNORM, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
    ID3D12Resource* depth = tex(DXGI_FORMAT_R32_FLOAT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
    ID3D12Resource* mvec = tex(DXGI_FORMAT_R16G16_FLOAT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
    ID3D12Resource* outI = tex(DXGI_FORMAT_R8G8B8A8_UNORM, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    ID3D12Resource* up = buf(D3D12_HEAP_TYPE_UPLOAD, (UINT64)pitch * H, D3D12_RESOURCE_STATE_GENERIC_READ);
    ID3D12Resource* rb = buf(D3D12_HEAP_TYPE_READBACK, (UINT64)pitch * H, D3D12_RESOURCE_STATE_COPY_DEST);
    uint8_t* upp; D3D12_RANGE rg0 = {0, 0}; up->Map(0, &rg0, (void**)&upp);
    auto Trans = [](ID3D12Resource* r, D3D12_RESOURCE_STATES a, D3D12_RESOURCE_STATES b) { D3D12_RESOURCE_BARRIER x = {}; x.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION; x.Transition.pResource = r; x.Transition.StateBefore = a; x.Transition.StateAfter = b; x.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES; return x; };
    auto copyTex = [&](ID3D12Resource* t, DXGI_FORMAT f) { D3D12_TEXTURE_COPY_LOCATION d = {}; d.pResource = t; d.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; D3D12_TEXTURE_COPY_LOCATION sc = {}; sc.pResource = up; sc.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; sc.PlacedFootprint.Footprint = {f, W, H, 1, pitch}; cl->CopyTextureRegion(&d, 0, 0, 0, &sc, nullptr); };

    // create the feature
    NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_Parameter_CreationNodeMask, (unsigned)(1));
    NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_Parameter_VisibilityNodeMask, (unsigned)(1));
    NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_Parameter_Width, (unsigned)(W));
    NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_Parameter_Height, (unsigned)(H));
    NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_DLSSG_Parameter_BackbufferFormat, (unsigned)((unsigned)DXGI_FORMAT_R8G8B8A8_UNORM));
    NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_DLSSG_Parameter_InternalWidth, (unsigned)(W));
    NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_DLSSG_Parameter_InternalHeight, (unsigned)(H));
    NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_DLSSG_Parameter_DynamicResolution, (unsigned)(0));
    NVSDK_NGX_Handle* handle = nullptr;
    al->Reset(); cl->Reset(al, nullptr);
    r = p_Create(cl, NVSDK_NGX_Feature_FrameGeneration, params, &handle);
    printf("CreateFeature(FrameGeneration) -> 0x%08x handle=%p\n", (unsigned)r, (void*)handle);
    cl->Close(); ID3D12CommandList* ls0[] = {cl}; q->ExecuteCommandLists(1, ls0); wait();
    if (NVSDK_NGX_FAILED(r) || !handle) { p_NVSDK_NGX_D3D12_Shutdown1(dev); return 5; }

    // scrolling test texture: blurred noise
    const int TW = W + 800; std::vector<uint8_t> texd((size_t)TW * H * 4);
    { uint32_t st = 12345; std::vector<float> n((size_t)TW * H * 3); for (auto& v : n) { st = st * 1664525u + 1013904223u; v = (st >> 24) / 255.f; }
      std::vector<float> t2(n.size()); for (int pass = 0; pass < 2; pass++) { for (int y = 0; y < (int)H; y++) for (int x = 0; x < TW; x++) for (int c = 0; c < 3; c++) { float a = 0; int cnt = 0; for (int dx = -3; dx <= 3; dx++) { int xx = x + dx; if (xx >= 0 && xx < TW) { a += n[((size_t)y * TW + xx) * 3 + c]; cnt++; } } t2[((size_t)y * TW + x) * 3 + c] = a / cnt; } n = t2; }
      for (size_t i = 0; i < (size_t)TW * H; i++) { for (int c = 0; c < 3; c++) texd[i * 4 + c] = (uint8_t)std::min(255.f, n[i * 3 + c] * 255.f * 1.6f); texd[i * 4 + 3] = 255; } }
    auto frameAt = [&](int off, std::vector<uint8_t>& out) { out.resize((size_t)W * H * 4); for (UINT y = 0; y < H; y++) memcpy(&out[(size_t)y * W * 4], &texd[((size_t)y * TW + off) * 4], (size_t)W * 4); };

    // constant depth + zero motion vectors (a screen filter has no engine data)
    { al->Reset(); cl->Reset(al, nullptr);
      for (UINT y = 0; y < H; y++) { float* row = (float*)(upp + (size_t)y * pitch); for (UINT x = 0; x < W; x++) row[x] = 0.5f; } copyTex(depth, DXGI_FORMAT_R32_FLOAT);
      auto b1 = Trans(depth, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE); cl->ResourceBarrier(1, &b1);
      memset(upp, 0, (size_t)pitch * H); copyTex(mvec, DXGI_FORMAT_R16G16_FLOAT);
      auto b2 = Trans(mvec, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE); cl->ResourceBarrier(1, &b2);
      cl->Close(); ID3D12CommandList* ls[] = {cl}; q->ExecuteCommandLists(1, ls); wait(); }

    float ident[16] = {1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1};
    D3D12_RESOURCE_STATES backState = D3D12_RESOURCE_STATE_COPY_DEST;
    for (int i = 0; i < 12; i++) {
        std::vector<uint8_t> f; frameAt(8 * i, f);
        for (UINT y = 0; y < H; y++) memcpy(upp + (size_t)y * pitch, &f[(size_t)y * W * 4], (size_t)W * 4);
        al->Reset(); cl->Reset(al, nullptr);
        if (backState != D3D12_RESOURCE_STATE_COPY_DEST) { auto b = Trans(back, backState, D3D12_RESOURCE_STATE_COPY_DEST); cl->ResourceBarrier(1, &b); }
        copyTex(back, DXGI_FORMAT_R8G8B8A8_UNORM);
        { auto b = Trans(back, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE); cl->ResourceBarrier(1, &b); backState = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE; }
        NVSDK_NGX_Parameter_SetD3d12Resource_(params, NVSDK_NGX_DLSSG_Parameter_Backbuffer, back);
        NVSDK_NGX_Parameter_SetD3d12Resource_(params, NVSDK_NGX_DLSSG_Parameter_MVecs, mvec);
        NVSDK_NGX_Parameter_SetD3d12Resource_(params, NVSDK_NGX_DLSSG_Parameter_Depth, depth);
        NVSDK_NGX_Parameter_SetD3d12Resource_(params, NVSDK_NGX_DLSSG_Parameter_OutputInterpolated, outI);
        NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_DLSSG_Parameter_MultiFrameCount, (unsigned)(1));
        NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_DLSSG_Parameter_MultiFrameIndex, (unsigned)(1));
        NVSDK_NGX_Parameter_SetF_(params, NVSDK_NGX_DLSSG_Parameter_MvecScaleX, 1.f);
        NVSDK_NGX_Parameter_SetF_(params, NVSDK_NGX_DLSSG_Parameter_MvecScaleY, 1.f);
        NVSDK_NGX_Parameter_SetF_(params, NVSDK_NGX_DLSSG_Parameter_JitterOffsetX, 0.f);
        NVSDK_NGX_Parameter_SetF_(params, NVSDK_NGX_DLSSG_Parameter_JitterOffsetY, 0.f);
        NVSDK_NGX_Parameter_SetVoidPointer_(params, NVSDK_NGX_DLSSG_Parameter_CameraViewToClip, (void*)ident);
        NVSDK_NGX_Parameter_SetVoidPointer_(params, NVSDK_NGX_DLSSG_Parameter_ClipToCameraView, (void*)ident);
        NVSDK_NGX_Parameter_SetVoidPointer_(params, NVSDK_NGX_DLSSG_Parameter_ClipToLensClip, (void*)ident);
        NVSDK_NGX_Parameter_SetVoidPointer_(params, NVSDK_NGX_DLSSG_Parameter_ClipToPrevClip, (void*)ident);
        NVSDK_NGX_Parameter_SetVoidPointer_(params, NVSDK_NGX_DLSSG_Parameter_PrevClipToClip, (void*)ident);
        NVSDK_NGX_Parameter_SetF_(params, NVSDK_NGX_DLSSG_Parameter_CameraNear, 0.1f);
        NVSDK_NGX_Parameter_SetF_(params, NVSDK_NGX_DLSSG_Parameter_CameraFar, 1000.f);
        NVSDK_NGX_Parameter_SetF_(params, NVSDK_NGX_DLSSG_Parameter_CameraFOV, 1.0f);
        NVSDK_NGX_Parameter_SetF_(params, NVSDK_NGX_DLSSG_Parameter_CameraAspectRatio, (float)W / H);
        NVSDK_NGX_Parameter_SetF_(params, NVSDK_NGX_DLSSG_Parameter_CameraUpY, 1.f);
        NVSDK_NGX_Parameter_SetF_(params, NVSDK_NGX_DLSSG_Parameter_CameraRightX, 1.f);
        NVSDK_NGX_Parameter_SetF_(params, NVSDK_NGX_DLSSG_Parameter_CameraFwdZ, 1.f);
        NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_DLSSG_Parameter_ColorBuffersHDR, (unsigned)(0));
        NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_DLSSG_Parameter_DepthInverted, (unsigned)(0));
        NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_DLSSG_Parameter_CameraMotionIncluded, (unsigned)(0));
        NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_DLSSG_Parameter_Reset, (unsigned)(i == 0 ? 1 : 0));
        NVSDK_NGX_Parameter_SetUI_(params, NVSDK_NGX_DLSSG_Parameter_MvecDilated, (unsigned)(0));
        r = p_Eval(cl, handle, params, nullptr);
        if (NVSDK_NGX_FAILED(r)) printf("frame %d EvaluateFeature -> 0x%08x\n", i, (unsigned)r);
        { auto b = Trans(outI, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE); cl->ResourceBarrier(1, &b); }
        { D3D12_TEXTURE_COPY_LOCATION s2 = {}; s2.pResource = outI; s2.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; D3D12_TEXTURE_COPY_LOCATION d2 = {}; d2.pResource = rb; d2.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; d2.PlacedFootprint.Footprint = {DXGI_FORMAT_R8G8B8A8_UNORM, W, H, 1, pitch}; cl->CopyTextureRegion(&d2, 0, 0, 0, &s2, nullptr); }
        { auto b = Trans(outI, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS); cl->ResourceBarrier(1, &b); }
        cl->Close(); ID3D12CommandList* ls[] = {cl}; q->ExecuteCommandLists(1, ls); wait();
        uint8_t* rp; D3D12_RANGE rr = {0, (SIZE_T)pitch * H}; rb->Map(0, &rr, (void**)&rp);
        // compare the output with: the current frame, the previous frame, and the true midpoint (offset 8*i-4)
        std::vector<uint8_t> prevF, midF; frameAt(std::max(0, 8 * i - 8), prevF); frameAt(std::max(0, 8 * i - 4), midF);
        double dc = 0, dp = 0, dm = 0; size_t n = 0;
        for (UINT y = 0; y < H; y += 4) for (UINT x = 0; x < W; x += 4) for (int c = 0; c < 3; c++) { int o = rp[(size_t)y * pitch + x * 4 + c]; dc += abs(o - f[((size_t)y * W + x) * 4 + c]); dp += abs(o - prevF[((size_t)y * W + x) * 4 + c]); dm += abs(o - midF[((size_t)y * W + x) * 4 + c]); n++; }
        printf("frame %2d: MAD out vs current %.2f, vs previous %.2f, vs true midpoint %.2f\n", i, dc / n, dp / n, dm / n);
        D3D12_RANGE wr = {0, 0}; rb->Unmap(0, &wr);
    }
    p_NVSDK_NGX_D3D12_Shutdown1(dev);
    return 0;
}
