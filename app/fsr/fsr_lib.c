// SPDX-License-Identifier: MIT
/* AMD FidelityFX Super Resolution 1.0 (EASU upscale + RCAS sharpen, MIT - see LICENSE-AMD-FSR.txt) as a
 * standalone GLES 3.1 library, same shape as ../nis/nis_lib.c (EGL on the DRM device, offscreen, glReadPixels).
 * The shaders are AMD's headers verbatim (gen_shaders.py assembles them into fsr_shaders.h); FSR 1 is a spatial
 * single-frame filter - FSR 2/3/4 are temporal and need motion vectors/depth, which a screen capture does not have. */
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES3/gl31.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fsr_shaders.h"

void fsr_easu_constants(unsigned *c0, unsigned *c1, unsigned *c2, unsigned *c3, float in_w, float in_h, float out_w, float out_h);
void fsr_rcas_constants(unsigned *c, float sharpness_stops);

typedef EGLBoolean (*PFN_eglQueryDevicesEXT)(EGLint, EGLDeviceEXT*, EGLint*);
typedef const char* (*PFN_eglQueryDeviceStringEXT)(EGLDeviceEXT, EGLint);
typedef EGLDisplay (*PFN_eglGetPlatformDisplayEXT)(EGLenum, void*, const EGLint*);

typedef struct {
    EGLDisplay dpy; EGLContext ctx; EGLSurface surf;
    GLuint prog_easu, prog_rcas, src_tex, mid_tex, dst_tex, fbo_mid, fbo_dst;
    int src_w, src_h, dst_w, dst_h;
    char err[512];
} fsr_upscaler;

static const char *VS_SRC =
    "#version 310 es\n"
    "const vec2 pos[3] = vec2[3](vec2(-1.0,-1.0), vec2(3.0,-1.0), vec2(-1.0,3.0));\n"
    "void main() { gl_Position = vec4(pos[gl_VertexID], 0.0, 1.0); }\n";

static GLuint build_program(const char *fs_src, char *err, size_t errn) {
    GLuint vs = glCreateShader(GL_VERTEX_SHADER), fs = glCreateShader(GL_FRAGMENT_SHADER), prog;
    GLint ok = 0;
    glShaderSource(vs, 1, &VS_SRC, NULL); glCompileShader(vs);
    glGetShaderiv(vs, GL_COMPILE_STATUS, &ok);
    if (!ok) { glGetShaderInfoLog(vs, (GLsizei)errn, NULL, err); return 0; }
    glShaderSource(fs, 1, &fs_src, NULL); glCompileShader(fs);
    glGetShaderiv(fs, GL_COMPILE_STATUS, &ok);
    if (!ok) { glGetShaderInfoLog(fs, (GLsizei)errn, NULL, err); return 0; }
    prog = glCreateProgram(); glAttachShader(prog, vs); glAttachShader(prog, fs); glLinkProgram(prog);
    glGetProgramiv(prog, GL_LINK_STATUS, &ok);
    if (!ok) { glGetProgramInfoLog(prog, (GLsizei)errn, NULL, err); return 0; }
    return prog;
}

static GLuint make_tex(int w, int h, GLint filter) {
    GLuint t; glGenTextures(1, &t); glBindTexture(GL_TEXTURE_2D, t);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, filter); glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, filter);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE); glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    return t;
}

static GLuint make_fbo(GLuint tex) {
    GLuint f; glGenFramebuffers(1, &f); glBindFramebuffer(GL_FRAMEBUFFER, f);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
    return glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE ? f : 0;
}

