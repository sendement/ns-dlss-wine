// SPDX-License-Identifier: MIT
// Tiny Wine-side bridge: creates two FILE-BACKED named sections so a native
// Linux process (which cannot speak wineserver's own named-object protocol
// at all) can still satisfy the DLSS worker's OpenFileMappingA(name) calls.
//
// The trick: CreateFileMappingA(hFile, ..., name) does two things at once -
// it registers `name` in wineserver's object namespace (which the worker's
// OpenFileMappingA looks up, and which only Wine processes under the SAME
// WINEPREFIX can see), AND it backs that section with a real file under
// Z:\tmp\... (== /tmp/... on the Linux side, Wine's standard Z: passthrough).
// A native Linux process can mmap() that same file directly by path - no
// wineserver protocol needed for the actual pixel bytes, only the bridge
// needs to be a real Wine process for the *name* to resolve.
//
// Usage: shm_bridge.exe <in_path> <in_bytes> <in_name> <out_path> <out_bytes> <out_name>
// Creates both sections, prints "READY" to stdout, then idles until stdin closes.
#include <windows.h>
#include <cstdio>
#include <cstdlib>
#include <string>

static HANDLE MakeSection(const char *path, unsigned long long bytes, const char *name)
{
    HANDLE hFile = CreateFileA(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                               nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (hFile == INVALID_HANDLE_VALUE)
    {
        fprintf(stderr, "CreateFileA(%s) failed %lu\n", path, GetLastError());
        return nullptr;
    }
    LARGE_INTEGER size;
    size.QuadPart = (LONGLONG)bytes;
    if (!SetFilePointerEx(hFile, size, nullptr, FILE_BEGIN) || !SetEndOfFile(hFile))
    {
        fprintf(stderr, "sizing %s to %llu failed %lu\n", path, bytes, GetLastError());
        CloseHandle(hFile);
        return nullptr;
    }
    HANDLE hMap = CreateFileMappingA(hFile, nullptr, PAGE_READWRITE,
                                     (DWORD)(bytes >> 32), (DWORD)(bytes & 0xFFFFFFFFu), name);
    if (hMap == nullptr)
    {
        fprintf(stderr, "CreateFileMappingA(%s) failed %lu\n", name, GetLastError());
        CloseHandle(hFile);
        return nullptr;
    }
    // hFile can close now - the mapping keeps the file backing alive.
    CloseHandle(hFile);
    fprintf(stderr, "section '%s' <- %s (%llu bytes) ready\n", name, path, bytes);
    return hMap;
}

int main(int argc, char **argv)
{
    if (argc != 7)
    {
        fprintf(stderr, "usage: shm_bridge <in_path> <in_bytes> <in_name> "
                        "<out_path> <out_bytes> <out_name>\n");
        return 1;
    }
    HANDLE inMap = MakeSection(argv[1], strtoull(argv[2], nullptr, 10), argv[3]);
    HANDLE outMap = MakeSection(argv[4], strtoull(argv[5], nullptr, 10), argv[6]);
    if (!inMap || !outMap)
        return 1;

    printf("READY\n");
    fflush(stdout);

    // Idle until a quit-file appears next to the input section's backing
    // file. NOT stdin: a backgrounded/harness-managed process's stdin is
    // commonly already at EOF (no real console, no held-open pipe), which
    // made fgets() return immediately and the process exit right after
    // printing READY - the actual cause of every "OpenFileMapping failed,
    // err=2" seen testing this bridge, not a wineserver/session issue.
    const std::string quitPath = std::string(argv[1]) + ".quit";
    for (;;)
    {
        DWORD attrs = GetFileAttributesA(quitPath.c_str());
        if (attrs != INVALID_FILE_ATTRIBUTES) break;
        Sleep(100);
    }
    CloseHandle(inMap);
    CloseHandle(outMap);
    return 0;
}
