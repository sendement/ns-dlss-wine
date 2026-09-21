// SPDX-License-Identifier: MIT
// Feasibility probe: is NVIDIA's NATIVE Linux DLSS-G snippet (libnvidia-ngx-dlssg.so, from the NVIDIA/DLSS repo) usable through NGX-on-Vulkan on this machine?
// Build: g++ -O2 -std=c++17 -Ithird_party/nvidia-dlss/include dev/ngx_dlssg_vk_probe.cpp third_party/nvidia-dlss/lib/Linux_x86_64/libnvsdk_ngx.a -ldl -lvulkan -o runtime/ngx_vk_probe
// Run:   runtime/ngx_vk_probe <dir with libnvidia-ngx-dlssg.so.*>
#include <vulkan/vulkan.h>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <string>
#include <vector>
#include <set>
#include <algorithm>
#include <cstdint>
#include <cmath>
#include "nvsdk_ngx_helpers_dlssg_vk.h"
#include "nvsdk_ngx_helpers_vk.h"
#include "nvsdk_ngx_vk.h"
#include "nvsdk_ngx_defs_dlssg.h"
#include "nvsdk_ngx_params_dlssg.h"

static void ngxLog(const char* msg, NVSDK_NGX_Logging_Level, NVSDK_NGX_Feature) { fputs(msg, stdout); }

