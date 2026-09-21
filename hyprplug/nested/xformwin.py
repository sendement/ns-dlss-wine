# SPDX-License-Identifier: MIT
# Test client: buffer 300x200 (TL red, TR green, BL blue, BR white), wl_surface.set_buffer_transform(argv[1]);
# optional argv[2]="crop": wp_viewporter source = right half of the (transformed) surface, destination unchanged aspect.
import mmap, os, sys
import numpy as np
from pywayland.client import Display
from pywayland.protocol.wayland import WlCompositor, WlShm
from pywayland.protocol.xdg_shell import XdgWmBase
from pywayland.protocol.viewporter import WpViewporter
T = int(sys.argv[1]); CROP = len(sys.argv) > 2 and sys.argv[2] == "crop"
BW, BH = 300, 200
d = Display(); d.connect(); reg = d.get_registry(); g = {}
def on_global(r, name, iface, ver):
    if iface == "wl_compositor": g["comp"] = r.bind(name, WlCompositor, 4)
    elif iface == "wl_shm": g["shm"] = r.bind(name, WlShm, 1)
    elif iface == "xdg_wm_base": g["xdg"] = r.bind(name, XdgWmBase, 1)
    elif iface == "wp_viewporter": g["vpt"] = r.bind(name, WpViewporter, 1)
reg.dispatcher["global"] = on_global; d.roundtrip(); d.roundtrip()
img = np.zeros((BH, BW, 4), np.uint8)
img[:BH//2, :BW//2] = (0, 0, 255, 255); img[:BH//2, BW//2:] = (0, 255, 0, 255)       # BGRA: red | green
img[BH//2:, :BW//2] = (255, 0, 0, 255); img[BH//2:, BW//2:] = (255, 255, 255, 255)   # blue | white
fd = os.memfd_create("b"); os.ftruncate(fd, BW*BH*4); mm = mmap.mmap(fd, BW*BH*4); mm.write(img.tobytes())
pool = g["shm"].create_pool(fd, BW*BH*4); buf = pool.create_buffer(0, BW, BH, BW*4, 0)
s = g["comp"].create_surface(); xs = g["xdg"].get_xdg_surface(s); top = xs.get_toplevel(); top.set_title("xform"); top.set_app_id("xform")
g["xdg"].dispatcher["ping"] = lambda wm, serial: wm.pong(serial)
tw, th = (BH, BW) if T & 1 else (BW, BH)     # surface-space size after the transform
def configure(x, serial):
    x.ack_configure(serial); s.set_buffer_transform(T)
    if CROP:
        v = g["vpt"].get_viewport(s); v.set_source(tw // 2, 0, tw // 2, th); v.set_destination(tw // 2, th)
    s.attach(buf, 0, 0); s.commit()
xs.dispatcher["configure"] = configure
s.commit()
while True: d.dispatch(block=True)
