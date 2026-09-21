# user_files/ - files you provide yourself

Some things this project needs are **not part of the repository** (proprietary or third-party binaries, and files whose licences do not allow redistribution).
Put them into this folder - **anywhere inside it, subfolders are fine; files are found by name (case-insensitive)**. The application checks and installs them into
`runtime/` before it starts a feature that needs them (or run `python3 app/userfiles.py check` / `install` yourself); nothing is ever downloaded automatically.

You are responsible for having the right to use these files - each comes from a project with its own licence (see `docs/licensing.md`). The names below are the
upstream projects to look at; find their releases yourself.

## Core: DLSS5 neural rendering
| File | Where to get it |
|---|---|
| `nvngx.dll` (the worker host - it is an EXE despite the name), `nvngx_dlssnr.dll`, `Spout.dll`, `SpoutDX.dll` | **NeuralScreen** (GitHub: perseval-BLR/NeuralScreen) - the `native/` folder of a release archive |

## RTX Video Super Resolution upscaler (optional)
| File | Where to get it |
|---|---|
| `nvngx_vsr.dll`, `nvngx_truehdr.dll`, `neuroframe_engine_upscaling.dll` | **Visual Enhancer** (GitHub: Merserk/dlss5-visual-enhancer) - `bin/runtime/rtx_video/` and the bridge DLL of its runtime |

## DLSS frame generation (optional)
| File | Where to get it |
|---|---|
| `nvngx_dlssg.dll` | **NVIDIA/DLSS** (GitHub) - `lib/Windows_x86_64/rel/` (fetched by `tools/fetch_third_party.sh`), or Visual Enhancer's `bin/runtime/dlssg/` |
| `neuroframe_engine_frame_interpolation.dll` | *optional*, only for the legacy `NS_DLSSG_IMPL=bridge` path - **Visual Enhancer** (Merserk/dlss5-visual-enhancer), `bin/runtime/dlssg/` |
| `d3dcompiler_47.dll` | Microsoft's real build (`winetricks d3dcompiler_47` downloads it into a prefix - copy it from there) |
| `d3d12.dll`, `d3d12core.dll`, `nvofapi64.dll`, `ngxdlssg_host.exe` | **built from this repository**: `tools/build_patched_wine_libs.sh`, `tools/build_all.sh` (they land in `runtime/artifacts/` and are picked up automatically) |

## FSR 3.1 frame generation (optional)
| File | Where to get it |
|---|---|
| `amd_fidelityfx_framegeneration_dx12.dll` | **AMD FidelityFX SDK** 2.3.0 (GitHub: GPUOpen-LibrariesAndSDKs/FidelityFX-SDK) - `Kits/FidelityFX/signedbin/` |
| `d3dcompiler_47.dll`, patched `d3d12*.dll` | as above |

## Lossless Scaling frame generation and LS1 upscaler (optional `ns-mako` module)
`Lossless.dll` from the Steam game **Lossless Scaling** - found automatically in `~/.local/share/Steam/steamapps/common/Lossless Scaling/`; if your copy lives elsewhere,
put `Lossless.dll` here. It is only ever read at runtime; nothing derived from it is stored.

## Also required (not files you drop here)
- A **Proton** build (e.g. *Proton - Experimental*) from Steam and one Proton game started once - `tools/setup_prefix.sh` copies its prefix into `runtime/prefix/`.
- The NVIDIA driver with Vulkan and `libcuda.so.1` (the nvcuda shim forwards to it).


## RTX Video Super Resolution without Wine (recommended)

`python3 app/userfiles.py install vsr-native` creates `runtime/venv-vsr` and pip-installs NVIDIA's own `nvidia-vfx` package (from `https://pypi.nvidia.com`, ~500 MB;
NVIDIA's proprietary licence - it is installed on your machine, this project never redistributes it) plus `cupy-cuda12x` and the CUDA runtime wheels. The `rtx_vsr`
upscaler then runs natively and none of the `vsr` files above (`nvngx_vsr.dll`, `nvngx_truehdr.dll`, the bridge DLL) or Wine are needed for it. Without that venv
the upscaler falls back to the Wine bridge host.
