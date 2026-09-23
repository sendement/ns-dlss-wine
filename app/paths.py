# SPDX-License-Identifier: MIT
"""Where things live. Everything the project does not ship itself (proprietary / third-party binaries, the Wine prefix, build outputs) lives under
`runtime/` (git-ignored); the user drops the files we cannot redistribute into `user_files/` and `userfiles.py` installs them into `runtime/`.
"""
import glob
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP_DIR)
USER_FILES = os.environ.get("NS_USER_FILES", os.path.join(ROOT, "user_files"))
RUNTIME = os.environ.get("NS_RUNTIME", os.path.join(ROOT, "runtime"))

ARTIFACTS = os.path.join(RUNTIME, "artifacts")        # outputs of tools/build_all.sh (hosts, patched Wine libraries, shim)
WORKER_DLSS5 = os.path.join(RUNTIME, "worker")        # DLSS5 (neural rendering) worker
WORKER_VSR = os.path.join(RUNTIME, "worker_vsr")      # RTX Video Super Resolution host
VENV_VSR = os.path.join(RUNTIME, "venv-vsr")          # venv of the native RTX VSR host (nvidia-vfx + cupy)
VENV_VSR_PYTHON = os.path.join(VENV_VSR, "bin", "python")
VENV_JPEG = os.path.join(RUNTIME, "venv-jpeg")        # venv of the GPU JPEG encode host (torch + torchvision/nvjpeg)
VENV_JPEG_PYTHON = os.path.join(VENV_JPEG, "bin", "python")
WORKER_DLSSG_VK = os.path.join(RUNTIME, "worker_dlssg_vk")  # native (Vulkan) DLSS frame generation host
WORKER_DLSSG = os.path.join(RUNTIME, "worker_dlssg")  # DLSS frame generation host
WORKER_FSR3 = os.path.join(RUNTIME, "worker_fsr3")    # FSR 3.1 frame generation host
WINE_NVCUDA = os.path.join(RUNTIME, "wine_nvcuda")    # nvcuda shim (WINEDLLPATH)
PREFIX = os.environ.get("NS_WINEPREFIX", os.path.join(RUNTIME, "prefix"))


def wine_binary() -> str:
    """The Wine used for the workers: $NS_WINE, else a Proton build found in the Steam library (Experimental first)."""
    env = os.environ.get("NS_WINE")
    if env:
        return env
    steam = os.path.expanduser("~/.local/share/Steam")
    cands = [os.path.join(steam, "steamapps/common/Proton - Experimental/files/bin/wine")]
    cands += sorted(glob.glob(os.path.join(steam, "steamapps/common/Proton*/files/bin/wine")), reverse=True)
    cands += sorted(glob.glob(os.path.join(steam, "compatibilitytools.d/*/files/bin/wine")), reverse=True)
    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[0]


