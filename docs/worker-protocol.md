# Neural render worker protocol (v1)

The role this protocol describes: a process that takes one real-time RGBA8 frame at a time and returns a processed RGBA8 frame - denoised, reconstructed,
optionally reconstructed at a larger size - with its working size and a small set of tunables changeable **live**, without a process restart. NVIDIA DLSS5
neural rendering is the motivating case (and the only implementation this project ships an adapter for today: `hosts/worker_adapter.cpp`, described below),
but nothing here is DLSS-specific - a denoiser, a style filter, or a "pass the frame through unchanged" test worker can all speak it.

Like `docs/module-protocol.md`, frames cross the process boundary through memory-mapped files; the core does not link or import worker code.

## Process contract
```
<exec...> MAX_W MAX_H MAX_OUT_W MAX_OUT_H IN OUT CTL [key=value ...]
```
* `MAX_W`/`MAX_H`, `MAX_OUT_W`/`MAX_OUT_H`: the largest work size and output size the core will ever request of this process instance (its mmap regions are
  sized once, at start-up, from these - a live resize within that ceiling doesn't need new files; a resize beyond it needs a new process).
* `IN`: one RGBA8 frame, `MAX_W * MAX_H * 4` bytes.
* `OUT`: one RGBA8 frame, `MAX_OUT_W * MAX_OUT_H * 4` bytes.
* `CTL`: 128 bytes, created zeroed by the core, memory-mapped by both sides.
* `key=value`: worker-specific start-up-only options (a model-variant hint that's cached on load and can't change live, a runtime path, a device name, ...).
  A generic client never needs to know these exist; a specific worker documents the ones it reads.

## Control block
All numbers little-endian. Both sides issue a memory barrier between writing a field and writing the sequence number that makes it visible.

| offset | field | writer | meaning |
|---|---|---|---|
| 0 | `state` (u32) | worker | 0 starting, 1 ready (buffers allocated; waiting for the first reconfigure - see below), 2 error |
| 4 | `req_seq` (u32) | core | bumped after filling the frame in `IN` (mode 0) or the reconfigure slot (mode 1) |
| 8 | `req_mode` (u32) | core | 0 = process the frame in `IN`; 1 = apply the reconfigure slot |
| 12 | `ack_seq` (u32) | worker | highest `req_seq` completed; requests are answered strictly in order |
| 16 | `ok` (u32) | worker | 1 = the completed request succeeded; 0 = frame dropped (e.g. still warming up) or reconfigure rejected |
| 20 | `code` (u32) | worker | opaque diagnostic code for the last request (0 = nothing to report); shown in logs, never interpreted by the core |
| 24 | quit (u32) | core | 1 = exit |
| 28 | `out_w` (u32) | worker | the CURRENT output width, echoed after every completed reconfigure (a worker may clamp what it was asked for) |
| 32 | `out_h` (u32) | worker | ditto, height |
| 36 | `work_w` (u32) | core | reconfigure slot: new work width |
| 40 | `work_h` (u32) | core | reconfigure slot: new work height |
| 44 | `req_out_w` (u32) | core | reconfigure slot: requested output width |
| 48 | `req_out_h` (u32) | core | reconfigure slot: requested output height |
| 52 | `warmup` (u32) | core | reconfigure slot: frames of history to prime before the first real output (0 = worker's own default) |
| 56 | `flags` (u32) | core | reconfigure slot: bit0 = try to keep temporal history (best-effort; a worker that always resets on reconfigure may ignore it) |
| 60 | `style` (u32) | core | reconfigure slot: tunable, worker-defined meaning (e.g. a named look/preset index) |
| 64 | `auto_mask` (u32) | core | reconfigure slot: tunable, worker-defined (e.g. 0/1 toggle) |
| 68 | `ui_correction` (u32) | core | reconfigure slot: tunable, worker-defined |
| 72 | `intensity` (f32) | core | reconfigure slot: tunable, worker-defined strength, typically 0..1+ |
| 76 | `local_tone` (f32) | core | reconfigure slot: tunable, worker-defined |
| 80 | `local_structure` (f32) | core | reconfigure slot: tunable, worker-defined |
| 84 | `skin_structure` (f32) | core | reconfigure slot: tunable, worker-defined |

The seven tunables (`style` .. `skin_structure`) are a fixed, named set so a generic settings panel can expose sliders for them without knowing which worker
is behind the protocol; a worker with no equivalent knob just ignores the ones it doesn't use. `MAX_W`/`MAX_H`/`MAX_OUT_W`/`MAX_OUT_H` bound every later
reconfigure - asking for something larger fails that request with `ok = 0`.

## Sequencing
Exactly one request is ever in flight (frame or reconfigure) - the core waits for `ack_seq == req_seq` before it fills the next one. **The very first request
after `state` becomes 1 must be a reconfigure** (mode 1): that is how the worker learns its initial size and tunables and actually creates whatever internal
feature/context it needs; a worker that receives a frame request first should fail it. A later reconfigure (a live resize, or a parameter change from the
settings panel) works the same way, mid-stream, without restarting the process - expect it to be far cheaper than a restart but not free (a visible hitch is
normal; callers should debounce continuous controls like a slider, not call reconfigure on every tick).

A frame request (mode 0) expects `IN` to hold one RGBA8 frame at the CURRENT `work_w`/`work_h` (the ones set by the last successful reconfigure). On
`ok = 1`, `OUT` holds one RGBA8 frame at `out_w`/`out_h` (as echoed in the control block); on `ok = 0` no frame was produced (e.g. the worker is still
priming its temporal history) and the core keeps showing the previous one.

## Reference adapter: NeuralScreen
`hosts/worker_adapter.cpp` (MIT, this repository) implements this open protocol on the outward-facing side and, internally, speaks NeuralScreen's own DLSS5
worker wire format on stdin/stdout to a child process (`nvngx.dll --live`, PolyForm Strict 1.0.0, user-supplied - see `user_files/README.md`). That inner
format was learned by reading the worker's source and is described here only as facts about the interface, not reproduced as code: a fixed-size
header/resize command (magic, width, height, two mode-dependent fields, then the same seven tunables as above plus a width/height pair used only when the
worker should reconstruct at a larger size than it was fed), a live resize command that reuses the header's exact layout and gets a small acknowledgement
back, and a per-frame exchange of a small header followed by the raw pixel and (always-zero, in this project) motion-vector bytes, answered with a header
followed by the raw output pixels. The child process is started lazily, on the first reconfigure - that is the first point at which its work/output sizes
are known, and one of its start-only options depends on whether they differ. The adapter owns all of that; nothing upstream of it needs to know
NeuralScreen exists. A different worker that speaks the open protocol directly needs no adapter and no Wine.
