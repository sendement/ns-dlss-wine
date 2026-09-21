# SPDX-License-Identifier: MIT
# Wayland test client with subsurfaces: dark-blue parent with a transparent hole, a red/green subsurface BELOW it (like an overlaid
# video plane), and a yellow subsurface ABOVE. Static content; class "subwin".
import mmap, os, sys, time
import numpy as np
from pywayland.client import Display
from pywayland.protocol.wayland import WlCompositor, WlShm, WlSubcompositor
from pywayland.protocol.xdg_shell import XdgWmBase

W, H = 800, 600
d = Display(); d.connect(); reg = d.get_registry()
g = {}
def on_global(r, name, iface, ver):
    if iface == "wl_compositor": g["comp"] = r.bind(name, WlCompositor, 4)
    elif iface == "wl_shm": g["shm"] = r.bind(name, WlShm, 1)
    elif iface == "wl_subcompositor": g["sub"] = r.bind(name, WlSubcompositor, 1)
    elif iface == "xdg_wm_base": g["xdg"] = r.bind(name, XdgWmBase, 1)
reg.dispatcher["global"] = on_global; d.roundtrip(); d.roundtrip()
keep = []
def buf(w, h, arr):   # ARGB8888 premultiplied
    fd = os.memfd_create("b"); os.ftruncate(fd, w * h * 4); mm = mmap.mmap(fd, w * h * 4); mm.write(arr.tobytes())
    pool = g["shm"].create_pool(fd, w * h * 4); b = pool.create_buffer(0, w, h, w * 4, 0)   # 0 = ARGB8888
    keep.extend([mm, pool, b]); return b
def solid(w, h, bgra): a = np.zeros((h, w, 4), np.uint8); a[:] = bgra; return a

main = np.zeros((H, W, 4), np.uint8); main[:] = (120, 30, 20, 255)          # B,G,R,A: dark blue
main[150:450, 200:600] = (0, 0, 0, 0)                                        # transparent hole
video = np.zeros((300, 400, 4), np.uint8); video[:, :200] = (0, 0, 255, 255); video[:, 200:] = (0, 255, 0, 255)   # red | green
tag = solid(100, 50, (0, 255, 255, 255))                                     # yellow

s_main = g["comp"].create_surface(); s_vid = g["comp"].create_surface(); s_tag = g["comp"].create_surface()
xs = g["xdg"].get_xdg_surface(s_main); top = xs.get_toplevel(); top.set_title("subwin"); top.set_app_id("subwin")
g["xdg"].dispatcher["ping"] = lambda wm, serial: wm.pong(serial)
def configure(x, serial): x.ack_configure(serial); s_main.attach(buf(W, H, main), 0, 0); s_main.commit()
xs.dispatcher["configure"] = configure
sub_v = g["sub"].get_subsurface(s_vid, s_main); sub_v.set_position(200, 150); sub_v.place_below(s_main); sub_v.set_desync()
sub_t = g["sub"].get_subsurface(s_tag, s_main); sub_t.set_position(10, 10); sub_t.set_desync()
s_vid.attach(buf(400, 300, video), 0, 0); s_vid.commit()
s_tag.attach(buf(100, 50, tag), 0, 0); s_tag.commit()
s_main.commit()
while True:
    d.dispatch(block=True)
