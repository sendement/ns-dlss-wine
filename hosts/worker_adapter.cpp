// SPDX-License-Identifier: MIT
// Reference adapter for docs/worker-protocol.md: implements the open worker protocol on the outward (Linux-facing) side and, internally, speaks
// NeuralScreen's own DLSS5 worker wire format on stdin/stdout to a child process ("nvngx.dll --live", PolyForm Strict 1.0.0, user-supplied - never
// distributed by this project). The inner format was learned by reading that worker's source; only the facts of the interface are reproduced here
// (field order and sizes, magic numbers), not its code or comments - see docs/worker-protocol.md's "Reference adapter" section.
//
//   worker_adapter.exe MAX_W MAX_H MAX_OUT_W MAX_OUT_H IN OUT CTL [key=value ...]
//     key=value: nr_preset=N (model-variant hint, read once at worker start), exe=NAME (worker binary, default nvngx.dll)
//   WA_DEBUG=1 (env var): trace every pipe read/write to stderr (byte counts, magic numbers) - use with NS_WORKER_LOG=1 to also see the worker's own log.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>

static void* MapFile(const char* path, size_t bytes) {
    HANDLE f = CreateFileA(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (f == INVALID_HANDLE_VALUE) { fprintf(stderr, "[worker-adapter] open %s failed %lu\n", path, GetLastError()); return nullptr; }
    HANDLE m = CreateFileMappingA(f, nullptr, PAGE_READWRITE, 0, (DWORD)bytes, nullptr);
    if (!m) { fprintf(stderr, "[worker-adapter] mapping failed %lu\n", GetLastError()); return nullptr; }
    return MapViewOfFile(m, FILE_MAP_ALL_ACCESS, 0, 0, bytes);
}

// Open protocol control block (docs/worker-protocol.md).
struct Ctl {
    volatile uint32_t state, req_seq, req_mode, ack_seq, ok, code, quit, out_w, out_h;
    volatile uint32_t work_w, work_h, req_out_w, req_out_h, warmup, flags, style, auto_mask, ui_correction;
    volatile float intensity, local_tone, local_structure, skin_structure;
};

// The NeuralScreen worker's own wire format (facts of the interface only - see docs/worker-protocol.md). All packed, no padding, matching the byte-exact
// layout the worker reads/writes.
#pragma pack(push, 1)
struct InnerHeader {   // sent for both the initial configure and every later live resize
    uint32_t magic, w, h, slot4, slot5, profile, preset, style, auto_mask, ui_correction;
    float intensity, local_tone, local_structure, skin_structure;
    uint32_t full_w, full_h;
};
struct InnerAckRest { uint32_t ok, code, reserved; int64_t pts; };      // the configure/resize acknowledgement, after its magic
struct InnerFrameHdr { uint32_t magic, index, is_first, flags; int64_t pts; };
struct InnerOutRest { uint32_t out_index, ok, byte_count, code; int64_t pts; };   // the per-frame reply, after its magic
#pragma pack(pop)
static_assert(sizeof(InnerHeader) == 64, "inner configure header layout");
static_assert(sizeof(InnerAckRest) == 20, "inner ack layout");
static_assert(sizeof(InnerFrameHdr) == 24, "inner frame header layout");
static_assert(sizeof(InnerOutRest) == 24, "inner out-reply layout");

enum : uint32_t { MAGIC_VIDEO = 0x33563544, MAGIC_RESIZE = 0x5A534E52, MAGIC_ACK = 0x4B434152, MAGIC_FRAME = 0x314D5246, MAGIC_OUT = 0x3154554F };

static HANDLE g_childIn, g_childOutRead;   // pipes to/from the NeuralScreen worker process

static bool WritePipe(const void* p, size_t n) { DWORD w; return WriteFile(g_childIn, p, (DWORD)n, &w, nullptr) && w == n; }
static bool ReadPipe(void* p, size_t n) {
    uint8_t* d = (uint8_t*)p; size_t got = 0;
    const bool dbg = getenv("WA_DEBUG") != nullptr;
    while (got < n) {
        DWORD avail = 0; if (dbg) PeekNamedPipe(g_childOutRead, nullptr, 0, nullptr, &avail, nullptr);
        DWORD r; bool ok = ReadFile(g_childOutRead, d + got, (DWORD)(n - got), &r, nullptr);
        if (dbg) fprintf(stderr, "[worker-adapter] DEBUG ReadPipe want=%zu got=%zu avail_before=%lu readfile_ok=%d r=%lu\n", n, got, (unsigned long)avail, ok, (unsigned long)r);
        if (!ok || r == 0) return false;
        got += r;
    }
    return true;
}

static bool SpawnWorker(const char* dir, const char* exe, const std::map<std::string, std::string>& opts) {
    HANDLE inR, inW, outR, outW;
    SECURITY_ATTRIBUTES sa = {sizeof(sa), nullptr, TRUE};
    CreatePipe(&inR, &inW, &sa, 0); SetHandleInformation(inW, HANDLE_FLAG_INHERIT, 0);
    CreatePipe(&outR, &outW, &sa, 0); SetHandleInformation(outR, HANDLE_FLAG_INHERIT, 0);
    g_childIn = inW; g_childOutRead = outR;
    auto it = opts.find("nr_preset");
    if (it != opts.end()) SetEnvironmentVariableA("NS_NR_PRESET", it->second.c_str());   // model-variant hint, cached by the worker on first read
    STARTUPINFOA si = {sizeof(si)}; si.dwFlags = STARTF_USESTDHANDLES; si.hStdInput = inR; si.hStdOutput = outW; si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    PROCESS_INFORMATION pi = {};
    std::string cmd = std::string("\"") + exe + "\" --live";
    std::vector<char> cmdbuf(cmd.begin(), cmd.end()); cmdbuf.push_back(0);
    bool ok = CreateProcessA(nullptr, cmdbuf.data(), nullptr, nullptr, TRUE, 0, nullptr, dir, &si, &pi);
    CloseHandle(inR); CloseHandle(outW);
    if (!ok) { fprintf(stderr, "[worker-adapter] CreateProcess(%s) failed %lu\n", exe, GetLastError()); return false; }
    CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
    return true;
}

// Sends a configure/resize-shaped header. `waitAck`: block for the matching acknowledgement (used for a live resize, which does send one). The very first
// configure does NOT get a synchronous reply on this wire format - any ack-shaped message the worker eventually emits for it surfaces later, opportunistically,
// and is skipped wherever it's next encountered (the frame-reply loop below, or a later resize's ack wait) - the same way the original client never blocked
// on it either, relying on actual frame results to tell success from failure.
static bool InnerConfigure(uint32_t magic, uint32_t w, uint32_t h, uint32_t warmup, uint32_t slot5, const Ctl* c, uint32_t full_w, uint32_t full_h, bool waitAck, uint32_t* outCode) {
    InnerHeader h_ = {magic, w, h, warmup, slot5, 0, 0, c->style, c->auto_mask, c->ui_correction,
                      c->intensity, c->local_tone, c->local_structure, c->skin_structure, full_w, full_h};
    if (!WritePipe(&h_, sizeof h_)) return false;
    if (!waitAck) return true;
    for (;;) {
        uint32_t m; if (!ReadPipe(&m, 4)) return false;
        if (m == MAGIC_ACK) { InnerAckRest r; if (!ReadPipe(&r, sizeof r)) return false; *outCode = r.code; return r.ok != 0; }
        uint8_t skip[sizeof(InnerAckRest)]; if (!ReadPipe(skip, sizeof skip)) return false;   // some other reply of the same shape; not ours here
    }
}

int main(int argc, char** argv) {
    if (argc < 8) { fprintf(stderr, "[worker-adapter] usage: MAX_W MAX_H MAX_OUT_W MAX_OUT_H IN OUT CTL [key=value ...]\n"); return 2; }
    const uint32_t maxW = atoi(argv[1]), maxH = atoi(argv[2]), maxOutW = atoi(argv[3]), maxOutH = atoi(argv[4]);
    Ctl* c = (Ctl*)MapFile(argv[7], sizeof(Ctl));
    if (!c) return 2;
    auto fail = [&](const char* why) { fprintf(stderr, "[worker-adapter] %s\n", why); c->state = 2; return 3; };
    uint8_t* in = (uint8_t*)MapFile(argv[5], (size_t)maxW * maxH * 4);
    uint8_t* out = (uint8_t*)MapFile(argv[6], (size_t)maxOutW * maxOutH * 4);
    if (!in || !out) return fail("cannot map the frame files");

    std::map<std::string, std::string> opts;
    for (int i = 8; i < argc; i++) { std::string a = argv[i]; auto e = a.find('='); if (e != std::string::npos) opts[a.substr(0, e)] = a.substr(e + 1); }
    std::string exe = opts.count("exe") ? opts["exe"] : "nvngx.dll";
    char dir[MAX_PATH]; GetCurrentDirectoryA(sizeof dir, dir);

    uint32_t curW = 0, curH = 0, curOutW = 0, curOutH = 0, frameIndex = 0;
    bool primed = false;
    static std::vector<uint8_t> zeroMotion;   // no engine motion data for a screen filter - sent once, then reused every frame
    __sync_synchronize(); c->state = 1;   // buffers ready; the worker process itself is spawned lazily, once the first reconfigure tells us the sizes

    uint32_t next = 1, spins = 0;
    while (!c->quit) {
        if (next > c->req_seq) { if (++spins > 2000) Sleep(1); else Sleep(0); continue; }
        spins = 0;
        bool ok = false; uint32_t code = 0;
        if (c->req_mode == 1) {
            uint32_t w = c->work_w, h = c->work_h, ow = c->req_out_w ? c->req_out_w : w, oh = c->req_out_h ? c->req_out_h : h;
            if (w > maxW || h > maxH || ow > maxOutW || oh > maxOutH) {
                fprintf(stderr, "[worker-adapter] reconfigure %ux%u -> %ux%u exceeds the %ux%u/%ux%u ceiling this process was started with\n", w, h, ow, oh, maxW, maxH, maxOutW, maxOutH);
            } else {
                const bool first = !primed;
                if (first) {
                    if (ow != w || oh != h) SetEnvironmentVariableA("NS_NR_SMALL", "1");   // internal-upscale mode: read once at worker start, like nr_preset
                    if (!SpawnWorker(dir, exe.c_str(), opts)) { fail("could not start the worker process"); break; }
                }
                ok = InnerConfigure(first ? MAGIC_VIDEO : MAGIC_RESIZE, w, h, c->warmup, first ? 0 : c->flags, c, (ow != w || oh != h) ? ow : 0, (ow != w || oh != h) ? oh : 0, /*waitAck=*/!first, &code);
                if (ok) {
                    curW = w; curH = h; curOutW = ow; curOutH = oh; primed = true; frameIndex = 0;
                    const bool reconstructing = (ow != w || oh != h);
                    // The motion-vector plane must be sized to the RECONSTRUCTION TARGET (ow x oh), not the work size, whenever the worker reconstructs to
                    // a larger output - sending it at the work size silently starves the worker of input bytes it's still waiting to read (its main
                    // thread blocks in a plain pipe read; there is no error on either side, just a permanent stall on the very first frame).
                    const size_t mvW = reconstructing ? ow : w, mvH = reconstructing ? oh : h;
                    zeroMotion.assign(mvW * mvH * 4, 0);
                }
            }
            c->out_w = curOutW; c->out_h = curOutH;
        } else if (!primed) {
            fprintf(stderr, "[worker-adapter] frame request before the first reconfigure - ignored\n");
        } else {
            InnerFrameHdr fh = {MAGIC_FRAME, frameIndex, frameIndex == 0 ? 1u : 0u, 0, (int64_t)frameIndex};
            const size_t colorBytes = (size_t)curW * curH * 4;
            ok = WritePipe(&fh, sizeof fh) && WritePipe(in, colorBytes) && WritePipe(zeroMotion.data(), zeroMotion.size());
            frameIndex++;
            uint32_t byteCount = 0;
            if (ok) {
                for (;;) {
                    uint32_t m; if (!ReadPipe(&m, 4)) { ok = false; break; }
                    if (getenv("WA_DEBUG")) fprintf(stderr, "[worker-adapter] DEBUG reply magic=0x%08x\n", m);
                    if (m == MAGIC_OUT) { InnerOutRest r; if (!ReadPipe(&r, sizeof r)) { ok = false; break; }
                        if (getenv("WA_DEBUG")) fprintf(stderr, "[worker-adapter] DEBUG OUT reply: out_index=%u ok=%u byte_count=%u code=%u\n", r.out_index, r.ok, r.byte_count, r.code);
                        byteCount = r.byte_count; code = r.code; ok = r.ok != 0; break; }
                    uint8_t skip[sizeof(InnerAckRest)]; if (!ReadPipe(skip, sizeof skip)) { ok = false; break; }   // some other reply of the ack shape; not ours here
                }
            }
            if (ok && byteCount) ok = ReadPipe(out, byteCount);
            else ok = false;   // no frame produced this round (e.g. still priming temporal history) - not an error, just nothing to show
            if (getenv("WA_DEBUG")) fprintf(stderr, "[worker-adapter] DEBUG frame %u final ok=%d byteCount=%u\n", frameIndex - 1, ok, byteCount);
        }
        c->code = code;
        MemoryBarrier(); c->ok = ok ? 1 : 0; c->ack_seq = next;
        ++next;
    }
    return 0;
}
