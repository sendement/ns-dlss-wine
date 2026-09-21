# SPDX-License-Identifier: MIT
"""Files this project cannot ship (proprietary or third-party binaries) and build outputs, per feature - checked and installed before a feature is used.

The user puts the files anywhere inside `user_files/` (matched by file name, subfolders are fine); build outputs of `tools/build_all.sh` land in
`runtime/artifacts/`. `ensure(feature)` copies whatever is found into the feature's runtime folder and returns what is still missing, with a hint on where it
comes from. Nothing is ever downloaded automatically.

    python3 app/userfiles.py check            # every feature
    python3 app/userfiles.py install dlssg    # install one feature's files, then report
"""
import os
import shutil
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402


@dataclass(frozen=True)
class Item:
    name: str              # file name (matched case-insensitively anywhere in user_files/ or runtime/artifacts/)
    origin: str            # where a user gets it / how it is built (project names only)
    build: bool = False    # True = produced by tools/build_all.sh (looked up in runtime/artifacts/ only)


@dataclass(frozen=True)
class Feature:
    key: str
    title: str
    dest: str                                  # runtime folder the files are installed into
    items: tuple = field(default_factory=tuple)
    needs: tuple = ()                          # other features that must be present as well (e.g. the Wine runtime)


NVCUDA_SHIM = Item("nvcudashim.dll", "built from shim/ by tools/build_all.sh", build=True)
D3D12 = (
    Item("d3d12.dll", "patched vkd3d-proton (patches/) - tools/build_patched_wine_libs.sh", build=True),
    Item("d3d12core.dll", "patched vkd3d-proton (patches/) - tools/build_patched_wine_libs.sh", build=True),
    Item("d3dcompiler_47.dll", "Microsoft's real d3dcompiler_47.dll (e.g. `winetricks d3dcompiler_47` downloads it into a prefix; copy it from there)"),
)

FEATURES = {f.key: f for f in (
    Feature("dlss5", "DLSS5 neural rendering worker (core)", paths.WORKER_DLSS5, (
        Item("nvngx.dll", "NeuralScreen (perseval-BLR): native/nvngx.dll - the worker host"),
        Item("nvngx_dlssnr.dll", "NeuralScreen (perseval-BLR): native/nvngx_dlssnr.dll"),
        Item("Spout.dll", "NeuralScreen (perseval-BLR): native/Spout.dll"),
        Item("SpoutDX.dll", "NeuralScreen (perseval-BLR): native/SpoutDX.dll"),
    ), needs=("wine",)),
    Feature("vsr", "RTX Video Super Resolution upscaler", paths.WORKER_VSR, (
        Item("nvngx_vsr.dll", "Visual Enhancer (Merserk): bin/runtime/rtx_video/"),
        Item("nvngx_truehdr.dll", "Visual Enhancer (Merserk): bin/runtime/rtx_video/"),
        Item("neuroframe_engine_upscaling.dll", "Visual Enhancer (Merserk): the upscaling bridge"),
        Item("vsr_host.exe", "built from hosts/vsr_host.cpp by tools/build_all.sh", build=True),
    ), needs=("wine", "nvcuda")),
    Feature("dlssg", "NVIDIA DLSS frame generation", paths.WORKER_DLSSG, (
        Item("nvngx_dlssg.dll", "NVIDIA/DLSS (GitHub): lib/Windows_x86_64/rel/nvngx_dlssg.dll (or Visual Enhancer: bin/runtime/dlssg/)"),
        Item("nvofapi64.dll", "patched dxvk-nvapi (patches/) - tools/build_patched_wine_libs.sh", build=True),
        Item("ngxdlssg_host.exe", "built from hosts/ngxdlssg_host.cpp by tools/build_all.sh (needs third_party/nvidia-dlss headers)", build=True),
    ) + D3D12, needs=("wine", "nvcuda")),
    Feature("dlssg-vk", "NVIDIA DLSS frame generation, native Linux (Vulkan, no Wine)", paths.WORKER_DLSSG_VK, (
        Item("libnvidia-ngx-dlssg.so.310.9.1", "NVIDIA/DLSS (GitHub): lib/Linux_x86_64/rel/ (staged from third_party/ by tools/build_all.sh after tools/fetch_third_party.sh)"),
        Item("ngxdlssg_vk_host", "built from hosts/ngxdlssg_vk_host.cpp by tools/build_all.sh (needs third_party/nvidia-dlss and the Vulkan headers)", build=True),
    )),
    Feature("fsr3", "AMD FSR 3.1 frame generation", paths.WORKER_FSR3, (
        Item("amd_fidelityfx_framegeneration_dx12.dll", "AMD FidelityFX SDK 2.3.0 (GPUOpen-LibrariesAndSDKs/FidelityFX-SDK): Kits/FidelityFX/signedbin/"),
        Item("fsr3fg_host.exe", "built from hosts/fsr3fg_host.cpp by tools/build_all.sh", build=True),
    ) + D3D12, needs=("wine",)),
)}


def _index(root: str) -> dict:
    out = {}
    for base, _dirs, files in os.walk(root):
        for f in files:
            out.setdefault(f.lower(), os.path.join(base, f))
    return out


def wine_status() -> list:
    """Problems with the Wine runtime (empty = fine)."""
    out = []
    if not os.path.isfile(paths.wine_binary()):
        out.append(f"Wine binary not found ({paths.wine_binary()}): install a Proton build (Proton - Experimental) from Steam or set NS_WINE")
    if not os.path.isdir(os.path.join(paths.PREFIX, "drive_c")):
        out.append(f"no Wine prefix at {paths.PREFIX}: run tools/setup_prefix.sh (copies an already-initialised Proton prefix; nothing of yours is modified)")
    return out


