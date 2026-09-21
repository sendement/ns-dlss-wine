// SPDX-License-Identifier: MIT
// NATIVE Linux DLSS Frame Generation host (no Wine): NVIDIA's NGX-on-Vulkan with libnvidia-ngx-dlssg.so (published in the NVIDIA/DLSS repository), driven through
// libnvsdk_ngx.a. Same file protocol and two-slot pipeline as the Wine host (ngxdlssg_host.cpp): the Python side writes RGBA8 frames into an "in" file (two slots) and
// bumps req_seq in a control block; we generate the frames between the previous and the current real frame and write them (BGRA8, cairo order) into a ring of
// regions of the "out" file. No engine data exists for a screen filter: depth is a constant plane, motion vectors are zero or a global vector hint from the client,
// and the feature derives its own optical flow (NVOF, through the driver's libnvidia-opticalflow) from the colour frames.
//
// usage: ngxdlssg_vk_host width height count in out ctrl dir   (dir = folder holding libnvidia-ngx-dlssg.so.*)
#include <vulkan/vulkan.h>
#include <fcntl.h>
#include <immintrin.h>
#include <sys/mman.h>
#include <unistd.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>
#include "nvsdk_ngx_vk.h"
#include "nvsdk_ngx_helpers_vk.h"
#include "nvsdk_ngx_helpers_dlssg_vk.h"
#include "nvsdk_ngx_defs_dlssg.h"
#include "nvsdk_ngx_params_dlssg.h"

static void* MapFile(const char* path, size_t bytes) {
    int fd = open(path, O_RDWR);
    if (fd < 0) { fprintf(stderr, "[ngxg-vk] open %s failed\n", path); return nullptr; }
    void* p = mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    return p == MAP_FAILED ? nullptr : p;
}

struct Ctrl { volatile uint32_t state;   // 0 starting, 1 ready, 2 error
              volatile uint32_t req_seq, ack_seq, ok, quit;
              volatile float total_ms, ngx_ms;
              volatile uint32_t generated, info, frame_flags;
              volatile float flow_ms;
              volatile float mv_x, mv_y; };
// Per-slot request data of the two-slot pipeline (at byte 64 of the control file): the Python side fills slot seq%2 before bumping req_seq, the host fills
// gen/info/ok[seq%4] before it acknowledges.
struct Ext { volatile uint32_t flags[2]; volatile float mvx[2], mvy[2]; volatile uint32_t gen[4], info[4], ok[4]; };

#define VK(x) do { VkResult _r = (x); if (_r != VK_SUCCESS) { fprintf(stderr, "[ngxg-vk] %s failed: %d (line %d)\n", #x, (int)_r, __LINE__); return fail(20); } } while (0)

// Row-band parallel-for (the big CPU copies are memory-bound; one thread reaches only part of the bandwidth).
template <class F> static void parallelRows(uint32_t rows, F fn) {
    const uint32_t bands = 4; std::thread th[bands - 1];
    for (uint32_t b = 0; b + 1 < bands; b++) th[b] = std::thread([=, &fn] { fn(rows * b / bands, rows * (b + 1) / bands); });
    fn(rows * (bands - 1) / bands, rows);
    for (auto& t : th) t.join();
}

static void NVSDK_CONV ngxLog(const char* msg, NVSDK_NGX_Logging_Level, NVSDK_NGX_Feature) { if (getenv("NGXG_LOG")) fputs(msg, stderr); }
static double nowMs() { return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now().time_since_epoch()).count(); }

