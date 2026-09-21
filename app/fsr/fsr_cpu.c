// SPDX-License-Identifier: MIT
/* CPU-side FSR constants (AMD's own FsrEasuCon / FsrRcasCon), compiled separately so the CPU and GPU flavours of
 * ffx_a.h never meet in one translation unit. */
#include <stdint.h>
#include <math.h>
#include <string.h>
#include <stdlib.h>
#define A_CPU 1
#include "ffx_a.h"
#include "ffx_fsr1.h"

void fsr_easu_constants(unsigned *c0, unsigned *c1, unsigned *c2, unsigned *c3, float in_w, float in_h, float out_w, float out_h) {
    FsrEasuCon(c0, c1, c2, c3, in_w, in_h, in_w, in_h, out_w, out_h);
}
void fsr_rcas_constants(unsigned *c, float sharpness_stops) {
    FsrRcasCon(c, sharpness_stops);
}
