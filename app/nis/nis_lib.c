// SPDX-License-Identifier: MIT
/* Standalone GLES port of NVIDIA Image Scaling (NIS)'s NVScaler algorithm
 * (github.com/NVIDIAGameWorks/NVIDIAImageScaling, MIT license) as a plain
 * fragment shader instead of the original's shared-memory compute shader -
 * correctness-preserving but not cache-tile-optimized (each output pixel
 * independently re-fetches its own 6x6 luma neighborhood via texelFetch;
 * fine at our resolutions, just not maximally GPU-cache-efficient).
 *
 * Used to upscale a reduced-resolution DLSS5-cleaned frame back up to
 * display resolution with a proper edge-adaptive scaler+sharpener instead
 * of naive bilinear/nearest - see ../README.md.
 *
 * Algorithm ported from NIS_Scaler.h's NVScaler()/GetEdgeMap()/EvalPoly6()/
 * FilterNormal()/AddDirFilters()/CalcLTI(); config math ported from
 * NIS_Config.h's NVScalerUpdateConfig(); the 64x6 coef_scale/coef_usm
 * tables are copied verbatim from the same header (they're small
 * closed-form filter coefficients, not learned weights).
 */
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES3/gl3.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

typedef EGLBoolean (*PFN_eglQueryDevicesEXT)(EGLint, EGLDeviceEXT*, EGLint*);
typedef const char* (*PFN_eglQueryDeviceStringEXT)(EGLDeviceEXT, EGLint);
typedef EGLDisplay (*PFN_eglGetPlatformDisplayEXT)(EGLenum, void*, const EGLint*);

typedef struct {
    EGLDisplay dpy;
    EGLContext ctx;
    EGLSurface surf;
    GLuint prog, vs, fs;
    GLuint src_tex, dst_tex, fbo, coef_scale_tex, coef_usm_tex;
    int src_w, src_h, dst_w, dst_h;
    char err[256];
} nis_upscaler;

#include "nis_coef.h" /* coef_scale[64][8], coef_usm[64][8] - copied from NIS_Config.h */

static const char *VS_SRC =
    "#version 300 es\n"
    "const vec2 pos[3] = vec2[3](vec2(-1.0,-1.0), vec2(3.0,-1.0), vec2(-1.0,3.0));\n"
    "void main() { gl_Position = vec4(pos[gl_VertexID], 0.0, 1.0); }\n";

/* NOTE: kFilterSize here is 6 (the actual filter taps used), matching
 * NIS_Scaler.h's NVScaler path - the coef tables have 8 columns in the
 * header but only the first 6 are ever read by the algorithm. */