/* sharpness: 0..1 (1 = sharpest); RCAS itself takes "stops of attenuation", 0 = sharpest, 2 = none. */
fsr_upscaler* fsr_init(const char *card_path, int src_w, int src_h, int dst_w, int dst_h, float sharpness) {
    fsr_upscaler *self = calloc(1, sizeof(*self));
    if (!self) return NULL;
    self->src_w = src_w; self->src_h = src_h; self->dst_w = dst_w; self->dst_h = dst_h;

    PFN_eglQueryDevicesEXT qd = (PFN_eglQueryDevicesEXT)eglGetProcAddress("eglQueryDevicesEXT");
    PFN_eglQueryDeviceStringEXT qs = (PFN_eglQueryDeviceStringEXT)eglGetProcAddress("eglQueryDeviceStringEXT");
    PFN_eglGetPlatformDisplayEXT gp = (PFN_eglGetPlatformDisplayEXT)eglGetProcAddress("eglGetPlatformDisplayEXT");
    if (!qd || !qs || !gp) { snprintf(self->err, sizeof self->err, "missing required EGL extension functions"); fprintf(stderr, "fsr: %s\n", self->err); free(self); return NULL; }
    EGLDeviceEXT devices[32]; EGLint nd = 0; qd(32, devices, &nd);
    EGLDisplay dpy = EGL_NO_DISPLAY;
    for (int i = 0; i < nd; i++) {
        const char *dc = qs(devices[i], EGL_DRM_DEVICE_FILE_EXT);
        if (dc && strcmp(dc, card_path) == 0) { dpy = gp(EGL_PLATFORM_DEVICE_EXT, devices[i], NULL); break; }
    }
    if (dpy == EGL_NO_DISPLAY && nd > 0) dpy = gp(EGL_PLATFORM_DEVICE_EXT, devices[0], NULL);
    if (dpy == EGL_NO_DISPLAY) { snprintf(self->err, sizeof self->err, "failed to get egl display"); fprintf(stderr, "fsr: %s\n", self->err); free(self); return NULL; }
    self->dpy = dpy;
    EGLint maj, min;
    if (!eglInitialize(dpy, &maj, &min)) { snprintf(self->err, sizeof self->err, "eglInitialize failed"); fprintf(stderr, "fsr: %s\n", self->err); free(self); return NULL; }
    eglBindAPI(EGL_OPENGL_ES_API);
    EGLint cfg_attr[] = { EGL_SURFACE_TYPE, EGL_DONT_CARE, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT, EGL_NONE };
    EGLConfig cfg; EGLint ncfg = 0;
    if (!eglChooseConfig(dpy, cfg_attr, &cfg, 1, &ncfg) || ncfg != 1) { snprintf(self->err, sizeof self->err, "eglChooseConfig failed"); fprintf(stderr, "fsr: %s\n", self->err); free(self); return NULL; }
    EGLint ctx_attr[] = { EGL_CONTEXT_MAJOR_VERSION, 3, EGL_CONTEXT_MINOR_VERSION, 1, EGL_NONE };  /* textureGather needs ES 3.1 */
    self->ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attr);
    if (!self->ctx) { snprintf(self->err, sizeof self->err, "eglCreateContext (ES 3.1) failed"); fprintf(stderr, "fsr: %s\n", self->err); free(self); return NULL; }
    self->surf = EGL_NO_SURFACE;
    if (!eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, self->ctx)) {
        EGLint pb[] = { EGL_WIDTH, 16, EGL_HEIGHT, 16, EGL_NONE };
        self->surf = eglCreatePbufferSurface(dpy, cfg, pb);
        if (!self->surf || !eglMakeCurrent(dpy, self->surf, self->surf, self->ctx)) { snprintf(self->err, sizeof self->err, "eglMakeCurrent failed"); fprintf(stderr, "fsr: %s\n", self->err); free(self); return NULL; }
    }

    self->prog_easu = build_program(FS_EASU, self->err, sizeof self->err);
    if (!self->prog_easu) { fprintf(stderr, "fsr: %s\n", self->err); free(self); return NULL; }
    self->prog_rcas = build_program(FS_RCAS, self->err, sizeof self->err);
    if (!self->prog_rcas) { fprintf(stderr, "fsr: %s\n", self->err); free(self); return NULL; }

    self->src_tex = make_tex(src_w, src_h, GL_LINEAR);
    self->mid_tex = make_tex(dst_w, dst_h, GL_NEAREST);
    self->dst_tex = make_tex(dst_w, dst_h, GL_NEAREST);
    self->fbo_mid = make_fbo(self->mid_tex);
    self->fbo_dst = make_fbo(self->dst_tex);
    if (!self->fbo_mid || !self->fbo_dst) { snprintf(self->err, sizeof self->err, "fbo incomplete"); fprintf(stderr, "fsr: %s\n", self->err); free(self); return NULL; }

    unsigned c0[4], c1[4], c2[4], c3[4], rc[4];
    fsr_easu_constants(c0, c1, c2, c3, (float)src_w, (float)src_h, (float)dst_w, (float)dst_h);
    float stops = 2.0f * (1.0f - (sharpness < 0 ? 0 : sharpness > 1 ? 1 : sharpness));
    fsr_rcas_constants(rc, stops);
    glUseProgram(self->prog_easu);
    glUniform1i(glGetUniformLocation(self->prog_easu, "srcTex"), 0);
    glUniform4uiv(glGetUniformLocation(self->prog_easu, "con0"), 1, c0);
    glUniform4uiv(glGetUniformLocation(self->prog_easu, "con1"), 1, c1);
    glUniform4uiv(glGetUniformLocation(self->prog_easu, "con2"), 1, c2);
    glUniform4uiv(glGetUniformLocation(self->prog_easu, "con3"), 1, c3);
    glUseProgram(self->prog_rcas);
    glUniform1i(glGetUniformLocation(self->prog_rcas, "srcTex"), 0);
    glUniform4uiv(glGetUniformLocation(self->prog_rcas, "con"), 1, rc);
    glGetError();
    return self;
}

int fsr_upscale(fsr_upscaler *self, const unsigned char *src_rgba, unsigned char *dst_rgba) {
    eglMakeCurrent(self->dpy, self->surf, self->surf, self->ctx);  /* contexts are per-thread: re-assert ours */
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, self->src_tex);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, self->src_w, self->src_h, GL_RGBA, GL_UNSIGNED_BYTE, src_rgba);
    glViewport(0, 0, self->dst_w, self->dst_h);
    glBindFramebuffer(GL_FRAMEBUFFER, self->fbo_mid);
    glUseProgram(self->prog_easu);
    glBindTexture(GL_TEXTURE_2D, self->src_tex);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glBindFramebuffer(GL_FRAMEBUFFER, self->fbo_dst);
    glUseProgram(self->prog_rcas);
    glBindTexture(GL_TEXTURE_2D, self->mid_tex);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glReadPixels(0, 0, self->dst_w, self->dst_h, GL_RGBA, GL_UNSIGNED_BYTE, dst_rgba);
    GLenum e = glGetError();
    if (e != GL_NO_ERROR) { snprintf(self->err, sizeof self->err, "gl error during upscale: 0x%x", e); return -1; }
    return 0;
}

const char* fsr_last_error(fsr_upscaler *self) { return self->err; }

void fsr_close(fsr_upscaler *self) {
    if (!self) return;
    if (self->dpy) {
        eglMakeCurrent(self->dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
        if (self->ctx) eglDestroyContext(self->dpy, self->ctx);
        if (self->surf != EGL_NO_SURFACE) eglDestroySurface(self->dpy, self->surf);
        eglTerminate(self->dpy);
    }
    free(self);
}
