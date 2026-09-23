// SPDX-License-Identifier: MIT
// MV3 requires a service_worker entry in the manifest, but there's nothing for it to do: content.js now owns the WebSocket to app/yt_bridge.py directly
// (see content.js's comment above SRC_HDR_BYTES for why the earlier background-relay design was dropped - it required base64-encoding every frame over
// chrome.runtime.Port, which became the actual throughput bottleneck once frames got large).
