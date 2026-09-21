// SPDX-License-Identifier: MIT
// Spike: can the signed FSR frame-generation DLL be loaded under Wine, and does a DX12 (vkd3d-proton) context get created on it?
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <cstdio>
#include <cstring>
#include "ffx_api.h"
#include "ffx_api_types.h"
#include "dx12/ffx_api_dx12.h"
#include "ffx_framegeneration.h"

int main() {
    HMODULE fg = LoadLibraryA("amd_fidelityfx_framegeneration_dx12.dll");
    printf("LoadLibrary FG dll: %p (err %lu)\n", (void*)fg, fg ? 0 : GetLastError());
    if (!fg) return 1;
    auto create = (PfnFfxCreateContext)GetProcAddress(fg, "ffxCreateContext");
    auto destroy = (PfnFfxDestroyContext)GetProcAddress(fg, "ffxDestroyContext");
    printf("ffxCreateContext=%p ffxDestroyContext=%p\n", (void*)create, (void*)destroy);

    HMODULE d3d = LoadLibraryA("d3d12.dll");
    auto mk = (PFN_D3D12_CREATE_DEVICE)GetProcAddress(d3d, "D3D12CreateDevice");
    ID3D12Device* dev = nullptr;
    HRESULT hr = mk(nullptr, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), (void**)&dev);
    printf("D3D12CreateDevice hr=0x%08lx dev=%p\n", (unsigned long)hr, (void*)dev);
    if (FAILED(hr)) return 2;

    ffxCreateBackendDX12Desc backend{}; backend.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12; backend.device = dev;
    ffxCreateContextDescFrameGenerationVersion ver{}; ver.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION_VERSION; ver.version = FFX_FRAMEGENERATION_VERSION;
    ffxCreateContextDescFrameGeneration cd{}; cd.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION;
    cd.flags = 0; cd.displaySize = {1920, 1080}; cd.maxRenderSize = {1920, 1080}; cd.backBufferFormat = FFX_API_SURFACE_FORMAT_R8G8B8A8_UNORM;
    cd.header.pNext = &backend.header; backend.header.pNext = &ver.header;
    ffxContext ctx = nullptr;
    ffxReturnCode_t rc = create(&ctx, &cd.header, nullptr);
    printf("ffxCreateContext(FRAMEGENERATION) rc=%u ctx=%p\n", (unsigned)rc, ctx);
    if (ctx) { rc = destroy(&ctx, nullptr); printf("destroy rc=%u\n", (unsigned)rc); }
    return 0;
}
