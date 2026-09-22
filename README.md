# Live neural screen filter for Linux (DLSS5 / upscaling / frame generation)

A live filter for any Wayland window on Linux: it takes the window's picture, runs it through the **DLSS5 neural-rendering model** (through an open, documented protocol -
`docs/worker-protocol.md` - a reference adapter, `hosts/worker_adapter.cpp`, speaks it to NeuralScreen's Wine worker; a different worker could plug in without Wine at all),
optionally upscales it (NIS, FSR 1, RTX Video Super Resolution; MAKO Scaler / LS1 through the optional `ns-mako` module), optionally inserts **generated frames** (NVIDIA DLSS frame
generation, AMD FSR 3.1 frame generation; Lossless Scaling frame generation through `ns-mako`) and puts the result back over the window - all controlled live from a small settings panel.

> Status: a research project developed and tested on **one machine** (CachyOS/Arch, Hyprland 0.56.2, Intel iGPU driving the desktop + NVIDIA RTX 5070 Mobile, Proton - Experimental).
> Expect rough edges. It is independent and not affiliated with or endorsed by NVIDIA, AMD or the authors of the projects it interoperates with.
> **The repository contains no proprietary files** - you provide them yourself (`user_files/README.md`).

## Also in this repository: a YouTube browser extension
`extension/` + `app/yt_bridge.py` - the same DLSS5/frame-generation backends, reached from a Chromium/Vivaldi extension instead of the desktop overlay: it
crops a YouTube video to the player's aspect ratio, then optionally DLSS5-reconstructs and/or frame-generates it, through a small local WebSocket bridge
that drives `app/worker.py` and `app/framegen/` directly. See `extension/README.md`.

## What it does
- **Capture**
  - *Hyprland plugin* (recommended, `hyprplug/`): the compositor hands the window's **own buffer** (including subsurfaces, e.g. video planes) to the filter through shared memory
    (zero-copy dma-buf slots, GPU-side downscale) and draws the result over the window with the window's rounded corners, inside the border. The plugin is fault-shielded (exceptions/signals inside it are caught and reported instead of taking the compositor down).
  - *Toplevel export* (`hyprland-toplevel-export`) as the fallback path; a headless "proxy desktop" (`NS_PROXY=1`).
- **Model input resolution** slider, DLSS5 parameters, before/after comparison divider, FPS readout, tray icon, hotkeys.
- **Upscalers** (plugin architecture, `app/upscalers/`): none, NVIDIA Image Scaling, AMD FSR 1, RTX Video Super Resolution (native `nvidia-vfx`, or the Wine host); MAKO Scaler and LS1 come from the `ns-mako` module.
- **Frame generation** (`app/framegen/`, always last in the chain, paced by a presenter): DLSS-G (x2..x4), FSR 3.1 (x2, optical flow + a global-motion hint because a screen filter has no engine vectors),
  Lossless Scaling FG via the `ns-mako` module (arbitrary interpolation positions, adaptive mode).

## How it is put together
```
window --(plugin: client buffer, zero-copy)--> app/live_filter.py --stdin/shm--> DLSS5 worker (Wine, Proton prefix)
                                                    |  upscaler (GL/Vulkan libs or a Wine host)      \
                                                    |  frame generation (own thread)                  +-- Wine hosts use the nvcuda shim (shim/) and patched vkd3d-proton / dxvk-nvapi (patches/)
window <--(plugin draws the result, watchdog)-------+
```
The Windows-only NVIDIA/AMD components run under **Wine (Proton)**; `shim/` forwards Wine's CUDA calls to the Linux driver, `patches/` add D3D12 shared heaps and a BGRA optical-flow format,
`hosts/` are the small Windows executables that drive the closed libraries. Details: `docs/development-log.md` (chronological engineering notes).

