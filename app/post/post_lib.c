// SPDX-License-Identifier: MIT
/* Composition post-pass, see ../postprocess.py for the definitions. One pass over the full-res pixels; the
 * low-frequency terms (tone / grain / skin) come from planes at 1/4 resolution, blurred there and sampled
 * bilinearly - so the cost is ~ two frame reads + one write, not a pile of full-res float temporaries. */
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#ifdef _OPENMP
#include <omp.h>
#endif

#define DIV 4

typedef struct {
    float color, tone, grain, skin, shimmer;
    int use_mask, prev_valid, tone_radius; /* radius in FULL-res px */
} PostParams;

static inline float luma(float r, float g, float b) { return 0.2126f * r + 0.7152f * g + 0.0722f * b; }
static inline float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

/* separable box blur, radius r (small-res px), `passes` times (3 ~ gaussian); clamp-to-edge */
static void blur_plane(float *p, float *tmp, int w, int h, int r, int passes) {
    if (r < 1) return;
    for (int it = 0; it < passes; it++) {
        for (int y = 0; y < h; y++) {
            float *row = p + (size_t)y * w, *o = tmp + (size_t)y * w; double acc = 0;
            for (int k = -r; k <= r; k++) acc += row[k < 0 ? 0 : (k >= w ? w - 1 : k)];
            for (int x = 0; x < w; x++) {
                o[x] = (float)(acc / (2 * r + 1));
                int a = x - r, b = x + r + 1;
                acc += row[b >= w ? w - 1 : b] - row[a < 0 ? 0 : a];
            }
        }
        for (int x = 0; x < w; x++) {
            double acc = 0;
            for (int k = -r; k <= r; k++) acc += tmp[(size_t)(k < 0 ? 0 : (k >= h ? h - 1 : k)) * w + x];
            for (int y = 0; y < h; y++) {
                p[(size_t)y * w + x] = (float)(acc / (2 * r + 1));
                int a = y - r, b = y + r + 1;
                acc += tmp[(size_t)(b >= h ? h - 1 : b) * w + x] - tmp[(size_t)(a < 0 ? 0 : a) * w + x];
            }
        }
    }
}

static inline float sample(const float *pl, int sw, int sh, float fx, float fy) {
    fx = clampf(fx, 0.f, sw - 1.f); fy = clampf(fy, 0.f, sh - 1.f);
    int x0 = (int)fx, y0 = (int)fy, x1 = x0 + 1 < sw ? x0 + 1 : x0, y1 = y0 + 1 < sh ? y0 + 1 : y0;
    float ax = fx - x0, ay = fy - y0;
    float a = pl[(size_t)y0 * sw + x0], b = pl[(size_t)y0 * sw + x1], c = pl[(size_t)y1 * sw + x0], d = pl[(size_t)y1 * sw + x1];
    return (a + (b - a) * ax) * (1 - ay) + (c + (d - c) * ax) * ay;
}

/* src/out/dst: RGBA8, w*h. mask: w*h uint8 or NULL. prev_out: w*h*4 (RGBA8) and prev_luma: w*h, both read (if
 * prev_valid) and always written when shimmer > 0. */
