# Development log

Chronological engineering notes from building the project (reverse-engineering steps, measurements, dead ends). It is a **historical record**, not the user
documentation - start with the top-level `README.md`. Things that changed since these notes were written:

- the directory layout: `track1/` is now `app/`; `worker/`, `worker_dlssg/`, `worker_vsr/`, `worker_fsr3/`, `pfx/`, `wine_nvcuda/` now live under `runtime/`
  (git-ignored) and are filled from `user_files/` by `app/userfiles.py`; host sources are in `hosts/`, the nvcuda shim in `shim/`;
- the **lsfg-vk backend was removed** (its licence, CC BY-NC-ND, forbids derivatives) - the "Lossless Scaling frame generation" method is the MAKO backend;
- proprietary / third-party binaries are never part of the repository (`user_files/README.md`, `docs/licensing.md`).

---

## Capture: KMS/DRM direct (track1/kms/) — the new fastest path

Confirmed working 2026-09-18. `track1/kms/kms_probe.c` is a standalone capture
probe built from `gpu-screen-recorder`'s own wire protocol
(`kms_client.c`/`kms_shared.h`, copied verbatim — see "Getting the source"
below) talking to the system's already-installed `/usr/bin/gsr-kms-server`
(already has `cap_sys_admin` set via `setcap`, confirmed with `getcap`).
`gsr_kms_client_init()` finds it automatically via `PATH` — **no need to
build or setcap our own server**, the one from the `gpu-screen-recorder`
pacman package is reused as-is.

The protocol: the server hands back a DMA-BUF fd for each active DRM plane
(zero-copy, GPU-resident — width/height/pixel_format/modifier + per-plane
fd/pitch/offset, see `gsr_kms_response_item` in `kms_shared.h`). The client
imports it as an EGL image (`eglCreateImageKHR` with `EGL_LINUX_DMA_BUF_EXT`
and the per-plane fd/offset/pitch/modifier attrs) and binds it as a GL
texture — no compositor round trip per frame at all, unlike wlr-screencopy.

**Result: 215fps for a full 2560x1600 capture+import+blit+glFinish (no
encode)**, vs wlr-screencopy's fixed ~11-19ms/frame round trip (~40-70fps at
much smaller *downscaled* sizes, see below). This removes capture as Track
1's bottleneck entirely — verified with real, live desktop content (own
terminal + a browser window), not synthetic test data.

### Getting the source (git.dec05eba.com is Anubis-protected, don't fight it)