static const char *FS_SRC =
    "#version 300 es\n"
    "precision highp float;\n"
    "uniform sampler2D srcTex;\n"
    "uniform sampler2D coefScaleTex;\n"
    "uniform sampler2D coefUsmTex;\n"
    "uniform vec2 kScale;      /* srcW/dstW, srcH/dstH */\n"
    "uniform vec2 kSrcNorm;    /* 1/srcW, 1/srcH */\n"
    "uniform ivec2 kSrcSizeM1; /* srcW-1, srcH-1, for clamping */\n"
    "uniform float kDetectRatio, kDetectThres, kMinContrastRatio, kRatioNorm, kContrastBoost, kEps;\n"
    "uniform float kSharpStartY, kSharpScaleY, kSharpStrengthMin, kSharpStrengthScale, kSharpLimitMin, kSharpLimitScale;\n"
    "out vec4 fragColor;\n"
    "\n"
    "float luma(vec3 c) { return 0.2126*c.r + 0.7152*c.g + 0.0722*c.b; }\n"
    "\n"
    "float fetchY(int ix, int iy) {\n"
    "    ivec2 c = clamp(ivec2(ix, iy), ivec2(0), kSrcSizeM1);\n"
    "    return luma(texelFetch(srcTex, c, 0).rgb);\n"
    "}\n"
    "\n"
    "float coefScale(int phase, int i) { return texelFetch(coefScaleTex, ivec2(i, phase), 0).r; }\n"
    "float coefUSM(int phase, int i) { return texelFetch(coefUsmTex, ivec2(i, phase), 0).r; }\n"
    "\n"
    "vec4 computeEdge(int cx, int cy) {\n"
    "    float p00=fetchY(cx-1,cy-1), p01=fetchY(cx,cy-1), p02=fetchY(cx+1,cy-1);\n"
    "    float p10=fetchY(cx-1,cy),   p11=fetchY(cx,cy),   p12=fetchY(cx+1,cy);\n"
    "    float p20=fetchY(cx-1,cy+1), p21=fetchY(cx,cy+1), p22=fetchY(cx+1,cy+1);\n"
    "    float g_0   = abs(p00+p01+p02 - p20-p21-p22);\n"
    "    float g_45  = abs(p10+p00+p01 - p21-p22-p12);\n"
    "    float g_90  = abs(p00+p10+p20 - p02-p12-p22);\n"
    "    float g_135 = abs(p10+p20+p21 - p01-p02-p12);\n"
    "    float g0_90_max = max(g_0,g_90), g0_90_min = min(g_0,g_90);\n"
    "    float g45_135_max = max(g_45,g_135), g45_135_min = min(g_45,g_135);\n"
    "    if (g0_90_max + g45_135_max == 0.0) return vec4(0.0);\n"
    "    float e_0_90 = min(g0_90_max/(g0_90_max+g45_135_max), 1.0);\n"
    "    float e_45_135 = 1.0 - e_0_90;\n"
    "    bool c_0_90 = (g0_90_max > g0_90_min*kDetectRatio) && (g0_90_max > kDetectThres) && (g0_90_max > g45_135_min);\n"
    "    bool c_45_135 = (g45_135_max > g45_135_min*kDetectRatio) && (g45_135_max > kDetectThres) && (g45_135_max > g0_90_min);\n"
    "    bool c_g_0_90 = (g0_90_max == g_0);\n"
    "    bool c_g_45_135 = (g45_135_max == g_45);\n"
    "    float f0 = (c_0_90 && c_45_135) ? e_0_90 : 1.0;\n"
    "    float f1 = (c_0_90 && c_45_135) ? e_45_135 : 1.0;\n"
    "    float w0 = (c_0_90 && c_g_0_90) ? f0 : 0.0;\n"
    "    float w90 = (c_0_90 && !c_g_0_90) ? f0 : 0.0;\n"
    "    float w45 = (c_45_135 && c_g_45_135) ? f1 : 0.0;\n"
    "    float w135 = (c_45_135 && !c_g_45_135) ? f1 : 0.0;\n"
    "    return vec4(w0, w90, w45, w135);\n"
    "}\n"
    "\n"
    "float calcLTI(float p0,float p1,float p2,float p3,float p4,float p5,int phase) {\n"
    "    bool sel0 = phase <= 32;\n"
    "    float sel = sel0 ? p0 : p3;\n"
    "    float a_min = min(min(p1,p2), sel), a_max = max(max(p1,p2), sel);\n"
    "    sel = sel0 ? p2 : p5;\n"
    "    float b_min = min(min(p3,p4), sel), b_max = max(max(p3,p4), sel);\n"
    "    float a_cont = a_max-a_min, b_cont = b_max-b_min;\n"
    "    float ratio = max(a_cont,b_cont) / (min(a_cont,b_cont) + kEps);\n"
    "    return (1.0 - clamp((ratio - kMinContrastRatio) * kRatioNorm, 0.0, 1.0)) * kContrastBoost;\n"
    "}\n"
    "\n"
    "float evalPoly6(float pxl[6], int phase) {\n"
    "    float y = 0.0;\n"
    "    for (int i = 0; i < 6; i++) y += coefScale(phase, i) * pxl[i];\n"
    "    float yUsm = 0.0;\n"
    "    for (int i = 0; i < 6; i++) yUsm += coefUSM(phase, i) * pxl[i];\n"
    "    float yScale = 1.0 - clamp((y - kSharpStartY) * kSharpScaleY, 0.0, 1.0);\n"
    "    float ySharpness = yScale * kSharpStrengthScale + kSharpStrengthMin;\n"
    "    yUsm *= ySharpness;\n"
    "    float yLimit = (yScale * kSharpLimitScale + kSharpLimitMin) * y;\n"
    "    yUsm = min(yLimit, max(-yLimit, yUsm));\n"
    "    yUsm *= calcLTI(pxl[0], pxl[1], pxl[2], pxl[3], pxl[4], pxl[5], phase);\n"
    "    return y + yUsm;\n"
    "}\n"
    "\n"
    "#define P(a,b) p[(a)*6+(b)]\n"
    "\n"
    "float filterNormal(float p[36], int fxInt, int fyInt) {\n"
    "    float hAcc = 0.0;\n"
    "    for (int j = 0; j < 6; j++) {\n"
    "        float vAcc = 0.0;\n"
    "        for (int i = 0; i < 6; i++) vAcc += P(i,j) * coefScale(fyInt, i);\n"
    "        hAcc += vAcc * coefScale(fxInt, j);\n"
    "    }\n"
    "    return hAcc;\n"
    "}\n"
    "\n"
    "float addDirFilters(float p[36], float fx, float fy, int fxInt, int fyInt, vec4 w) {\n"
    "    float f = 0.0;\n"
    "    if (w.x > 0.0) {\n"
    "        float d[6];\n"
    "        for (int i = 0; i < 6; i++) d[i] = mix(P(i,2), P(i,3), fx);\n"
    "        f += evalPoly6(d, fyInt) * w.x;\n"
    "    }\n"
    "    if (w.y > 0.0) {\n"
    "        float d[6];\n"
    "        for (int i = 0; i < 6; i++) d[i] = mix(P(2,i), P(3,i), fy);\n"
    "        f += evalPoly6(d, fxInt) * w.y;\n"
    "    }\n"
    "    if (w.z > 0.0) {\n"
    "        float b45 = 0.5 + 0.5 * (fx - fy);\n"
    "        float t1 = mix(P(2,1), P(1,2), b45);\n"
    "        float t3 = mix(P(3,2), P(2,3), b45);\n"
    "        float t5 = mix(P(4,3), P(3,4), b45);\n"
    "        float bb = b45 - 0.5;\n"
    "        float a = bb >= 0.0 ? P(0,2) : P(2,0);\n"
    "        float b = bb >= 0.0 ? P(1,3) : P(3,1);\n"
    "        float c = bb >= 0.0 ? P(2,4) : P(4,2);\n"
    "        float e = bb >= 0.0 ? P(3,5) : P(5,3);\n"
    "        float t0 = mix(P(1,1), a, abs(bb));\n"
    "        float t2 = mix(P(2,2), b, abs(bb));\n"
    "        float t4 = mix(P(3,3), c, abs(bb));\n"
    "        float t6 = mix(P(4,4), e, abs(bb));\n"
    "        float d[6];\n"
    "        float pp45 = fx + fy;\n"
    "        if (pp45 >= 1.0) { d[0]=t1; d[1]=t2; d[2]=t3; d[3]=t4; d[4]=t5; d[5]=t6; pp45 -= 1.0; }\n"
    "        else { d[0]=t0; d[1]=t1; d[2]=t2; d[3]=t3; d[4]=t4; d[5]=t5; }\n"
    "        f += evalPoly6(d, int(pp45 * 64.0)) * w.z;\n"
    "    }\n"
    "    if (w.w > 0.0) {\n"
    "        float b135 = 0.5 * (fx + fy);\n"
    "        float t1 = mix(P(3,1), P(4,2), b135);\n"
    "        float t3 = mix(P(2,2), P(3,3), b135);\n"
    "        float t5 = mix(P(1,3), P(2,4), b135);\n"
    "        float bb = b135 - 0.5;\n"
    "        float a = bb >= 0.0 ? P(5,2) : P(3,0);\n"
    "        float b = bb >= 0.0 ? P(4,3) : P(2,1);\n"
    "        float c = bb >= 0.0 ? P(3,4) : P(1,2);\n"
    "        float e = bb >= 0.0 ? P(2,5) : P(0,3);\n"
    "        float t0 = mix(P(4,1), a, abs(bb));\n"
    "        float t2 = mix(P(3,2), b, abs(bb));\n"
    "        float t4 = mix(P(2,3), c, abs(bb));\n"
    "        float t6 = mix(P(1,4), e, abs(bb));\n"
    "        float d[6];\n"
    "        float pp135 = 1.0 + (fx - fy);\n"
    "        if (pp135 >= 1.0) { d[0]=t1; d[1]=t2; d[2]=t3; d[3]=t4; d[4]=t5; d[5]=t6; pp135 -= 1.0; }\n"
    "        else { d[0]=t0; d[1]=t1; d[2]=t2; d[3]=t3; d[4]=t4; d[5]=t5; }\n"
    "        f += evalPoly6(d, int(pp135 * 64.0)) * w.w;\n"
    "    }\n"
    "    return f;\n"
    "}\n"
    "\n"
    "void main() {\n"
    "    ivec2 dst = ivec2(gl_FragCoord.xy);\n"
    "    float srcX = (0.5 + float(dst.x)) * kScale.x - 0.5;\n"
    "    float srcY = (0.5 + float(dst.y)) * kScale.y - 0.5;\n"
    "    int ix0 = int(floor(srcX));\n"
    "    int iy0 = int(floor(srcY));\n"
    "    float fx = srcX - float(ix0);\n"
    "    float fy = srcY - float(iy0);\n"
    "    int fxInt = clamp(int(fx * 64.0), 0, 63);\n"
    "    int fyInt = clamp(int(fy * 64.0), 0, 63);\n"
    "\n"
    "    float p[36];\n"
    "    for (int i = 0; i < 6; i++)\n"
    "        for (int j = 0; j < 6; j++)\n"
    "            p[i*6+j] = fetchY(ix0+j-2, iy0+i-2);\n"
    "\n"
    "    vec4 e00 = computeEdge(ix0, iy0);\n"
    "    vec4 e01 = computeEdge(ix0+1, iy0);\n"
    "    vec4 e10 = computeEdge(ix0, iy0+1);\n"
    "    vec4 e11 = computeEdge(ix0+1, iy0+1);\n"
    "    vec4 h0 = mix(e00, e01, fx);\n"
    "    vec4 h1 = mix(e10, e11, fx);\n"
    "    vec4 w = mix(h0, h1, fy);\n"
    "\n"
    "    float baseWeight = 1.0 - w.x - w.y - w.z - w.w;\n"
    "    float opY = filterNormal(p, fxInt, fyInt) * baseWeight;\n"
    "    opY += addDirFilters(p, fx, fy, fxInt, fyInt, w);\n"
    "\n"
    "    vec2 coord = vec2((srcX + 0.5) * kSrcNorm.x, (srcY + 0.5) * kSrcNorm.y);\n"
    "    vec4 op = texture(srcTex, coord);\n"
    "    float y = luma(op.rgb);\n"
    "    float corr = opY - y;\n"
    "    fragColor = clamp(vec4(op.rgb + corr, 1.0), 0.0, 1.0);\n"
    "}\n";