int post_apply(const uint8_t *src, const uint8_t *out, uint8_t *dst, int w, int h, const PostParams *p,
               const uint8_t *mask, uint8_t *prev_out, uint8_t *prev_luma) {
    int sw = (w + DIV - 1) / DIV, sh = (h + DIV - 1) / DIV;
    size_t sn = (size_t)sw * sh;
    float *ys_s = malloc(sn * 4 * sizeof(float)), *yo_s = ys_s + sn, *skin = yo_s + sn, *tmp = skin + sn;
    float *g_s = malloc(sn * 2 * sizeof(float)), *g_o = g_s + sn;  /* fine-scale blurred luma, for grain */
    if (!ys_s || !g_s) { free(ys_s); free(g_s); return 1; }

    /* 1/4-res planes: mean luma of both frames + skin-ness of the source */
    #pragma omp parallel for schedule(static)
    for (int sy = 0; sy < sh; sy++) {
        for (int sx = 0; sx < sw; sx++) {
            float as = 0, ao = 0, sr = 0, sg = 0, sb = 0; int n = 0;
            for (int dy = 0; dy < DIV; dy++) {
                int y = sy * DIV + dy; if (y >= h) break;
                for (int dx = 0; dx < DIV; dx++) {
                    int x = sx * DIV + dx; if (x >= w) break;
                    const uint8_t *a = src + ((size_t)y * w + x) * 4, *b = out + ((size_t)y * w + x) * 4;
                    as += luma(a[0], a[1], a[2]); ao += luma(b[0], b[1], b[2]);
                    sr += a[0]; sg += a[1]; sb += a[2]; n++;
                }
            }
            float inv = 1.f / n; size_t i = (size_t)sy * sw + sx;
            ys_s[i] = as * inv; yo_s[i] = ao * inv;
            sr *= inv; sg *= inv; sb *= inv;
            float cb = -0.1687f * sr - 0.3313f * sg + 0.5f * sb + 128.f, cr = 0.5f * sr - 0.4187f * sg - 0.0813f * sb + 128.f;
            skin[i] = (cb >= 77 && cb <= 127 && cr >= 133 && cr <= 173 && ys_s[i] > 40) ? 1.f : 0.f;
        }
    }
    if (p->grain > 0) { memcpy(g_s, ys_s, sn * sizeof(float)); memcpy(g_o, yo_s, sn * sizeof(float)); blur_plane(g_s, tmp, sw, sh, 1, 2); blur_plane(g_o, tmp, sw, sh, 1, 2); }
    int tr = p->tone_radius / DIV; if (tr < 1) tr = 1;
    float *lp_s = ys_s, *lp_o = yo_s;
    float *gs_hi = NULL;
    if (p->grain > 0 && p->tone > 0) { /* grain needs the un-blurred-by-tone planes too: keep copies */
        gs_hi = malloc(sn * 2 * sizeof(float)); memcpy(gs_hi, ys_s, sn * sizeof(float)); memcpy(gs_hi + sn, yo_s, sn * sizeof(float));
    }
    if (p->tone > 0) { blur_plane(lp_s, tmp, sw, sh, tr, 3); blur_plane(lp_o, tmp, sw, sh, tr, 3); }
    if (p->skin > 0) blur_plane(skin, tmp, sw, sh, 2, 2);
    (void)gs_hi;

    int shim = p->shimmer > 0;
    int use_prev = shim && p->prev_valid;
    float fdiv = 1.f / DIV;

    #pragma omp parallel for schedule(static)
    for (int y = 0; y < h; y++) {
        float fy = (y + 0.5f) * fdiv - 0.5f;
        for (int x = 0; x < w; x++) {
            size_t i = (size_t)y * w + x; const uint8_t *a = src + i * 4, *b = out + i * 4; uint8_t *d = dst + i * 4;
            float fx = (x + 0.5f) * fdiv - 0.5f;
            float s[3] = { a[0], a[1], a[2] }, o[3] = { b[0], b[1], b[2] };
            float ys = luma(s[0], s[1], s[2]), yo = luma(o[0], o[1], o[2]);
            float r[3] = { o[0], o[1], o[2] };
            if (p->tone > 0) {
                float dy = p->tone * (sample(lp_s, sw, sh, fx, fy) - sample(lp_o, sw, sh, fx, fy));
                r[0] += dy; r[1] += dy; r[2] += dy;
            }
            if (p->color < 1.f) {
                float k = 1.f - p->color, dl = ys - yo;
                for (int c = 0; c < 3; c++) r[c] += k * ((s[c] - o[c]) - dl);
            }
            if (p->grain > 0) {
                float hs = ys - sample(g_s, sw, sh, fx, fy), ho = yo - sample(g_o, sw, sh, fx, fy);
                float lost = fmaxf(fabsf(hs) - fabsf(ho), 0.f) * (hs < 0 ? -1.f : 1.f) * p->grain;
                r[0] += lost; r[1] += lost; r[2] += lost;
            }
            if (p->skin > 0) {
                float m = p->skin * clampf(sample(skin, sw, sh, fx, fy), 0.f, 1.f);
                for (int c = 0; c < 3; c++) r[c] += m * (s[c] - r[c]);
            }
            if (mask) {
                float m = mask[i] * (1.f / 255.f);
                for (int c = 0; c < 3; c++) r[c] = s[c] + m * (r[c] - s[c]);
            }
            if (shim) {
                if (use_prev) {
                    float still = clampf(1.f - fabsf(ys - prev_luma[i]) * (1.f / 14.f), 0.f, 1.f);
                    float wgt = p->shimmer * still * still;
                    const uint8_t *pv = prev_out + i * 4;
                    for (int c = 0; c < 3; c++) r[c] += wgt * (pv[c] - r[c]);
                }
            }
            for (int c = 0; c < 3; c++) d[c] = (uint8_t)(clampf(r[c], 0.f, 255.f) + 0.5f);
            d[3] = 255;
            if (shim) { uint8_t *pv = prev_out + i * 4; pv[0] = d[0]; pv[1] = d[1]; pv[2] = d[2]; pv[3] = 255; prev_luma[i] = (uint8_t)(ys + 0.5f); }
        }
    }
    free(ys_s); free(g_s); free(gs_hi);
    return 0;
}