`git clone https://git.dec05eba.com/gpu-screen-recorder` and browsing
`/tree/` both fail — the whole `git.dec05eba.com` host is behind an Anubis
proof-of-work anti-scraping challenge (confirmed via `curl`: HTTP 200 but an
Anubis JS-challenge page instead of git/file content). **Don't circumvent
it** — it's the author's deliberate anti-scraping measure. The fix: the
Flathub packaging manifest for this app
(`github.com/flathub/com.dec05eba.gpu_screen_recorder`, plain GitHub, no
Anubis) pins an exact source snapshot tarball on a **different, ungated
subdomain**:
```
https://dec05eba.com/snapshot/gpu-screen-recorder.git.r<N>.<hash>.tar.gz
```
(check the manifest's `.yml` for the current URL/hash). That subdomain
serves plain gzip over HTTP with no Anubis gate — `curl`/`git clone`-free,
just download and `tar xzf`. Kept extracted at `track1/gsr-src/` for
reference; the files that mattered: `kms/kms_shared.h`,
`kms/client/kms_client.c`+`.h` (the protocol, copied into `track1/kms/`
almost verbatim — only the `log.h`/`utils.h` includes were swapped for a
tiny local `shim.h`/`shim.c` providing just `gsr_log()` and
`generate_random_characters_standard_alphabet()`), and `src/capture/kms.c` +
`src/egl.c` (reference only, for the EGL/dma-buf-import approach — not
copied, reimplemented minimally in `kms_probe.c`).

### The two traps that cost the most time

1. **Wrong GPU.** This is a hybrid Intel+NVIDIA laptop; `/dev/dri/card1` is
   the NVIDIA discrete GPU, but the actual KMS scanout (the connected
   `eDP-*`/`DP-*` connectors, checked via
   `/sys/class/drm/card*-*/status`) happens on **`/dev/dri/card0` (Intel)**.
   Pass `card0` as `gsr_kms_client_init()`'s `card_path` and as the EGL
   device-match target, not `card1`.
2. **Silent black-frame trap: unset texture filter on the imported
   texture.** `glEGLImageTargetTexture2DOES()` reports zero GL errors, the
   FBO reports complete, `glReadPixels` reports zero errors — and the
   result is uniformly `(0,0,0,255)` for every single pixel anyway. Cause:
   GLES's default `GL_TEXTURE_MIN_FILTER` is
   `GL_NEAREST_MIPMAP_LINEAR`, which needs a full mipmap chain; a
   single-level imported texture is therefore "incomplete", and sampling an
   incomplete texture is well-defined GLES behavior that silently returns
   `(0,0,0,1)` — no error anywhere in the pipeline to point at it. Fix:
   `glTexParameteri(target, GL_TEXTURE_MIN_FILTER, GL_LINEAR)` (and
   `MAG_FILTER`) on the imported texture immediately after binding, before
   the first draw. Cost about an hour to find, ruled out CCS-compression
   modifiers (Intel Arrow Lake's `I915_FORMAT_MOD_4_TILED_MTL_RC_CCS_CC`,
   modifier value 15) as a red herring first by testing with modifier attrs
   stripped entirely (still black — so not a modifier/decompression issue).

### EGL context: no window needed at all

Uses `EGL_PLATFORM_DEVICE_EXT` (via `eglQueryDevicesEXT` +
`eglGetPlatformDisplayEXT`, matched to the target card by comparing
`EGL_DRM_DEVICE_FILE_EXT` device strings) — a genuinely headless EGL
context with no X11/Wayland window or surface at all
(`eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx)`, i.e.
surfaceless). This is exactly the config `gpu-screen-recorder` itself uses
(see `src/egl.c`'s `gsr_egl_get_device_display`) and is what makes this
approach usable from a plain background process, no compositor window
required.

### Wired into `live_filter.py`: set `NS_USE_KMS=1`

`track1/kms/kms_capture_lib.c` builds to `track1/kms/libkmscapture.so` - a
persistent version of `kms_probe.c` (one `gsr_kms_client` connection + one
EGL context kept alive for the process lifetime, not spawned per frame) with
a small C API (`kms_capture_init`/`_get_frame`/`_close`) wrapped by
`track1/kms_capture.py` via `ctypes`. `KmsCapture.capture_region(x, y, w, h,
out_w, out_h)` does crop + scale + readback in a single GPU pass (the vertex
shader remaps `[0,1]` UV into the requested sub-rect before sampling) and
returns genuine RGBA order already - no separate PIL resize or BGR channel
swap needed, unlike the wlr-screencopy path.

**Confirmed on a real Vivaldi window at 800x670, `NS_USE_KMS=1
NS_USE_SHM=1`: ~105fps steady-state, `queue-wait` (capture) at flat 0ms/f
and worker `write`/`read` down to ~0.4ms/f each** - capture and the SHM
transport are now both completely off the critical path; the remaining
~8.4ms/frame is the DLSSNR network's own GPU compute (`ngx-wait`), a real
compute cost rather than a fixable overhead. Compare to the
wlr-screencopy+SHM path's earlier documented ~45.7fps ceiling at the same
resolution - roughly a **2.3x** end-to-end speedup just from removing the
capture bottleneck.

**One real bug hit while wiring it in, worth remembering for any future
work mixing threads with an EGL/GL context**: `KmsCapture` (like any EGL
context) is thread-affine - `eglMakeCurrent` binds the context to whichever
thread calls it. Constructing it on `live_filter.py`'s main thread and then
calling `capture_region()` from the separate `capture_loop` thread produced
a **silent-looking `"fbo incomplete"` error** on the very first scaled
capture (same-size crops didn't trip it, since the standalone smoke test in
`kms_capture.py` never exercised the scale path) - GL calls issued from a
thread with no current EGL context don't raise a clear "wrong thread"
error, they just misbehave. Fix: construct `KmsCapture` **inside**
`capture_loop` itself (and close it there too, in a `finally`), not in
`main()`.

**Multi-monitor selection**: `KmsCapture(want_w=..., want_h=...)` matches
a monitor by its DRM plane size (from `hyprctl monitors -j`); `live_filter.py`
resolves this automatically via `get_monitor_for_window()`, which also
converts the window's Hyprland-global coordinates into the monitor-local
coordinates `KmsCapture` expects (KMS plane coordinates aren't aware of
Hyprland's global multi-monitor layout at all).

## Live settings overlay: `NS_UI=1`

`python3 live_filter.py <window_class> ` with `NS_UI=1` set replaces the
fixed env-var configuration with a live UI: a floating, draggable settings
panel plus a click-through overlay positioned exactly over the captured
window, showing the DLSS5+upscaler result in place.

- **`Worker.reconfigure()`** (in `live_filter.py`) drives the worker's
  `RNSZ` wire message (`RESIZE_MAGIC = "RNSZ"`, discovered by reading
  `dlss5-feed-host64.cpp`'s `RunVideo()`) - reconfigures resolution *and*
  DLSSNR parameters on the **already-running** worker process, no Wine
  restart. `VideoResizeCmd` reuses `VideoHeader`'s exact `"<10I4f2I"`
  layout (just `RESIZE_MAGIC` and `flags` in the `frame_count` slot); the
  reply is a `VideoResizeAck` (`"RACK"`, `"<4Iq"`). The worker's own log
  distinguishes a full feature-recreate resize from a params-only change
  ("no composite - the network is the output" for the latter) - params-only
  changes are cheaper than a resize, but neither is free, so every live
  control (`live_state.py`'s `LiveState.stable_snapshot()`) is **debounced**
  (0.25s of no further change) rather than firing on every slider tick.
  `profile`/`preset` header fields exist in the wire protocol but are never
  read anywhere in the worker - `live_state.DlssParams` deliberately
  excludes them. The one thing RNSZ *can't* change: `NS_NR_PRESET` (→
  `DLSSNR.Hint.Render.Preset`, an NGX model-variant hint) is read once at
  worker launch and cached - the panel's "NR preset" control is the one
  restart-triggering control, clearly labeled as such.
- **`upscalers/`** - a small plugin interface (`Upscaler` ABC in
  `upscalers/base.py`: `configure(src_w,src_h,dst_w,dst_h,**settings)` /
  `upscale(frame)` / `close()`, plus a `Setting` list the panel uses to
  auto-generate controls) so a new upscaler is one file + one line in
  `upscalers/__init__.py`'s `REGISTRY`. `none_upscaler.py` (plain PIL
  bilinear, zero GPU dependency) and `nis_upscaler.py` (adapts the
  existing `NisUpscaler`) are the two entries so far.
- **`settings_panel.py`** - a floating `GtkLayerShell` panel (dragged by
  recomputing its own margins on `motion-notify-event`, since layer-shell
  surfaces have no compositor-level window move) with the resolution
  slider (shows the live `WxH` fed to the model), upscaler picker +
  per-upscaler settings, DLSS5 parameter controls, NR-preset restart
  control, FPS label, and the compare-mode switch.
- **`screen_overlay.py`** - the click-through overlay, positioned via
  `GtkLayerShell.set_monitor()` (matched to the right `Gdk.Monitor` by
  geometry) + `TOP`/`LEFT` anchors with monitor-local margins, rendered
  via a `Gtk.DrawingArea` + Cairo (needed for the divider line/handle).
  Click-through via `Gdk.Window.input_shape_combine_region()` - an empty
  region when compare mode is off (100% pass-through), or a region
  covering only the divider handle when it's on (drag only reachable
  through that rect; the rest of the screen stays click-through the whole
  time, including mid-drag, via GTK/Wayland's normal implicit pointer
  grab).

### Two real compositor-interaction bugs found while wiring this up

**1. Blur/animation ghosting, fixed by `set_exclusive_zone(-1)`.** Both new
windows showed a bizarre cascading, shrinking "echo" of their own content
tiled diagonally across the screen. Turned out to be this system's Hyprland
config (`~/.config/hypr/hyprland/rules.lua`) reserving a strip of screen
space for the Caelestia shell's own panel (`hyprctl monitors -j`'s
`"reserved"` field, `[60,10,10,10]` here) - **the user noticed the shift
matched the panel's own size exactly**, which was the key clue. Layer-shell
surfaces respect other surfaces' exclusive zones by default; ours didn't
need to (we want to cover exactly the target window's rect, panel or no
panel). Fix: `GtkLayerShell.set_exclusive_zone(window, -1)` ("ignore other
surfaces' exclusive zones") on both windows - resolved it completely, no
Hyprland config changes needed after all (a `layerrule` with `no_anim`/
`blur = false` was tried first, following this config's own precedent for
`caelestia-*` namespaces, but didn't fix it - the exclusive-zone fix did).

**2. A genuine capture feedback loop, fixed by switching capture backends
for this specific use case.** Once the panel-ghosting bug above was fixed,
the *video content itself* still showed a fine, dense ringing/noise
pattern - **the user correctly diagnosed this one too**: "it's feeding its
own already-processed frame back into the model, over and over." Root
cause: the overlay is deliberately positioned exactly on top of the
window it's processing, but both existing capture backends
(`wl_capture.ScreencopyCapture` / `kms_capture.KmsCapture`) read the
**compositor's final, fully-composited output** - once the overlay is
visible, every subsequent capture reads its own previous frame instead of
the real window, each pass re-sharpening/re-denoising the last one's
result. Neither backend can be fixed for this - they're screen/output
capture by design. The real fix: **`toplevel_capture.py`'s
`ToplevelCapture`**, built on Hyprland's own `hyprland-toplevel-export-v1`
protocol, which exports one **client surface** directly, bypassing
compositing (and, therefore, whatever's drawn on top of it) entirely -
exactly what `grimblast copy active`-style tools use to screenshot one
window without capturing overlays. Its v1 request takes a plain `uint`
window-address handle, but that's a 32-bit field and this system's real
window addresses (64-bit, ASLR'd) overflow it outright - used the v2
request (`capture_toplevel_with_wlr_toplevel_handle`) instead, which needs
a real `zwlr_foreign_toplevel_handle_v1` object obtained by binding
`wlr-foreign-toplevel-management-unstable-v1` and matching its `app_id`
events against the target window class (bindings generated the same way
as `wl_capture.py`'s, now in `wl_protocols/hyprland_toplevel_export_v1.py`
+ `wl_protocols/wlr_foreign_toplevel_management_unstable_v1.py`).
`NS_UI=1` now **always** uses this backend regardless of `NS_USE_KMS` -
it's a correctness requirement for the overlay use case, not a speed
choice (measured ~59fps standalone, competitive with wlr-screencopy
anyway).

### Hardening + usability pass (live-tested by the user)

- **Target window is no longer hardcoded**: `NS_UI=1 python3 live_filter.py [class]` -
  without an argument it takes the active window; the panel has a "Target
  window" picker (+ refresh) that switches capture/overlay live
  (`LiveState.target_class`; `capture_loop` rebuilds its `ToplevelCapture`,
  `poll_geometry` re-pins the overlay, including to another monitor). A
  "Выключить" button in the panel header shuts everything down cleanly.
- **Races/hangs fixed** (all found by live use): (1) "Restart with preset"
  used a fixed `sleep` before reusing threads - now `join()`s the old
  capture/process threads; (2) after a window resize the slider recomputes
  the work resolution -> `reconfigure()`, but a stale-size frame could still
  sit in `frame_q`; feeding it to the worker hung forever (pipe read has no
  timeout) - frames whose size != the applied resolution are now dropped
  before `worker.process()`; (3) `capture_loop` died permanently on a single
  transient capture error - now skips/retries (rebuilds the capture after 10
  in a row); (4) `Worker.reconfigure()` choked on a late `CACK` before `RACK`
  - now skips stray replies like `process()` does; (5) click-through:
  GDK resets the input shape, so it is reasserted every frame; (6) panel drag
  stutter: margin commits throttled to ~60/s.
- **DRM card numbering is not stable across reboots** (Intel was `card0`,
  then `card1`): `kms_capture.detect_display_card()` picks the card with a
  connected connector; `KMS_CARD` / `NisUpscaler` use it by default.
- **DLSS effect is real but subtle** (verified directly, bypassing the UI):
  output alpha is 255; `intensity=0` is an exact passthrough; default vs
  passthrough differs by ~8/255 mean on a web page, other knobs shift it by
  only 0.5-3 more levels (`intensity` saturates above ~1). Expect little
  visible change on flat UI content; FPS cost is DLSS itself at native size.
- **Whole-screen mode: deferred.** Capturing a full output re-captures our own
  overlay (the same feedback cascade as before); the only feedback-free routes
  are compositing per-window `ToplevelCapture`s or one overlay per window,
  both much heavier. Not implemented.
- Known limits: `Worker.process()` reads have no timeout (a real desync still
  hangs); "restart with preset" blocks the GTK thread while the worker
  restarts; compare-mode divider drag not yet exercised live.

### Reference-app findings applied (Merserk/dlss5-visual-enhancer)

- **NR style is a named enum**: Default=0 / Natural=1 / Cinematic=2 (panel now has
  a dropdown; our old default `style=1` was already Natural). Defaults aligned
  with the reference app: `local_tone=1.0`, `auto_mask=0` (auto-enabled when
  skin structure > -1, like there).
- **NR Preset does nothing**: swept `NS_NR_PRESET` 0-3 - outputs are bit-identical
  - and the reference app has *removed* its "NR Preset" ("before NR Preset
  removal" in its metadata code). The panel control was removed.
- **RTX Video Super Resolution under Wine - WORKING** (`upscalers/rtx_vsr_upscaler.py`, registry key
  `rtx_vsr`, panel entry "RTX VSR", quality 1-4). Release v10.0 ships NVIDIA's `nvngx_vsr.dll` plus the
  author's MIT bridge `neuroframe_engine_upscaling.dll` (C ABI `rtxv_*`). `vsr_host.cpp`/`worker_vsr/` is the
  Wine-side host (argv + mmap'd in/out/ctrl files; stdin must be /dev/null). Proton's builtin `nvcuda.dll` is a
  stub (`cuInit = 801`), so `nvcuda_shim/` provides a real one: `gen.py` generates a PE part (12-arg MS-ABI
  forwarders, own stubs handed out by `cuGetProcAddress`, `cuDeviceGetLuid` via DXGI, `cuGetExportTable`
  trampolines) and a unix `.so` (`__wine_unix_call_funcs`, dlopens host `libcuda.so.1`). Wine gotchas: Proton's
  own `nvcuda` builtin wins over `WINEDLLPATH`, so the real code is `nvcudashim` (fake-builtin copy with the
  32-byte "Wine builtin DLL" signature at 0x40 in the prefix `system32`) behind a native forwarder `nvcuda.dll`
  (`WINEDLLOVERRIDES=nvcuda=n`, `WINEDLLPATH=wine_nvcuda`).
  Three things had to be solved:
  1. Bare-array export tables (`c693336e`) have no size header - copy them from index 0.
  2. **cudart's driver "integrity check"** (export table `d4082055` entry 0, `fn(id, unix_time, out16)`, error
     103 `cudaErrorSoftwareValidityNotEstablished` when it fails). For id%10>=2 libcuda answers HMAC-MD2
     (key derived from an obfuscated 64-byte table) over: `0x32f0, id, pid, tid, &table6bd5, &tableD408,
     &fn, cookie` + 28 bytes per device (uuid, +0xc88, +0xc80). cudart recomputes it from ITS view (Windows
     pid/tid and OUR fake table/function addresses), so libcuda's answer never matched. The shim now computes it
     itself (`es_hook` in the PE part; per-device bytes read out of libcuda memory by unix id `0xFFFFFFFD` -
     **offsets are specific to libcuda 615.71.09**: count at +0x6b46f50, device array at +0x6b46d50, fields at
     device+0xc06/0xc37/0xc88/0xc80/0xa66c). Verified bit-exact against the real function natively first.
  3. `cuGetProcAddress("cuArrayCreate")` must yield the `_v2` ABI (legacy list in `getproc`), the unversioned
     libcuda export is the 32-bit-size v1 layout (was `CUDA_ERROR_INVALID_CONTEXT`).
  Result on the RTX 5070: 400x336 -> 800x672 in ~1 ms (~1000 fps), 400x336 -> 1600x1344 in 1.6 ms; visibly
  sharper text than bilinear. Full chain (`chain_test`): shrink 2x -> DLSS5 -> VSR -> original size ~175 fps;
  shrink 4x ~215 fps but text gets blurry (VSR cannot recover 4x-shrunk UI text). Startup of the host ~3.5 s,
  a size/quality change restarts it (~1-3 s). Debug aids: `NVCUDA_TRACE=1` (per-call + raw table-call trace),
  `VSR_DEBUG_EXIT=1` (host hooks nvngx_vsr's ExitProcess/error site).
- **Reviewed `NIGos/dlss5-bridge` and `NapXDD/addon-dlssnr-linux` (2026-09-19).** The former is a ReShade
  D3D11/Vulkan->D3D12 bridge for game DLSS (not useful for screen capture; its Proton reports all
  hit the same vkd3d `d3d12core` fault). The latter is the relevant one: a GPL-3 ReShade add-on that
  drives `nvngx_dlssnr.dll` directly as NGX feature 18 through a forwarder DLL whose filename contains
  `nvngx.dll` (same caller-gate trick as our disguised worker; recipe from OptiScaler_DLSSNR), on an
  RTX 5070 / driver 610.57. It uses the same `DLSSNR.*` parameters we do (plus `Hint.Render.Preset` 0-7,
  `UICorrection=1`, `DepthInverted`, `Reset`, subrects, `MVecScale`). Its README warns the model build
  matters (tested sha256 `e16bcf15...fc8e`, 165,840,496 B; a mismatched build reported Success then
  crashed minutes in). **Ours is `dcc0dc24...d36f`** - different build, stable so far in our tests.
  Re-checked here with the current image: `ui_correction=1` and `auto_mask=1` change output by <0.6/255;
  style Default is ~2.6/255 from input vs ~10.8 for Natural (our default), Cinematic ~5.3 - so nothing new.
  Its "colour bridge" (display-referred encode, white point) is irrelevant to us: screen capture is already
  display-referred. For the still-blocked native track, its `nr_runner.hpp` shows the parameter block
  reuse ("driver core's capability parameter block", setter vtable slots found by round-tripping a value).

### 2026-09-19 (evening): live pipeline performance pass, RTX VSR as default upscaler

Measured with `NS_PROFILE=1` (per-stage ms every 60 frames in the log). Target window 3440x1440 (~19 MB/frame):

- **The 25-30 fps plateau was not the slider or the upscaler.** Three serial costs hid behind it: (1) the overlay's
  RGBA->cairo BGRA conversion + two copies + paint ran on the GTK thread every frame (~27 ms at 1975x1398) -
  now `screen_overlay.CairoFrame.from_rgba()` runs on the upscaler thread (5 ms via slice copies, not fancy
  indexing) and the draw handler only paints (~3 ms); (2) worker.process and upscale ran back to back - now
  `live_filter.UpscalerHost` owns the upscaler on its own thread (single-slot hand-off, stale frame dropped,
  commands ordered before frames, fallback to the plain resize if an upscaler fails); (3) capture was a single
  ~15-30 ms compositor round trip - now `NS_CAPTURE_THREADS` (default 4; standalone 3 -> 120 fps, 4 -> 146, 5 -> 151, 6 -> 155, saturates at ~4) parallel `ToplevelCapture`s, each on its
  own Wayland connection (standalone: 1 thread 30 fps, 2 -> 71, 3 -> 90); a stale finisher is dropped by sequence
  number. The panel fps is a ~1 s sliding window (it used to be a since-start average that lagged after any change).
- **Upscaler cost at 3440x1440** (from 1/2 and 1/4 size): RTX VSR 9.5 / 5.5 ms, NIS 16.5 / 17.6 ms, "none" (PIL
  bilinear) 54 / 46 ms - the 12-18 fps seen "without an upscaler" was the PIL resize. Default is now `rtx_vsr`
  (`NS_UPSCALER=none|nis|rtx_vsr`). Result reported live: 40+ fps.
- **Capture backends compared.** wlr-screencopy ~40-70 fps (11-19 ms fixed per call); KMS/DRM 215 fps capture-only
  (~105 in the pipeline); toplevel-export ~30 fps per thread. The last one is the slowest but the only one that
  does not capture our own overlay (the other two read the composited output -> feedback trail), and it always
  copies the whole window, so the resolution slider does not speed it up.
- **KMS retried in the UI pipeline (`NS_CAPTURE=kms`, experimental):** still feeds back. A screenshot with the overlay
  active shows the window region filled with accumulated noise/kaleidoscope artefacts - the overlay is part of the
  scanout that KMS reads, exactly as before. Only a layout where the result is NOT drawn over the captured region
  (second monitor / beside the window) can use KMS. toplevel-export stays the default.
- Orphaned `vsr_host.exe` processes survive a `pkill` of the app - kill them (`pkill -f vsr_host`) after force-quits.

### 2026-09-20: compare-mode speedup, hide/show hotkeys, tray icon

- **Compare divider cost ~19 fps -> "much better"**: `UpscalerHost._prep_compare` builds the "before" frame only for the
  strip left of the divider (+96 px margin), on a helper thread parallel to the upscale (it used to resize + convert
  the whole frame serially).
- **Hotkeys** (Hyprland binds in `~/.config/caelestia/hypr-user.lua`, they just send signals): `SUPER+F10` ->
  `SIGUSR2` shows/hides the settings panel (filter keeps running); `SUPER+SHIFT+F10` -> `SIGWINCH` turns the
  filter overlay on/off (capture + processing idle while off, worker and VSR host stay alive so it comes back
  instantly). GLib's `unix_signal_add` only accepts HUP/INT/TERM/USR1/USR2/WINCH (SIGURG silently fails).
  `SIGUSR1` toggles the `NS_PROFILE` stage-timing output at runtime.
- **Tray icon** (AyatanaAppIndicator3 / StatusNotifierItem, icon `video-display`): hide/show overlay, hide/show panel,
  quit. `state.active` gates the capture loops.
- Find the app pid with `pgrep -f "^python3 live_filter.py"` (a plain `pgrep -f` also matches the harness shell).

### 2026-09-20: composition post-pass (ideas from the reference app's NR settings)

Panel section "Composition" (collapsed by default), all values no-ops at their defaults:
- `postprocess.py` + `post/post_lib.c` (`libpost.so`, C + OpenMP, ~1.5-5 ms at 1720x720; the numpy reference
  `PostProcess.apply_numpy` was 25-95 ms and stays only to check the C version, mean diff 0.3-1.1/255). Runs on the
  upscaler thread at the WORKING resolution, before the upscaler, where both the captured source and the model
  output exist. Low-frequency terms come from 1/4-res planes blurred there and sampled bilinearly.
  - **NR colour strength** c: chroma (rgb - Y) blended source<->model. **Tone preservation** t: model's
    low-frequency luma pulled toward the source's (radius ~min(w,h)/40). **Detail-Only** button = colour 0 + tone 1
    (visible side effect: a faint glow around large bright areas - it is the low-pass difference).
  - **Grain preservation**: source fine detail put back where the model smoothed it. **Face/skin protection**:
    source shows through over skin-coloured (YCbCr box) pixels - crude, not a face detector.
  - **Shimmer suppression**: static pixels (source luma delta < 14/255) blended with the previous result; default 0
    here (the reference defaults to 0.7 - it is aimed at video).
  - **NR mask image** (+ feather px): grayscale image stretched to the frame, white = model applies, black = source.
    Entered as a path (Browse button opens a plain file dialog).
- **NR passes 1-4**: the result is fed through the worker again (`process_loop`); latency scales with passes, and the
  worker's temporal state sees the same frame repeatedly - treat as experimental.
- **Resolution slider 10-200%** (was 10-100): above 100% the captured frame is bilinearly enlarged, processed by the
  model at that size (clamped to 7680x4320) and then brought back DOWN to the display size by the plain resize
  (`UpscalerHost._configure` forces `none` when res > display). Expensive: 200% of a 2272x1398 window is ~12.7 MP.
- Not done, by request: DLSS Frame Generation (own menu with several generation methods later). Found in the
  reference release but unused: `neuroframe_engine_neural_rendering.dll` (C API `dlss5nr_*`, float buffers via CUDA),
  `neuroframe_caller.dll`, `nvngx_dlssnr.dll` (165,830,144 B - a third model build), `neuroframe_engine_frame_interpolation.dll`
  + `nvngx_dlssg.dll`.

### 2026-09-20: FSR 1.0 upscaler plugin (`fsr`)

`track1/fsr/` + `fsr_upscale.py` + `upscalers/fsr_upscaler.py` (panel entry "FSR 1 (EASU+RCAS)", sharpness 0-1, default
0.85 = RCAS ~0.3 stops). AMD FidelityFX FSR 1.0 (MIT, `LICENSE-AMD-FSR.txt`): the shaders are AMD's `ffx_a.h` /
`ffx_fsr1.h` verbatim, assembled into GLES 3.1 fragment shaders by `gen_shaders.py` (`textureGather`, so it needs an
ES 3.1 context); the EASU/RCAS constants come from AMD's own CPU-side `FsrEasuCon`/`FsrRcasCon` (`fsr_cpu.c`). Same
EGL-on-the-DRM-device setup as the NIS library; two passes (EASU -> RCAS) into RGBA8 targets, then `glReadPixels`.
One patch to AMD's header at assembly time: Mesa rejects `con[2]=0;` (int -> uint), so those become `0u`.
- Quality on the test image (400x336 -> 800x672, mean |err| vs the original): FSR 6.31, NIS 6.28, bilinear 7.67 -
  visually equal to NIS, both clearly sharper than bilinear.
- Cost: 5.8 ms at 800x672, ~20 ms at 3440x1440 (glReadPixels-bound, like NIS 17 ms; RTX VSR is 5-10 ms).
- **Why only FSR 1:** FSR 2/3 are temporal (need motion vectors, depth, jitter) and FSR 4 additionally needs
  RDNA4 FP8 / ML weights - a screen capture has none of those inputs, so the spatial FSR 1 is the only member of
  the family that can run here. "Works like NVIDIA's" holds in the sense of being a drop-in plugin, not a neural one.

### 2026-09-20: frame generation (LSFG via lsfg-vk) - working, confirmed live by the user

Panel section "Frame generation" (method Off / LSFG / Simple blend (test), multiplier 2-4, flow scale, performance mode).
- **Design.** `framegen/` is a backend registry like `upscalers/`. Each real frame (after upscaling, display size) goes to
  the backend, which returns the frames to show between the previous and this real frame. `live_filter.Presenter` spaces
  `[generated..., real]` evenly over the measured real-frame interval (EMA), so the overlay updates at multiplier x the
  real rate for ~(m-1)/m of an interval of extra latency. Headless test with the blend backend: a 20 fps source showed at
  ~34-39 fps, gaps ~25 ms. The panel shows "X fps shown (Y real)".
- **LSFG backend** (`framegen/nsfg_bridge.cpp` -> `libnsfg.so`, `framegen/nsfg.py`): a C bridge over lsfg-vk's own
  pipeline library (Vulkan compute, CPU only touches pixels) - `third_party/lsfg-vk` (upstream `git.lsfg-vk.dev/lsfg-vk`,
  commit fd8c317, **CC BY-NC-ND 4.0: private use only, do not redistribute**), built by `framegen/build.sh`. Modelled on
  lsfg-vk's `debug` command: the pipeline exports its 2-layer source image, destination image and a timeline semaphore as
  fds; we import them into a Vulkan device of our own, upload each real frame with VK_EXT_host_image_copy (0.7 ms) and read
  generated frames back with a GPU `vkCmdCopyImageToBuffer` into a host-cached buffer. Gotchas found: (1) reading the image
  through host image copy takes ~480 ms at 1720x720 (uncached CPU reads over PCIe) - use the GPU copy (0.5 ms); (2) the
  timeline-semaphore signalling thread must already run before `dispatch()`; (3) build with `-DNDEBUG` (vulkan.hpp's
  header-version assert fires otherwise). Generation itself is ~0.4 ms at 1720x720, flow 0.5; a full submit ~2 ms.
- **The DLL.** lsfg-vk 2.0 reads `lsfg-vk.dll`, shipped by the **"lsfg-vk" beta branch of Lossless Scaling on Steam**
  (Steam -> Lossless Scaling -> Properties -> Betas -> `lsfg-vk`; the manifest shows `BetaKey "lsfg-vk"`, and `lsfg-vk.dll`
  appears next to Lossless.dll). The default-branch `Lossless.dll` has the same SPIR-V under ids 303-400 in another order;
  `tools/remap_lossless_dll.py` (renumbering a copy) loaded but produced black frames - kept only as a curiosity, not used
  when `lsfg-vk.dll` exists. **Verified with the official file:** a moving image (12 px/frame) gave an interpolated frame
  matching a 54 px shift exactly (expected 54), ~1.6-2 ms per submit at 1720x720.
- Submit cost (includes a 19 MB np.roll used by the benchmark itself): 9.4 ms at 2272x1398, 13 ms at 3440x1440 (x2), 19 ms
  (x3); real cost is lower. Flow scale barely matters here (0.5 vs 1.0: 13.1 vs 12.7 ms).
- Not done: NVIDIA DLSS-G backend (needs motion vectors/optical flow; the reference release has `nvngx_dlssg.dll` +
  `neuroframe_engine_frame_interpolation.dll`).

### 2026-09-20: reviewed MAKO (`eugeniosegala/MAKO`) - what is worth taking

MAKO ("Motion-Adaptive Kernel Orchestration", GPL-3.0-or-later, v3.3.0 of 2026-09-18) is the continuation of the
lsfg-vk-experimental / Decky LSFG-VK line: a Vulkan layer + Decky plugin for SteamOS. It descends from the **GPL-3
lsfg-vk v2 tree** (upstream commit 8b0da26, before lsfg-vk's own relicensing to CC BY-NC-ND), so its `engine/mako-backend`
is the same pipeline we vendored, under a licence that allows modification. Nothing was built or run from it yet.
- `mako-backend/include/mako-backend/mako.hpp`: `Instance::openContext(sourceFds, destFds[], syncFd, w, h, encoding, flow,
  perf)` with the same exported-fd design as lsfg-vk, plus **`scheduleFrames(ctx, timestamps)`** (generate frames at
  arbitrary interpolation positions 0..1), several destination images at once, `scheduleFrameHistory()` (advance the
  temporal state without generating - for adaptive skipping) and HDR encodings. Takes the default-branch `Lossless.dll`:
  `extraction/model_resources.cpp` finds the relocated shader table (our 303-400 layout) itself - no "lsfg-vk" Steam branch.
- **Adaptive Frame Generation** (`mako-render/src/adaptive_scheduler.cpp`, ~2900 lines, documented in
  `engine/docs/ADAPTIVE-VALIDATION.md`): targets a fps between 30 and 240 by choosing 0-4 generated frames per real frame
  from the measured cadence, with a smooth-cadence mode. Our Presenter is fixed-multiplier only.
- **Scalers:** "MAKO Scaler" = open single-pass compute shader (`mako-render/src/shaders/spatial_scaling.comp`, 191 lines:
  sharpened-cubic reconstruction + contrast-aware anti-ringing + local sharpening) - a natural next upscaler plugin next
  to NIS/FSR/VSR. "LS1 Quality/Performance" = Lossless Scaling's own scaler graph (4 stages of D3D11 bytecode translated
  to SPIR-V with vkd3d-shader at runtime: `loadLs1ShaderSet`); running it needs the pass graph from `mako-render` too.
- Ideas not taken: HDR10/scRGB paths, Gamescope/WSI handling (SteamOS specifics).
- Candidate next steps, in order of value/effort: (1) MAKO Scaler as an upscaler plugin (DONE, see below); (2) swap our bridge to
  the MAKO backend for timestamp scheduling + default-branch DLL (medium); (3) adaptive multiplier in the Presenter using
  its scheduler ideas (medium); (4) LS1 (DONE, see below).

### 2026-09-20: MAKO Scaler upscaler plugin (`mako`)

`track1/mako_scaler/` (`mako_lib.c` -> `libmakoscaler.so`, ctypes wrapper in `__init__.py`) + `upscalers/mako_upscaler.py`
(panel entry "MAKO Scaler", sharpness 0-1, default 0.8 as in MAKO). Port of MAKO's `spatial_scaling.comp` (Vulkan compute)
to a GLES 3.0 fragment shader with the same maths and constants: nine filtered taps folding the sharpened-cubic (tension
0.8) 4x4 reconstruction, clamp to the 2x2 neighbourhood, contrast-aware anti-ringing (blend toward bilinear), and a bounded
local sharpening term with an envelope. **Derived from GPL-3.0-or-later code - `mako_scaler/MAKO-LICENSE-NOTE.txt`.**
- Test (800x672 UI screenshot shrunk 2x, mean |err| vs the original): MAKO 8.26, FSR 8.27, NIS 7.73, bilinear 10.23 -
  visually all three are close; NIS scores best on this image, MAKO is a touch crisper on text edges.
- Speed: 2.1 ms at 800x672; **7.6 ms at 1720x720 -> 3440x1440** (single pass; FSR ~20 ms, NIS ~17 ms there, RTX VSR 5-10 ms).
- Gotcha (test artefact, not an app bug): the GLES libraries (NIS/FSR/MAKO) each `eglTerminate` their display on close, and
  they share one EGL display per device - closing one while another is alive breaks the other (garbage output, 0 ms).
  `UpscalerHost` only ever keeps one alive (closes the old before creating the new), so the app is fine; in scripts, test
  them one at a time.

### 2026-09-20: frame generation on MAKO's backend + adaptive multiplier

- **`mako` backend** (panel: "LSFG (MAKO backend)"; the lsfg-vk pipeline stays as "LSFG (lsfg-vk pipeline)"):
  `framegen/nsmako_bridge.cpp` -> `libnsmako.so` (`framegen/build_mako.sh`; vendored `third_party/mako`, GPL-3.0-or-later;
  the bridge is GPL too), wrapper `framegen/nsmako.py`. MAKO's `Instance::openContext` IMPORTS images, so the bridge creates
  (with mako-common's helpers) two source images, `capacity` destination images and a timeline semaphore, exports them as
  fds and drives `scheduleFrames(ctx, timestamps)` / `scheduleFrameHistory(ctx)` like mako-cli's temporal-quality tool.
  Uploads go through a write-combined staging buffer, readback through a HOST_CACHED buffer (same 100x lesson as before).
  - Reads the ordinary **default-branch `Lossless.dll`** (MAKO's resolver finds the relocated shader table: log line
    `resource_layout ... precision=fp32 compatibility=structural-and-vulkan`); `lsfg-vk.dll` is only a fallback.
  - **Arbitrary interpolation positions verified:** a 12 px/frame scene, t = 0.25 / 0.5 / 0.75 -> shifts 63 / 66 / 69 px
    (expected exactly that); t=0.5 -> 54 px.
  - Cost per real frame (x2, includes upload/readback): 6.6 ms at 1720x720 and 13.3 ms at 3440x1440, vs 4.1 / 11.9 ms for the
    lsfg-vk pipeline bridge - a little slower (per-call command buffers/fences and polling; not tuned).
- **Pacing by timestamp.** `Presenter.schedule` now takes per-frame offsets: a generated frame at position t is shown at
  `now + (t - t_first) * interval` and the real frame at `now + (1 - t_first) * interval` (same latency as before,
  now valid for any set of positions).
- **Adaptive frame generation** (panel: "Adaptive: aim for a target fps", target 30-240, multiplier = ceiling; MAKO backend
  only): per real frame the planner wants `target * interval` displayed frames, i.e. that minus one generated, with error
  diffusion for the fractional part; 0 generated frames still calls `scheduleFrameHistory` so the temporal state keeps
  advancing. Headless test with a ~25 fps source and a x4 ceiling: target 45 -> 42.6 shown, 60 -> 56.3, 90 -> 79.4.
  (MAKO's own scheduler is ~2900 lines with cadence recovery and Gamescope logic; this is the simple core of the idea.)

### 2026-09-20: LS1 (Lossless Scaling) upscaler plugin (`ls1`)

`track1/ls1_scaler/` (`ls1_bridge.cpp` -> `libls1scaler.so` via `ls1_scaler/build.sh`, wrapper `__init__.py`) +
`upscalers/ls1_upscaler.py` (panel "LS1 (Lossless Scaling)": variant/sharpness 0-1 = one of five learned variants, and a
"Performance graph" checkbox). The four compute stages (two in the performance graph) are the user's own shaders from
`Lossless.dll`, translated D3D11 -> SPIR-V at runtime by MAKO's loader (`loadLs1ShaderSet`, vkd3d-shader) and kept in
memory only; the pass graph (source image, R8_SNORM feature image at 2x, two intermediates, descriptor layout, dispatch
order, barriers) follows MAKO's `Ls1Pipeline` -> GPL-3.0-or-later. Vulkan compute, not GL, so no shared-EGL caveat. The
translator loaded here without installing anything (if it ever does not: Arch package `vkd3d`).
- Test (800x672 UI shrunk 2x, mean |err| vs original): **LS1 quality 7.38, LS1 performance 7.91**, NIS 7.73, MAKO 8.26,
  FSR 8.27, bilinear 10.23 - LS1 quality is the best of all of them here.
- Speed: 1.7 ms at 800x672; **6.3 ms (quality) / 5.8 ms (performance) at 1720x720 -> 3440x1440**, comparable to MAKO Scaler
  (7.6) and RTX VSR (5-10), faster than NIS (17) / FSR (20).

### 2026-09-20: NVIDIA DLSS Frame Generation WORKS under Wine (framegen method "dlssg")

`framegen/nsdlssg.py` (backend "NVIDIA DLSS-G (Wine bridge)") drives `worker_dlssg/dlssg_host.exe` (`dlssg_host.cpp`; mmap files +
control block like `vsr_host`; args `w h count in out ctrl dir`; NV12 output converted to RGBA in the host, BT.709 limited range)
around Merserk's `neuroframe_engine_frame_interpolation.dll` (ABI `fi_*`, from `frame_interpolation/native.py`): CUDA + D3D12 +
NVOF optical flow + NGX `DLSSG.*`. It builds motion/depth inputs itself from two colour frames. Verified: a 12 px/frame scene gave
an interpolated frame matching a 78 px shift exactly (between 72 and 84), colour range/matrix correct (mean 61.1 = 61.1).
Four things had to be fixed, in this order (each error message pointed at the next one):
1. **D3D12 shared heaps** (`CreateSharedHandle failed`): the bridge aliases one buffer between D3D12 and CUDA via
   `CreateHeap(SHARED)` -> `CreateSharedHandle(heap)` -> `cuImportExternalMemory`. vkd3d-proton only shared committed textures/fences.
   `patches/vkd3d-proton-shared-heaps.patch` (base commit in `vkd3d-proton.base`): heaps with `D3D12_HEAP_FLAG_SHARED` are allocated
   with `VkExportMemoryAllocateInfo` (which also disables suballocation) and `CreateSharedHandle` gets a heap branch. Built with
   meson + mingw (`--cross-file build-win64.txt`, submodules fetched) and dropped next to the host as `d3d12.dll` /
   `d3d12core.dll` (application directory wins for native overrides, the prefix is untouched).
2. **CUDA external memory** (`cuImportExternalMemory ... 801`): libcuda on Linux takes opaque fds only, the bridge passes an NT
   handle. Wine's Vulkan layer exports memory as a D3DKMT "shared resource", not an fd object (`wine_server_handle_to_fd` gives
   STATUS_OBJECT_TYPE_MISMATCH). The shim (`nvcuda_shim/gen.py`, unix side) now resolves win32u.so's non-exported
   `d3dkmt_open_resource` / `d3dkmt_object_get_fd` through the ELF symbol table (so it survives Proton rebuilds as long as
   win32u.so is not stripped), opens the handle, gets the fd and imports it as OPAQUE_FD. `NVCUDA_TRACE=1` prints the translation.
3. **HLSL compiler** (`temporal shader down ... GetDimensions is not defined on RWTexture2D<float>`): Wine's built-in
   d3dcompiler_47 (vkd3d-shader) lacks that method. A real Microsoft `d3dcompiler_47.dll` (fetched with `winetricks -q
   d3dcompiler_47` in a throw-away prefix) sits next to the host.
4. **NVOF input format** (`NVIDIA Optical Flow SLOW initialization failed:` with an empty reason): the bridge scans the NVOF
   D3D12 input-format list for 87 = `DXGI_FORMAT_B8G8R8A8_UNORM`; dxvk-nvapi only advertised R8_UNORM (its Vulkan session already
   supports ABGR8). `patches/dxvk-nvapi-nvof-bgra.patch` makes `GetSurfaceFormatCount/GetSurfaceFormatD3D12` report
   {R8_UNORM, NV12, B8G8R8A8_UNORM} for inputs; the built `nvofapi64.dll` sits next to the host.
Host environment: `WINEDLLOVERRIDES=dxgi,d3d11,d3d12,d3d12core,nvcuda,d3dcompiler_47,nvofapi64=n`, `WINEDLLPATH=wine_nvcuda`.
- **Cost (x2, per real frame, includes the caller's np.roll ~ 4-10 ms):** 13 ms at 800x672, 18 ms at 1720x720, **51 ms at 3440x1440** -
  the bridge itself is 2.5-3 ms, the rest is moving 19 MB frames (write in, CPU NV12->RGBA in the host, read out). Fine at working
  resolution, too slow at full display size as the last stage - LSFG/MAKO (13 ms there) stays the better default. Ideas: OpenMP
  colour conversion in the host, skip the RGBA round trip.
- Start-up 1-4 s. `multiplier` maps to `count = multiplier - 1` (max 4); uniform spacing only.

### 2026-09-20: frame generation on its own thread; why DLSS-G cost 40 ms

User report: with frame generation 30 fps shown, without 55. Cause: (1) the DLSS-G backend costs ~34-40 ms per real frame at
1668x1398, and (2) generation ran in the SAME thread as the upscale/convert stage, so it added to it (real ~19 fps -> x2 ~38).
- **Threading:** `UpscalerHost` now hands each real frame to a dedicated frame-generation thread (`_fg_run`; one-slot queue, the
  thread owns the backend). Generation overlaps capture / DLSS5 / upscale of the next frame; if it is still busy the frame is shown
  without generated ones (FIRST version - it caused visible judder, see next paragraph). Headless (~24 fps source, x2): blend 39.6 fps,
  MAKO 38.6 fps shown vs 19.8 without.
- **Judder fix:** (1) the hand-over is now a blocking put (0.25 s timeout) - when generation is the slowest stage the pipeline settles
  at ITS rate with a steady cadence, instead of showing some frames without generated ones (irregular spacing); (2) display times are
  anchored to when generation STARTS on a frame plus a constant latency (1.15 x smoothed generation time + 4 ms), not to when it happened
  to finish (finish times jitter) nor to the enqueue time (made everything 'late' -> bursts). Test with a fake 35 ms backend and a 50 fps
  source: gap std 18.4 ms (bursts) -> 1.7 ms.
- **DLSS-G breakdown at 1668x1398** (`DLSSG_PROF=1` prints host timings): bridge `process` 8.7 ms (NGX 5.5), then
  `fi_surface_copy_to_host` 15.5 ms - it waits for the still-running DLSS-G GPU work, so this is mostly GPU time, not PCIe -
  and colour conversion 2.7 ms (was ~17 ms single-threaded float; now integer maths over up to 8 threads). Net 34 ms per frame:
  (first measurement; the 'waits for GPU' reading was WRONG, see the next section.)

### 2026-09-20: DLSS-G 34 -> ~11 ms: the readback was 2100 synchronous cuMemcpyDtoH calls

Probe: delaying `fi_surface_copy_to_host` by 40 ms did not change its own time, so it was not waiting for the GPU. `NVCUDA_TRACE=1`
showed the bridge reads NV12 back **row by row**: ~2100 `cuMemcpyDtoH_v2` calls per frame (1398 Y + 699 UV rows), ~7 us each through
the PE -> unix call. Fix in the shim (`nvcuda_shim/gen.py`): `cuMemcpyDtoH_v2` for reads <= 1 MB does a read-ahead - one
`cuMemGetAddressRange` to learn the allocation bounds, one bulk copy of up to 8 MB into a shim buffer, then serves the following rows from
it; any other forwarded call (kernel launch, memset, H2D, sync ...) drops the cache. `NVCUDA_NOCACHE=1` disables it. Output is
bit-identical with the cache on/off. Result at 1668x1398, x2, back-to-back frames: `copy_to_host` 15.5 -> 0.6 ms, `submit` wall
34 -> ~11 ms (host: process 6.0 ms of which NGX 4.7, flow 0.1). Remaining cost: NGX ~5 ms, colour conversion ~2-3 ms, IPC ~2 ms.
`nvcuda_shim/build.sh` regenerates + builds the shim, re-applies the Wine builtin signature (bytes 0x40..0x60 of the deployed PE; without
it Wine does not load the unix half and `cuInit` returns 801) and deploys it. Beware: interrupted benches leave orphan
`dlssg_host.exe` processes holding the GPU - kill them before timing.

## Upscaling the shrunk-for-speed frame back up: NIS, not bilinear

`nis/` — a from-scratch GLES fragment-shader port of **NVIDIA Image
Scaling (NIS)**'s `NVScaler` algorithm
(`github.com/NVIDIAGameWorks/NVIDIAImageScaling`, MIT license) - an
edge-adaptive single-frame scaler+sharpener, NOT a neural network (no App
ID/model-authorization gate, no Wine needed at all - the coefficient
"weights" are small closed-form filter tables, copied verbatim into
`nis/nis_coef.h`). Ported from the original's shared-memory compute shader
into a plain per-pixel fragment shader (each output pixel independently
re-derives its own 6x6 neighborhood via `texelFetch` instead of the
original's cooperative tile caching) - correctness-preserving, just not
maximally cache-efficient; fine at our resolutions. `nis/nis_lib.c` builds
to `nis/libnisupscale.so`, wrapped by `nis_upscale.py`'s `NisUpscaler`
(same ctypes-library pattern as `kms_capture.py`).

**Why**: DLSSNR's own "upscale" mode (`NS_NR_SMALL`, see above) doesn't
reduce capture/IPC/NGX-compute cost - the protocol always wants a
full-output-resolution color frame regardless. Genuinely shrinking the
frame BEFORE it ever reaches capture+DLSS, then upscaling the *result* back
up ourselves, reduces cost at every stage - but a naive bilinear/nearest
upscale afterward looks soft/blocky. NIS is the right tool for exactly
this: a fast, non-neural, single-frame scaler+sharpener, designed for
"cheaply restore a shrunk frame," not "add detail a temporal/game-integrated
network would need motion vectors for" (which is what classic DLSS Super
Resolution is, and doesn't fit a screen-capture pipeline - see the
notes on this tradeoff).

**Wired into `live_filter.py`**: `NS_NIS_FACTOR=4` (linear shrink factor,
independent of and mutually exclusive with `NS_WORK_SCALE`) runs capture +
DLSS at `1/4` the `NS_MAX_DIM`-derived display size, then `NisUpscaler`
restores the full size before showing the frame.
`NS_NIS_SHARPNESS` (default `0.5`) matches NIS's own 0-1 sharpness slider.

**Confirmed real Vivaldi window, `NS_USE_KMS=1 NS_USE_SHM=1
NS_NIS_FACTOR=4 NS_MAX_DIM=1600`: ~54fps steady-state** at a 1600x1328
*displayed* size (working DLSS resolution only 400x332) - `ngx-wait` down
to ~7ms/frame (a quarter-linear/16th-area frame is cheap to run DLSSNR on)
plus `nis` at ~10ms/frame (upscaling to the much larger 1600x1328 output).
Visually confirmed correct and readable on real video content (not just
synthetic test patterns) - see "the darkening bug" below for the one real
issue hit and fixed along the way.

**A visual quality check against plain bilinear** (shrink 4x, upscale back
with each method) on a real captured frame: NIS is visibly sharper -
sidebar search-box text stays legible, building/block edges stay crisp,
where bilinear noticeably softens both. Confirms the port reproduces NIS's
real behavior, not just "compiles and runs."

### The darkening bug: EGL contexts are current-on-a-THREAD, and there can be more than one

The first full-pipeline test produced badly crushed-to-black output (mean
pixel value dropped ~100x, e.g. 2.2/255 -> 0.02/255) even though capture and
DLSS stages both reported normal-looking, consistent brightness right up
to the NIS step. **Two synthetic isolated tests of `NisUpscaler` alone
(random noise, and a uniform flat-dark image) both worked perfectly** -
which was the useful clue: the bug wasn't in the NIS port's *math* at all,
it only appeared once `KmsCapture` and `NisUpscaler` were used
**interleaved on the same thread** in the same process.

Root cause: `KmsCapture` and `NisUpscaler` each hold their **own** EGL
context (each library calls `eglMakeCurrent` once, at init). EGL contexts
are current-on-a-*thread*, not current-on-a-*process* - calling
`cap.capture_region(...)` makes KMS's context current on that thread;
the *next* call to `nis.upscale(...)` then ran with KMS's context still
current, not NIS's own, because `nis_upscale()` never re-asserted it. GL
calls under the wrong context don't raise an error - they silently operate
on whatever object IDs exist in that OTHER context's namespace (each
context numbers its own textures/programs starting near 1, so IDs
collide numerically between unrelated contexts), producing wrong-but-not-
obviously-broken output instead of a crash. This is the same *category* of
bug as the earlier `KmsCapture`-in-`live_filter.py`-thread bug (see below),
one level more subtle: that one was wrong-thread, this one is
right-thread-wrong-context.

**Fix, applied to both libraries**: `eglMakeCurrent(self->dpy, self->surf,
self->surf, self->ctx)` at the very start of every per-frame call
(`kms_capture_get_frame()` and `nis_upscale()`), not just once at init -
makes each library robust to whatever other EGL-using code shares its
thread. **Rule of thumb for any future EGL/GLES library here**: never
assume your context is still current just because you made it current
once - re-assert it every time you're about to issue GL calls, unless
you're certain nothing else on that thread also touches EGL.

### Still not done

- Only grabs the `PRIMARY` plane per monitor — cursor/overlay plane
  compositing (`KMS_PLANE_TYPE_CURSOR`/`_OVERLAY` items in the response)
  isn't implemented; not yet verified whether Hyprland already composites
  the cursor into the primary plane or needs it drawn separately.
- `kms_probe.c` (the original standalone benchmark) is superseded by
  `kms_capture_lib.c`/`kms_capture.py` for actual use but kept around as a
  simpler single-file reference/smoke-test.

## Capture: wlr-screencopy, not PipeWire

Tried switching Track 1's capture from `wlr-screencopy` to
`org.freedesktop.portal.ScreenCast` + PipeWire (`track1/pw_capture.py`, kept
as working reference code with two real bugs fixed along the way - see
comments in the file and the "ninth pass" notes). The per-call fetch cost
dropped ~5000x (0.002ms vs wlr-screencopy's fixed ~11-19ms/call), but
Hyprland's own portal backend (`xdg-desktop-portal-hyprland` 1.4.1) only
pushes new frames at a flat, hardcoded **2fps** - confirmed for both window
and monitor capture, with actively-changing content, so it's not a
damage-tracking artifact. 2fps is far worse than wlr-screencopy's already-
working ~40-70fps. **Stick with wlr-screencopy on this compositor** - only
worth revisiting `pw_capture.py` if testing on a different compositor whose
portal implementation doesn't have this cap.

## Track 1 (screen-capture live filter)

`track1/live_filter.py` + `track1/wl_capture.py` — the screen-capture live
filter (captures an arbitrary window via wlr-screencopy, runs it through the
worker here, shows the result in a layer-shell preview). Confirmed working
2026-09-17 on a real Vivaldi window.

```bash
./setup_prefix.sh
cd track1 && python3 live_filter.py vivaldi-stable   # or another window class
```

Two env knobs, both tested:
- `NS_MAX_DIM` (default 800): the actual capture/work/output resolution
  (longest edge). **This is the real FPS lever** — steady-state ~40fps at
  800x508, ~70fps at 400x254 (a quarter the pixels) on this GPU, `write`
  time roughly tracking pixel count. Trades resolution for speed directly.
- `NS_WORK_SCALE` (e.g. `0.5`): uses DLSSNR's own built-in upscale
  (`NS_NR_SMALL`) to run the network at a smaller internal resolution while
  keeping the OUTPUT at the full `NS_MAX_DIM` size. Only a small win
  (~5-8%, from less NGX compute) — **does not** reduce `write` time, because
  the wire protocol always wants the color frame at output resolution
  regardless of this flag (checked directly in `dlss5-feed-host64.cpp`).
  Can't be combined with `NS_MAX_DIM` to get "small pipe payload + big
  output" - the protocol doesn't allow that combination.

Note on reading the printed FPS/timing lines: they're cumulative averages
(`total/n`), so every run shows FPS "climbing" for the first ~500 frames -
that's `CreateFeature`'s one-time ~400-500ms setup cost getting diluted into
the average, not the worker warming up. Only the tail of a long run reflects
real steady-state per-frame cost.

Uses the plain pipe protocol (see "SHM transport" below for why, not the
dormant SHM path).

# NeuralScreen DLSS5 under Wine — the working recipe

Confirmed working 2026-09-17. `probe.py` sends 8 synthetic frames through the
worker and all 8 come back with real, non-echoed DLSS5 Neural Rendering
output (`NVSDK_NGX_D3D12_Init` / `Init_Ext` / `CreateFeature(18)` all
`Success`, 444MB VRAM allocated for the model, verified per-pixel output that
differs frame to frame — genuine neural output, not a passthrough).

## The one rule that matters

**Never mix DXVK and vkd3d-proton from different sources.** System Wine's own
DXVK/vkd3d-proton, or a hand-assembled combo (e.g. a separately downloaded
DXVK release paired with the distro `vkd3d-proton-mingw-git` package), breaks
NGX's internal D3D12/DXGI interop checks — `D3D12CreateDevice` still looks
fine (device creates, DXR/SM6.9 report correctly), but `NVSDK_NGX_D3D12_Init`
fails with a generic `0xBAD00001` every time, and no amount of caller-identity
spoofing, forwarder tricks, or directory-naming games fixes it. This cost an
entire session to rediscover.

The fix: use **one Proton build's own bundled `wine` binary**, pointed at a
prefix that Proton itself already set up with its own matched DXVK+vkd3d-proton
trio (dxgi.dll + d3d12.dll + d3d12core.dll, same release, same build).

## Quick start

```bash
./setup_prefix.sh          # copies a real, already-Proton-initialized prefix
                            # (default: NieR Replicant's, appid 1113560) into
                            # ./pfx — never touches the source
python3 probe.py            # runs the worker, sends 8 frames, checks the replies
```

If NieR Replicant (or whatever the default source prefix is) ever gets
uninstalled, pass a different already-initialized Proton prefix as
`./setup_prefix.sh /path/to/some/other/compatdata/<appid>/pfx`. It doesn't
need to be the same game the worker will eventually run alongside — any
prefix from a Proton build with a real DXVK+vkd3d-proton pair works, since
the worker never touches the game itself.

**Don't point `WINEPREFIX` straight at a live game's compatdata directory.**
Running wine against it directly can trigger wineboot's version-mismatch
auto-repair. It refused to overwrite existing files when this was tried
(`error=80`, harmless) — but treat that as luck, not a guarantee. Always copy
first.

`setup_prefix.sh` defaults to Proton-Experimental. Proton's own
`files/share/default_pfx/` template exists as a lighter-weight alternative
but wasn't gotten working this session (missing `dosdevices/c:` symlink,
`wineboot --init` on it hit "could not load kernel32.dll") — copying a real
game's prefix is what actually worked, so that's the default.

## worker/ contents

- `nvngx.dll` — NOT actually a DLL (`IMAGE_FILE_DLL` bit unset in its PE
  header) — it's `dlss5-feed-host64.cpp` compiled to a console EXE and
  deliberately named `nvngx.dll` on disk. `nvngx_dlssnr.dll`'s own internal
  caller-identity check requires the calling module's path to contain the
  substring `"nvngx.dll"` (see `native/ns_forwarder.cpp` in the NeuralScreen
  source for the measured proof) — this naming is what satisfies that, no
  extra tricks needed once the Wine/DXVK/vkd3d-proton environment is correct.
- `nvngx_dlssnr.dll` — the real model (165MB). Not shipped here: the user obtains it themselves (see `user_files/README.md`).
- `Spout.dll` / `SpoutDX.dll` — hard runtime dependencies of the worker build; the worker will not load without them next to it.

## The wire protocol (stdin/stdout)

`probe.py` speaks a small subset of NeuralScreen's own `protocol.py`
(`dlss5_converter`-side module) — see
`HEADER_FMT`/`FRAME_FMT`/`OUT_FMT` in `probe.py` for the exact struct
layouts. The full protocol (described below) also
defines a dormant, never-yet-exercised-by-us SHM transport (`SHMI`/`SACK` for
input, `OUTS`/`OAK2` for output, via Win32 named file mappings) that avoids
shipping frame bytes through the pipe at all — worth using instead of the
raw pipe path for anything performance-sensitive (Track 1's live filter,
Track 2's in-game injection).

## SHM transport: working, not yet wired into live_filter.py

`shm_bridge.cpp`/`.exe` is a small Wine-side helper that lets a native Linux
process satisfy the worker's `SHMI`/`OUTS` shared-memory path (avoids
sending frame bytes through the pipe): it registers a named section
(`CreateFileMappingA(hFile, ..., name)`) backed by a real file under
`Z:\tmp\...` (== `/tmp/...`), so wineserver can resolve the *name* for the
worker's `OpenFileMappingA` while a plain Linux `mmap()` on the same file
handles the actual bytes with no wineserver protocol involved.

It looked flaky at first (worked once, failed 3 times) - the real bug turned
out to be that the bridge idled on stdin closing, and every launch method
tried (shell `&`, even the agent harness's own background-command mode) gave
it a stdin that was already at EOF, so it exited right after printing
`READY`, before anything used its sections. Fixed by idling on a sentinel
file (`<in_path>.quit`) instead of stdin. **Now verified reliable: 4/4 clean
runs**, full round trip (`SHMI` -> `OUTS` -> 5 frames) with real NGX output
returned entirely through shared memory (zero pixel bytes over the pipe
either direction). See `shm_test_full.py` for the working repro/test
harness and the memory file's "seventh pass" for the full story.

**Wired into `live_filter.py`**: set `NS_USE_SHM=1`. Confirmed on a real
Vivaldi window at 800x508: **~45.7fps steady-state vs ~40fps over the plain
pipe** (same resolution, same window) - `write` drops to ~0.2ms/frame
(from ~6-13ms over the pipe), and the bottleneck moves to screen capture
(`queue-wait`, ~14ms) rather than the worker. Use a ~2s delay between
sending the video-stream header and sending `SHMI`/`OUTS` (already wired
in) - a shorter delay (0.5s) hit one failure in 3 runs even with the bridge
bug fixed, likely a smaller, separate wineserver-registration-propagation
delay.

## What this does NOT unblock

The **native**, no-Wine-at-all path (`/usr/lib/libnvidia-ngx.so`, see the
memory file's "Track 3") is a separate thing and still needs a real
NVIDIA-issued App ID — this recipe fixes the Wine path specifically, it
doesn't touch that blocker.

## Backlog (2026-09-20)

- **DLSS-G further speed-ups** (after the 34 -> ~11 ms readback fix): keep NV12 end-to-end to the presenter (skip RGBA conversion,
  ~2-3 ms); run DLSS-G at the working (pre-upscale) resolution; reduce NGX work per frame (~5 ms, the main remainder).
- **MAKO bridge overhead:** per-call command buffers/fences and polling.
- **KMS capture without feedback:** see the 2026-09-20 KMS section (in progress).

## Proxy desktop (`NS_PROXY=1`, 2026-09-20)

`NS_UI=1 NS_PROXY=1 python3 live_filter.py vivaldi-stable` - the source app is moved to a **headless Hyprland output** (`NSPROXY`,
sized like the window, fullscreen there) and the filtered picture is shown in an ordinary window (`ns-dlss-proxy`, class set through
`GLib.set_prgname`) on the real desktop, which can be moved/resized/put on any workspace. Code: `track1/proxy_desktop.py`
(`ProxyDesktop`, `ProxyCapture`, `InputForwarder`, `ProxyWindow`).
- **Why:** output-level screencopy is fast (headless 1600x1400: ~7.6 ms per capture vs ~11 ms on the physical output; 4 threads ->
  the capture is no longer the bottleneck) but sees everything scanned out, so the result cannot sit over the source (feedback, the
  reason KMS/screencopy were rejected for the overlay). On a headless output the source is fully alive (frame callbacks, input) and
  invisible. Also: no click-through hacks, normal window semantics for the result. The `beside` overlay placement (`NS_CAPTURE=kms`)
  remains as the simpler alternative.
- **Pointer:** the proxy window forwards motion/buttons/scroll through a uinput ABSOLUTE pointer (`/dev/uinput`, python-evdev): warp onto
  the source's pixel (exact - verified 1:1 against `hyprctl cursorpos`), event, warp back (2 ms). During a drag (button held) the
  pointer stays on the source and returns to where it started on release. Hover is throttled to 90 Hz; the proxy ignores its own
  pointer events for 30 ms after a warp-back so it does not feed on them.
- **Keyboard:** not forwarded. A window rule (`no_focus`, class `ns-dlss-proxy`) keeps keyboard focus off the proxy, so after the first
  click on it the source has focus and typing goes straight to it.
- **Hyprland 0.56 Lua API used:** `hyprctl eval 'hl.dispatch(hl.dsp.window.move({window="address:..", workspace="name:nsproxy", follow=false}))'`,
  `hl.dsp.focus{monitor|workspace|window}`, `hl.dsp.window.fullscreen{mode="fullscreen"}` (a TOGGLE), `hl.monitor{output,mode,position,scale}`
  (re-applying a monitor rule shifted the physical monitor - `engage()` puts it back), `hl.window_rule{match={class=..}, no_focus=true}`,
  `hyprctl output create|remove headless NSPROXY`. Cursor position through the raw command socket (`j/cursorpos`, ~0.1 ms).
- **Safety:** state (address / workspace / monitor) is kept in `~/.cache/ns-proxy-state.json`; SIGTERM/SIGINT/exit/toggle-off restore the
  app and remove the output; after a hard crash `python3 proxy_desktop.py restore`. The filter hotkey (SUPER+SHIFT+F10) in this mode
  means "app back on the real desktop" / "back to the proxy". The user's `hypr-user.lua` treats an extra output as "external monitor"
  (`has_external_monitor()` disables eDP-1) - irrelevant with DP-1 connected, but keep in mind.

- **Backlog: proxy desktop nuances** (2026-09-20, mode works, parked): window interaction details - focus handling beyond the first click,
  drag return position (pointer returns to where the drag started), hover/cursor shape and cursor visibility, IME/keyboard layouts,
  popups/menus/tooltips that live outside the source window's surface, resize of the proxy vs the fixed source size, multi-window apps,
  fractional-scale outputs, compare-divider drag, and the interplay with the user's `hypr-user.lua` monitor logic.

### 2026-09-20: MAKO bridge overhead (backlog item done)

Profile (`NSMAKO_PROF=1` prints per-stage ms of `nsmako_submit`, average of 30) at 1668x1398, flow 0.5: x2 = 5.5 ms per real frame,
x4 = 12.5 ms - already cheap (upload 1.6, GPU wait ~1, readback 2.2 per generated frame). Two changes:
- **Pipelined readback:** one host buffer per generated frame and all `vkCmdCopyImageToBuffer`s submitted at once (each ordered after its
  timeline value, own fence); the CPU memcpy of frame j overlaps the GPU copy of frame j+1. Readback x4: 6.8 -> 3.6 ms.
- **Output ring in `nsmako.py`:** the C side writes straight into the next of 6 preallocated (pages pre-touched) arrays and frames are
  returned as views - no second 9 MB `.copy()` per frame. Consumers convert them to CairoFrame right away, so 6 submits of life is plenty.
Result: x2 5.5 -> 5.0 ms, x4 12.5 -> 8.1 ms. Per-call command buffers/fences were NOT the cost (upload is the remaining ~1.6 ms, mostly the
9 MB write-combined memcpy). The waitReady 200 us poll costs ~0.1 ms on average - left alone. Backlog item closed.

## Hyprland plugin `nsproxy` (started 2026-09-20; step 1: fault shield)

`hyprplug/` - a Hyprland plugin (C++, built against the installed 0.56.2 headers: `cd hyprplug/build && cmake .. -G Ninja && ninja`) that will
replace the proxy-desktop hack: read the target window's texture inside the compositor, feed it to our external pipeline, draw the result
in place of the window (right z-order, native input/focus, no hidden output, no feedback).
**Requirement from the user: a plugin failure must not take the shell down - it must report an error.** Design:
- **Thin plugin, heavy external process.** The plugin only exports/imports pixels (shared memory + unix socket); all DLSS/upscale/FG logic stays in
  our own process. If that process dies or hangs (no result within ~250 ms), the plugin stops drawing and the original window is shown.
- **Fault shield (`hyprplug/src/guard.hpp`).** Every entry point (render-stage listener, hyprctl command, later socket handlers) runs in
  `Guard::run()`: C++ exceptions caught; SIGSEGV/SIGBUS/SIGFPE/SIGILL/SIGABRT raised inside a guarded section are turned into a report by a
  `sigsetjmp`/`siglongjmp` handler (signals outside a guard are chained to Hyprland's own crash reporter); a circuit breaker disables the plugin
  after 3 exceptions in 10 s or at once after a signal. Reports: `~/.cache/nsproxy-plugin.log` + an on-screen Hyprland notification.
  `hyprctl nsproxy status | reset | selftest <throw|throw_int|segv|trap|abort|render_*>` (the `render_*` ones fault inside the real render path).
  Limits: it cannot repair memory a fault already corrupted (a wild write into Hyprland's heap can still crash later) - hence "keep the plugin small".
- **No function hooks** (fragile across versions) - only the event bus, decorations and hyprctl commands; the plugin refuses to load on an
  API-hash mismatch (clean error, not a crash) - rebuild after each Hyprland update.
- **Safe development:** everything is tested in a NESTED Hyprland window (`hyprplug/nested/`, `hyprplug/test_shield.sh` starts one and injects every
  fault kind). ALWAYS use `hyprctl -i <nested signature>` - a bare `hyprctl` talks to the real session.
- **Verified (nested):** throw / throw of a non-exception / SIGSEGV / SIGILL / SIGABRT, each both in a hyprctl command and inside the render stage
  listener: compositor survives, plugin reports and goes idle; 3 exceptions within 10 s trip the breaker; `reset` re-arms.
- **Next:** window texture export (GL -> shared memory ring, PBO), result import + draw at RENDER_POST_WINDOW clipped to the window, watchdog
  fallback, then the external-process side (reuse UpscalerHost). Input needs nothing: the window stays where it is.

### nsproxy plugin, step 2 (2026-09-21): the pixel path works end to end (tested in a nested Hyprland)

`NS_UI=1 NS_PLUGIN=1 python3 live_filter.py <class>` (with the plugin loaded: `hyprctl plugin load <project>/hyprplug/build/libnsproxy.so`).
`live_filter` runs `hyprctl nsproxy attach <class>`, connects to `$XDG_RUNTIME_DIR/nsproxy.sock`, and the compositor does the rest.
- **Export:** a custom render-pass element (`CNsElement`, `EK_CUSTOM`) queued at `RENDER_POST_WINDOW` for the target window reads the window's
  rectangle out of the monitor framebuffer with `glReadPixels` into a 3-deep PBO ring (async, no GPU stall) and copies it (RGBA8, TOP-DOWN -
  Hyprland's framebuffers are already top-down; flipping was wrong) into a shared-memory slot; a wake-up byte goes down the socket. Throttled to
  `maxFps` (120); unchanged frames are not published (memcmp), and a few extra repaints (`flush`) make the async readback deliver the last change.
- **Result:** `plugin_bridge.PluginOverlay.update_frame()` publishes the `CairoFrame` (BGRA premultiplied) into a result slot; the plugin uploads it to
  a `CGLTexture(DRM_FORMAT_ARGB8888, ...)` and draws it over the window with `renderTexturePrimitive()` in the same pass element, right AFTER the read.
  (`renderTexture()` drew nothing from a pass element; the primitive path works.) Layer-shell surfaces (the settings panel) stay on top - correct z-order
  for free; popups of the source window too. Compare mode is composed in Python (raw left of the divider).
- **Feedback trap found and fixed:** Hyprland repaints only DAMAGED regions - elsewhere the framebuffer still holds our previous overlay, and reading
  it back produced a "tunnel" (the pipeline eating its own output). Fix: at `RENDER_PRE_WINDOW` for the target window the whole window box is added to the
  frame damage (`m_renderData.damage.add(box)`), so under our element there is always the real window. (Later option: read the client's buffer texture directly.)
- **Watchdog:** a result older than 300 ms is ignored (the real window shows through); a dropped socket switches the override off; `detach`/exit removes it.
- **Crash found by the nested test (outside the shield!):** pass elements outlive their frame; on `plugin unload` Hyprland destroyed a `CNsElement` whose
  vtable was already unloaded -> SIGSEGV in `CRenderPass::clear()`. Fix: `PLUGIN_EXIT` and `detach` call `m_renderPass.removeAllOfType("nsproxy")`. Verified with
  5 load/attach/unload cycles under gdb. Rule: nothing with code from the plugin may stay referenced by the compositor after unload.
- **Test in the nested compositor** (`hyprplug/nested/`; run the app with `WAYLAND_DISPLAY=<nested display> HYPRLAND_INSTANCE_SIGNATURE=<nested sig>` so every
  `hyprctl` goes to it): a Gtk test window with a known pattern -> exported pixels verified (red/green/blue/white positions and RGBA order), an inverted
  result published from Python appeared on screen (red->cyan etc.), and the full DLSS5 pipeline ran on it (~915 results in 25 s, no faults).
- **Not yet done:** run in the real session; real-window behaviour (video, popups, fractional scale, workspace-slide animation offsets, multi-monitor
  scale); the hot-path cost in the compositor (PBO memcpy 6 MB per exported frame on the render thread); reading the client texture instead of the framebuffer.

### nsproxy plugin, step 3 (2026-09-21): isolated capture of the CLIENT's buffer (no more framebuffer readback)
Feedback from the first real-session run: "the effect is like KMS" - right, step 2 read the monitor framebuffer (the final composited frame), which
contains our own previous result, the panel, opacity/blur rules. Now `exportFrame()` takes the window's OWN buffer: `window->wlSurface()->resource()->m_current.texture`
(the client's texture: RGBA/RGBX 2D or EGLImage `GL_TEXTURE_EXTERNAL_OES`) is drawn by a tiny private shader (fullscreen triangle, `gl_VertexID`) into a private
RGBA8 FBO of the buffer's size, which is read through the same 3-deep PBO ring; every GL state item touched is saved and restored (program, FBOs, VAO, viewport,
active texture/binding, pack buffer, blend/scissor/depth/cull/stencil). Row 0 of the client texture is the image top, so no flip. Verified in the nested
compositor: exports are bit-exact pattern, and with a magenta override published over the window not a single re-export happens (before, the override leaked back in).
Limits: only the MAIN surface (subsurfaces - e.g. some video overlays - and popups are not in the export; popups are still drawn on top of the result by
the compositor); buffer transforms/viewporter cropping are ignored; the result is drawn stretched over the window box.
- **Static windows kept losing the effect:** the 300 ms watchdog treated "no new frame -> no new result" as a dead pipeline. `PluginLink` now has a heartbeat thread
  that refreshes `res_ns` every 50 ms unless a received frame has gone unanswered for 0.4 s (a real stall).

### nsproxy plugin, step 4 (2026-09-21): the result is drawn with the window's own rounded corners, inside the compositor's border
User report: "the render overlaps the Hyprland border". The border itself was intact, but the result was a plain rectangle, whose square corners poked over the rounded
border corners. Now the override is drawn by our own shader (`drawRounded`): a quad over the window's surface box, coverage from a rounded-rect SDF with the window's
`rounding()` (x monitor scale) as radius and 1 px antialiasing, premultiplied blend; borders/shadows drawn by Hyprland stay visible around it. Pitfalls found on the way:
- **Damage/scissor:** an own draw must honour the compositor's damage like its elements do - the scissor left over from the previous element was a 42 px strip, so only that strip
  showed. Now: one scissored draw per rect of `m_renderData.damage` (monitor pixels, row 0 = top), intersected with the box; scissor state restored.
- **Black texture:** a fresh `CGLTexture` has the GL default mipmapped MIN_FILTER and samples as black until `setTexParameter(MIN/MAG_FILTER, LINEAR)` (+ clamp wrap) is set after `bind()`.
- Save/restore around the draw: program, VAO, active texture + binding, blend enable/func, cull/depth/stencil enables, colour mask, scissor. Falls back to `renderTexturePrimitive` if the
  shader cannot be built. `NSPROXY_DEBUG=1` (compositor env) logs the first draw's viewport/box/scissor/state.
Verified in the nested compositor with a 6 px border and rounding 24: yellow/cyan/magenta inverted pattern, correct orientation, corners follow the border.
Limit: only the circular radius; a `rounding_power` (squircle) setting is ignored.

**Status 2026-09-21:** the nsproxy plugin path (isolated client-buffer capture, rounded-corner override, heartbeat watchdog) is confirmed working in the real session by the user.
Backlog: subsurfaces/popups in the export, buffer transform/viewporter, rounding_power, PBO memcpy cost on the render thread, hot-reload safety, KMS/proxy-desktop modes kept as alternatives.

### nsproxy plugin backlog pass (2026-09-21): items 1, 5, 2 done
**1. Export cost on the compositor's render thread** (measured with `avg export / avg draw` in `hyprctl nsproxy status`; animated 2508x1388 window, 61 fps):
before ~1.5 ms export + 1.4 ms draw per frame. Changes: (a) change detection by the surface's own `commit` signal (`res->m_events.commit`) instead of a 14 MB `memcmp`
per frame; reads are issued only for new commits, extra repaints only while a read is in flight (`inFlight`); (b) **GPU-side downscale**: the pipeline sets
`want_w/want_h` in the shared header (`PluginLink.set_export_size`, called from the capture loop with the slider's work size) and the copy shader renders straight to that
size - export cost native 1.39 ms -> half 0.31 ms -> third 0.16 ms, and Python no longer does the PIL resize. Remaining fixed cost: the result upload (~1.3 ms for a 14 MB
result at native size); a zero-copy path (udmabuf / EGL dma-buf import of the shared memory) is the next idea if it ever matters.
**5. Hot reload:** `hyprplug/test_reload.sh N` runs a nested Hyprland under gdb with an animated window and N x (load, attach, client streaming, unload WHILE streaming): 10 cycles, 0 signals.
`hyprplug/test_shield.sh` (fault injection) still passes. (Gotcha for these scripts: never `pkill -f`/`pgrep -f` with a pattern that the tool's own command line contains - it kills the tool shell, exit 144.)
**2. Subsurfaces:** the export is now the whole surface TREE composited (own shader, premultiplied blending, painted below-subsurfaces -> parent -> above-subsurfaces, recursive,
positions from `posRelativeToParent`/`m_position` scaled logical -> export pixels; RGBX textures forced opaque). Chromium-style video planes below a parent with a transparent hole
are now in the picture. Verified with `hyprplug/nested/subwin.py` (parent with a hole + a red/green plane below + a yellow tag above): the export shows all three in place.
Still not in the export: xdg popups (the compositor draws them over our result anyway), buffer transforms/viewporter cropping, `rounding_power`.

**Status 2026-09-21:** backlog pass (export cost, hot-reload test, subsurfaces) confirmed working in the real session by the user. Remaining plugin backlog: buffer transforms/viewporter, rounding_power, optional udmabuf zero-copy.

### nsproxy plugin: rounding_power, buffer transforms, viewporter (2026-09-21)
- **`rounding_power`:** the override's corner is now `pow(pow(vx,p)+pow(vy,p), 1/p)` against the radius (Hyprland's own formula), p = `window->roundingPower()`; p = 2 is the circle used before.
- **Buffer transform / viewporter:** the export quads sample the client texture through a per-surface UV transform (`wl_output_transform` 0..7 as a 2x2 matrix + offset) and the `wp_viewport`
  source rect. Direction convention (found the hard way): a buffer transform describes how the BUFFER was rotated relative to the surface, so the compositor applies the inverse -
  transform 90 (buffer rotated CCW) = surface is the buffer rotated CW (`u = b, v = 1 - a`); 270 the other way; the flipped forms compose with the flip. Export canvas size = the surface
  as Hyprland sizes it (logical size x buffer scale, or the viewport source rect in px). `hyprplug/nested/test_xform.sh` (fresh nested compositor per case, client `xformwin.py` with a 4-colour
  buffer): all 8 transforms + a viewport crop match - even transforms/crop bit-exactly against what the compositor itself draws; odd (90/270) ones against the protocol definition, because
  Hyprland 0.56 draws them distorted (it stretches the rotated texture into the unswapped size), so it cannot serve as the reference there. A rotated viewport crop could not be tested (Hyprland
  rejects the source rect against the unrotated buffer and kills the client).
- **Bug found by the test:** a NEW pipeline connection (or a re-attach) to a static window got no first frame, because "nothing committed since the last read" - the bridge now counts connections
  (`connectSeq`) and the export resets its commit tracking on a new one.

### nsproxy plugin: zero-copy in both directions via udmabuf (2026-09-21)
Protocol v2: every slot row starts on a 256-byte pitch (`pitchFor(w)`), both sides; the shared memfd is created sealable (`F_SEAL_SHRINK|GROW`) so `/dev/udmabuf` (user ACL: `getfacl /dev/udmabuf`)
can wrap each slot in a dma-buf (`UDMABUF_CREATE`, cached fds in `Bridge::slotDmabuf`).
- **Result upload:** the result slot dma-buf is imported as an EGLImage (`EGL_LINUX_DMA_BUF_EXT`, ARGB8888, linear) and sampled directly by the override shader - no `glTexSubImage`.
- **Export:** the surface-tree composite is rendered straight into an EGLImage-backed FBO over an export slot (ABGR8888 = R,G,B,A bytes), a `GLsync` fence marks completion, and the slot is published
  when the fence has signalled (at most 2 frames in flight; the newest finished one wins) - no PBO ring, no memcpy, no `glReadPixels`.
- **Fallback:** any failure (no udmabuf, import/FBO error) logs it and permanently switches that direction back to the copying path; `NSPROXY_ZEROCOPY=0` (compositor environment) forces the copying path.
- **Coherency:** works on this machine (Intel iGPU renders the desktop; system-memory dma-bufs are coherent), verified by content: an identity pipeline over an animated window - every exported frame contains the
  exact 200x200 square, no torn/stale frames, no position jumps. A discrete-GPU compositor would need `DMA_BUF_IOCTL_SYNC` around CPU access - not needed here.
- **Numbers** (animated 2508x1388 window, identity pipeline, `hyprplug/nested/bench_zc.sh`): avg export 1586 us -> 196 us, avg draw 1104 us -> 39 us per compositor frame; pipeline throughput 48.5 -> 57 fps.
  Full DLSS5 pipeline in the nested compositor: export 155 us / draw 18 us.
- The Python side hands the pipeline its own copy of each export (the slot is rewritten two exports later) - that copy is CPU work in the pipeline process, not in the compositor.
- Regression scripts (all nested, all pass): `test_shield.sh`, `test_reload.sh 15`, `nested/test_xform.sh`, `nested/bench_zc.sh`.
All planned plugin backlog items are done.

**Status 2026-09-21:** zero-copy (udmabuf) plugin path, rounding_power, buffer transforms/viewporter confirmed working in the real session by the user. Plugin backlog is empty.

### DLSS-G speed-up pass (2026-09-21)
Baseline at 1668x1398, x2 (per real frame): host `process` 6.0 ms (NGX ~4.7), `copy_to_host` 0.6 ms (after the shim fix), NV12->RGBA 2.1-3.5 ms, Python write-in ~1 ms + 9 MB `.copy()` of the
output + **`CairoFrame.from_rgba` 4.4 ms per generated frame in the frame-generation thread** (channel shuffle in numpy) -> ~16 ms of FG-thread time. Changes:
- **BGRA straight from the host** (`DLSSG_BGRA=1`): the NV12 conversion writes cairo's B,G,R,A order, so the generated frames need no channel shuffle: `CairoFrame.from_bgra()` wraps the buffer
  without copying (`FrameGenBackend.output_bgra = True` tells `_fg_run`). -4.4 ms per generated frame.
- **Output ring in the shared file** (`DLSSG_RING=4`): the host writes request k's frames into region `k % 4` and Python hands out views into it - no `.copy()`. Valid for 3 further submits, far
  longer than the presenter holds a frame.
- **AVX2 NV12 conversion** (`conv_rows_avx2`, runtime-checked, `DLSSG_NO_AVX2=1` disables): 8 pixels per iteration in 32-bit lanes with the scalar formulas - output bit-identical to the scalar
  code (verified) - plus a persistent Windows thread-pool instead of 8 `CreateThread`s per frame: conversion 2.9 -> 1.2 ms. (More threads did not help the scalar code: 1 thread 6.6 ms,
  2 threads 3.3, 4-8 ~3.4.)
- **Tried and dropped:** a wake-up socket so an idle host blocks instead of polling `Sleep(1)`. AF_UNIX is not supported by this Wine (WSAEAFNOSUPPORT 10047); loopback TCP worked but gave no
  measurable gain (9.4 ms with and without), so the code was removed.
Result (synthetic 1668x1398 x2, 30 ms gaps like a real pipeline): `submit` wall 11.2 -> 9.4 ms and the FG thread no longer spends 4.4 ms converting -> ~16 -> ~9.4 ms per real frame. In the full
pipeline (nested compositor, animated 1548x848 window, DLSS-G x2 through the plugin, `NS_FRAMEGEN=dlssg:2` preset): `framegen` 11.3 ms per real frame, 46.8 real -> 92.0 fps shown.
Remaining cost is NGX itself (~4.7 ms of the 6 ms `process`) + the GPU->host copy; what is left to try: DLSS-G at the working (pre-upscale) resolution, less NGX work per frame, an NV12
result path converted by the compositor plugin's shader (moves the 1.2 ms + the BGRA write off the CPU).
Gotcha: never run `pkill -f`/`pgrep -f` patterns that occur in the command line of the tool shell itself (also matched: `[d]lssg_host` inside a command that mentions `dlssg_host.cpp`); a leftover
`dlssg_host.exe` from a crashed benchmark makes the next DLSS-G session hang at creation - kill stale ones by PID before timing.

## FSR 3.1 frame generation (2026-09-21) - `framegen` method "fsr3"

Request: extend the "no engine vectors" approach that made DLSS-G work to the FSR 3/4 line. Findings from the FidelityFX SDK 2.3.0 "Redstone" (`third_party/fidelityfx-sdk`, MIT; cloned at v2.3.0):
- **FSR 4 (ML upscaler 4.1.1 and ML frame generation 4.0.1) needs AMD hardware** (RDNA4 matrix units / an AMD driver extension) - it cannot run on this NVIDIA GPU, so it is out. **FSR 3.1 frame generation** is compute-shader based and vendor independent -> done.
  The FSR 3.1 *upscaler* is temporal (needs motion vectors + jitter); for a screen filter it would only smear, so it is not offered (the existing spatial FSR 1 upscaler stays).
- The SDK ships signed **DX12** DLLs (`Kits/FidelityFX/signedbin/amd_fidelityfx_framegeneration_dx12.dll`, 40 MB, exports `ffxCreateContext/Configure/Dispatch/Destroy/Query`); the SDK itself says Vulkan is unsupported in 2.x.
  They load and run under Wine on vkd3d-proton (the patched d3d12.dll already in `worker_dlssg`): probe `fsr3/probe.cpp` -> `ffxCreateContext(FRAMEGENERATION)` rc=0.
- **Host:** `fsr3/fsr3fg_host.cpp` (mingw, `x86_64-w64-mingw32-g++ -O2 -std=c++17 -mavx2 -mf16c -I<sdk>/api/include -I<sdk>/framegeneration/include ... -static -ldxguid`) -> `worker_fsr3/fsr3fg_host.exe`
  (+ the FG dll, patched d3d12/d3d12core, d3dcompiler_47). Same file/control protocol as the DLSS-G host, so `framegen/nsfsr3.py` is a 10-line subclass of `DlssgFrameGen` (class attributes EXE / WORKER / DLL_OVERRIDES / MAX_COUNT; the DLSS-G wrapper was
  generalised for it). Plain D3D12: upload buffer -> RGBA8 texture (ping-pong pair), depth + motion-vector planes, `ffxConfigure` + `ffxDispatch(PREPARE_V2)` + `ffxDispatch(FRAMEGENERATION)` on one command list, output texture -> readback -> BGRA into the shared output ring
  (AVX2 R/B swap). No swapchain (`FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY`). Manual: `fsr3/run_wine.sh`.
- **Gotchas found by reading the SDK provider source** (`framegeneration/fsr3/internal/ffx_provider_fsr3framegeneration.cpp`): (1) `ffxConfigure(frameID)` must be called EVERY frame with the frame's id - the dispatch treats
  `frameID != lastConfigureFrameID` as a discontinuity and resets, which made the "generated" frame an exact copy of the current one; (2) `FrameIndexSinceLastReset() < 10` forces the game-vector path for the first 10 frames (measure after warm-up);
  (3) this provider interpolates ONE frame (the midpoint, `outputs[0]`) per dispatch -> **x2 only** (`MAX_COUNT = 1`).
- **How it works without engine data:** the interpolated colour is `lerp(optical-flow colour, game-vector colour, bias)`, bias from colour similarity, so with a constant depth plane (0.5) and zero motion vectors FSR falls back to its own optical flow where it matters.
  Measured on natural content (MAD vs the true midpoint, 8-bit units, steady state): camera pan blend 6.8 / FSR3 4.9 (ghosting); moving object blend 1.2 / FSR3 0.72.
- **Global-motion hint (`nsfsr3._GlobalMotion`):** phase correlation of the 1/4-scale green channel (Hann window, sub-pixel peak refinement, ~4 ms) gives a global translation between consecutive frames; the host turns it into a uniform vector field (`Ctrl.mv_x/mv_y`,
  half-float plane at 1/4 render size, sign `FSR3_MV_SIGN=-1` = from the current pixel back to its previous position; uploaded only when it changes). FSR then trusts its game-vector path where it matches (camera pans, page scrolling) and its optical flow elsewhere. Guards: peak
  confidence >= 0.08, a 1 px dead zone, and the peak must dominate the zero-shift correlation (otherwise a moving object on a static background produced one bad frame). Result: **pan 4.9 -> 0.56**, object 0.72 (unchanged). `NS_FSR3_MOTION=0` disables the hint.
- **Backend comparison** (same test, MAD vs true midpoint, lower is better; 1280x720): pan - blend 6.4, FSR3+hint 0.56, DLSS-G 0.80, MAKO/LSFG 1.12; object - blend 1.2, FSR3+hint 0.72, DLSS-G 0.92, MAKO 0.46. (FSR3 without the hint: 4.9 / 0.72.)
- **Cost** (1668x1398, x2, per real frame): host CPU in+record 0.9 ms, GPU+wait 15.5 ms, readback+convert 0.8 ms, Python phase correlation 4.1 ms -> ~18.8 ms wall (DLSS-G 9.4, MAKO 5) - the GPU compute of FSR's optical flow + inpainting is the bulk. 1280x720: ~5.4 ms.
  Depth/MV at full display size made it 26 ms with no quality gain - hence the 1/4 render size (`FSR3_RENDER_DIV`, default 4).
- **In the app:** panel "Frame generation" method list now has "AMD FSR 3.1 frame generation (x2 only, Wine)"; `NS_FRAMEGEN=fsr3:2` presets it. End-to-end in the nested compositor through the Hyprland plugin: `framegen` 6.5 ms at 1548x848, 44.7 real -> 89.0 fps shown.
- Not done / ideas: run the optical flow at reduced resolution (the GPU passes dominate); a block-wise (not only global) motion hint for scenes with several motions; the FSR 3.1 upscaler with estimated vectors; HDR (`FFX_FRAMEGENERATION_ENABLE_HIGH_DYNAMIC_RANGE`, PQ/scRGB transfer functions).

### Plugin mode: choosing / switching the target window (2026-09-21)
Report: capturing a Steam window "only attaches to the terminal". Causes and fixes:
- `live_filter.py` without an argument takes the ACTIVE window as the target - the terminal it was started from - and in plugin mode the panel's target picker changed only `state.target_class`, which the plugin never saw. Now `retarget_plugin()`
  (in the 300 ms geometry poll) runs `hyprctl nsproxy attach <class>` when the picked class changes and keeps the old target with a status message if the attach fails.
- Matching: `nsproxy attach` (plugin `findWindow`) and `get_window_geometry` now prefer an EXACT class match over a substring one (`steam` = the client, `steam_app_204030` = the game) and the plugin accepts `attach address:0x...`.
- Fullscreen games: Hyprland may put a fullscreen window on the screen directly (direct scan-out), bypassing the render pass the plugin hooks; while attached the plugin now sets `m_directScanoutBlocked` (only clears it if it was the one that set it).
- `hyprplug/nested/run_retarget.sh` re-attaches between two windows while the pipeline streams: attach A -> B -> A, unknown class is rejected, client stays connected.
Usage: `NS_UI=1 NS_PLUGIN=1 python3 live_filter.py steam_app_204030` (or any class from `hyprctl clients`), or start with any window and pick the target in the panel.

### BUG FIX 2026-09-21: horizontal stripe trails on motion = zero-copy paths were not cache-coherent
Report: "on any application horizontal interference like in Assassin's/Cyberpunk, a short trail of horizontal stripes when the picture moves". Cause: the udmabuf zero-copy paths (introduced with "zero-copy in both
directions") on this machine are NOT coherent between the GPU and the CPU caches: the GPU reads/writes the slot memory without snooping CPU caches, so plain CPU reads returned stale cache lines of the previous frame in
the slot (export direction) and plain CPU writes sat in the cache while the GPU read the old memory (result direction) - bands of rows from an older frame. My earlier "integrity" test only counted pixels of a moving square,
which does not change when rows come from two frames; it passed. Reproduced with a strict per-row check (`nested/sink_check.py`: every row of the exported 200x200 square must start at the same x; `nested/run_result_tear.sh`:
25 screenshots of a moving bar): zero-copy export torn 458/495 frames, zero-copy result torn 25/25 screenshots; the copying paths 0.
Confirmed by experiment: `clflush` of the slot before publishing removed the export tearing (but cost 3 ms on the compositor thread).
**Fix (keeps the zero-copy benefit, moves the cache work to the pipeline process):** `track1/nsmem/nsmem.c` (built on first use by `plugin_bridge`, `libnsmem.so`):
`nsmem_copy_rows_evict` - copy rows out of an export slot into a private array, then `clflushopt` the slot's lines (the GPU rewrites the slot later); `nsmem_nt_copy_rows` - non-temporal AVX2 stores into a result slot (they bypass the CPU
cache), `sfence`. `PluginLink.wait_frame()` now returns a private copy made that way and `publish()` uses the non-temporal copy. Protocol: the client sets `Header.client_flags` bit 0 when it uses the helpers; the plugin uses its zero-copy
paths only for such a client (otherwise the copying PBO/upload paths - also the fallback if no compiler is available, `NS_NSMEM=0` simulates it). Result after the fix (zero-copy on): export TORN 0 of 496, result TORN 0 of 25,
avg export 60 us / draw 19 us per compositor frame (better than before); helper disabled: 0 torn, 275/250 us. `test_xform.sh` and `test_reload.sh` still pass.
Lesson: udmabuf/dma-buf sharing between a CPU process and the iGPU needs explicit cache management here (or DMA_BUF_IOCTL_SYNC where the exporter implements it - udmabuf's is a no-op on x86); test tearing per row, not by pixel counts.

### Two windows of one class (2026-09-21): "it does not attach Vivaldi"
The real session had TWO `vivaldi-stable` windows: workspace 1 (hidden, 3328x1378) and workspace 3 (visible, 1668x1398, the one with the video). `nsproxy attach vivaldi-stable` and `get_window_geometry` took the FIRST match in Hyprland's
list = the hidden window, which is never rendered -> no frames. Now: plugin `findWindow` scores candidates (visible workspace +4, exact class +2), `live_filter.resolve_window()` ranks (visible workspace first, then most recently focused;
`address:0x..` accepted) and `live_filter` attaches the plugin BY ADDRESS and pins `state.target_class` to that address so the geometry poll follows the same window; the attach reply says `address:0x.. (visible)` or `(HIDDEN workspace)`.
Test: `hyprplug/nested/run_dup.sh` (two windows of one class, the first on a hidden workspace).
