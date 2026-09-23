# ns-dlss-yt — DLSS5 / VSR / frame generation for YouTube

A browser extension (Chromium/Vivaldi, MV3) that crops the video to the player's aspect ratio, then optionally runs it through **DLSS5** (neural
reconstruction/denoise), **RTX VSR** (up/downscale) and/or **frame generation** — the same backends the live screen filter uses (`app/worker.py`,
`app/upscalers/`, `app/framegen/`), reached through a small local WebSocket bridge (`app/yt_bridge.py`). See `docs/worker-protocol.md` and the bridge's
own docstring for the wire protocol.

Pipeline: crop (in the page, free) → VSR downscale (own menu, an independent resize step - "Разрешение") → DLSS5 (processes whatever size it's handed,
1:1, no internal scaling of its own) → VSR upscale (back to the actual on-screen size, not the crop's native resolution) → frame generation. Any stage
is skippable from its own menu; the frame just flows through unchanged when off. DLSS5's own internal reconstruction was tried and dropped - it
corrupted its output for any non-1:1 ratio (see `app/yt_bridge.py`'s docstring) - VSR does the real up/downscale instead.

## Run it

1. Start the bridge (from the repository root):
   ```sh
   python3 app/yt_bridge.py            # listens on ws://127.0.0.1:8765
   ```
   It needs the `dlss5` feature ready (`python3 app/userfiles.py check dlss5`) and, for frame generation, `dlssg` (native, no extra setup) or another
   installed module. `NS_WORKER_LOG=1` / `NS_MODULE_LOG=1` on the bridge process show the backends' own log output if something doesn't come up.
2. Load the extension: `vivaldi://extensions` (or `chrome://extensions`) → enable *Developer mode* → *Load unpacked* → select this `extension/` folder.
3. Open a YouTube video, go fullscreen, click the new button in the player controls (top-right). An overlay appears over the video:
   - drag it to choose which part of the frame gets kept (only shown when the video's own aspect ratio doesn't already match the player);
   - the **DLSS5**, **VSR** and **Кадры** pill buttons open a menu each - a checkbox to enable that stage plus its sliders (DLSS5: style preset
     Дефолтный/Нейтральный/Кинематографичный plus intensity/tone/structure sliders; VSR: downscale "Разрешение" and "Детализация" quality);
   - **✓** applies the crop and starts the pipeline with whatever settings are set at that moment; **✕** cancels.
4. To change settings afterwards, click the button again (this turns the pipeline off), then once more to reopen the same overlay, adjust, and confirm.
   This mirrors the crop-adjustment step exactly - settings are only ever sent to the bridge once, on confirm, not while you're dragging a slider: the
   DLSS5/VSR backends rebuild their own worker process on a size change (seconds, not milliseconds), so live-updating on every slider tick would make
   the pipeline spend most of its time restarting instead of processing frames. Resizing the player window/tab (theater mode, fullscreen) alone doesn't
   need the overlay - VSR's upscale target follows the on-screen size automatically, debounced so a resize drag only reconfigures once it settles.

Settings (and the crop position, for videos with a matching aspect ratio, for the rest of the session) persist via `chrome.storage.local`.

## Notes
* DRM-protected streams can't be read into a canvas (`getImageData` throws) - the console says so; the extension falls back to doing nothing for those.
* One bridge connection per turned-on player; turning the button off closes it and restores the page.
* Console logging (`[ns-yt]` prefix, `F12` → Console) shows connection state and every `config_ack` - check there first if the picture isn't updating.