static GLuint compile_shader(GLenum type, const char *src, char *errbuf, size_t errbuf_size) {
    GLuint s = glCreateShader(type);
    glShaderSource(s, 1, &src, NULL);
    glCompileShader(s);
    GLint ok = 0;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        glGetShaderInfoLog(s, (GLsizei)errbuf_size, NULL, errbuf);
        glDeleteShader(s);
        return 0;
    }
    return s;
}

/* Port of NIS_Config.h's NVScalerUpdateConfig - only the fields our shader
 * actually uses (no HDR mode, no viewport origin support - we always
 * scale the whole src texture into the whole dst texture). */
typedef struct {
    float kDetectRatio, kDetectThres, kMinContrastRatio, kRatioNorm, kContrastBoost, kEps;
    float kSharpStartY, kSharpScaleY, kSharpStrengthMin, kSharpStrengthScale, kSharpLimitMin, kSharpLimitScale;
    float kScaleX, kScaleY, kSrcNormX, kSrcNormY;
} nis_config;

static void nis_update_config(nis_config *c, float sharpness, int src_w, int src_h, int dst_w, int dst_h) {
    if (sharpness < 0.0f) sharpness = 0.0f;
    if (sharpness > 1.0f) sharpness = 1.0f;
    float slider = sharpness - 0.5f;
    float MaxScale = (slider >= 0.0f) ? 1.25f : 1.75f;
    float MinScale = (slider >= 0.0f) ? 1.25f : 1.0f;
    float LimitScale = (slider >= 0.0f) ? 1.25f : 1.0f;

    c->kDetectRatio = 2.0f * 1127.0f / 1024.0f;
    float kDetectThres = 64.0f / 1024.0f;
    float kMinContrastRatio = 2.0f;
    float kMaxContrastRatio = 10.0f;
    float kSharpStartY = 0.45f;
    float kSharpEndY = 0.9f;
    float kSharpStrengthMin = fmaxf(0.0f, 0.4f + slider * MinScale * 1.2f);
    float kSharpStrengthMax = 1.6f + slider * MaxScale * 1.8f;
    float kSharpLimitMin = fmaxf(0.1f, 0.14f + slider * LimitScale * 0.32f);
    float kSharpLimitMax = 0.5f + slider * LimitScale * 0.6f;

    c->kRatioNorm = 1.0f / (kMaxContrastRatio - kMinContrastRatio);
    c->kSharpScaleY = 1.0f / (kSharpEndY - kSharpStartY);
    c->kSharpStrengthScale = kSharpStrengthMax - kSharpStrengthMin;
    c->kSharpLimitScale = kSharpLimitMax - kSharpLimitMin;
    c->kSharpStartY = kSharpStartY;
    c->kSharpStrengthMin = kSharpStrengthMin;
    c->kSharpLimitMin = kSharpLimitMin;
    c->kDetectThres = kDetectThres;
    c->kMinContrastRatio = kMinContrastRatio;
    c->kContrastBoost = 1.0f;
    c->kEps = 1.0f / 255.0f;

    c->kSrcNormX = 1.0f / (float)src_w;
    c->kSrcNormY = 1.0f / (float)src_h;
    c->kScaleX = (float)src_w / (float)dst_w;
    c->kScaleY = (float)src_h / (float)dst_h;
}

