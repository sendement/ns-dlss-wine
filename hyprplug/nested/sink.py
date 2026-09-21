# SPDX-License-Identifier: MIT
import sys,time,numpy as np
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'app'))
from plugin_bridge import PluginLink
L=PluginLink(); L.set_export_size(int(sys.argv[2]) if len(sys.argv)>2 else 0, int(sys.argv[3]) if len(sys.argv)>3 else 0); n=0; t0=time.time(); last=None; changes=0
while time.time()-t0<float(sys.argv[1]):
    try: a,seq=L.wait_frame(1.0)
    except TimeoutError: continue
    n+=1
    # echo the frame back as the "result" (identity pipeline)
    bgra=np.empty_like(a); bgra[...,0]=a[...,2]; bgra[...,1]=a[...,1]; bgra[...,2]=a[...,0]; bgra[...,3]=255
    L.publish(bgra); L.set_override(True)
print("frames received",n,"in",round(time.time()-t0,1),"s ->",round(n/(time.time()-t0),1),"fps")
L.close()
