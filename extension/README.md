# ns-dlss-yt — DLSS5 / frame generation for YouTube

A browser extension (Chromium/Vivaldi, MV3) that crops the video to the player's aspect ratio, then optionally runs it through **DLSS5** (neural
reconstruction, its own internal upscale) and/or **frame generation** — the same backends the live screen filter uses (`app/worker.py`, `app/framegen/`),
reached through a small local WebSocket bridge (`app/yt_bridge.py`). See `docs/worker-protocol.md` and the bridge's own docstring for the wire protocol.

Pipeline: crop (in the page, free) → DLSS5 (own internal upscale) → frame generation. Either stage is skippable from its own panel; the frame just flows
through unchanged when off.

## Run it

1. Start the bridge (from the repository root):
   ```sh
   python3 app/yt_bridge.py            # listens on ws://127.0.0.1:8765
   ```
   It needs the `dlss5` feature ready (`python3 app/userfiles.py check dlss5`) and, for frame generation, `dlssg` (native, no extra setup) or another
   installed module. `NS_WORKER_LOG=1` / `NS_MODULE_LOG=1` on the bridge process show the backends' own log output if something doesn't come up.
2. Load the extension: `vivaldi://extensions` (or `chrome://extensions`) → enable *Developer mode* → *Load unpacked* → select this `extension/` folder.
3. Open a YouTube video, go fullscreen, click the new button in the player controls (top-right) to turn cropping on, then use the **DLSS5** and **Кадры**
   panels next to it to enable each stage and adjust their sliders.

Settings persist per-browser-profile (`chrome.storage.local`), not per video.

## Notes
* DRM-protected streams can't be read into a canvas (`getImageData` throws) - the console says so; the extension falls back to doing nothing for those.
* The crop is always centred ("cover", no manual reposition) - unlike the sample extension this one started from, which let you drag the crop position.
* One bridge connection per turned-on player; turning the button off closes it and restores the page.
