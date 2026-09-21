# SPDX-License-Identifier: MIT
import gi, math, time; gi.require_version("Gtk","3.0")
from gi.repository import Gtk, GLib
class W(Gtk.Window):
    def __init__(s):
        super().__init__(title="anim"); s.set_default_size(1600,1200); a=Gtk.DrawingArea(); a.connect("draw",s.d); s.add(a); s.a=a
        s.set_wmclass("animwin","animwin"); GLib.timeout_add(16, lambda: (a.queue_draw(), True)[1])
    def d(s,a,cr):
        w,h=a.get_allocated_width(),a.get_allocated_height(); t=time.time()
        cr.set_source_rgb(.1,.1,.1); cr.paint()
        x=(math.sin(t*2)*.4+.5)*w
        cr.set_source_rgb(1,0,0); cr.rectangle(x-100,h/2-100,200,200); cr.fill()
        cr.set_source_rgb(0,1,0); cr.rectangle(0,0,w*((t%2)/2),30); cr.fill()
w=W(); w.connect("destroy",Gtk.main_quit); w.show_all(); Gtk.main()