int main(int argc, char** argv) {
    setvbuf(stdout, nullptr, _IONBF, 0);
    const char* dir = argc > 1 ? argv[1] : ".";
    unsigned ic = 0, dc = 0; const char **iexts = nullptr, **dexts = nullptr;
    NVSDK_NGX_Result r = NVSDK_NGX_VULKAN_RequiredExtensions(&ic, &iexts, &dc, &dexts);
    printf("RequiredExtensions -> 0x%08x: %u instance, %u device\n", (unsigned)r, ic, dc);
    std::set<std::string> ie, de;
    for (unsigned i = 0; i < ic; i++) { printf("  I %s\n", iexts[i]); ie.insert(iexts[i]); }
    for (unsigned i = 0; i < dc; i++) { printf("  D %s\n", dexts[i]); de.insert(dexts[i]); }

    VkApplicationInfo ai = {VK_STRUCTURE_TYPE_APPLICATION_INFO}; ai.apiVersion = VK_API_VERSION_1_3; ai.pApplicationName = "ngx-probe";
    std::vector<const char*> iv; for (auto& s : ie) iv.push_back(s.c_str());
    VkInstanceCreateInfo ici = {VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO}; ici.pApplicationInfo = &ai; ici.enabledExtensionCount = iv.size(); ici.ppEnabledExtensionNames = iv.data();
    VkInstance inst; VkResult vr = vkCreateInstance(&ici, nullptr, &inst); printf("vkCreateInstance -> %d\n", vr); if (vr) return 2;
    unsigned n = 0; vkEnumeratePhysicalDevices(inst, &n, nullptr); std::vector<VkPhysicalDevice> pds(n); vkEnumeratePhysicalDevices(inst, &n, pds.data());
    VkPhysicalDevice pd = VK_NULL_HANDLE;
    for (auto p : pds) { VkPhysicalDeviceProperties pr; vkGetPhysicalDeviceProperties(p, &pr); printf("GPU: %s (vendor 0x%x)\n", pr.deviceName, pr.vendorID); if (pr.vendorID == 0x10de && !pd) pd = p; }
    if (!pd) return 3;

    // feature-specific requirements (DLSS-G)
    NVSDK_NGX_FeatureDiscoveryInfo fdi = {}; fdi.SDKVersion = NVSDK_NGX_Version_API; fdi.FeatureID = NVSDK_NGX_Feature_FrameGeneration;
    fdi.Identifier.IdentifierType = NVSDK_NGX_Application_Identifier_Type_Project_Id; fdi.Identifier.v.ProjectDesc.ProjectId = "3f2a9c1e-7b4d-4e8a-9f61-c5d02a8b7e14";
    fdi.Identifier.v.ProjectDesc.EngineType = NVSDK_NGX_ENGINE_TYPE_CUSTOM; fdi.Identifier.v.ProjectDesc.EngineVersion = "1.0";
    wchar_t wdir[512]; mbstowcs(wdir, dir, 512); fdi.ApplicationDataPath = wdir;
    NVSDK_NGX_FeatureRequirement req = {};
    r = NVSDK_NGX_VULKAN_GetFeatureRequirements(inst, pd, &fdi, &req);
    printf("GetFeatureRequirements(FrameGeneration) -> 0x%08x: supported=0x%x minHW=0x%x minOS=%s\n", (unsigned)r, (unsigned)req.FeatureSupported, (unsigned)req.MinHWArchitecture, req.MinOSVersion);

    // device with the required extensions on a graphics+compute queue
    uint32_t qn = 0; vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, nullptr); std::vector<VkQueueFamilyProperties> qf(qn); vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, qf.data());
    uint32_t qi = 0; for (uint32_t i = 0; i < qn; i++) if ((qf[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) && (qf[i].queueFlags & VK_QUEUE_COMPUTE_BIT)) { qi = i; break; }
    std::vector<const char*> dv; for (auto& s : de) dv.push_back(s.c_str());
    float prio = 1.f; VkDeviceQueueCreateInfo qci = {VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO}; qci.queueFamilyIndex = qi; qci.queueCount = 1; qci.pQueuePriorities = &prio;
    VkPhysicalDeviceVulkan12Features f12 = {VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES}; f12.bufferDeviceAddress = VK_TRUE; f12.timelineSemaphore = VK_TRUE;
    VkPhysicalDeviceVulkan13Features f13 = {VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES}; f13.synchronization2 = VK_TRUE; f12.pNext = &f13;
    VkDeviceCreateInfo dci = {VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO}; dci.pNext = &f12; dci.queueCreateInfoCount = 1; dci.pQueueCreateInfos = &qci;
    dci.enabledExtensionCount = dv.size(); dci.ppEnabledExtensionNames = dv.data();
    VkDevice dev; vr = vkCreateDevice(pd, &dci, nullptr, &dev); printf("vkCreateDevice -> %d\n", vr); if (vr) return 4;

    const wchar_t* paths[1] = {wdir};
    NVSDK_NGX_FeatureCommonInfo info = {}; info.PathListInfo.Path = paths; info.PathListInfo.Length = 1;
    info.LoggingInfo.LoggingCallback = ngxLog; info.LoggingInfo.MinimumLoggingLevel = NVSDK_NGX_LOGGING_LEVEL_ON; info.LoggingInfo.DisableOtherLoggingSinks = false;
    r = NVSDK_NGX_VULKAN_Init_with_ProjectID("3f2a9c1e-7b4d-4e8a-9f61-c5d02a8b7e14", NVSDK_NGX_ENGINE_TYPE_CUSTOM, "1.0", L"/tmp", inst, pd, dev, vkGetInstanceProcAddr, vkGetDeviceProcAddr, &info, NVSDK_NGX_Version_API);
    printf("VULKAN_Init_with_ProjectID -> 0x%08x\n", (unsigned)r);
    NVSDK_NGX_Parameter* caps = nullptr; r = NVSDK_NGX_VULKAN_GetCapabilityParameters(&caps); printf("GetCapabilityParameters -> 0x%08x\n", (unsigned)r);
    if (caps) {
        int avail = -1, initRes = -1, needsDrv = -1; unsigned mj = 0, mn = 0;
        NVSDK_NGX_Parameter_GetI(caps, NVSDK_NGX_Parameter_FrameGeneration_Available, &avail);
        NVSDK_NGX_Parameter_GetI(caps, NVSDK_NGX_Parameter_FrameGeneration_FeatureInitResult, &initRes);
        NVSDK_NGX_Parameter_GetI(caps, NVSDK_NGX_Parameter_FrameGeneration_NeedsUpdatedDriver, &needsDrv);
        NVSDK_NGX_Parameter_GetUI(caps, NVSDK_NGX_Parameter_FrameGeneration_MinDriverVersionMajor, &mj);
        NVSDK_NGX_Parameter_GetUI(caps, NVSDK_NGX_Parameter_FrameGeneration_MinDriverVersionMinor, &mn);
        printf("FrameGeneration: Available=%d FeatureInitResult=0x%x NeedsUpdatedDriver=%d MinDriver=%u.%u\n", avail, (unsigned)initRes, needsDrv, mj, mn);
    }

    // ---------------- create the feature and interpolate frames of a scrolling texture ----------------
    const uint32_t W = 1280, H = 720; const VkDeviceSize pixBytes = (VkDeviceSize)W * H * 4;
    VkPhysicalDeviceMemoryProperties mp; vkGetPhysicalDeviceMemoryProperties(pd, &mp);
    auto memType = [&](uint32_t bits, VkMemoryPropertyFlags want) { for (uint32_t i = 0; i < mp.memoryTypeCount; i++) if ((bits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & want) == want) return i; return 0u; };
    VkQueue queue; vkGetDeviceQueue(dev, qi, 0, &queue);
    VkCommandPoolCreateInfo cpi = {VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO}; cpi.queueFamilyIndex = qi; cpi.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    VkCommandPool pool; vkCreateCommandPool(dev, &cpi, nullptr, &pool);
    VkCommandBufferAllocateInfo cbi = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO}; cbi.commandPool = pool; cbi.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY; cbi.commandBufferCount = 1;
    VkCommandBuffer cb; vkAllocateCommandBuffers(dev, &cbi, &cb);
    VkFenceCreateInfo fci = {VK_STRUCTURE_TYPE_FENCE_CREATE_INFO}; VkFence fence; vkCreateFence(dev, &fci, nullptr, &fence);
    struct Img { VkImage img; VkImageView view; VkDeviceMemory mem; VkFormat fmt; NVSDK_NGX_Resource_VK res; };
    auto mkImg = [&](VkFormat fmt, VkImageUsageFlags use) {
        Img I = {}; I.fmt = fmt;
        VkImageCreateInfo ii = {VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO}; ii.imageType = VK_IMAGE_TYPE_2D; ii.format = fmt; ii.extent = {W, H, 1}; ii.mipLevels = 1; ii.arrayLayers = 1; ii.samples = VK_SAMPLE_COUNT_1_BIT;
        ii.tiling = VK_IMAGE_TILING_OPTIMAL; ii.usage = use; ii.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED; vkCreateImage(dev, &ii, nullptr, &I.img);
        VkMemoryRequirements mr; vkGetImageMemoryRequirements(dev, I.img, &mr); VkMemoryAllocateInfo mai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO}; mai.allocationSize = mr.size; mai.memoryTypeIndex = memType(mr.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        vkAllocateMemory(dev, &mai, nullptr, &I.mem); vkBindImageMemory(dev, I.img, I.mem, 0);
        VkImageViewCreateInfo vi = {VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO}; vi.image = I.img; vi.viewType = VK_IMAGE_VIEW_TYPE_2D; vi.format = fmt; vi.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}; vkCreateImageView(dev, &vi, nullptr, &I.view);
        I.res = NVSDK_NGX_Create_ImageView_Resource_VK(I.view, I.img, vi.subresourceRange, fmt, W, H, true);
        return I; };
    const VkImageUsageFlags U = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    Img back = mkImg(VK_FORMAT_R8G8B8A8_UNORM, U), depth = mkImg(VK_FORMAT_R32_SFLOAT, U), mvec = mkImg(VK_FORMAT_R16G16_SFLOAT, U), outI = mkImg(VK_FORMAT_R8G8B8A8_UNORM, U);
    auto mkBuf = [&](VkDeviceSize size, void** map) { VkBuffer b; VkBufferCreateInfo bi = {VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO}; bi.size = size; bi.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT; vkCreateBuffer(dev, &bi, nullptr, &b);
        VkMemoryRequirements mr; vkGetBufferMemoryRequirements(dev, b, &mr); VkMemoryAllocateInfo mai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO}; mai.allocationSize = mr.size; mai.memoryTypeIndex = memType(mr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
        VkDeviceMemory m; vkAllocateMemory(dev, &mai, nullptr, &m); vkBindBufferMemory(dev, b, m, 0); vkMapMemory(dev, m, 0, size, 0, map); return b; };
    void *upP, *rbP; VkBuffer upB = mkBuf(pixBytes, &upP), rbB = mkBuf(pixBytes, &rbP);
    auto layout = [&](Img& I, VkImageLayout from, VkImageLayout to, VkAccessFlags sa, VkAccessFlags da) { VkImageMemoryBarrier b = {VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER}; b.oldLayout = from; b.newLayout = to; b.srcAccessMask = sa; b.dstAccessMask = da;
        b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED; b.image = I.img; b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, 0, 0, nullptr, 0, nullptr, 1, &b); };
    auto begin = [&]() { vkResetCommandBuffer(cb, 0); VkCommandBufferBeginInfo bi = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO}; bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT; vkBeginCommandBuffer(cb, &bi); };
    auto submit = [&]() { vkEndCommandBuffer(cb); VkSubmitInfo si = {VK_STRUCTURE_TYPE_SUBMIT_INFO}; si.commandBufferCount = 1; si.pCommandBuffers = &cb; vkQueueSubmit(queue, 1, &si, fence); vkWaitForFences(dev, 1, &fence, VK_TRUE, 20000000000ull); vkResetFences(dev, 1, &fence); };
    auto upload = [&](Img& I) { VkBufferImageCopy c = {}; c.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}; c.imageExtent = {W, H, 1}; vkCmdCopyBufferToImage(cb, upB, I.img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &c); };

    NVSDK_NGX_Parameter* params = nullptr; r = NVSDK_NGX_VULKAN_AllocateParameters(&params); printf("AllocateParameters -> 0x%08x\n", (unsigned)r);
    NVSDK_NGX_DLSSG_Create_Params cp = {}; cp.Width = W; cp.Height = H; cp.NativeBackbufferFormat = VK_FORMAT_R8G8B8A8_UNORM; cp.RenderWidth = W; cp.RenderHeight = H;
    NVSDK_NGX_Handle* handle = nullptr;
    begin(); r = NGX_VK_CREATE_DLSSG(cb, 1, 1, &handle, params, &cp); printf("CreateFeature(FrameGeneration) -> 0x%08x handle=%p\n", (unsigned)r, (void*)handle); submit();
    if (NVSDK_NGX_FAILED(r) || !handle) { NVSDK_NGX_VULKAN_Shutdown1(dev); return 5; }

    const int TW = W + 800; std::vector<uint8_t> texd((size_t)TW * H * 4);
    { uint32_t st = 12345; std::vector<float> n((size_t)TW * H * 3), t2; for (auto& v : n) { st = st * 1664525u + 1013904223u; v = (st >> 24) / 255.f; }
      t2.resize(n.size()); for (int pass = 0; pass < 2; pass++) { for (int y = 0; y < (int)H; y++) for (int x = 0; x < TW; x++) for (int c = 0; c < 3; c++) { float a = 0; int cnt = 0; for (int dx = -3; dx <= 3; dx++) { int xx = x + dx; if (xx >= 0 && xx < TW) { a += n[((size_t)y * TW + xx) * 3 + c]; cnt++; } } t2[((size_t)y * TW + x) * 3 + c] = a / cnt; } n = t2; }
      for (size_t i = 0; i < (size_t)TW * H; i++) { for (int c = 0; c < 3; c++) texd[i * 4 + c] = (uint8_t)std::min(255.f, n[i * 3 + c] * 255.f * 1.6f); texd[i * 4 + 3] = 255; } }
    auto frameAt = [&](int off, std::vector<uint8_t>& out) { out.resize(pixBytes); for (uint32_t y = 0; y < H; y++) memcpy(&out[(size_t)y * W * 4], &texd[((size_t)y * TW + off) * 4], (size_t)W * 4); };

    // constant depth + zero motion vectors
    { for (uint32_t i = 0; i < W * H; i++) ((float*)upP)[i] = 0.5f; begin(); layout(depth, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0, VK_ACCESS_TRANSFER_WRITE_BIT); upload(depth);
      layout(depth, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL, VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT); submit();
      memset(upP, 0, pixBytes); begin(); layout(mvec, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0, VK_ACCESS_TRANSFER_WRITE_BIT); upload(mvec);
      layout(mvec, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL, VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT);
      layout(outI, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, 0, VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT); submit(); }

    NVSDK_NGX_VK_DLSSG_Eval_Params ep = {}; ep.pBackbuffer = &back.res; ep.pDepth = &depth.res; ep.pMVecs = &mvec.res; ep.pOutputInterpFrame = &outI.res;
    NVSDK_NGX_DLSSG_Opt_Eval_Params op = {}; op.multiFrameCount = 1; op.multiFrameIndex = 1; op.mvecScale[0] = op.mvecScale[1] = 1.f;
    for (int a = 0; a < 4; a++) op.cameraViewToClip[a][a] = op.clipToCameraView[a][a] = op.clipToLensClip[a][a] = op.clipToPrevClip[a][a] = op.prevClipToClip[a][a] = 1.f;
    op.cameraNear = 0.1f; op.cameraFar = 1000.f; op.cameraFOV = 1.f; op.cameraAspectRatio = (float)W / H; op.cameraUp[1] = 1.f; op.cameraRight[0] = 1.f; op.cameraFwd[2] = 1.f;
    bool backInit = false;
    for (int i = 0; i < 12; i++) {
        std::vector<uint8_t> f; frameAt(8 * i, f); memcpy(upP, f.data(), pixBytes);
        begin();
        layout(back, backInit ? VK_IMAGE_LAYOUT_GENERAL : VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_ACCESS_SHADER_READ_BIT, VK_ACCESS_TRANSFER_WRITE_BIT); backInit = true; upload(back);
        layout(back, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL, VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT);
        op.reset = (i == 0);
        r = NGX_VK_EVALUATE_DLSSG(cb, handle, params, &ep, &op);
        if (NVSDK_NGX_FAILED(r)) printf("frame %d EvaluateFeature -> 0x%08x\n", i, (unsigned)r);
        layout(outI, VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
        { VkBufferImageCopy c = {}; c.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}; c.imageExtent = {W, H, 1}; vkCmdCopyImageToBuffer(cb, outI.img, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, rbB, 1, &c); }
        layout(outI, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL, VK_ACCESS_TRANSFER_READ_BIT, VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT);
        submit();
        std::vector<uint8_t> prevF, midF; frameAt(std::max(0, 8 * i - 8), prevF); frameAt(std::max(0, 8 * i - 4), midF);
        const uint8_t* rp = (const uint8_t*)rbP; double dc = 0, dp = 0, dm = 0; size_t cnt = 0;
        for (uint32_t y = 0; y < H; y += 4) for (uint32_t x = 0; x < W; x += 4) for (int c = 0; c < 3; c++) { size_t o = ((size_t)y * W + x) * 4 + c; dc += abs(rp[o] - f[o]); dp += abs(rp[o] - prevF[o]); dm += abs(rp[o] - midF[o]); cnt++; }
        printf("frame %2d: MAD out vs current %.2f, vs previous %.2f, vs true midpoint %.2f\n", i, dc / cnt, dp / cnt, dm / cnt);
    }
    NVSDK_NGX_VULKAN_ReleaseFeature(handle);
    NVSDK_NGX_VULKAN_Shutdown1(dev);
    return 0;
}
