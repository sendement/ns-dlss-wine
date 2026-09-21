# Module protocol (v1)

Optional backends (frame generators, upscalers) can live **outside this repository** - in their own repository, under their own license - and are plugged in
without any code of theirs being imported or linked by the core: the core only reads a manifest and starts the module's executables, which exchange frames with
it through memory-mapped files. That process boundary is what keeps the core independent of the module's license (this document is the whole interface: a module
is written against it, not against the core's code).

## Layout and discovery
A module is a folder containing `module.json`. The core looks in `modules/*/` (next to this repository's `app/`), in every folder of `NS_MODULES_PATH`
(colon separated) and in `~/.local/share/ns-dlss/modules/*/`. `tools/fetch_modules.sh` clones known modules into `modules/`. A module whose manifest is invalid,
whose executables are missing, or whose `check` command fails is skipped and the reason is shown by `python3 app/modules.py`.

```json
{
  "module_version": 1,
  "name": "mako",
  "title": "MAKO (Lossless Scaling)",
  "license": "GPL-3.0-or-later",
  "check": ["bin/mako_check"],
  "framegen": [
    {"key": "mako", "title": "Lossless Scaling FG (MAKO)", "exec": ["bin/mako_fg_host"], "max_count": 3, "output": "rgba", "supports_timestamps": true}
  ],
  "upscalers": [
    {"key": "mako_scaler", "title": "MAKO Scaler", "exec": ["bin/mako_scale_host"],
     "settings": [{"key": "sharpness", "label": "Sharpness", "kind": "float", "default": 0.8, "min": 0.0, "max": 1.0, "step": 0.05}]}
  ]
}
```
* `exec` is relative to the module folder; the process runs with that folder as its working directory.
* `check` (optional) is run with no arguments; exit status 0 = the module's own requirements are met, otherwise its stdout/stderr (first line) is the reason.
* `output` = pixel order of generated frames: `rgba` or `bgra` (cairo order; saves the caller a shuffle).
* `settings` entries are `key`, `label`, `kind` (`float`|`int`|`bool`), `default`, `min`, `max`, `step`; their values reach the host as `key=value` arguments.
  Changing a setting or the frame size restarts the host.
* A key must not collide with a built-in backend (`none`, `nis`, `fsr`, `rtx_vsr`, `mako` is *not* built in, `dlssg`, `fsr3`, `blend`).

## Control file
All numbers are little-endian, all offsets in bytes. 192 bytes, created (zeroed) by the core, memory-mapped by both sides.

| offset | field | writer | meaning |
|---|---|---|---|
| 0 | `state` | host | 0 starting, 1 ready, 2 error (the host exits after writing 2) |
| 4 | `req_seq` | core | number of the newest request (1, 2, 3, ...) |
| 8 | `ack_seq` | host | number of the newest request that is finished; requests are answered in order |
| 12 | `ok` | host | upscalers: 1 if the last request succeeded |
| 16 | `quit` | core | 1 = exit |
| 64 + 20*s | request slot `s = seq % 2` (frame generators): `u32 flags` (bit0 = reset: first frame / discontinuity), `u32 nts`, `f32 ts[3]` | core | filled **before** `req_seq` is bumped |
| 104 + 8*k | result slot `k = seq % 4` (frame generators): `u32 gen` (frames written), `u32 ok` | host | filled **before** `ack_seq` is bumped |

Both sides must issue a memory barrier between filling a slot and bumping the sequence number. The host may poll (spin then sleep a millisecond).

## Frame generators
`<exec...> WIDTH HEIGHT MAX_COUNT IN OUT CTL [key=value ...]`
* `IN`: two RGBA8 frames back to back (`WIDTH*HEIGHT*4` bytes each); request `seq` uses frame slot `seq % 2`.
* `OUT`: ring of 4 regions of `MAX_COUNT` frames; request `seq` writes its frames into region `seq % 4`, frame `i` at `(region*MAX_COUNT + i) * WIDTH*HEIGHT*4`.
* Request `seq` carries the newest real frame; the host returns the frames to show **between the previous real frame and this one**, one per timestamp `ts[0..nts)`
  (strictly increasing, in (0,1); `nts <= MAX_COUNT`; the core sends uniform positions unless the manifest says `supports_timestamps`, in which case they may be
  arbitrary). `nts = 0` only advances the temporal history. The first frame (and any frame with the reset flag) produces `gen = 0`.
* At most two requests are in flight: the core waits for `ack_seq >= seq - 2` before it writes slot `seq % 2` again, so a host may overlap the CPU work of one frame with the GPU work of the previous one.
* Generated frames stay valid until the host writes region `seq % 4` again (three later requests).
* Extra `key=value` arguments: `flow_scale`, `performance`, and any custom option declared in the manifest.

## Upscalers
`<exec...> IN_WIDTH IN_HEIGHT OUT_WIDTH OUT_HEIGHT IN OUT CTL [key=value ...]`
* `IN`: one RGBA8 frame; `OUT`: one RGBA8 frame (alpha 255). The core writes `IN`, bumps `req_seq` and waits for `ack_seq`; `ok` tells whether the frame is valid.
* One request at a time; the host is restarted on any size or setting change.

## Startup
The core starts the host, then waits (up to 90 s) for `state == 1`. A host that fails writes `state = 2` and a one-line reason to stderr (shown with `NS_MODULE_LOG=1`).
