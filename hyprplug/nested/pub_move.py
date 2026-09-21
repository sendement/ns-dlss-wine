# SPDX-License-Identifier: MIT
# Result-path tearing test: publish full-window frames with a white vertical bar moving right at ~60 fps; the compositor must always show a RIGID bar.
import sys, time, numpy as np
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'app'))
from plugin_bridge import PluginLink
L = PluginLink(); a, _ = L.wait_frame(3.0); h, w = a.shape[:2]
L.set_override(True); t0 = time.time(); i = 0
frame = np.zeros((h, w, 4), np.uint8); frame[..., 3] = 255; frame[..., :3] = 40
while time.time() - t0 < float(sys.argv[1]):
    f = frame.copy(); x = (i * 16) % (w - 60); f[:, x:x + 40, :3] = 255
    L.publish(f); i += 1
    time.sleep(max(0.0, t0 + i / 60.0 - time.time()))
L.close()
