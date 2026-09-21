# SPDX-License-Identifier: MIT
# Identity pipeline + STRICT integrity check for animwin: the exported 200x200 red square must be one rigid block - every row of it must start at the same x
# (a torn frame = rows from two different frames = different x per row band). Also publishes the frame back so the result path can be checked on screen.
import sys, time, numpy as np
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'app'))
from plugin_bridge import PluginLink
L = PluginLink(); n = torn = missing = 0; t0 = time.time()
while time.time() - t0 < float(sys.argv[1]):
    try: a, seq = L.wait_frame(1.0)
    except TimeoutError: continue
    n += 1
    red = (a[..., 0] == 255) & (a[..., 1] == 0) & (a[..., 2] == 0)
    rows = np.where(red.any(1))[0]
    if len(rows) < 150: missing += 1
    else:
        left = np.array([np.argmax(red[r]) for r in rows])
        if left.max() != left.min(): torn += 1
    bgra = np.empty_like(a); bgra[..., 0] = a[..., 2]; bgra[..., 1] = a[..., 1]; bgra[..., 2] = a[..., 0]; bgra[..., 3] = 255
    L.publish(bgra); L.set_override(True)
dt = time.time() - t0
print(f"frames {n} ({n/dt:.1f} fps), TORN {torn}, missing-square {missing}")
L.close()