nis_upscaler* nis_init(const char *card_path, int src_w, int src_h, int dst_w, int dst_h, float sharpness) {
    nis_upscaler *self = calloc(1, sizeof(nis_upscaler));
    if (!self) return NULL;
    self->src_w = src_w; self->src_h = src_h;
    self->dst_w = dst_w; self->dst_h = dst_h;

    PFN_eglQueryDevicesEXT eglQueryDevicesEXT_ = (PFN_eglQueryDevicesEXT)eglGetProcAddress("eglQueryDevicesEXT");
    PFN_eglQueryDeviceStringEXT eglQueryDeviceStringEXT_ = (PFN_eglQueryDeviceStringEXT)eglGetProcAddress("eglQueryDeviceStringEXT");
    PFN_eglGetPlatformDisplayEXT eglGetPlatformDisplayEXT_ = (PFN_eglGetPlatformDisplayEXT)eglGetProcAddress("eglGetPlatformDisplayEXT");
    if (!eglQueryDevicesEXT_ || !eglQueryDeviceStringEXT_ || !eglGetPlatformDisplayEXT_) {
        snprintf(self->err, sizeof(self->err), "missing required EGL extension functions");
        free(self);
        return NULL;
    }

    EGLDeviceEXT devices[32];
    EGLint num_devices = 0;
    eglQueryDevicesEXT_(32, devices, &num_devices);
    EGLDisplay dpy = EGL_NO_DISPLAY;
    for (int i = 0; i < num_devices; ++i) {
        const char *dev_card = eglQueryDeviceStringEXT_(devices[i], EGL_DRM_DEVICE_FILE_EXT);
        if (dev_card && strcmp(dev_card, card_path) == 0) {
            dpy = eglGetPlatformDisplayEXT_(EGL_PLATFORM_DEVICE_EXT, devices[i], NULL);
            break;
        }
    }
    if (dpy == EGL_NO_DISPLAY && num_devices > 0)
        dpy = eglGetPlatformDisplayEXT_(EGL_PLATFORM_DEVICE_EXT, devices[0], NULL);
    if (dpy == EGL_NO_DISPLAY) {
        snprintf(self->err, sizeof(self->err), "failed to get egl display");
        free(self);
        return NULL;
    }
    self->dpy = dpy;

    EGLint major, minor;
    if (!eglInitialize(dpy, &major, &minor)) {
        snprintf(self->err, sizeof(self->err), "eglInitialize failed");
        free(self);
        return NULL;
    }
    eglBindAPI(EGL_OPENGL_ES_API);

    EGLint cfg_attr[] = { EGL_SURFACE_TYPE, EGL_DONT_CARE, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT, EGL_NONE };
    EGLConfig cfg;
    EGLint num_cfg = 0;
    if (!eglChooseConfig(dpy, cfg_attr, &cfg, 1, &num_cfg) || num_cfg != 1) {
        snprintf(self->err, sizeof(self->err), "eglChooseConfig failed");
        free(self);
        return NULL;
    }
    EGLint ctx_attr[] = { EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE };
    self->ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attr);
    if (!self->ctx) {
        snprintf(self->err, sizeof(self->err), "eglCreateContext failed");
        free(self);
        return NULL;
    }
    self->surf = EGL_NO_SURFACE;
    if (!eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, self->ctx)) {
        EGLint pb_attr[] = { EGL_WIDTH, 16, EGL_HEIGHT, 16, EGL_NONE };
        self->surf = eglCreatePbufferSurface(dpy, cfg, pb_attr);
        if (!self->surf || !eglMakeCurrent(dpy, self->surf, self->surf, self->ctx)) {
            snprintf(self->err, sizeof(self->err), "eglMakeCurrent failed");
            free(self);
            return NULL;
        }
    }

    self->vs = compile_shader(GL_VERTEX_SHADER, VS_SRC, self->err, sizeof(self->err));
    if (!self->vs) { free(self); return NULL; }
    self->fs = compile_shader(GL_FRAGMENT_SHADER, FS_SRC, self->err, sizeof(self->err));
    if (!self->fs) { free(self); return NULL; }
    self->prog = glCreateProgram();
    glAttachShader(self->prog, self->vs);
    glAttachShader(self->prog, self->fs);
    glLinkProgram(self->prog);
    GLint linked = 0;
    glGetProgramiv(self->prog, GL_LINK_STATUS, &linked);
    if (!linked) {
        glGetProgramInfoLog(self->prog, sizeof(self->err), NULL, self->err);
        free(self);
        return NULL;
    }

    /* coefficient textures: 6 wide (only kFilterSize=6 taps used), 64 tall */
    float scale_flat[64 * 6], usm_flat[64 * 6];
    for (int ph = 0; ph < 64; ph++)
        for (int i = 0; i < 6; i++) {
            scale_flat[ph * 6 + i] = coef_scale_table[ph][i];
            usm_flat[ph * 6 + i] = coef_usm_table[ph][i];
        }
    glGenTextures(1, &self->coef_scale_tex);
    glBindTexture(GL_TEXTURE_2D, self->coef_scale_tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_R32F, 6, 64, 0, GL_RED, GL_FLOAT, scale_flat);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);

    glGenTextures(1, &self->coef_usm_tex);
    glBindTexture(GL_TEXTURE_2D, self->coef_usm_tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_R32F, 6, 64, 0, GL_RED, GL_FLOAT, usm_flat);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);

    glGenTextures(1, &self->src_tex);
    glBindTexture(GL_TEXTURE_2D, self->src_tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, src_w, src_h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);

    glGenTextures(1, &self->dst_tex);
    glBindTexture(GL_TEXTURE_2D, self->dst_tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, dst_w, dst_h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);

    glGenFramebuffers(1, &self->fbo);
    glBindFramebuffer(GL_FRAMEBUFFER, self->fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, self->dst_tex, 0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        snprintf(self->err, sizeof(self->err), "fbo incomplete");
        free(self);
        return NULL;
    }

    nis_config nis_cfg;
    nis_update_config(&nis_cfg, sharpness, src_w, src_h, dst_w, dst_h);

    glUseProgram(self->prog);
    glUniform1i(glGetUniformLocation(self->prog, "srcTex"), 0);
    glUniform1i(glGetUniformLocation(self->prog, "coefScaleTex"), 1);
    glUniform1i(glGetUniformLocation(self->prog, "coefUsmTex"), 2);
    glUniform2f(glGetUniformLocation(self->prog, "kScale"), nis_cfg.kScaleX, nis_cfg.kScaleY);
    glUniform2f(glGetUniformLocation(self->prog, "kSrcNorm"), nis_cfg.kSrcNormX, nis_cfg.kSrcNormY);
    glUniform2i(glGetUniformLocation(self->prog, "kSrcSizeM1"), src_w - 1, src_h - 1);
    glUniform1f(glGetUniformLocation(self->prog, "kDetectRatio"), nis_cfg.kDetectRatio);
    glUniform1f(glGetUniformLocation(self->prog, "kDetectThres"), nis_cfg.kDetectThres);
    glUniform1f(glGetUniformLocation(self->prog, "kMinContrastRatio"), nis_cfg.kMinContrastRatio);
    glUniform1f(glGetUniformLocation(self->prog, "kRatioNorm"), nis_cfg.kRatioNorm);
    glUniform1f(glGetUniformLocation(self->prog, "kContrastBoost"), nis_cfg.kContrastBoost);
    glUniform1f(glGetUniformLocation(self->prog, "kEps"), nis_cfg.kEps);
    glUniform1f(glGetUniformLocation(self->prog, "kSharpStartY"), nis_cfg.kSharpStartY);
    glUniform1f(glGetUniformLocation(self->prog, "kSharpScaleY"), nis_cfg.kSharpScaleY);
    glUniform1f(glGetUniformLocation(self->prog, "kSharpStrengthMin"), nis_cfg.kSharpStrengthMin);
    glUniform1f(glGetUniformLocation(self->prog, "kSharpStrengthScale"), nis_cfg.kSharpStrengthScale);
    glUniform1f(glGetUniformLocation(self->prog, "kSharpLimitMin"), nis_cfg.kSharpLimitMin);
    glUniform1f(glGetUniformLocation(self->prog, "kSharpLimitScale"), nis_cfg.kSharpLimitScale);

    return self;
}