## Requirements
- Linux with Wayland; **Hyprland >= 0.56** for the plugin path (the toplevel-export fallback needs Hyprland's protocol as well).
- NVIDIA RTX GPU + driver with Vulkan and `libcuda.so.1`; Steam with a **Proton** build (Proton - Experimental) and one Proton game started once (its prefix is copied).
- Build tools: `gcc`, `cmake`, `ninja`, `meson`, `mingw-w64` (gcc, g++), `glslang`, `git`; libraries: EGL/GLESv2, libdrm, GTK3, gtk-layer-shell, AyatanaAppIndicator3, PyGObject, Vulkan headers,
  Hyprland development headers (plugin). (`vkd3d`, cmake and ninja are needed only to build the optional `ns-mako` module.)
- Python packages: `pip install -r requirements.txt` (or your distro's packages).
- Files you provide: see **`user_files/README.md`** (DLSS5 worker files, NVIDIA/bridge DLLs, `d3dcompiler_47.dll`, AMD FSR3 DLL, Lossless Scaling).

## Install
```sh
tools/fetch_third_party.sh          # NVIDIA NGX headers/libraries -> third_party/   (add --with-fsr3 for the AMD FidelityFX SDK headers: only FSR 3.1 frame generation needs them)
tools/build_all.sh                  # native libraries, Wine hosts, nvcuda shim, Hyprland plugin (each step skipped if its toolchain is missing)
tools/build_patched_wine_libs.sh    # patched vkd3d-proton / dxvk-nvapi (only for DLSS-G and FSR 3 frame generation)
tools/setup_prefix.sh               # Wine prefix -> runtime/prefix (a copy of a Proton prefix; yours is not modified)
# put the files listed in user_files/README.md into user_files/, then:
python3 app/userfiles.py check      # what is present / missing per feature (the app also checks and installs before it starts)
```

## Run
```sh
hyprplug/nsproxy.sh load            # once per Hyprland session (loads the plugin; `unload`, `status`, `reset` also exist)
NS_PLUGIN=1 ./nsdlss vivaldi-stable # a window class (see `hyprctl clients`), or address:0x...; without an argument the active window is used
```
The window is picked among windows of that class - one on a *visible* workspace first. You can switch the target from the panel at any time.
Without the plugin just run `./nsdlss <class>` (toplevel export path).

Useful environment variables: `NS_UPSCALER=none|nis|fsr|rtx_vsr` (+ module keys such as `mako_scaler`, `ls1`), `NS_FRAMEGEN=dlssg:2|fsr3:2` (+ module keys, e.g. `mako:3`) (start-up preset; the panel changes everything live), `NS_PROFILE=1` (stage timings; `kill -USR1 <pid>` toggles),
`NS_CAPTURE_THREADS`, `NS_WINE`, `NS_WINEPREFIX`, `NS_RUNTIME`, `NS_USER_FILES`, `NS_FSR3_MOTION=0`, `NSPROXY_ZEROCOPY=0` (compositor environment: force the copying paths).
Hotkeys are Hyprland binds you add yourself: `SIGUSR2` shows/hides the panel, `SIGWINCH` switches the filter on/off, e.g. `bind = SUPER, F10, exec, pkill -USR2 -f live_filter.py`.

## Tests (nested Hyprland - the real session is never touched)
`hyprplug/test_shield.sh` (fault injection into the plugin), `hyprplug/test_reload.sh` (load/unload stress under gdb), `hyprplug/nested/test_xform.sh` (buffer transforms), `hyprplug/nested/bench_zc.sh`,
`hyprplug/nested/run_result_tear.sh` (tearing checks), `hyprplug/nested/run_dup.sh`, `run_retarget.sh`, `run_live_fg.sh` (end-to-end). Always address a nested compositor with `hyprctl -i <signature>`.

## Repository layout
`app/` the filter (Python + small C libraries) - `hyprplug/` the Hyprland plugin - `hosts/` neural-render worker / frame-generation / upscaler hosts (native Linux and Wine) - `modules/` optional third-party modules (git-ignored) - `shim/` nvcuda shim generator - `patches/` vkd3d-proton / dxvk-nvapi patches
`tools/` setup and build scripts - `dev/` probes and benchmarks - `docs/` licensing, development log - `user_files/` you drop your files here - `runtime/`, `third_party/` (git-ignored, created by the scripts).

## Optional modules
Backends that must keep their own (copyleft) license live **outside** this repository as *modules* and are plugged in through a process boundary - see `docs/module-protocol.md`
(`tools/fetch_modules.sh` clones the ones listed in `tools/modules.list` into `modules/`, `python3 app/modules.py` shows what was found). The first one is `ns-mako`
(GPL-3.0): Lossless Scaling frame generation, the MAKO Scaler and LS1 through MAKO's backend; it needs the user's own Lossless Scaling.

## Licence
MIT (see `LICENSE`), with third-party components under their own licences (`THIRD-PARTY-NOTICES.md`, `docs/licensing.md`). No proprietary files are included or downloaded automatically.
The GPL-3.0 `ns-mako` module is a separate program with its own repository and license; this repository contains none of its code.
