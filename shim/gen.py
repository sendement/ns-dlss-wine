#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generates the PE (nvcuda.dll) and unix (nvcuda.so) halves of a Wine nvcuda
shim that forwards the CUDA Driver API to the host's libcuda.so.1.
Every function is forwarded as 12 raw 64-bit args (SysV/MS-ABI safe for
int/pointer/size_t signatures, which is all the driver API uses)."""
import subprocess, re, sys
SPECIAL = {"cuGetProcAddress", "cuGetProcAddress_v2", "cuGetExportTable", "cuDeviceGetLuid"}
names = set(subprocess.check_output(
    "nm -D /usr/lib/libcuda.so.1 | awk '$2==\"T\" && $3 ~ /^cu/ {print $3}'", shell=True, text=True).split())
# names Windows callers import that libcuda may lack (stubbed as NOT_SUPPORTED)
extra = set()
for dll in sys.argv[1:]:
    out = subprocess.check_output(["x86_64-w64-mingw32-objdump", "-p", dll], text=True)
    extra |= set(re.findall(r"\s(cu[A-Za-z0-9_]+)\s*$", out, re.M))
names |= extra
import os
if os.path.exists('extra_names.txt'):
    names |= set(open('extra_names.txt').read().split())
names -= SPECIAL
names = sorted(names)
print(f"{len(names)} forwarded functions ({len(extra - set(names))} extra)")

GETEXP_ID = len(names)      # unix-side alias for the real cuGetExportTable
PTRCALL_ID = 0xFFFFFFFE    # "call this raw function pointer" entry
with open("pe_stubs.c", "w") as f:
    f.write('''#include <windows.h>
typedef unsigned long long u64;
typedef LONG (NTAPI *NtQVM_t)(HANDLE, PVOID, ULONG, PVOID, SIZE_T, PSIZE_T);
typedef LONG (NTAPI *UnixCall_t)(u64, unsigned, void *);
struct call { u64 a[12]; u64 ret; };
static u64 g_handle; static UnixCall_t g_call; static int g_init;
extern IMAGE_DOS_HEADER __ImageBase;
static int init(void) {
    if (g_init) return g_init > 0;
    HMODULE nt = GetModuleHandleA("ntdll.dll");
    NtQVM_t q = (NtQVM_t)GetProcAddress(nt, "NtQueryVirtualMemory");
    g_call = (UnixCall_t)GetProcAddress(nt, "__wine_unix_call");
    /* 1000 = MemoryWineUnixFuncs (old) / MemoryWineLoadUnixLib (new): both return the handle */
    LONG st = q ? q(GetCurrentProcess(), &__ImageBase, 1000, &g_handle, sizeof(g_handle), NULL) : -1;
    g_init = (st == 0 && g_call) ? 1 : -1;
    if (g_init < 0) OutputDebugStringA("nvcuda shim: unix handle init failed\\n");
    return g_init > 0;
}
static u64 fwd64(unsigned id, struct call *c) {
    if (!init()) return 801;
    struct { struct call c; unsigned id; } m; m.c = *c; m.id = id;
    if (g_call(g_handle, 0, &m)) return 801;
    return m.c.ret;
}
static int fwd(unsigned id, struct call *c) { return (int)fwd64(id, c); }
#define A u64 a0,u64 a1,u64 a2,u64 a3,u64 a4,u64 a5,u64 a6,u64 a7,u64 a8,u64 a9,u64 a10,u64 a11
#define P {a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10,a11}
''')
    # D2H read-ahead cache: the NV12 readback of the frame-generation bridge is ~2100 row-sized synchronous
    # cuMemcpyDtoH calls (~7 us each through the unix call). Serve consecutive small reads from one bulk copy;
    # any other forwarded call (kernel launch, memset, H2D, sync, ...) drops the cache.
    ID_DTOH = names.index("cuMemcpyDtoH_v2"); ID_RANGE = names.index("cuMemGetAddressRange_v2")
    f.write("static SRWLOCK g_rl = SRWLOCK_INIT; static unsigned char *g_rc; static u64 g_rlo, g_rhi;\n")
    f.write("static volatile LONG g_rgen;\n")
    for i, n in enumerate(names):
        if n == "cuMemcpyDtoH_v2":
            f.write(f"""int {n}(A) {{
    u64 dst = a0, src = a1, len = a2;
    static int nocache = -1; if (nocache < 0) nocache = GetEnvironmentVariableA("NVCUDA_NOCACHE", NULL, 0) > 0;
    if (nocache || len == 0 || len > (1u << 20)) {{ struct call c = {{ P }}; return fwd({i}, &c); }}
    AcquireSRWLockExclusive(&g_rl);
    if (!(g_rc && src >= g_rlo && src + len <= g_rhi)) {{
        u64 base = 0, size = 0; struct call r = {{ {{ 0, 0, src }} }};
        u64 ptrs[2] = {{ 0, 0 }};
        r.a[0] = (u64)&ptrs[0]; r.a[1] = (u64)&ptrs[1]; r.a[2] = src;
        int hit = 0;
        if (fwd64({ID_RANGE}, &r) == 0) {{ base = ptrs[0]; size = ptrs[1]; }}
        if (size && src + len <= base + size) {{
            u64 n = base + size - src; if (n > (8u << 20)) n = 8u << 20;
            if (!g_rc) g_rc = (unsigned char *)VirtualAlloc(NULL, 8u << 20, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
            if (g_rc) {{
                struct call c = {{ {{ (u64)g_rc, src, n }} }};
                if (fwd64({i}, &c) == 0) {{ g_rlo = src; g_rhi = src + n; hit = 1; }}
            }}
        }}
        if (!hit) {{ g_rc = g_rc; g_rhi = g_rlo = 0; ReleaseSRWLockExclusive(&g_rl); struct call c = {{ P }}; return fwd64({i}, &c); }}
    }}
    memcpy((void *)dst, g_rc + (src - g_rlo), len);
    ReleaseSRWLockExclusive(&g_rl);
    return 0;
}}
""")
        else:
            f.write(f"int {n}(A) {{ struct call c = {{ P }}; if (g_rhi) {{ AcquireSRWLockExclusive(&g_rl); g_rlo = g_rhi = 0; ReleaseSRWLockExclusive(&g_rl); }} return fwd({i}, &c); }}\n")
    f.write('''
/* Function pointers handed out by real libcuda would be SysV-ABI and crash
   when called from Windows code - hand out our own exported stubs instead. */
static void dbg(const char *fmt, const char *a) { char b[300]; wsprintfA(b, fmt, a ? a : "(null)"); OutputDebugStringA(b); }
static int getproc(const char *sym, void **pfn) {
    if (!sym || !pfn) return 1;
    /* cuda.h #defines these to their _v2 ABI (64-bit sizes, CUDA 3.2); the unversioned libcuda export is
       the ancient v1 layout. Everything else keeps its plain name (later _vN variants change semantics). */
    static const char *legacy[] = { "cuDeviceTotalMem", "cuCtxCreate", "cuModuleGetGlobal", "cuMemGetInfo", "cuMemAlloc",
        "cuMemAllocPitch", "cuMemFree", "cuMemGetAddressRange", "cuMemAllocHost", "cuMemHostGetDevicePointer",
        "cuMemcpyHtoD", "cuMemcpyDtoH", "cuMemcpyDtoD", "cuMemcpyDtoA", "cuMemcpyAtoD", "cuMemcpyHtoA", "cuMemcpyAtoH",
        "cuMemcpyAtoA", "cuMemcpyHtoAAsync", "cuMemcpyAtoHAsync", "cuMemcpy2D", "cuMemcpy2DUnaligned", "cuMemcpy3D",
        "cuMemcpyHtoDAsync", "cuMemcpyDtoHAsync", "cuMemcpyDtoDAsync", "cuMemcpy2DAsync", "cuMemcpy3DAsync",
        "cuMemsetD8", "cuMemsetD16", "cuMemsetD32", "cuMemsetD2D8", "cuMemsetD2D16", "cuMemsetD2D32", "cuArrayCreate",
        "cuArrayGetDescriptor", "cuArray3DCreate", "cuArray3DGetDescriptor", "cuTexRefSetAddress", "cuTexRefGetAddress",
        "cuTexRefSetAddress2D", "cuStreamDestroy", "cuEventDestroy", "cuCtxDestroy", "cuCtxPopCurrent", "cuCtxPushCurrent",
        "cuGraphicsResourceGetMappedPointer", "cuLinkCreate", "cuLinkAddData", "cuLinkAddFile", "cuMemHostRegister",
        "cuDevicePrimaryCtxSetFlags", "cuDevicePrimaryCtxRelease", "cuDevicePrimaryCtxReset", "cuStreamBeginCapture",
        "cuGraphicsResourceSetMapFlags", "cuGLCtxCreate", NULL };
    void *p = NULL; char vn[160];
    if (lstrlenA(sym) < 140) for (int i = 0; legacy[i] && !p; i++) if (!lstrcmpA(sym, legacy[i])) { lstrcpyA(vn, sym); lstrcatA(vn, "_v2"); p = (void *)GetProcAddress((HMODULE)&__ImageBase, vn); }
    if (!p) p = (void *)GetProcAddress((HMODULE)&__ImageBase, sym);
    dbg("nvcudashim: cuGetProcAddress(%s)\\n", sym); if (!p) dbg("nvcudashim:   MISS %s\\n", sym);
    *pfn = p; return p ? 0 : 500;
}
int cuGetProcAddress(const char *sym, void **pfn, int ver, u64 flags) { return getproc(sym, pfn); }
int cuGetProcAddress_v2(const char *sym, void **pfn, int ver, u64 flags, void *status) {
    if (status) *(int *)status = 0; return getproc(sym, pfn); }
/* cuGetExportTable: real libcuda hands back tables of SysV-ABI function pointers, which
   Windows callers would call with the MS ABI. Wrap each entry in a stub that forwards the
   raw call through the unix side. */
#define MAXT 8
#define MAXE 64
static u64 g_real[MAXT][MAXE]; static u64 g_fake[MAXT][MAXE + 1]; static unsigned char g_uuid[MAXT][16]; static int g_nt;

/* ---- cudart "integrity check" handshake (export table d4082055, entry 0) ----
   libcuda answers with HMAC-MD2 over (version, pid, tid, addresses of the two
   tables and of the function, cookie, per-device ids). cudart recomputes it from
   ITS view: the Windows pid/tid and the (fake) table/function addresses we handed
   out - so the real libcuda answer never matches. Recompute it here instead; the
   per-device chunks are read out of libcuda's memory by the unix side. */
static const unsigned char IC_KEYTAB[64] = {107,207,50,15,164,73,211,168,51,248,208,142,18,78,168,0,235,148,44,143,52,73,222,246,191,41,145,32,199,101,246,186,120,92,102,39,167,178,115,146,219,34,30,32,20,111,135,255,165,195,24,1,102,100,165,14,112,81,82,167,128,75,223,239};
static const unsigned char IC_MD2S[256] = {41,46,67,201,162,216,124,1,61,54,84,161,236,240,6,19,98,167,5,243,192,199,115,140,152,147,43,217,188,76,130,202,30,155,87,60,253,212,224,22,103,66,111,24,138,23,229,18,190,78,196,214,218,158,222,73,160,251,245,142,187,47,238,122,169,104,121,145,21,178,7,63,148,194,16,137,11,34,95,33,128,127,93,154,90,144,50,39,53,62,204,231,191,247,151,3,255,25,48,179,72,165,181,209,215,94,146,42,172,86,170,198,79,184,56,210,150,164,125,182,118,252,107,226,156,116,4,241,69,157,112,89,100,113,135,32,134,91,207,101,230,45,168,2,27,96,37,173,174,176,185,246,28,70,97,105,52,64,126,15,85,71,163,35,221,81,175,58,195,92,249,206,186,197,234,38,44,83,13,110,133,40,132,9,211,223,205,244,65,129,77,82,106,220,55,200,108,193,171,250,36,225,123,8,12,189,177,74,120,136,149,139,227,99,232,109,233,203,213,254,59,0,29,57,242,239,183,14,102,88,208,228,166,119,114,248,235,117,75,10,49,68,80,180,143,237,31,26,219,153,141,51,159,17,131,20};
typedef unsigned char u8; typedef unsigned u32;
struct md2 { u8 X[48]; u8 buf[16]; u8 C[16]; u8 L; int n; };
static void md2_init(struct md2 *m) { memset(m, 0, sizeof *m); }
static void md2_block(struct md2 *m) {
    u8 t = 0; int i, j;
    for (i = 0; i < 16; i++) { m->X[16 + i] = m->buf[i]; m->X[32 + i] = m->X[i] ^ m->buf[i]; }
    for (j = 0; j < 18; j++) { for (i = 0; i < 48; i++) t = m->X[i] ^= IC_MD2S[t]; t = (u8)(t + j); }
    t = m->L;
    for (i = 0; i < 16; i++) t = m->C[i] ^= IC_MD2S[m->buf[i] ^ t];
    m->L = t;
}
static void md2_upd(struct md2 *m, const u8 *p, int n) { while (n--) { m->buf[m->n++] = *p++; if (m->n == 16) { md2_block(m); m->n = 0; } } }
static void md2_fin(struct md2 *m, u8 *out) {
    u8 pad = 16 - m->n, c[16]; u8 pb[16]; memset(pb, pad, pad); md2_upd(m, pb, pad);
    memcpy(c, m->C, 16); md2_upd(m, c, 16); memcpy(out, m->X, 16);
}
/* key derived from the obfuscated table */
static void ic_key(u8 *key) {
    u32 r8 = 0xffffff8b; unsigned i = 13; int guard = 0;
    memset(key, 0, 16);
    do {
        u32 ka = IC_KEYTAB[i], kb = IC_KEYTAB[i + 16], kc = IC_KEYTAB[i + 32], kd = IC_KEYTAB[i + 48];
        u32 v = ka ^ kb ^ r8; u32 e = kc ^ kb ^ kd;
        key[(v & 0xff) >> 4] = (u8)e;
        r8 = ~(e ^ r8); i = v & 0xf;
    } while (i != 13 && ++guard < 64);
}
/* devs: n chunks of 28 bytes */
static void ic_mac(u32 id, u64 cookie, u32 pid, u32 tid, u64 t6bd5, u64 td408, u64 fn,
                   const u8 *devs, unsigned ndev, u8 *out) {
    u8 key[16], msg[48], k2[16], d1[16]; struct md2 m; int i;
    ic_key(key);
    u32 w[4] = { 0x32f0, id, pid, tid }; memcpy(msg, w, 16);
    memcpy(msg + 16, &t6bd5, 8); memcpy(msg + 24, &td408, 8); memcpy(msg + 32, &fn, 8); memcpy(msg + 40, &cookie, 8);
    for (i = 0; i < 16; i++) k2[i] = key[i] ^ 0x36;
    md2_init(&m); md2_upd(&m, k2, 16); md2_upd(&m, msg, 48); md2_upd(&m, devs, ndev * 28); md2_fin(&m, d1);
    for (i = 0; i < 16; i++) k2[i] = key[i] ^ 0x5c;
    md2_init(&m); md2_upd(&m, k2, 16); md2_upd(&m, d1, 16); md2_fin(&m, out);
}

static int is_uuid(int ti, const unsigned char *u) { return !memcmp(g_uuid[ti], u, 16); }
static const unsigned char UUID_CLS[16] = {0x6b,0xd5,0xfb,0x6c,0x5b,0xf4,0xe7,0x4a,0x89,0x87,0xd9,0x39,0x12,0xfd,0x9d,0xf9};
static const unsigned char UUID_IC[16]  = {0xd4,0x08,0x20,0x55,0xbd,0xe6,0x70,0x4b,0x8d,0x34,0xba,0x12,0x3c,0x66,0xe1,0xf2};
static int es_hook(int ti, int k, u64 a0, u64 a1, u64 a2, u64 *ret) {
    if (k != 0 || !is_uuid(ti, UUID_IC) || (a0 % 10) < 2) return 0;
    int tc = -1; for (int i = 0; i < g_nt; i++) if (is_uuid(i, UUID_CLS)) tc = i;
    if (tc < 0) return 0;
    unsigned char buf[4 + 8 * 28], mac[16]; struct call c = {{ (u64)buf }};
    if (fwd(0xFFFFFFFDu, &c)) return 0;
    unsigned nd = *(unsigned *)buf; if (nd > 8) nd = 8;
    ic_mac((u32)a0, a1, GetCurrentProcessId(), GetCurrentThreadId(), (u64)g_fake[tc], (u64)g_fake[ti], g_fake[ti][1], buf + 4, nd, mac);
    memcpy((void *)a2, mac, 16); *ret = 0; return 1;
}
static u64 es_0_0(A) { u64 rv; if (es_hook(0, 0, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][0],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_1(A) { u64 rv; if (es_hook(0, 1, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][1],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_2(A) { u64 rv; if (es_hook(0, 2, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][2],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_3(A) { u64 rv; if (es_hook(0, 3, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][3],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_4(A) { u64 rv; if (es_hook(0, 4, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][4],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_5(A) { u64 rv; if (es_hook(0, 5, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][5],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_6(A) { u64 rv; if (es_hook(0, 6, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][6],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_7(A) { u64 rv; if (es_hook(0, 7, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][7],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_8(A) { u64 rv; if (es_hook(0, 8, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][8],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_9(A) { u64 rv; if (es_hook(0, 9, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][9],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_10(A) { u64 rv; if (es_hook(0, 10, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][10],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_11(A) { u64 rv; if (es_hook(0, 11, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][11],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_12(A) { u64 rv; if (es_hook(0, 12, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][12],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_13(A) { u64 rv; if (es_hook(0, 13, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][13],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_14(A) { u64 rv; if (es_hook(0, 14, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][14],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_15(A) { u64 rv; if (es_hook(0, 15, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][15],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_16(A) { u64 rv; if (es_hook(0, 16, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][16],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_17(A) { u64 rv; if (es_hook(0, 17, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][17],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_18(A) { u64 rv; if (es_hook(0, 18, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][18],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_19(A) { u64 rv; if (es_hook(0, 19, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][19],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_20(A) { u64 rv; if (es_hook(0, 20, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][20],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_21(A) { u64 rv; if (es_hook(0, 21, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][21],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_22(A) { u64 rv; if (es_hook(0, 22, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][22],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_23(A) { u64 rv; if (es_hook(0, 23, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][23],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_24(A) { u64 rv; if (es_hook(0, 24, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][24],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_25(A) { u64 rv; if (es_hook(0, 25, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][25],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_26(A) { u64 rv; if (es_hook(0, 26, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][26],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_27(A) { u64 rv; if (es_hook(0, 27, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][27],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_28(A) { u64 rv; if (es_hook(0, 28, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][28],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_29(A) { u64 rv; if (es_hook(0, 29, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][29],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_30(A) { u64 rv; if (es_hook(0, 30, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][30],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_31(A) { u64 rv; if (es_hook(0, 31, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][31],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_32(A) { u64 rv; if (es_hook(0, 32, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][32],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_33(A) { u64 rv; if (es_hook(0, 33, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][33],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_34(A) { u64 rv; if (es_hook(0, 34, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][34],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_35(A) { u64 rv; if (es_hook(0, 35, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][35],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_36(A) { u64 rv; if (es_hook(0, 36, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][36],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_37(A) { u64 rv; if (es_hook(0, 37, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][37],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_38(A) { u64 rv; if (es_hook(0, 38, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][38],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_39(A) { u64 rv; if (es_hook(0, 39, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][39],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_40(A) { u64 rv; if (es_hook(0, 40, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][40],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_41(A) { u64 rv; if (es_hook(0, 41, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][41],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_42(A) { u64 rv; if (es_hook(0, 42, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][42],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_43(A) { u64 rv; if (es_hook(0, 43, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][43],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_44(A) { u64 rv; if (es_hook(0, 44, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][44],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_45(A) { u64 rv; if (es_hook(0, 45, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][45],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_46(A) { u64 rv; if (es_hook(0, 46, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][46],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_47(A) { u64 rv; if (es_hook(0, 47, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][47],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_48(A) { u64 rv; if (es_hook(0, 48, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][48],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_49(A) { u64 rv; if (es_hook(0, 49, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][49],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_50(A) { u64 rv; if (es_hook(0, 50, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][50],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_51(A) { u64 rv; if (es_hook(0, 51, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][51],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_52(A) { u64 rv; if (es_hook(0, 52, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][52],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_53(A) { u64 rv; if (es_hook(0, 53, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][53],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_54(A) { u64 rv; if (es_hook(0, 54, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][54],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_55(A) { u64 rv; if (es_hook(0, 55, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][55],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_56(A) { u64 rv; if (es_hook(0, 56, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][56],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_57(A) { u64 rv; if (es_hook(0, 57, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][57],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_58(A) { u64 rv; if (es_hook(0, 58, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][58],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_59(A) { u64 rv; if (es_hook(0, 59, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][59],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_60(A) { u64 rv; if (es_hook(0, 60, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][60],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_61(A) { u64 rv; if (es_hook(0, 61, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][61],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_62(A) { u64 rv; if (es_hook(0, 62, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][62],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_0_63(A) { u64 rv; if (es_hook(0, 63, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[0][63],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_0(A) { u64 rv; if (es_hook(1, 0, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][0],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_1(A) { u64 rv; if (es_hook(1, 1, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][1],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_2(A) { u64 rv; if (es_hook(1, 2, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][2],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_3(A) { u64 rv; if (es_hook(1, 3, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][3],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_4(A) { u64 rv; if (es_hook(1, 4, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][4],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_5(A) { u64 rv; if (es_hook(1, 5, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][5],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_6(A) { u64 rv; if (es_hook(1, 6, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][6],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_7(A) { u64 rv; if (es_hook(1, 7, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][7],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_8(A) { u64 rv; if (es_hook(1, 8, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][8],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_9(A) { u64 rv; if (es_hook(1, 9, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][9],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_10(A) { u64 rv; if (es_hook(1, 10, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][10],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_11(A) { u64 rv; if (es_hook(1, 11, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][11],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_12(A) { u64 rv; if (es_hook(1, 12, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][12],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_13(A) { u64 rv; if (es_hook(1, 13, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][13],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_14(A) { u64 rv; if (es_hook(1, 14, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][14],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_15(A) { u64 rv; if (es_hook(1, 15, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][15],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_16(A) { u64 rv; if (es_hook(1, 16, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][16],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_17(A) { u64 rv; if (es_hook(1, 17, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][17],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_18(A) { u64 rv; if (es_hook(1, 18, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][18],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_19(A) { u64 rv; if (es_hook(1, 19, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][19],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_20(A) { u64 rv; if (es_hook(1, 20, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][20],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_21(A) { u64 rv; if (es_hook(1, 21, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][21],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_22(A) { u64 rv; if (es_hook(1, 22, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][22],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_23(A) { u64 rv; if (es_hook(1, 23, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][23],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_24(A) { u64 rv; if (es_hook(1, 24, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][24],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_25(A) { u64 rv; if (es_hook(1, 25, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][25],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_26(A) { u64 rv; if (es_hook(1, 26, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][26],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_27(A) { u64 rv; if (es_hook(1, 27, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][27],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_28(A) { u64 rv; if (es_hook(1, 28, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][28],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_29(A) { u64 rv; if (es_hook(1, 29, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][29],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_30(A) { u64 rv; if (es_hook(1, 30, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][30],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_31(A) { u64 rv; if (es_hook(1, 31, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][31],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_32(A) { u64 rv; if (es_hook(1, 32, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][32],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_33(A) { u64 rv; if (es_hook(1, 33, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][33],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_34(A) { u64 rv; if (es_hook(1, 34, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][34],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_35(A) { u64 rv; if (es_hook(1, 35, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][35],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_36(A) { u64 rv; if (es_hook(1, 36, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][36],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_37(A) { u64 rv; if (es_hook(1, 37, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][37],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_38(A) { u64 rv; if (es_hook(1, 38, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][38],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_39(A) { u64 rv; if (es_hook(1, 39, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][39],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_40(A) { u64 rv; if (es_hook(1, 40, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][40],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_41(A) { u64 rv; if (es_hook(1, 41, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][41],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_42(A) { u64 rv; if (es_hook(1, 42, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][42],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_43(A) { u64 rv; if (es_hook(1, 43, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][43],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_44(A) { u64 rv; if (es_hook(1, 44, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][44],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_45(A) { u64 rv; if (es_hook(1, 45, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][45],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_46(A) { u64 rv; if (es_hook(1, 46, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][46],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_47(A) { u64 rv; if (es_hook(1, 47, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][47],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_48(A) { u64 rv; if (es_hook(1, 48, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][48],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_49(A) { u64 rv; if (es_hook(1, 49, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][49],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_50(A) { u64 rv; if (es_hook(1, 50, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][50],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_51(A) { u64 rv; if (es_hook(1, 51, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][51],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_52(A) { u64 rv; if (es_hook(1, 52, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][52],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_53(A) { u64 rv; if (es_hook(1, 53, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][53],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_54(A) { u64 rv; if (es_hook(1, 54, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][54],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_55(A) { u64 rv; if (es_hook(1, 55, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][55],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_56(A) { u64 rv; if (es_hook(1, 56, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][56],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_57(A) { u64 rv; if (es_hook(1, 57, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][57],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_58(A) { u64 rv; if (es_hook(1, 58, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][58],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_59(A) { u64 rv; if (es_hook(1, 59, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][59],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_60(A) { u64 rv; if (es_hook(1, 60, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][60],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_61(A) { u64 rv; if (es_hook(1, 61, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][61],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_62(A) { u64 rv; if (es_hook(1, 62, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][62],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_1_63(A) { u64 rv; if (es_hook(1, 63, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[1][63],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_0(A) { u64 rv; if (es_hook(2, 0, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][0],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_1(A) { u64 rv; if (es_hook(2, 1, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][1],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_2(A) { u64 rv; if (es_hook(2, 2, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][2],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_3(A) { u64 rv; if (es_hook(2, 3, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][3],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_4(A) { u64 rv; if (es_hook(2, 4, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][4],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_5(A) { u64 rv; if (es_hook(2, 5, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][5],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_6(A) { u64 rv; if (es_hook(2, 6, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][6],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_7(A) { u64 rv; if (es_hook(2, 7, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][7],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_8(A) { u64 rv; if (es_hook(2, 8, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][8],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_9(A) { u64 rv; if (es_hook(2, 9, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][9],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_10(A) { u64 rv; if (es_hook(2, 10, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][10],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_11(A) { u64 rv; if (es_hook(2, 11, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][11],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_12(A) { u64 rv; if (es_hook(2, 12, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][12],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_13(A) { u64 rv; if (es_hook(2, 13, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][13],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_14(A) { u64 rv; if (es_hook(2, 14, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][14],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_15(A) { u64 rv; if (es_hook(2, 15, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][15],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_16(A) { u64 rv; if (es_hook(2, 16, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][16],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_17(A) { u64 rv; if (es_hook(2, 17, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][17],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_18(A) { u64 rv; if (es_hook(2, 18, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][18],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_19(A) { u64 rv; if (es_hook(2, 19, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][19],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_20(A) { u64 rv; if (es_hook(2, 20, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][20],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_21(A) { u64 rv; if (es_hook(2, 21, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][21],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_22(A) { u64 rv; if (es_hook(2, 22, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][22],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_23(A) { u64 rv; if (es_hook(2, 23, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][23],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_24(A) { u64 rv; if (es_hook(2, 24, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][24],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_25(A) { u64 rv; if (es_hook(2, 25, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][25],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_26(A) { u64 rv; if (es_hook(2, 26, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][26],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_27(A) { u64 rv; if (es_hook(2, 27, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][27],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_28(A) { u64 rv; if (es_hook(2, 28, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][28],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_29(A) { u64 rv; if (es_hook(2, 29, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][29],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_30(A) { u64 rv; if (es_hook(2, 30, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][30],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_31(A) { u64 rv; if (es_hook(2, 31, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][31],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_32(A) { u64 rv; if (es_hook(2, 32, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][32],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_33(A) { u64 rv; if (es_hook(2, 33, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][33],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_34(A) { u64 rv; if (es_hook(2, 34, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][34],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_35(A) { u64 rv; if (es_hook(2, 35, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][35],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_36(A) { u64 rv; if (es_hook(2, 36, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][36],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_37(A) { u64 rv; if (es_hook(2, 37, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][37],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_38(A) { u64 rv; if (es_hook(2, 38, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][38],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_39(A) { u64 rv; if (es_hook(2, 39, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][39],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_40(A) { u64 rv; if (es_hook(2, 40, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][40],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_41(A) { u64 rv; if (es_hook(2, 41, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][41],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_42(A) { u64 rv; if (es_hook(2, 42, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][42],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_43(A) { u64 rv; if (es_hook(2, 43, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][43],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_44(A) { u64 rv; if (es_hook(2, 44, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][44],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_45(A) { u64 rv; if (es_hook(2, 45, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][45],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_46(A) { u64 rv; if (es_hook(2, 46, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][46],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_47(A) { u64 rv; if (es_hook(2, 47, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][47],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_48(A) { u64 rv; if (es_hook(2, 48, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][48],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_49(A) { u64 rv; if (es_hook(2, 49, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][49],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_50(A) { u64 rv; if (es_hook(2, 50, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][50],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_51(A) { u64 rv; if (es_hook(2, 51, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][51],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_52(A) { u64 rv; if (es_hook(2, 52, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][52],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_53(A) { u64 rv; if (es_hook(2, 53, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][53],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_54(A) { u64 rv; if (es_hook(2, 54, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][54],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_55(A) { u64 rv; if (es_hook(2, 55, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][55],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_56(A) { u64 rv; if (es_hook(2, 56, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][56],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_57(A) { u64 rv; if (es_hook(2, 57, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][57],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_58(A) { u64 rv; if (es_hook(2, 58, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][58],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_59(A) { u64 rv; if (es_hook(2, 59, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][59],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_60(A) { u64 rv; if (es_hook(2, 60, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][60],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_61(A) { u64 rv; if (es_hook(2, 61, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][61],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_62(A) { u64 rv; if (es_hook(2, 62, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][62],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_2_63(A) { u64 rv; if (es_hook(2, 63, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[2][63],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_0(A) { u64 rv; if (es_hook(3, 0, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][0],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_1(A) { u64 rv; if (es_hook(3, 1, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][1],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_2(A) { u64 rv; if (es_hook(3, 2, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][2],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_3(A) { u64 rv; if (es_hook(3, 3, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][3],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_4(A) { u64 rv; if (es_hook(3, 4, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][4],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_5(A) { u64 rv; if (es_hook(3, 5, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][5],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_6(A) { u64 rv; if (es_hook(3, 6, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][6],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_7(A) { u64 rv; if (es_hook(3, 7, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][7],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_8(A) { u64 rv; if (es_hook(3, 8, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][8],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_9(A) { u64 rv; if (es_hook(3, 9, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][9],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_10(A) { u64 rv; if (es_hook(3, 10, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][10],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_11(A) { u64 rv; if (es_hook(3, 11, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][11],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_12(A) { u64 rv; if (es_hook(3, 12, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][12],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_13(A) { u64 rv; if (es_hook(3, 13, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][13],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_14(A) { u64 rv; if (es_hook(3, 14, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][14],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_15(A) { u64 rv; if (es_hook(3, 15, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][15],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_16(A) { u64 rv; if (es_hook(3, 16, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][16],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_17(A) { u64 rv; if (es_hook(3, 17, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][17],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_18(A) { u64 rv; if (es_hook(3, 18, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][18],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_19(A) { u64 rv; if (es_hook(3, 19, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][19],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_20(A) { u64 rv; if (es_hook(3, 20, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][20],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_21(A) { u64 rv; if (es_hook(3, 21, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][21],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_22(A) { u64 rv; if (es_hook(3, 22, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][22],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_23(A) { u64 rv; if (es_hook(3, 23, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][23],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_24(A) { u64 rv; if (es_hook(3, 24, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][24],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_25(A) { u64 rv; if (es_hook(3, 25, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][25],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_26(A) { u64 rv; if (es_hook(3, 26, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][26],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_27(A) { u64 rv; if (es_hook(3, 27, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][27],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_28(A) { u64 rv; if (es_hook(3, 28, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][28],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_29(A) { u64 rv; if (es_hook(3, 29, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][29],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_30(A) { u64 rv; if (es_hook(3, 30, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][30],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_31(A) { u64 rv; if (es_hook(3, 31, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][31],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_32(A) { u64 rv; if (es_hook(3, 32, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][32],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_33(A) { u64 rv; if (es_hook(3, 33, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][33],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_34(A) { u64 rv; if (es_hook(3, 34, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][34],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_35(A) { u64 rv; if (es_hook(3, 35, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][35],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_36(A) { u64 rv; if (es_hook(3, 36, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][36],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_37(A) { u64 rv; if (es_hook(3, 37, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][37],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_38(A) { u64 rv; if (es_hook(3, 38, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][38],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_39(A) { u64 rv; if (es_hook(3, 39, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][39],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_40(A) { u64 rv; if (es_hook(3, 40, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][40],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_41(A) { u64 rv; if (es_hook(3, 41, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][41],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_42(A) { u64 rv; if (es_hook(3, 42, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][42],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_43(A) { u64 rv; if (es_hook(3, 43, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][43],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_44(A) { u64 rv; if (es_hook(3, 44, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][44],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_45(A) { u64 rv; if (es_hook(3, 45, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][45],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_46(A) { u64 rv; if (es_hook(3, 46, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][46],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_47(A) { u64 rv; if (es_hook(3, 47, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][47],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_48(A) { u64 rv; if (es_hook(3, 48, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][48],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_49(A) { u64 rv; if (es_hook(3, 49, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][49],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_50(A) { u64 rv; if (es_hook(3, 50, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][50],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_51(A) { u64 rv; if (es_hook(3, 51, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][51],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_52(A) { u64 rv; if (es_hook(3, 52, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][52],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_53(A) { u64 rv; if (es_hook(3, 53, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][53],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_54(A) { u64 rv; if (es_hook(3, 54, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][54],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_55(A) { u64 rv; if (es_hook(3, 55, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][55],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_56(A) { u64 rv; if (es_hook(3, 56, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][56],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_57(A) { u64 rv; if (es_hook(3, 57, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][57],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_58(A) { u64 rv; if (es_hook(3, 58, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][58],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_59(A) { u64 rv; if (es_hook(3, 59, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][59],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_60(A) { u64 rv; if (es_hook(3, 60, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][60],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_61(A) { u64 rv; if (es_hook(3, 61, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][61],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_62(A) { u64 rv; if (es_hook(3, 62, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][62],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_3_63(A) { u64 rv; if (es_hook(3, 63, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[3][63],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_0(A) { u64 rv; if (es_hook(4, 0, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][0],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_1(A) { u64 rv; if (es_hook(4, 1, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][1],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_2(A) { u64 rv; if (es_hook(4, 2, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][2],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_3(A) { u64 rv; if (es_hook(4, 3, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][3],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_4(A) { u64 rv; if (es_hook(4, 4, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][4],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_5(A) { u64 rv; if (es_hook(4, 5, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][5],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_6(A) { u64 rv; if (es_hook(4, 6, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][6],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_7(A) { u64 rv; if (es_hook(4, 7, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][7],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_8(A) { u64 rv; if (es_hook(4, 8, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][8],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_9(A) { u64 rv; if (es_hook(4, 9, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][9],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_10(A) { u64 rv; if (es_hook(4, 10, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][10],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_11(A) { u64 rv; if (es_hook(4, 11, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][11],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_12(A) { u64 rv; if (es_hook(4, 12, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][12],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_13(A) { u64 rv; if (es_hook(4, 13, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][13],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_14(A) { u64 rv; if (es_hook(4, 14, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][14],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_15(A) { u64 rv; if (es_hook(4, 15, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][15],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_16(A) { u64 rv; if (es_hook(4, 16, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][16],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_17(A) { u64 rv; if (es_hook(4, 17, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][17],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_18(A) { u64 rv; if (es_hook(4, 18, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][18],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_19(A) { u64 rv; if (es_hook(4, 19, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][19],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_20(A) { u64 rv; if (es_hook(4, 20, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][20],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_21(A) { u64 rv; if (es_hook(4, 21, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][21],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_22(A) { u64 rv; if (es_hook(4, 22, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][22],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_23(A) { u64 rv; if (es_hook(4, 23, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][23],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_24(A) { u64 rv; if (es_hook(4, 24, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][24],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_25(A) { u64 rv; if (es_hook(4, 25, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][25],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_26(A) { u64 rv; if (es_hook(4, 26, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][26],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_27(A) { u64 rv; if (es_hook(4, 27, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][27],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_28(A) { u64 rv; if (es_hook(4, 28, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][28],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_29(A) { u64 rv; if (es_hook(4, 29, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][29],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_30(A) { u64 rv; if (es_hook(4, 30, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][30],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_31(A) { u64 rv; if (es_hook(4, 31, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][31],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_32(A) { u64 rv; if (es_hook(4, 32, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][32],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_33(A) { u64 rv; if (es_hook(4, 33, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][33],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_34(A) { u64 rv; if (es_hook(4, 34, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][34],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_35(A) { u64 rv; if (es_hook(4, 35, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][35],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_36(A) { u64 rv; if (es_hook(4, 36, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][36],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_37(A) { u64 rv; if (es_hook(4, 37, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][37],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_38(A) { u64 rv; if (es_hook(4, 38, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][38],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_39(A) { u64 rv; if (es_hook(4, 39, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][39],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_40(A) { u64 rv; if (es_hook(4, 40, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][40],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_41(A) { u64 rv; if (es_hook(4, 41, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][41],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_42(A) { u64 rv; if (es_hook(4, 42, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][42],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_43(A) { u64 rv; if (es_hook(4, 43, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][43],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_44(A) { u64 rv; if (es_hook(4, 44, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][44],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_45(A) { u64 rv; if (es_hook(4, 45, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][45],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_46(A) { u64 rv; if (es_hook(4, 46, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][46],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_47(A) { u64 rv; if (es_hook(4, 47, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][47],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_48(A) { u64 rv; if (es_hook(4, 48, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][48],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_49(A) { u64 rv; if (es_hook(4, 49, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][49],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_50(A) { u64 rv; if (es_hook(4, 50, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][50],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_51(A) { u64 rv; if (es_hook(4, 51, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][51],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_52(A) { u64 rv; if (es_hook(4, 52, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][52],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_53(A) { u64 rv; if (es_hook(4, 53, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][53],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_54(A) { u64 rv; if (es_hook(4, 54, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][54],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_55(A) { u64 rv; if (es_hook(4, 55, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][55],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_56(A) { u64 rv; if (es_hook(4, 56, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][56],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_57(A) { u64 rv; if (es_hook(4, 57, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][57],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_58(A) { u64 rv; if (es_hook(4, 58, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][58],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_59(A) { u64 rv; if (es_hook(4, 59, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][59],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_60(A) { u64 rv; if (es_hook(4, 60, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][60],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_61(A) { u64 rv; if (es_hook(4, 61, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][61],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_62(A) { u64 rv; if (es_hook(4, 62, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][62],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_4_63(A) { u64 rv; if (es_hook(4, 63, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[4][63],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_0(A) { u64 rv; if (es_hook(5, 0, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][0],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_1(A) { u64 rv; if (es_hook(5, 1, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][1],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_2(A) { u64 rv; if (es_hook(5, 2, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][2],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_3(A) { u64 rv; if (es_hook(5, 3, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][3],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_4(A) { u64 rv; if (es_hook(5, 4, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][4],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_5(A) { u64 rv; if (es_hook(5, 5, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][5],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_6(A) { u64 rv; if (es_hook(5, 6, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][6],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_7(A) { u64 rv; if (es_hook(5, 7, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][7],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_8(A) { u64 rv; if (es_hook(5, 8, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][8],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_9(A) { u64 rv; if (es_hook(5, 9, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][9],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_10(A) { u64 rv; if (es_hook(5, 10, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][10],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_11(A) { u64 rv; if (es_hook(5, 11, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][11],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_12(A) { u64 rv; if (es_hook(5, 12, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][12],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_13(A) { u64 rv; if (es_hook(5, 13, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][13],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_14(A) { u64 rv; if (es_hook(5, 14, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][14],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_15(A) { u64 rv; if (es_hook(5, 15, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][15],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_16(A) { u64 rv; if (es_hook(5, 16, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][16],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_17(A) { u64 rv; if (es_hook(5, 17, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][17],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_18(A) { u64 rv; if (es_hook(5, 18, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][18],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_19(A) { u64 rv; if (es_hook(5, 19, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][19],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_20(A) { u64 rv; if (es_hook(5, 20, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][20],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_21(A) { u64 rv; if (es_hook(5, 21, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][21],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_22(A) { u64 rv; if (es_hook(5, 22, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][22],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_23(A) { u64 rv; if (es_hook(5, 23, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][23],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_24(A) { u64 rv; if (es_hook(5, 24, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][24],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_25(A) { u64 rv; if (es_hook(5, 25, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][25],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_26(A) { u64 rv; if (es_hook(5, 26, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][26],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_27(A) { u64 rv; if (es_hook(5, 27, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][27],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_28(A) { u64 rv; if (es_hook(5, 28, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][28],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_29(A) { u64 rv; if (es_hook(5, 29, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][29],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_30(A) { u64 rv; if (es_hook(5, 30, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][30],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_31(A) { u64 rv; if (es_hook(5, 31, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][31],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_32(A) { u64 rv; if (es_hook(5, 32, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][32],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_33(A) { u64 rv; if (es_hook(5, 33, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][33],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_34(A) { u64 rv; if (es_hook(5, 34, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][34],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_35(A) { u64 rv; if (es_hook(5, 35, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][35],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_36(A) { u64 rv; if (es_hook(5, 36, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][36],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_37(A) { u64 rv; if (es_hook(5, 37, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][37],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_38(A) { u64 rv; if (es_hook(5, 38, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][38],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_39(A) { u64 rv; if (es_hook(5, 39, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][39],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_40(A) { u64 rv; if (es_hook(5, 40, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][40],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_41(A) { u64 rv; if (es_hook(5, 41, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][41],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_42(A) { u64 rv; if (es_hook(5, 42, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][42],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_43(A) { u64 rv; if (es_hook(5, 43, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][43],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_44(A) { u64 rv; if (es_hook(5, 44, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][44],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_45(A) { u64 rv; if (es_hook(5, 45, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][45],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_46(A) { u64 rv; if (es_hook(5, 46, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][46],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_47(A) { u64 rv; if (es_hook(5, 47, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][47],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_48(A) { u64 rv; if (es_hook(5, 48, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][48],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_49(A) { u64 rv; if (es_hook(5, 49, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][49],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_50(A) { u64 rv; if (es_hook(5, 50, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][50],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_51(A) { u64 rv; if (es_hook(5, 51, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][51],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_52(A) { u64 rv; if (es_hook(5, 52, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][52],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_53(A) { u64 rv; if (es_hook(5, 53, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][53],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_54(A) { u64 rv; if (es_hook(5, 54, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][54],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_55(A) { u64 rv; if (es_hook(5, 55, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][55],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_56(A) { u64 rv; if (es_hook(5, 56, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][56],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_57(A) { u64 rv; if (es_hook(5, 57, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][57],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_58(A) { u64 rv; if (es_hook(5, 58, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][58],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_59(A) { u64 rv; if (es_hook(5, 59, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][59],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_60(A) { u64 rv; if (es_hook(5, 60, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][60],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_61(A) { u64 rv; if (es_hook(5, 61, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][61],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_62(A) { u64 rv; if (es_hook(5, 62, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][62],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_5_63(A) { u64 rv; if (es_hook(5, 63, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[5][63],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_0(A) { u64 rv; if (es_hook(6, 0, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][0],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_1(A) { u64 rv; if (es_hook(6, 1, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][1],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_2(A) { u64 rv; if (es_hook(6, 2, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][2],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_3(A) { u64 rv; if (es_hook(6, 3, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][3],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_4(A) { u64 rv; if (es_hook(6, 4, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][4],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_5(A) { u64 rv; if (es_hook(6, 5, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][5],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_6(A) { u64 rv; if (es_hook(6, 6, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][6],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_7(A) { u64 rv; if (es_hook(6, 7, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][7],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_8(A) { u64 rv; if (es_hook(6, 8, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][8],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_9(A) { u64 rv; if (es_hook(6, 9, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][9],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_10(A) { u64 rv; if (es_hook(6, 10, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][10],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_11(A) { u64 rv; if (es_hook(6, 11, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][11],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_12(A) { u64 rv; if (es_hook(6, 12, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][12],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_13(A) { u64 rv; if (es_hook(6, 13, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][13],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_14(A) { u64 rv; if (es_hook(6, 14, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][14],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_15(A) { u64 rv; if (es_hook(6, 15, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][15],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_16(A) { u64 rv; if (es_hook(6, 16, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][16],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_17(A) { u64 rv; if (es_hook(6, 17, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][17],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_18(A) { u64 rv; if (es_hook(6, 18, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][18],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_19(A) { u64 rv; if (es_hook(6, 19, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][19],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_20(A) { u64 rv; if (es_hook(6, 20, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][20],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_21(A) { u64 rv; if (es_hook(6, 21, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][21],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_22(A) { u64 rv; if (es_hook(6, 22, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][22],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_23(A) { u64 rv; if (es_hook(6, 23, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][23],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_24(A) { u64 rv; if (es_hook(6, 24, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][24],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_25(A) { u64 rv; if (es_hook(6, 25, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][25],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_26(A) { u64 rv; if (es_hook(6, 26, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][26],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_27(A) { u64 rv; if (es_hook(6, 27, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][27],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_28(A) { u64 rv; if (es_hook(6, 28, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][28],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_29(A) { u64 rv; if (es_hook(6, 29, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][29],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_30(A) { u64 rv; if (es_hook(6, 30, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][30],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_31(A) { u64 rv; if (es_hook(6, 31, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][31],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_32(A) { u64 rv; if (es_hook(6, 32, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][32],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_33(A) { u64 rv; if (es_hook(6, 33, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][33],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_34(A) { u64 rv; if (es_hook(6, 34, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][34],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_35(A) { u64 rv; if (es_hook(6, 35, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][35],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_36(A) { u64 rv; if (es_hook(6, 36, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][36],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_37(A) { u64 rv; if (es_hook(6, 37, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][37],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_38(A) { u64 rv; if (es_hook(6, 38, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][38],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_39(A) { u64 rv; if (es_hook(6, 39, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][39],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_40(A) { u64 rv; if (es_hook(6, 40, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][40],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_41(A) { u64 rv; if (es_hook(6, 41, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][41],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_42(A) { u64 rv; if (es_hook(6, 42, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][42],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_43(A) { u64 rv; if (es_hook(6, 43, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][43],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_44(A) { u64 rv; if (es_hook(6, 44, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][44],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_45(A) { u64 rv; if (es_hook(6, 45, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][45],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_46(A) { u64 rv; if (es_hook(6, 46, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][46],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_47(A) { u64 rv; if (es_hook(6, 47, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][47],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_48(A) { u64 rv; if (es_hook(6, 48, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][48],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_49(A) { u64 rv; if (es_hook(6, 49, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][49],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_50(A) { u64 rv; if (es_hook(6, 50, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][50],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_51(A) { u64 rv; if (es_hook(6, 51, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][51],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_52(A) { u64 rv; if (es_hook(6, 52, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][52],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_53(A) { u64 rv; if (es_hook(6, 53, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][53],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_54(A) { u64 rv; if (es_hook(6, 54, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][54],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_55(A) { u64 rv; if (es_hook(6, 55, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][55],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_56(A) { u64 rv; if (es_hook(6, 56, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][56],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_57(A) { u64 rv; if (es_hook(6, 57, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][57],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_58(A) { u64 rv; if (es_hook(6, 58, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][58],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_59(A) { u64 rv; if (es_hook(6, 59, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][59],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_60(A) { u64 rv; if (es_hook(6, 60, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][60],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_61(A) { u64 rv; if (es_hook(6, 61, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][61],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_62(A) { u64 rv; if (es_hook(6, 62, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][62],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_6_63(A) { u64 rv; if (es_hook(6, 63, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[6][63],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_0(A) { u64 rv; if (es_hook(7, 0, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][0],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_1(A) { u64 rv; if (es_hook(7, 1, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][1],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_2(A) { u64 rv; if (es_hook(7, 2, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][2],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_3(A) { u64 rv; if (es_hook(7, 3, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][3],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_4(A) { u64 rv; if (es_hook(7, 4, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][4],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_5(A) { u64 rv; if (es_hook(7, 5, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][5],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_6(A) { u64 rv; if (es_hook(7, 6, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][6],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_7(A) { u64 rv; if (es_hook(7, 7, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][7],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_8(A) { u64 rv; if (es_hook(7, 8, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][8],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_9(A) { u64 rv; if (es_hook(7, 9, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][9],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_10(A) { u64 rv; if (es_hook(7, 10, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][10],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_11(A) { u64 rv; if (es_hook(7, 11, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][11],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_12(A) { u64 rv; if (es_hook(7, 12, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][12],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_13(A) { u64 rv; if (es_hook(7, 13, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][13],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_14(A) { u64 rv; if (es_hook(7, 14, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][14],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_15(A) { u64 rv; if (es_hook(7, 15, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][15],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_16(A) { u64 rv; if (es_hook(7, 16, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][16],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_17(A) { u64 rv; if (es_hook(7, 17, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][17],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_18(A) { u64 rv; if (es_hook(7, 18, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][18],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_19(A) { u64 rv; if (es_hook(7, 19, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][19],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_20(A) { u64 rv; if (es_hook(7, 20, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][20],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_21(A) { u64 rv; if (es_hook(7, 21, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][21],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_22(A) { u64 rv; if (es_hook(7, 22, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][22],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_23(A) { u64 rv; if (es_hook(7, 23, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][23],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_24(A) { u64 rv; if (es_hook(7, 24, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][24],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_25(A) { u64 rv; if (es_hook(7, 25, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][25],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_26(A) { u64 rv; if (es_hook(7, 26, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][26],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_27(A) { u64 rv; if (es_hook(7, 27, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][27],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_28(A) { u64 rv; if (es_hook(7, 28, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][28],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_29(A) { u64 rv; if (es_hook(7, 29, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][29],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_30(A) { u64 rv; if (es_hook(7, 30, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][30],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_31(A) { u64 rv; if (es_hook(7, 31, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][31],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_32(A) { u64 rv; if (es_hook(7, 32, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][32],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_33(A) { u64 rv; if (es_hook(7, 33, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][33],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_34(A) { u64 rv; if (es_hook(7, 34, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][34],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_35(A) { u64 rv; if (es_hook(7, 35, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][35],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_36(A) { u64 rv; if (es_hook(7, 36, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][36],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_37(A) { u64 rv; if (es_hook(7, 37, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][37],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_38(A) { u64 rv; if (es_hook(7, 38, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][38],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_39(A) { u64 rv; if (es_hook(7, 39, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][39],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_40(A) { u64 rv; if (es_hook(7, 40, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][40],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_41(A) { u64 rv; if (es_hook(7, 41, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][41],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_42(A) { u64 rv; if (es_hook(7, 42, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][42],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_43(A) { u64 rv; if (es_hook(7, 43, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][43],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_44(A) { u64 rv; if (es_hook(7, 44, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][44],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_45(A) { u64 rv; if (es_hook(7, 45, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][45],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_46(A) { u64 rv; if (es_hook(7, 46, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][46],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_47(A) { u64 rv; if (es_hook(7, 47, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][47],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_48(A) { u64 rv; if (es_hook(7, 48, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][48],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_49(A) { u64 rv; if (es_hook(7, 49, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][49],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_50(A) { u64 rv; if (es_hook(7, 50, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][50],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_51(A) { u64 rv; if (es_hook(7, 51, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][51],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_52(A) { u64 rv; if (es_hook(7, 52, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][52],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_53(A) { u64 rv; if (es_hook(7, 53, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][53],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_54(A) { u64 rv; if (es_hook(7, 54, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][54],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_55(A) { u64 rv; if (es_hook(7, 55, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][55],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_56(A) { u64 rv; if (es_hook(7, 56, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][56],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_57(A) { u64 rv; if (es_hook(7, 57, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][57],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_58(A) { u64 rv; if (es_hook(7, 58, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][58],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_59(A) { u64 rv; if (es_hook(7, 59, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][59],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_60(A) { u64 rv; if (es_hook(7, 60, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][60],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_61(A) { u64 rv; if (es_hook(7, 61, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][61],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_62(A) { u64 rv; if (es_hook(7, 62, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][62],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 es_7_63(A) { u64 rv; if (es_hook(7, 63, a0, a1, a2, &rv)) return rv; struct call c = { { g_real[7][63],a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10 } }; return fwd64(%PTR%u, &c); }
static u64 (*const g_stubs[MAXT][MAXE])(A) = {
    {es_0_0,es_0_1,es_0_2,es_0_3,es_0_4,es_0_5,es_0_6,es_0_7,es_0_8,es_0_9,es_0_10,es_0_11,es_0_12,es_0_13,es_0_14,es_0_15,es_0_16,es_0_17,es_0_18,es_0_19,es_0_20,es_0_21,es_0_22,es_0_23,es_0_24,es_0_25,es_0_26,es_0_27,es_0_28,es_0_29,es_0_30,es_0_31,es_0_32,es_0_33,es_0_34,es_0_35,es_0_36,es_0_37,es_0_38,es_0_39,es_0_40,es_0_41,es_0_42,es_0_43,es_0_44,es_0_45,es_0_46,es_0_47,es_0_48,es_0_49,es_0_50,es_0_51,es_0_52,es_0_53,es_0_54,es_0_55,es_0_56,es_0_57,es_0_58,es_0_59,es_0_60,es_0_61,es_0_62,es_0_63},
    {es_1_0,es_1_1,es_1_2,es_1_3,es_1_4,es_1_5,es_1_6,es_1_7,es_1_8,es_1_9,es_1_10,es_1_11,es_1_12,es_1_13,es_1_14,es_1_15,es_1_16,es_1_17,es_1_18,es_1_19,es_1_20,es_1_21,es_1_22,es_1_23,es_1_24,es_1_25,es_1_26,es_1_27,es_1_28,es_1_29,es_1_30,es_1_31,es_1_32,es_1_33,es_1_34,es_1_35,es_1_36,es_1_37,es_1_38,es_1_39,es_1_40,es_1_41,es_1_42,es_1_43,es_1_44,es_1_45,es_1_46,es_1_47,es_1_48,es_1_49,es_1_50,es_1_51,es_1_52,es_1_53,es_1_54,es_1_55,es_1_56,es_1_57,es_1_58,es_1_59,es_1_60,es_1_61,es_1_62,es_1_63},
    {es_2_0,es_2_1,es_2_2,es_2_3,es_2_4,es_2_5,es_2_6,es_2_7,es_2_8,es_2_9,es_2_10,es_2_11,es_2_12,es_2_13,es_2_14,es_2_15,es_2_16,es_2_17,es_2_18,es_2_19,es_2_20,es_2_21,es_2_22,es_2_23,es_2_24,es_2_25,es_2_26,es_2_27,es_2_28,es_2_29,es_2_30,es_2_31,es_2_32,es_2_33,es_2_34,es_2_35,es_2_36,es_2_37,es_2_38,es_2_39,es_2_40,es_2_41,es_2_42,es_2_43,es_2_44,es_2_45,es_2_46,es_2_47,es_2_48,es_2_49,es_2_50,es_2_51,es_2_52,es_2_53,es_2_54,es_2_55,es_2_56,es_2_57,es_2_58,es_2_59,es_2_60,es_2_61,es_2_62,es_2_63},
    {es_3_0,es_3_1,es_3_2,es_3_3,es_3_4,es_3_5,es_3_6,es_3_7,es_3_8,es_3_9,es_3_10,es_3_11,es_3_12,es_3_13,es_3_14,es_3_15,es_3_16,es_3_17,es_3_18,es_3_19,es_3_20,es_3_21,es_3_22,es_3_23,es_3_24,es_3_25,es_3_26,es_3_27,es_3_28,es_3_29,es_3_30,es_3_31,es_3_32,es_3_33,es_3_34,es_3_35,es_3_36,es_3_37,es_3_38,es_3_39,es_3_40,es_3_41,es_3_42,es_3_43,es_3_44,es_3_45,es_3_46,es_3_47,es_3_48,es_3_49,es_3_50,es_3_51,es_3_52,es_3_53,es_3_54,es_3_55,es_3_56,es_3_57,es_3_58,es_3_59,es_3_60,es_3_61,es_3_62,es_3_63},
    {es_4_0,es_4_1,es_4_2,es_4_3,es_4_4,es_4_5,es_4_6,es_4_7,es_4_8,es_4_9,es_4_10,es_4_11,es_4_12,es_4_13,es_4_14,es_4_15,es_4_16,es_4_17,es_4_18,es_4_19,es_4_20,es_4_21,es_4_22,es_4_23,es_4_24,es_4_25,es_4_26,es_4_27,es_4_28,es_4_29,es_4_30,es_4_31,es_4_32,es_4_33,es_4_34,es_4_35,es_4_36,es_4_37,es_4_38,es_4_39,es_4_40,es_4_41,es_4_42,es_4_43,es_4_44,es_4_45,es_4_46,es_4_47,es_4_48,es_4_49,es_4_50,es_4_51,es_4_52,es_4_53,es_4_54,es_4_55,es_4_56,es_4_57,es_4_58,es_4_59,es_4_60,es_4_61,es_4_62,es_4_63},
    {es_5_0,es_5_1,es_5_2,es_5_3,es_5_4,es_5_5,es_5_6,es_5_7,es_5_8,es_5_9,es_5_10,es_5_11,es_5_12,es_5_13,es_5_14,es_5_15,es_5_16,es_5_17,es_5_18,es_5_19,es_5_20,es_5_21,es_5_22,es_5_23,es_5_24,es_5_25,es_5_26,es_5_27,es_5_28,es_5_29,es_5_30,es_5_31,es_5_32,es_5_33,es_5_34,es_5_35,es_5_36,es_5_37,es_5_38,es_5_39,es_5_40,es_5_41,es_5_42,es_5_43,es_5_44,es_5_45,es_5_46,es_5_47,es_5_48,es_5_49,es_5_50,es_5_51,es_5_52,es_5_53,es_5_54,es_5_55,es_5_56,es_5_57,es_5_58,es_5_59,es_5_60,es_5_61,es_5_62,es_5_63},
    {es_6_0,es_6_1,es_6_2,es_6_3,es_6_4,es_6_5,es_6_6,es_6_7,es_6_8,es_6_9,es_6_10,es_6_11,es_6_12,es_6_13,es_6_14,es_6_15,es_6_16,es_6_17,es_6_18,es_6_19,es_6_20,es_6_21,es_6_22,es_6_23,es_6_24,es_6_25,es_6_26,es_6_27,es_6_28,es_6_29,es_6_30,es_6_31,es_6_32,es_6_33,es_6_34,es_6_35,es_6_36,es_6_37,es_6_38,es_6_39,es_6_40,es_6_41,es_6_42,es_6_43,es_6_44,es_6_45,es_6_46,es_6_47,es_6_48,es_6_49,es_6_50,es_6_51,es_6_52,es_6_53,es_6_54,es_6_55,es_6_56,es_6_57,es_6_58,es_6_59,es_6_60,es_6_61,es_6_62,es_6_63},
    {es_7_0,es_7_1,es_7_2,es_7_3,es_7_4,es_7_5,es_7_6,es_7_7,es_7_8,es_7_9,es_7_10,es_7_11,es_7_12,es_7_13,es_7_14,es_7_15,es_7_16,es_7_17,es_7_18,es_7_19,es_7_20,es_7_21,es_7_22,es_7_23,es_7_24,es_7_25,es_7_26,es_7_27,es_7_28,es_7_29,es_7_30,es_7_31,es_7_32,es_7_33,es_7_34,es_7_35,es_7_36,es_7_37,es_7_38,es_7_39,es_7_40,es_7_41,es_7_42,es_7_43,es_7_44,es_7_45,es_7_46,es_7_47,es_7_48,es_7_49,es_7_50,es_7_51,es_7_52,es_7_53,es_7_54,es_7_55,es_7_56,es_7_57,es_7_58,es_7_59,es_7_60,es_7_61,es_7_62,es_7_63},
};
int cuGetExportTable(const void **t, const void *uuid) {
    char b[200]; const unsigned char *u = (const unsigned char *)uuid;
    wsprintfA(b, "nvcudashim: cuGetExportTable uuid=%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x\\n",
        u[0],u[1],u[2],u[3],u[4],u[5],u[6],u[7],u[8],u[9],u[10],u[11],u[12],u[13],u[14],u[15]);
    OutputDebugStringA(b);
    for (int i = 0; i < g_nt; i++) if (!memcmp(g_uuid[i], uuid, 16)) { *t = g_fake[i]; return 0; }
    if (g_nt >= MAXT) return 801;
    const u64 *real = NULL;
    struct call c = {{ (u64)&real, (u64)uuid }};
    int r = fwd(%GETEXP%, &c);
    if (r || !real) return r ? r : 801;
    unsigned size = *(const unsigned *)real;
    /* Most tables start with a byte-size header; a few (e.g. c693336e) are bare function-pointer arrays. */
    int hdr = (size >= 16 && size <= 1024 && (size % 8) == 0);
    unsigned n = hdr ? size / 8 - 1 : 32; if (n > MAXE) n = MAXE;
    int ti = g_nt++; memcpy(g_uuid[ti], uuid, 16);
    if (hdr) g_fake[ti][0] = real[0];
    for (unsigned k = 0; k < n; k++) { u64 rv = real[k + hdr]; g_real[ti][k] = rv; g_fake[ti][k + hdr] = rv ? (u64)g_stubs[ti][k] : 0; }
    wsprintfA(b, "nvcudashim:   export table size=%u entries=%u real=%08x%08x fake=%08x%08x\\n", size, n, (unsigned)((u64)real>>32),(unsigned)(u64)real,(unsigned)((u64)g_fake[ti]>>32),(unsigned)(u64)g_fake[ti]); OutputDebugStringA(b);
    *t = g_fake[ti]; return 0;
}

/* LUID is a Windows-only CUDA notion (libcuda returns NOT_SUPPORTED). The
   callers use it to match the CUDA device to a DXGI adapter, so answer with
   the LUID of the first NVIDIA adapter as DXGI (DXVK) reports it. */
#define COBJMACROS
#include <dxgi.h>
int cuDeviceGetLuid(char *luid, unsigned *mask, int dev) {
    typedef HRESULT (WINAPI *CF)(REFIID, void **);
    HMODULE m = LoadLibraryA("dxgi.dll"); if (!m) return 100;
    CF cf = (CF)GetProcAddress(m, "CreateDXGIFactory1"); if (!cf) return 100;
    IDXGIFactory1 *fac = NULL; if (FAILED(cf(&IID_IDXGIFactory1, (void **)&fac))) return 100;
    int rc = 100; IDXGIAdapter1 *ad = NULL;
    for (UINT i = 0; IDXGIFactory1_EnumAdapters1(fac, i, &ad) == S_OK; i++) {
        DXGI_ADAPTER_DESC1 d; IDXGIAdapter1_GetDesc1(ad, &d);
        IDXGIAdapter1_Release(ad);
        if (d.VendorId == 0x10DE) { memcpy(luid, &d.AdapterLuid, 8); if (mask) *mask = 1; rc = 0; break; }
    }
    IDXGIFactory1_Release(fac); return rc;
}
BOOL WINAPI DllMain(HINSTANCE h, DWORD r, LPVOID x) { return TRUE; }
''')
with open("nvcuda.def", "w") as f:
    f.write("LIBRARY nvcudashim\nEXPORTS\n")
    for n in names + sorted(SPECIAL):
        f.write(f"  {n}\n")
with open("unix_side.c", "w") as f:
    f.write('''#define _GNU_SOURCE
#include <dlfcn.h>
#include <string.h>
#include <stdlib.h>
#include <sys/uio.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
typedef unsigned long long u64;
struct call { u64 a[12]; u64 ret; };
struct msg { struct call c; unsigned id; };
static const char *const names[] = {
''')
    for n in names + ["cuGetExportTable"]:
        f.write(f'    "{n}",\n')
    f.write('''};
#define N (sizeof(names)/sizeof(names[0]))
static void *fn[N]; static int inited;
static void init(void) {
    void *lib = dlopen("libcuda.so.1", RTLD_NOW | RTLD_GLOBAL);
    if (lib && getenv("NVCUDA_TRACE")) { void *sym = dlsym(lib, "cuInit"); fprintf(stderr, "cu: cuInit at %p\\n", sym); }
    if (!lib) { fprintf(stderr, "nvcuda shim: dlopen libcuda.so.1 failed: %s\\n", dlerror()); inited = 1; return; }
    for (unsigned i = 0; i < N; i++) fn[i] = dlsym(lib, names[i]);
    inited = 1;
}

#include <link.h>
#include <elf.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
struct elfq { const char *suffix; const char *name; void *result; };
static int elfq_cb(struct dl_phdr_info *info, size_t sz, void *data) {
    struct elfq *q = data; const char *path = info->dlpi_name; size_t pl = path ? strlen(path) : 0, sl = strlen(q->suffix);
    if (pl < sl || strcmp(path + pl - sl, q->suffix)) return 0;
    int fd = open(path, O_RDONLY); if (fd < 0) return 0;
    struct stat sb; if (fstat(fd, &sb)) { close(fd); return 0; }
    unsigned char *map = mmap(NULL, sb.st_size, PROT_READ, MAP_PRIVATE, fd, 0); close(fd);
    if (map == MAP_FAILED) return 0;
    Elf64_Ehdr *eh = (Elf64_Ehdr *)map; Elf64_Shdr *sh = (Elf64_Shdr *)(map + eh->e_shoff);
    for (int i = 0; i < eh->e_shnum; i++) {
        if (sh[i].sh_type != SHT_SYMTAB) continue;
        Elf64_Sym *sym = (Elf64_Sym *)(map + sh[i].sh_offset); size_t n = sh[i].sh_size / sizeof(Elf64_Sym);
        const char *str = (const char *)(map + sh[sh[i].sh_link].sh_offset);
        for (size_t k = 0; k < n; k++) if (sym[k].st_shndx && !strcmp(str + sym[k].st_name, q->name)) { q->result = (void *)(info->dlpi_addr + sym[k].st_value); break; }
    }
    munmap(map, sb.st_size);
    return q->result ? 1 : 0;
}
static void *elf_sym(const char *suffix, const char *name) { struct elfq q = { suffix, name, NULL }; dl_iterate_phdr(elfq_cb, &q); return q.result; }
typedef int (*F)(u64,u64,u64,u64,u64,u64,u64,u64,u64,u64,u64,u64);
static long call(void *args) {
    struct msg *m = args;
    if (!inited) init();
    if (m->id == 0xFFFFFFFDu) {   /* per-device integrity-check chunks straight out of libcuda's memory (driver-build specific) */
        unsigned char *o = (unsigned char *)m->c.a[0]; unsigned char *base = (unsigned char *)((unsigned long long)dlsym(RTLD_DEFAULT, "cuInit") - 0x3739c0ull);
        unsigned nd = *(unsigned *)(base + 0x6b46f50); if (nd > 8) nd = 8; memcpy(o, &nd, 4);
        for (unsigned i = 0; i < nd; i++) {
            unsigned char *dv = (unsigned char *)*(unsigned long long *)(base + 0x6b46d50 + i * 8); unsigned mode = *(unsigned *)(dv + 0xa66c);
            memcpy(o + 4 + i * 28, dv + ((mode - 2 <= 1) ? 0xc37 : 0xc06), 16); memcpy(o + 4 + i * 28 + 16, dv + 0xc88, 4); memcpy(o + 4 + i * 28 + 20, dv + 0xc80, 8);
        }
        m->c.ret = 0; return 0;
    }
    if (m->id == 0xFFFFFFFEu) {   /* raw function-pointer call (export-table entries) */
        typedef u64 (*P)(u64,u64,u64,u64,u64,u64,u64,u64,u64,u64,u64,u64);
        struct call *pc = &m->c;
        if (getenv("NVCUDA_FAKE_HS") && pc->a[1] >= 0x2f26 && pc->a[1] <= 0x2f28) { memset((void*)pc->a[3], atoi(getenv("NVCUDA_FAKE_HS")) - 1, 16); pc->ret = 0; return 0; }
        { static int tr=-1; if(tr<0) tr=getenv("NVCUDA_TRACE")!=0; if(tr) fprintf(stderr,"cu: RAW %p(%llx,%llx,%llx)\\n",(void*)pc->a[0],pc->a[1],pc->a[2],pc->a[3]); }
        pc->ret = ((P)pc->a[0])(pc->a[1],pc->a[2],pc->a[3],pc->a[4],pc->a[5],pc->a[6],pc->a[7],pc->a[8],pc->a[9],pc->a[10],pc->a[11],0);
        { static int tr2=-1; if(tr2<0) tr2=getenv("NVCUDA_TRACE")!=0; if(tr2) { fprintf(stderr,"cu:   RAW ret=%llx\\n",pc->ret); for (int ai = 1; ai <= 3; ai++) if (pc->a[ai] > 0x10000 && pc->a[ai] < 0x7fffffff) { unsigned q[4]; struct iovec lv = { q, 16 }, rv = { (void*)pc->a[ai], 16 }; if (process_vm_readv(getpid(), &lv, 1, &rv, 1, 0) == 16) fprintf(stderr,"cu:     arg%d-> %08x %08x %08x %08x\\n",ai,q[0],q[1],q[2],q[3]); } } }
        if (getenv("NVCUDA_ZAPPTR") && pc->ret > 0xFFFFFFFFull) pc->ret = 0;
        return 0;
    }
    if (m->id >= N || !fn[m->id]) { if (getenv("NVCUDA_TRACE")) fprintf(stderr, "cu: MISSING %s -> 801\\n", m->id < N ? names[m->id] : "?"); m->c.ret = 801; return 0; }  /* CUDA_ERROR_NOT_SUPPORTED */
    struct call *c = &m->c;
    /* D3D12/Win32 external memory (the DLSS-G bridge shares a D3D12 heap with CUDA): libcuda on Linux only takes opaque
       fds, so turn the Wine NT handle back into the unix fd behind it (vkd3d-proton -> winevulkan gave it out from an
       opaque fd) and import that as CU_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD. */
    static int imp_id = -2;
    if (imp_id == -2) { imp_id = -1; for (unsigned i = 0; i < N; i++) if (names[i] && !strcmp(names[i], "cuImportExternalMemory")) imp_id = (int)i; }
    struct { int type; int pad; union { int fd; struct { void *handle; const void *name; } w; } h; unsigned long long size; unsigned flags; unsigned res[16]; } xdesc;
    if ((int)m->id == imp_id && c->a[1]) {
        memcpy(&xdesc, (void *)c->a[1], sizeof xdesc);
        if (xdesc.type >= 2 && xdesc.type <= 7) {   /* WIN32, WIN32_KMT, D3D12_HEAP, D3D12_RESOURCE, D3D11_RESOURCE(_KMT) */
            /* An NT "shared resource" handle is not an fd object: win32u opens it as a D3DKMT resource first, and only that
               local object can be turned into an fd. Both helpers are non-exported (but not stripped) symbols of win32u.so,
               found through its ELF symbol table so this survives Proton rebuilds. */
            static void *(*open_res)(unsigned, void *, unsigned *, unsigned *); static int (*get_fd)(unsigned); static int looked;
            if (!looked) { looked = 1; open_res = (void *(*)(unsigned, void *, unsigned *, unsigned *))elf_sym("win32u.so", "d3dkmt_open_resource");
                get_fd = (int (*)(unsigned))elf_sym("win32u.so", "d3dkmt_object_get_fd"); }
            int fd = -1, st = -1;
            if (open_res && get_fd) {
                unsigned mu = 0, sy = 0; int kmt = (xdesc.type == 3 || xdesc.type == 7);
                unsigned local = (unsigned)(unsigned long long)open_res(kmt ? (unsigned)(unsigned long long)xdesc.h.w.handle : 0, kmt ? NULL : xdesc.h.w.handle, &mu, &sy);
                if (local) { fd = get_fd(local); st = fd < 0 ? -2 : 0; }
            }
            if (getenv("NVCUDA_TRACE")) fprintf(stderr, "cu: import external memory type=%d handle=%p size=%llu -> fd=%d (open_res=%p get_fd=%p)\\n", xdesc.type, xdesc.h.w.handle, xdesc.size, fd, (void *)open_res, (void *)get_fd);
            if (st != 0 || fd < 0) { m->c.ret = 801; return 0; }
            xdesc.type = 1; xdesc.h.fd = fd;
            c->a[1] = (unsigned long long)&xdesc;
        }
    }
    static int trace = -1; if (trace < 0) trace = getenv("NVCUDA_TRACE") != 0;
    m->c.ret = (unsigned)((F)fn[m->id])(c->a[0],c->a[1],c->a[2],c->a[3],c->a[4],c->a[5],c->a[6],c->a[7],c->a[8],c->a[9],c->a[10],c->a[11]);
    if (trace) fprintf(stderr, "cu: %s(%llx,%llx,%llx,%llx) -> %d\\n", names[m->id], c->a[0], c->a[1], c->a[2], c->a[3], m->c.ret);
    return 0;
}
typedef long (*entry_t)(void *);
__attribute__((visibility("default"))) const entry_t __wine_unix_call_funcs[] = { call };
__attribute__((visibility("default"))) const entry_t __wine_unix_call_wow64_funcs[] = { call };
''')

with open("nvcuda_fwd.def", "w") as f:
    f.write("LIBRARY nvcuda\nEXPORTS\n")
    for n in names + sorted(SPECIAL):
        f.write(f"  {n} = nvcudashim.{n}\n")

t = open("pe_stubs.c").read().replace("%PTR%", "4294967294").replace("%GETEXP%", str(GETEXP_ID))
open("pe_stubs.c", "w").write(t)