def nvcuda_status(install: bool = True) -> list:
    """The nvcuda shim (built by tools/build_all.sh) must be in WINEDLLPATH and installed into the prefix: Proton's builtin nvcuda.dll is replaced by our
    forwarder (a backup of the original is kept once) and the shim's PE half sits next to it."""
    shim = os.path.join(paths.WINE_NVCUDA, "x86_64-windows", "nvcudashim.dll")
    fwd = os.path.join(paths.WINE_NVCUDA, "nvcuda_forwarder.dll")
    if not (os.path.isfile(shim) and os.path.isfile(fwd)):
        return [f"the nvcuda shim is not built ({shim}): run tools/build_all.sh"]
    sys32 = os.path.join(paths.PREFIX, "drive_c", "windows", "system32")
    if not os.path.isdir(sys32):
        return []                                    # no prefix yet: wine_status() reports it
    def same(a, b):
        try:
            return os.path.getsize(a) == os.path.getsize(b) and open(a, "rb").read() == open(b, "rb").read()
        except OSError:
            return False
    target = os.path.join(sys32, "nvcuda.dll")
    if not same(target, fwd):
        if not install:
            return [f"the prefix still has Proton's nvcuda.dll (run: python3 app/userfiles.py install)"]
        backup = target + ".proton-orig"
        if os.path.isfile(target) and not os.path.islink(target) and not os.path.exists(backup):
            shutil.copy2(target, backup)
        if os.path.lexists(target):
            os.remove(target)
        shutil.copy2(fwd, target)
    if not same(os.path.join(sys32, "nvcudashim.dll"), shim):
        if not install:
            return ["the nvcuda shim is not installed in the prefix (run: python3 app/userfiles.py install)"]
        shutil.copy2(shim, os.path.join(sys32, "nvcudashim.dll"))
    return []


VSR_NATIVE_PACKAGES = ("nvidia-vfx", "cupy-cuda12x", "nvidia-cuda-runtime-cu12", "nvidia-cuda-nvrtc-cu12", "numpy")


def vsr_native_status() -> list:
    """Problems with the native RTX VSR environment (empty = ready): a venv with NVIDIA's `nvidia-vfx` (proprietary, pip from NVIDIA's own index) and cupy."""
    if not os.path.isfile(paths.VENV_VSR_PYTHON):
        return [f"no venv at {paths.VENV_VSR}: run `python3 app/userfiles.py install vsr-native` (pip-installs NVIDIA's nvidia-vfx from pypi.nvidia.com + cupy there)"]
    import subprocess
    r = subprocess.run([paths.VENV_VSR_PYTHON, "-c", "import cupy, nvvfx"], capture_output=True, text=True)
    return [] if r.returncode == 0 else [f"the venv {paths.VENV_VSR} cannot import nvvfx/cupy ({r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'unknown error'}): re-run `python3 app/userfiles.py install vsr-native`"]


def install_vsr_native() -> list:
    """Explicit user action (never called implicitly): create runtime/venv-vsr and pip-install NVIDIA's nvidia-vfx (own license, not redistributed by this project) + cupy."""
    import subprocess
    if not os.path.isfile(paths.VENV_VSR_PYTHON):
        subprocess.run([sys.executable, "-m", "venv", paths.VENV_VSR], check=True)
    subprocess.run([paths.VENV_VSR_PYTHON, "-m", "pip", "install", "--extra-index-url", "https://pypi.nvidia.com", *VSR_NATIVE_PACKAGES], check=True)
    return vsr_native_status()


def require_vsr_native():
    problems = vsr_native_status()
    if problems:
        raise RuntimeError("Native RTX VSR is not ready:\n  - " + "\n  - ".join(problems))


def ensure(feature: str, install: bool = True) -> list:
    """Install what can be installed for `feature` and return a list of human-readable problems (empty list = ready)."""
    f = FEATURES[feature]
    problems = []
    for dep in f.needs:
        problems += wine_status() if dep == "wine" else nvcuda_status(install) if dep == "nvcuda" else ensure(dep, install)
    user, arts = _index(paths.USER_FILES), _index(paths.ARTIFACTS)
    for it in f.items:
        dest = os.path.join(f.dest, it.name)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            continue
        src = (arts if it.build else {**arts, **user}).get(it.name.lower())
        if src and install:
            os.makedirs(f.dest, exist_ok=True)
            shutil.copy2(src, dest)
            continue
        problems.append(f"{it.name}: {it.origin}" + ("" if it.build else f"  -> put it into {paths.USER_FILES}/"))
    return problems


def explain(feature: str, problems: list) -> str:
    f = FEATURES.get(feature)
    title = f.title if f else feature
    return f"{title} is not ready:\n  - " + "\n  - ".join(problems) + f"\nSee user_files/README.md (files you provide) and the build steps in README.md."


def require(feature: str):
    """Raise RuntimeError with a full explanation when the feature cannot run yet (installing found files on the way)."""
    problems = ensure(feature)
    if problems:
        raise RuntimeError(explain(feature, problems))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "check"
    feats = argv[2:] or list(FEATURES)
    bad = 0
    for k in feats:
        if k == "vsr-native":
            problems = install_vsr_native() if cmd == "install" else vsr_native_status()
            print(f"[{'ok' if not problems else 'MISSING'}] RTX Video Super Resolution, native (nvidia-vfx + cupy in runtime/venv-vsr)")
            for p in problems:
                print("      -", p)
            bad += bool(problems)
            continue
        problems = ensure(k, install=(cmd == "install"))
        print(f"[{'ok' if not problems else 'MISSING'}] {FEATURES[k].title}")
        for p in problems:
            print("      -", p)
        bad += bool(problems)
    return 1 if bad and cmd == "check" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
