# SPDX-License-Identifier: MIT
"""Which DRM card drives the display (used to open an EGL device for the GL upscalers)."""
import glob
import os


def detect_display_card() -> str:
    """The /dev/dri/cardN whose connectors are actually connected (i.e. the GPU doing scanout) - card numbering is NOT stable across reboots
    (on a hybrid laptop Intel may be card0 one day and card1 the next), so a hardcoded path silently breaks."""
    for status in sorted(glob.glob("/sys/class/drm/card[0-9]*-*/status")):
        try:
            if open(status).read().strip() == "connected":
                return "/dev/dri/" + os.path.basename(os.path.dirname(status)).split("-")[0]
        except OSError:
            pass
    return "/dev/dri/card0"