int main(int argc, char** argv) {
    if (argc < 8) { fprintf(stderr, "[ngxg-vk] usage: width height count in out ctrl dir\n"); return 2; }
    const uint32_t w = atoi(argv[1]), h = atoi(argv[2]), count = std::max(1, std::min(3, atoi(argv[3])));
    Ctrl* ctrl = (Ctrl*)MapFile(argv[6], 64 + sizeof(Ext));
    if (!ctrl) return 2;
    Ext* ext = (Ext*)((uint8_t*)ctrl + 64);
    auto fail = [&](int code) { ctrl->state = 2; return code; };
    const uint32_t ring = getenv("NGXG_RING") ? std::max(1, atoi(getenv("NGXG_RING"))) : (getenv("DLSSG_RING") ? std::max(1, atoi(getenv("DLSSG_RING"))) : 1);
    const size_t bytes = (size_t)w * h * 4;
    uint8_t* in_map = (uint8_t*)MapFile(argv[4], bytes * 2);     // two input slots
    uint8_t* out_map = (uint8_t*)MapFile(argv[5], bytes * count * ring);
    if (!in_map || !out_map) return fail(8);
    const std::string dir = argv[7];
    wchar_t wdir[1024]; mbstowcs(wdir, dir.c_str(), 1024);

    // ---- Vulkan instance / device with what NGX asks for ----
    unsigned ic = 0, dc = 0; const char **iexts = nullptr, **dexts = nullptr;
    if (NVSDK_NGX_FAILED(NVSDK_NGX_VULKAN_RequiredExtensions(&ic, &iexts, &dc, &dexts))) { fprintf(stderr, "[ngxg-vk] RequiredExtensions failed\n"); return fail(4); }
    std::set<std::string> ie, de; for (unsigned i = 0; i < ic; i++) ie.insert(iexts[i]); for (unsigned i = 0; i < dc; i++) de.insert(dexts[i]);
    std::vector<const char*> iv; for (auto& s : ie) iv.push_back(s.c_str());
    VkApplicationInfo ai = {VK_STRUCTURE_TYPE_APPLICATION_INFO}; ai.apiVersion = VK_API_VERSION_1_3; ai.pApplicationName = "ngxdlssg";
    VkInstanceCreateInfo ici = {VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO}; ici.pApplicationInfo = &ai; ici.enabledExtensionCount = iv.size(); ici.ppEnabledExtensionNames = iv.data();
    VkInstance inst; VK(vkCreateInstance(&ici, nullptr, &inst));
    uint32_t n = 0; vkEnumeratePhysicalDevices(inst, &n, nullptr); std::vector<VkPhysicalDevice> pds(n); vkEnumeratePhysicalDevices(inst, &n, pds.data());
    VkPhysicalDevice pd = VK_NULL_HANDLE;
    for (auto p : pds) { VkPhysicalDeviceProperties pr; vkGetPhysicalDeviceProperties(p, &pr); if (pr.vendorID == 0x10de && !pd) pd = p; }
    if (!pd) { fprintf(stderr, "[ngxg-vk] no NVIDIA GPU\n"); return fail(3); }
    uint32_t qn = 0; vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, nullptr); std::vector<VkQueueFamilyProperties> qf(qn); vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, qf.data());
    uint32_t qi = 0; for (uint32_t i = 0; i < qn; i++) if ((qf[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) && (qf[i].queueFlags & VK_QUEUE_COMPUTE_BIT)) { qi = i; break; }
    std::vector<const char*> dv; for (auto& s : de) dv.push_back(s.c_str());
    float prio = 1.f; VkDeviceQueueCreateInfo qci = {VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO}; qci.queueFamilyIndex = qi; qci.queueCount = 1; qci.pQueuePriorities = &prio;
    VkPhysicalDeviceVulkan12Features f12 = {VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES}; f12.bufferDeviceAddress = VK_TRUE; f12.timelineSemaphore = VK_TRUE;
    VkPhysicalDeviceVulkan13Features f13 = {VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES}; f13.synchronization2 = VK_TRUE; f12.pNext = &f13;
    VkDeviceCreateInfo dci = {VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO}; dci.pNext = &f12; dci.queueCreateInfoCount = 1; dci.pQueueCreateInfos = &qci;
    dci.enabledExtensionCount = dv.size(); dci.ppEnabledExtensionNames = dv.data();
    VkDevice dev; VK(vkCreateDevice(pd, &dci, nullptr, &dev));
    VkQueue queue; vkGetDeviceQueue(dev, qi, 0, &queue);

    const wchar_t* paths[1] = {wdir};
    NVSDK_NGX_FeatureCommonInfo info = {}; info.PathListInfo.Path = paths; info.PathListInfo.Length = 1;
    info.LoggingInfo.LoggingCallback = ngxLog; info.LoggingInfo.MinimumLoggingLevel = NVSDK_NGX_LOGGING_LEVEL_ON; info.LoggingInfo.DisableOtherLoggingSinks = true;
    // ProjectId: GUID-like, no more than 4 identical digits in a row (NGX rejects it otherwise: 0xBAD00005)
    NVSDK_NGX_Result r = NVSDK_NGX_VULKAN_Init_with_ProjectID("3f2a9c1e-7b4d-4e8a-9f61-c5d02a8b7e14", NVSDK_NGX_ENGINE_TYPE_CUSTOM, "1.0", L"/tmp", inst, pd, dev,
                                                              vkGetInstanceProcAddr, vkGetDeviceProcAddr, &info, NVSDK_NGX_Version_API);
    if (NVSDK_NGX_FAILED(r)) { fprintf(stderr, "[ngxg-vk] NGX init failed 0x%08x\n", (unsigned)r); return fail(5); }
    NVSDK_NGX_Parameter* caps = nullptr; int avail = 0, initRes = 0;
    if (NVSDK_NGX_FAILED(NVSDK_NGX_VULKAN_GetCapabilityParameters(&caps)) || !caps) { fprintf(stderr, "[ngxg-vk] no capability parameters\n"); return fail(5); }
    NVSDK_NGX_Parameter_GetI(caps, NVSDK_NGX_Parameter_FrameGeneration_Available, &avail);
    NVSDK_NGX_Parameter_GetI(caps, NVSDK_NGX_Parameter_FrameGeneration_FeatureInitResult, &initRes);
    if (!avail) { fprintf(stderr, "[ngxg-vk] DLSS-G not available (init result 0x%x)\n", (unsigned)initRes); return fail(6); }

    // ---- resources ----
    VkPhysicalDeviceMemoryProperties mp; vkGetPhysicalDeviceMemoryProperties(pd, &mp);
    auto memType = [&](uint32_t bits, VkMemoryPropertyFlags want) { for (uint32_t i = 0; i < mp.memoryTypeCount; i++) if ((bits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & want) == want) return i; return 0u; };
    VkCommandPoolCreateInfo cpi = {VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO}; cpi.queueFamilyIndex = qi; cpi.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    VkCommandPool pool; VK(vkCreateCommandPool(dev, &cpi, nullptr, &pool));
    struct Img { VkImage img; VkImageView view; VkFormat fmt; NVSDK_NGX_Resource_VK res; };
    auto mkImg = [&](VkFormat fmt, Img* I) {
        I->fmt = fmt;
        VkImageCreateInfo ii = {VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO}; ii.imageType = VK_IMAGE_TYPE_2D; ii.format = fmt; ii.extent = {w, h, 1}; ii.mipLevels = 1; ii.arrayLayers = 1; ii.samples = VK_SAMPLE_COUNT_1_BIT;
        ii.tiling = VK_IMAGE_TILING_OPTIMAL; ii.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        ii.usage = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
        if (vkCreateImage(dev, &ii, nullptr, &I->img)) return false;
        VkMemoryRequirements mr; vkGetImageMemoryRequirements(dev, I->img, &mr);
        VkMemoryAllocateInfo mai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO}; mai.allocationSize = mr.size; mai.memoryTypeIndex = memType(mr.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        VkDeviceMemory mem; if (vkAllocateMemory(dev, &mai, nullptr, &mem)) return false; vkBindImageMemory(dev, I->img, mem, 0);
        VkImageViewCreateInfo vi = {VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO}; vi.image = I->img; vi.viewType = VK_IMAGE_VIEW_TYPE_2D; vi.format = fmt; vi.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        if (vkCreateImageView(dev, &vi, nullptr, &I->view)) return false;
        I->res = NVSDK_NGX_Create_ImageView_Resource_VK(I->view, I->img, vi.subresourceRange, fmt, w, h, true);
        return true; };
    auto mkBuf = [&](VkDeviceSize size, void** map) {
        VkBuffer b; VkBufferCreateInfo bi = {VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO}; bi.size = size; bi.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT;
        vkCreateBuffer(dev, &bi, nullptr, &b); VkMemoryRequirements mr; vkGetBufferMemoryRequirements(dev, b, &mr);
        VkMemoryAllocateInfo mai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO}; mai.allocationSize = mr.size;
        mai.memoryTypeIndex = memType(mr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT | VK_MEMORY_PROPERTY_HOST_CACHED_BIT);
        if (!(mp.memoryTypes[mai.memoryTypeIndex].propertyFlags & VK_MEMORY_PROPERTY_HOST_CACHED_BIT)) mai.memoryTypeIndex = memType(mr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
        VkDeviceMemory m; vkAllocateMemory(dev, &mai, nullptr, &m); vkBindBufferMemory(dev, b, m, 0); vkMapMemory(dev, m, 0, size, 0, map); return b; };
    Img back, depth, mvec, outs[3];
    if (!mkImg(VK_FORMAT_R8G8B8A8_UNORM, &back) || !mkImg(VK_FORMAT_R32_SFLOAT, &depth) || !mkImg(VK_FORMAT_R16G16_SFLOAT, &mvec)) { fprintf(stderr, "[ngxg-vk] image creation failed\n"); return fail(20); }
    for (uint32_t i = 0; i < count; i++) if (!mkImg(VK_FORMAT_R8G8B8A8_UNORM, &outs[i])) { fprintf(stderr, "[ngxg-vk] output image creation failed\n"); return fail(20); }

    struct Slot { VkCommandBuffer cb; VkFence fence; VkBuffer up, mvUp, readback[3]; void *up_p, *mvUp_p, *readback_p[3]; bool busy; };
    Slot slots[2] = {};
    for (auto& S : slots) {
        VkCommandBufferAllocateInfo cbi = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO}; cbi.commandPool = pool; cbi.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY; cbi.commandBufferCount = 1;
        VK(vkAllocateCommandBuffers(dev, &cbi, &S.cb));
        VkFenceCreateInfo fci = {VK_STRUCTURE_TYPE_FENCE_CREATE_INFO}; VK(vkCreateFence(dev, &fci, nullptr, &S.fence));
        S.up = mkBuf(bytes, &S.up_p); S.mvUp = mkBuf(bytes, &S.mvUp_p);
        for (uint32_t i = 0; i < count; i++) S.readback[i] = mkBuf(bytes, &S.readback_p[i]);
    }
    auto layout = [&](VkCommandBuffer cb, Img& I, VkImageLayout from, VkImageLayout to) {
        VkImageMemoryBarrier b = {VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER}; b.oldLayout = from; b.newLayout = to;
        b.srcAccessMask = VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_MEMORY_WRITE_BIT; b.dstAccessMask = VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_MEMORY_WRITE_BIT;
        b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED; b.image = I.img; b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, 0, 0, nullptr, 0, nullptr, 1, &b); };
    auto beginCb = [&](VkCommandBuffer cb) { vkResetCommandBuffer(cb, 0); VkCommandBufferBeginInfo bi = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO}; bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT; vkBeginCommandBuffer(cb, &bi); };
    auto copyToImage = [&](VkCommandBuffer cb, VkBuffer b, Img& I) {
        VkBufferImageCopy c = {}; c.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}; c.imageExtent = {w, h, 1}; vkCmdCopyBufferToImage(cb, b, I.img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &c); };
    auto submitSync = [&](VkCommandBuffer cb, VkFence f) { vkEndCommandBuffer(cb); VkSubmitInfo si = {VK_STRUCTURE_TYPE_SUBMIT_INFO}; si.commandBufferCount = 1; si.pCommandBuffers = &cb;
        vkQueueSubmit(queue, 1, &si, f); vkWaitForFences(dev, 1, &f, VK_TRUE, 20000000000ull); vkResetFences(dev, 1, &f); };
    auto fillMv = [&](uint8_t* p, float mx, float my) {   // the same half-float vector in every texel (tightly packed rows: buffer row length = width)
        const __m128i hh = _mm_cvtps_ph(_mm_setr_ps(mx, my, mx, my), 0); uint64_t two; _mm_storel_epi64((__m128i*)&two, hh);
        uint64_t* q = (uint64_t*)p; for (size_t i = 0; i < (size_t)w * h / 2; i++) q[i] = two; };

    NVSDK_NGX_Parameter* params = nullptr;
    if (NVSDK_NGX_FAILED(NVSDK_NGX_VULKAN_AllocateParameters(&params)) || !params) { fprintf(stderr, "[ngxg-vk] AllocateParameters failed\n"); return fail(5); }
    NVSDK_NGX_DLSSG_Create_Params cp = {}; cp.Width = w; cp.Height = h; cp.NativeBackbufferFormat = VK_FORMAT_R8G8B8A8_UNORM; cp.RenderWidth = w; cp.RenderHeight = h;
    NVSDK_NGX_Handle* handle = nullptr;
    beginCb(slots[0].cb); r = NGX_VK_CREATE_DLSSG(slots[0].cb, 1, 1, &handle, params, &cp); submitSync(slots[0].cb, slots[0].fence);
    if (NVSDK_NGX_FAILED(r) || !handle) { fprintf(stderr, "[ngxg-vk] CreateFeature failed 0x%08x\n", (unsigned)r); return fail(7); }
    { // constant depth plane (0.5) and a zero motion field, images to GENERAL
        for (size_t i = 0; i < (size_t)w * h; i++) ((float*)slots[0].up_p)[i] = 0.5f;
        beginCb(slots[0].cb);
        layout(slots[0].cb, depth, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL); copyToImage(slots[0].cb, slots[0].up, depth); layout(slots[0].cb, depth, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL);
        submitSync(slots[0].cb, slots[0].fence);
        fillMv((uint8_t*)slots[0].mvUp_p, 0.f, 0.f);
        beginCb(slots[0].cb);
        layout(slots[0].cb, mvec, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL); copyToImage(slots[0].cb, slots[0].mvUp, mvec); layout(slots[0].cb, mvec, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL);
        layout(slots[0].cb, back, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL);
        for (uint32_t i = 0; i < count; i++) layout(slots[0].cb, outs[i], VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL);
        submitSync(slots[0].cb, slots[0].fence);
    }
    const float mvSign = getenv("NGXG_MV_SIGN") ? (float)atof(getenv("NGXG_MV_SIGN")) : -1.f;
    NVSDK_NGX_VK_DLSSG_Eval_Params ep = {}; ep.pBackbuffer = &back.res; ep.pDepth = &depth.res; ep.pMVecs = &mvec.res;
    NVSDK_NGX_DLSSG_Opt_Eval_Params op = {}; op.multiFrameCount = count; op.mvecScale[0] = op.mvecScale[1] = 1.f;
    for (int a = 0; a < 4; a++) op.cameraViewToClip[a][a] = op.clipToCameraView[a][a] = op.clipToLensClip[a][a] = op.clipToPrevClip[a][a] = op.prevClipToClip[a][a] = 1.f;
    op.cameraNear = 0.1f; op.cameraFar = 1000.f; op.cameraFOV = 1.f; op.cameraAspectRatio = (float)w / h; op.cameraUp[1] = 1.f; op.cameraRight[0] = 1.f; op.cameraFwd[2] = 1.f;
    fprintf(stderr, "[ngxg-vk] ready %ux%u x%u (ring %u)\n", w, h, count, ring);
    __sync_synchronize(); ctrl->state = 1;

    // ---- two-slot pipeline ----
    // Slot s = seq % 2 owns the input region, staging buffers, command buffer and fence. The queue runs the slots in order (NGX's history stays sequential); the CPU
    // work of the next frame overlaps the GPU work of the current one, and a completion thread converts the finished frame's readback into the output ring and
    // acknowledges it while the main thread is already on the next frame.
    struct Done { int slot; uint64_t seq; uint32_t made; bool first, ok; double t_in, t_rec, t0; };
    std::mutex mu; std::condition_variable cv; std::deque<Done> dq; bool stopping = false;
    bool slotFree[2] = {true, true}; std::mutex smu; std::condition_variable scv;
    std::thread completion([&] {
        const __m256i sh = _mm256_setr_epi8(2,1,0,3, 6,5,4,7, 10,9,8,11, 14,13,12,15, 2,1,0,3, 6,5,4,7, 10,9,8,11, 14,13,12,15);
        for (;;) {
            Done d; { std::unique_lock<std::mutex> l(mu); cv.wait(l, [&] { return stopping || !dq.empty(); }); if (dq.empty()) return; d = dq.front(); dq.pop_front(); }
            Slot& S = slots[d.slot];
            vkWaitForFences(dev, 1, &S.fence, VK_TRUE, 10000000000ull); vkResetFences(dev, 1, &S.fence);
            const double t1 = nowMs(); const uint32_t made = d.ok ? d.made : 0;
            for (uint32_t i = 0; i < made; i++) {
                const uint8_t* rp = (const uint8_t*)S.readback_p[i]; uint8_t* dstp = out_map + ((size_t)(d.seq % ring) * count + i) * bytes;
                parallelRows(h, [&](uint32_t r0, uint32_t r1) {
                    for (uint32_t y = r0; y < r1; y++) {   // RGBA -> BGRA: swap bytes 0 and 2 of every pixel (AVX2 shuffle), alpha forced opaque
                        const uint8_t* s = rp + (size_t)y * w * 4; uint8_t* dd = dstp + (size_t)y * w * 4; uint32_t x = 0;
                        for (; x + 8 <= w; x += 8) {
                            __m256i px = _mm256_loadu_si256((const __m256i*)(s + x * 4));
                            px = _mm256_or_si256(_mm256_shuffle_epi8(px, sh), _mm256_set1_epi32((int)0xFF000000u));
                            _mm256_storeu_si256((__m256i*)(dd + x * 4), px);
                        }
                        for (; x < w; x++) { dd[x*4] = s[x*4+2]; dd[x*4+1] = s[x*4+1]; dd[x*4+2] = s[x*4]; dd[x*4+3] = 255; }
                    }
                });
            }
            const double t2 = nowMs(); const uint32_t k = (uint32_t)(d.seq % 4);
            ext->gen[k] = made; ext->info[k] = d.first ? 1 : 0; ext->ok[k] = d.ok ? 1 : 0;
            if (getenv("NGXG_PROF")) { static int cnt = 0; if (++cnt % 20 == 0) fprintf(stderr, "[ngxg-vk] host: copy-in %.1f ms, record %.1f ms, gpu+wait %.1f ms, readback->out %.1f ms, in flight %.1f ms\n",
                d.t_in, d.t_rec, t1 - d.t0 - d.t_in - d.t_rec, t2 - t1, t2 - d.t0); }
            __sync_synchronize(); ctrl->ok = d.ok ? 1 : 0; ctrl->ack_seq = (uint32_t)d.seq;
            { std::lock_guard<std::mutex> l(smu); slotFree[d.slot] = true; } scv.notify_all();
        }
    });

    uint64_t next = 1, frameId = 0; uint32_t spins = 0; float lastGx = 0.f, lastGy = 0.f; bool backInit = false;
    while (!ctrl->quit) {
        if (next > ctrl->req_seq) { if (++spins > 2000) usleep(1000); else usleep(0); continue; }
        spins = 0;
        const int si = (int)(next % 2); Slot& S = slots[si];
        { std::unique_lock<std::mutex> l(smu); scv.wait(l, [&] { return slotFree[si]; }); slotFree[si] = false; }   // the completion thread is done with this slot's previous frame
        const double q0 = nowMs();
        const bool first = frameId == 0 || (ext->flags[si] & 1);
        parallelRows(h, [&](uint32_t r0, uint32_t r1) { memcpy((uint8_t*)S.up_p + (size_t)r0 * w * 4, in_map + (size_t)si * bytes + (size_t)r0 * w * 4, (size_t)(r1 - r0) * w * 4); });
        const double qc = nowMs();
        VkCommandBuffer cb = S.cb; beginCb(cb);
        layout(cb, back, VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL); copyToImage(cb, S.up, back); layout(cb, back, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL);
        (void)backInit;
        const float gx = ext->mvx[si], gy = ext->mvy[si];
        if (gx != lastGx || gy != lastGy) {   // global content motion hint (display px) -> uniform vector field (uploaded only when it changed)
            lastGx = gx; lastGy = gy; fillMv((uint8_t*)S.mvUp_p, gx * mvSign, gy * mvSign);
            layout(cb, mvec, VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL); copyToImage(cb, S.mvUp, mvec); layout(cb, mvec, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL);
        }
        ++frameId;
        uint32_t made = 0; bool evalOk = true;
        for (uint32_t i = 0; i < count; i++) {
            ep.pOutputInterpFrame = &outs[i].res; op.multiFrameIndex = i + 1; op.reset = first ? 1 : 0;
            r = NGX_VK_EVALUATE_DLSSG(cb, handle, params, &ep, &op);
            if (NVSDK_NGX_FAILED(r)) { fprintf(stderr, "[ngxg-vk] EvaluateFeature failed 0x%08x\n", (unsigned)r); evalOk = false; break; }
            if (first) break;
            layout(cb, outs[i], VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
            VkBufferImageCopy c = {}; c.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}; c.imageExtent = {w, h, 1};
            vkCmdCopyImageToBuffer(cb, outs[i].img, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, S.readback[i], 1, &c);
            layout(cb, outs[i], VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL);
            made++;
        }
        const double qa = nowMs();
        vkEndCommandBuffer(cb); VkSubmitInfo si2 = {VK_STRUCTURE_TYPE_SUBMIT_INFO}; si2.commandBufferCount = 1; si2.pCommandBuffers = &cb;
        vkQueueSubmit(queue, 1, &si2, S.fence);
        { std::lock_guard<std::mutex> l(mu); dq.push_back({si, next, made, first, evalOk, qc - q0, qa - qc, q0}); } cv.notify_one();
        ++next;
    }
    { std::lock_guard<std::mutex> l(mu); stopping = true; } cv.notify_all(); completion.join();
    NVSDK_NGX_VULKAN_ReleaseFeature(handle); NVSDK_NGX_VULKAN_Shutdown1(dev);
    return 0;
}