int nis_upscale(nis_upscaler *self, const unsigned char *src_rgba, unsigned char *dst_rgba) {
    /* Re-assert our own context - EGL contexts are current-on-a-thread, not
     * current-on-a-process, so any other EGL-using library (e.g.
     * libkmscapture.so's KmsCapture) called on this same thread since our
     * last call would have switched the current context away from ours.
     * Without this, GL calls below silently operate on whatever context
     * (and its unrelated, differently-numbered objects) happened to be
     * current - no GL error, just wrong/stale-looking output. */
    eglMakeCurrent(self->dpy, self->surf, self->surf, self->ctx);

    glBindTexture(GL_TEXTURE_2D, self->src_tex);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, self->src_w, self->src_h, GL_RGBA, GL_UNSIGNED_BYTE, src_rgba);

    glBindFramebuffer(GL_FRAMEBUFFER, self->fbo);
    glViewport(0, 0, self->dst_w, self->dst_h);
    glUseProgram(self->prog);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, self->src_tex);
    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_2D, self->coef_scale_tex);
    glActiveTexture(GL_TEXTURE2);
    glBindTexture(GL_TEXTURE_2D, self->coef_usm_tex);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glReadPixels(0, 0, self->dst_w, self->dst_h, GL_RGBA, GL_UNSIGNED_BYTE, dst_rgba);
    GLenum err = glGetError();
    if (err != GL_NO_ERROR) {
        snprintf(self->err, sizeof(self->err), "gl error during upscale: 0x%x", err);
        return -1;
    }
    return 0;
}

const char* nis_last_error(nis_upscaler *self) {
    return self->err;
}

void nis_close(nis_upscaler *self) {
    if (!self) return;
    if (self->dpy) {
        eglMakeCurrent(self->dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
        if (self->ctx) eglDestroyContext(self->dpy, self->ctx);
        if (self->surf != EGL_NO_SURFACE) eglDestroySurface(self->dpy, self->surf);
        eglTerminate(self->dpy);
    }
    free(self);
}
