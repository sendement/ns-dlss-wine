// SPDX-License-Identifier: MIT
// nsproxy - Hyprland plugin (skeleton, step 1): the fault shield + hyprctl control/self-test commands.
#define WLR_USE_UNSTABLE
#include <hyprland/src/plugins/PluginAPI.hpp>
#include <hyprland/src/event/EventBus.hpp>
#include <hyprland/src/debug/log/Logger.hpp>
#include <hyprland/src/SharedDefs.hpp>

#include <hyprland/src/Compositor.hpp>
#include <hyprland/src/desktop/state/WindowState.hpp>
#include <hyprland/src/desktop/view/Window.hpp>
#include <hyprland/src/output/Monitor.hpp>
#include <hyprland/src/render/Renderer.hpp>
#include <hyprland/src/render/OpenGL.hpp>
#include <hyprland/src/protocols/core/Subcompositor.hpp>
#include <hyprland/src/protocols/core/Compositor.hpp>
#include <hyprland/src/render/gl/GLTexture.hpp>
#include <hyprland/src/render/pass/PassElement.hpp>
#include <hyprland/src/managers/eventLoop/EventLoopManager.hpp>
#include <hyprland/src/managers/eventLoop/EventLoopTimer.hpp>
#include <GLES3/gl32.h>
#include <GLES2/gl2ext.h>
#include <emmintrin.h>
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <drm_fourcc.h>

#include "guard.hpp"
#include "bridge.hpp"

inline HANDLE PHANDLE = nullptr;

namespace {
    // ---- the proxied window: export its pixels, draw the pipeline's result over it ----------------------------------------
    struct State {
        PHLWINDOWREF          window;
        std::string           match;
        bool                  attached = false;
        GLuint                pbo[3]   = {0, 0, 0};
        int                   pboW = 0, pboH = 0, pboIdx = 0, pboAge = 0;
        std::chrono::steady_clock::time_point lastExport{};
        int                   maxFps = 120;
        SP<Render::GL::CGLTexture> tex;
        uint64_t              lastRes = 0, lastDamaged = 0;
        int                   texW = 0, texH = 0;
        uint64_t              frame = 0, addedFrame = ~0ULL;
        int                   flush = 0, lastFbH = 0;
        Hyprutils::Signal::CHyprSignalListener commitListener;
        WP<CWLSurfaceResource> listenedRes;
        uint64_t              commitSeq = 0, lastReadCommit = ~0ULL;
        bool                  hadClient = false, inFlight = false;
        int                   lastOutW = 0, lastOutH = 0;
        int                   subsDrawn = 0;
        long                  zeroCopyDraws = 0, zcExports = 0;
        uint64_t              zSeq = 0;
        uint64_t              seenConnect = 0;
        double                exportUs = 0, drawUs = 0; long exportN = 0, drawN = 0;
        bool                  debugRect = false;   // extra repaints still needed so the async readback delivers the LAST change
        long                  exported = 0, drawn = 0, seenPost = 0, forcedDamage = 0, matched = 0, added = 0, draws = 0;
    } S;

    uint64_t nowNs() {
        timespec ts;
        clock_gettime(CLOCK_MONOTONIC, &ts);
        return (uint64_t)ts.tv_sec * 1'000'000'000ULL + ts.tv_nsec;
    }

    void glCheck(const char* what) {
        GLenum e = glGetError();
        if (e != GL_NO_ERROR)
            throw std::runtime_error(std::string("GL error ") + std::to_string(e) + " in " + what);
    }

    void freePbos() {
        if (S.pbo[0])
            glDeleteBuffers(3, S.pbo);
        S.pbo[0] = S.pbo[1] = S.pbo[2] = 0;
        S.pboW = S.pboH = 0;
    }

    // ---- isolated capture: copy the CLIENT's own buffer texture into a private FBO (own shader, GL state saved/restored) -------------
    // Reading the monitor framebuffer instead would include everything drawn there (our previous result, panels, opacity/blur rules).
    struct ClientCopy {
        GLuint prog2d = 0, progExt = 0, progOver = 0, fbo = 0, out = 0;
        bool overFailed = false;
        int w = 0, h = 0;
    } C;

    GLuint compile(GLenum type, const char* src) {
        GLuint sh = glCreateShader(type);
        glShaderSource(sh, 1, &src, nullptr);
        glCompileShader(sh);
        GLint ok = 0;
        glGetShaderiv(sh, GL_COMPILE_STATUS, &ok);
        if (!ok) {
            char log[512] = {};
            glGetShaderInfoLog(sh, sizeof log, nullptr, log);
            glDeleteShader(sh);
            throw std::runtime_error(std::string("shader: ") + log);
        }
        return sh;
    }

    // Textured quad into the private FBO: box = destination rect in FBO pixels (row 0 = top), useAlpha = keep the texture's alpha (else opaque).
    GLuint makeProgram(bool external) {
        static const char* vs = "#version 300 es\nuniform vec4 box;\nuniform vec2 vp;\nuniform vec4 uvRect;\nuniform mat2 uvM;\nuniform vec2 uvT;\nout vec2 uv;\nvoid main(){ vec2 c = vec2(float(gl_VertexID & 1), float((gl_VertexID >> 1) & 1));\n"
                                "uv = uvM * mix(uvRect.xy, uvRect.zw, c) + uvT;\n"
                                "vec2 px = box.xy + c * box.zw; gl_Position = vec4(px.x / vp.x * 2.0 - 1.0, px.y / vp.y * 2.0 - 1.0, 0.0, 1.0); }\n";
        const char* fs2d = "#version 300 es\nprecision highp float;\nin vec2 uv;\nuniform sampler2D tex;\nuniform float useAlpha;\nout vec4 o;\nvoid main(){ vec4 c = texture(tex, uv); o = vec4(c.rgb, mix(1.0, c.a, useAlpha)); if (useAlpha < 0.5) o.rgb = c.rgb; }\n";
        const char* fsExt = "#version 300 es\n#extension GL_OES_EGL_image_external_essl3 : require\nprecision highp float;\nin vec2 uv;\nuniform samplerExternalOES tex;\nuniform float useAlpha;\nout vec4 o;\nvoid main(){ vec4 c = texture(tex, uv); o = vec4(c.rgb, mix(1.0, c.a, useAlpha)); }\n";
        GLuint v = compile(GL_VERTEX_SHADER, vs), f = compile(GL_FRAGMENT_SHADER, external ? fsExt : fs2d);
        GLuint p = glCreateProgram();
        glAttachShader(p, v);
        glAttachShader(p, f);
        glLinkProgram(p);
        glDeleteShader(v);
        glDeleteShader(f);
        GLint ok = 0;
        glGetProgramiv(p, GL_LINK_STATUS, &ok);
        if (!ok) {
            glDeleteProgram(p);
            throw std::runtime_error("shader link failed");
        }
        return p;
    }

    void freeClientCopy() {
        if (C.prog2d) glDeleteProgram(C.prog2d);
        if (C.progExt) glDeleteProgram(C.progExt);
        if (C.progOver) glDeleteProgram(C.progOver);
        if (C.fbo) glDeleteFramebuffers(1, &C.fbo);
        if (C.out) glDeleteTextures(1, &C.out);
        C = ClientCopy{};
    }

    // Renders the client texture into C.fbo (w x h, RGBA8). Leaves the caller's GL state as it found it, except C.fbo is returned bound to
    // GL_READ_FRAMEBUFFER (the caller restores via the saved value).
    struct SavedGl {
        GLint prog, drawFb, readFb, vao, tex0, texExt, active, packBuf, bsr, bdr, bsa, bda;
        GLint vp[4];
        GLboolean blend, scissor, depth, cull, stencil;
        GLenum target;
    };

    // draws one texture as a quad at `box` (FBO pixels) with premultiplied blending; the FBO is already bound and configured
    // (u,v) = M (a,b) + T maps the surface-space position (a,b in 0..1, origin top-left) to buffer UV. The buffer is the surface rotated/flipped BY the
    // transform (wl_surface.set_buffer_transform), so the compositor applies its inverse: 90 = buffer rotated CCW -> surface = buffer rotated CW.
    struct UvXf { float m[4]; float t[2]; };
    UvXf uvTransform(int tr) {
        static const float tab[8][6] = {   // m00 m01 t0 | m10 m11 t1
            {1, 0, 0, 0, 1, 0}, {0, 1, 0, -1, 0, 1}, {-1, 0, 1, 0, -1, 1}, {0, -1, 1, 1, 0, 0},
            {-1, 0, 1, 0, 1, 0}, {0, 1, 0, 1, 0, 0}, {1, 0, 0, 0, -1, 1}, {0, -1, 1, -1, 0, 1}};
        const float* r = tab[tr & 7];
        return UvXf{{r[0], r[3], r[1], r[4]}, {r[2], r[5]}};   // column-major for glUniformMatrix2fv
    }

    void drawQuad(SP<Render::ITexture> t, const CBox& box, int fw, int fh, int transform = 0, const float* uvRect = nullptr) {
        if (!t || !t->ok() || !t->m_texID)
            return;
        const bool ext = t->m_type == Render::TEXTURE_EXTERNAL;
        const GLenum target = ext ? GL_TEXTURE_EXTERNAL_OES : GL_TEXTURE_2D;
        GLuint& prog = ext ? C.progExt : C.prog2d;
        if (!prog)
            prog = makeProgram(ext);
        glUseProgram(prog);
        glUniform1i(glGetUniformLocation(prog, "tex"), 0);
        glUniform4f(glGetUniformLocation(prog, "box"), (float)box.x, (float)box.y, (float)box.w, (float)box.h);
        glUniform2f(glGetUniformLocation(prog, "vp"), (float)fw, (float)fh);
        glUniform1f(glGetUniformLocation(prog, "useAlpha"), t->m_type == Render::TEXTURE_RGBX ? 0.f : 1.f);
        const UvXf xf = uvTransform(transform);
        static const float full[4] = {0.f, 0.f, 1.f, 1.f};
        const float* rc = uvRect ? uvRect : full;
        glUniform4f(glGetUniformLocation(prog, "uvRect"), rc[0], rc[1], rc[2], rc[3]);
        glUniformMatrix2fv(glGetUniformLocation(prog, "uvM"), 1, GL_FALSE, xf.m);
        glUniform2f(glGetUniformLocation(prog, "uvT"), xf.t[0], xf.t[1]);
        glBindTexture(target, t->m_texID);
        glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    }

    // The surface tree in paint order: subsurfaces below the parent (z < 0), the parent, then those above - recursively. Positions are
    // surface-local logical coordinates; `k` converts logical -> FBO pixels.
    void drawSurfaceTree(SP<CWLSurfaceResource> surf, Vector2D offset, double k, int fw, int fh, int depth) {
        if (!surf || depth > 6)
            return;
        std::vector<SP<CWLSubsurfaceResource>> below, above;
        for (auto& wsub : surf->m_subsurfaces) {
            auto sub = wsub.lock();
            if (!sub || !sub->m_surface.lock() || !sub->m_surface.lock()->m_mapped)
                continue;
            (sub->m_zIndex < 0 ? below : above).push_back(sub);
        }
        auto walk = [&](std::vector<SP<CWLSubsurfaceResource>>& v) {
            for (auto& sub : v)
                drawSurfaceTree(sub->m_surface.lock(), offset + sub->m_position, k, fw, fh, depth + 1);
        };
        walk(below);
        auto t = surf->m_current.texture;
        if (t) {
            const Vector2D sz = surf->m_current.size;
            const auto& st = surf->m_current;
            const int tr = (int)st.transform;
            const bool swap = tr & 1;                                   // 90/270 (and their flipped forms) swap the axes
            const double tw = swap ? t->m_size.y : t->m_size.x, th = swap ? t->m_size.x : t->m_size.y;   // buffer size in surface space, px
            float rc[4] = {0.f, 0.f, 1.f, 1.f};
            if (st.viewport.hasSource && tw > 0 && th > 0) {            // wp_viewport source rect: surface-local units (buffer px / scale)
                const double sc = std::max(1, st.scale);
                rc[0] = (float)(st.viewport.source.x * sc / tw);  rc[1] = (float)(st.viewport.source.y * sc / th);
                rc[2] = (float)((st.viewport.source.x + st.viewport.source.w) * sc / tw);  rc[3] = (float)((st.viewport.source.y + st.viewport.source.h) * sc / th);
            }
            drawQuad(t, CBox{offset.x * k, offset.y * k, sz.x * k, sz.y * k}, fw, fh, tr, rc);
            if (depth > 0)
                S.subsDrawn++;
        }
        walk(above);
    }

    void copyClientTexture(SP<CWLSurfaceResource> res, SP<Render::ITexture> tex, int w, int h, SavedGl& sv, GLuint targetFbo = 0) {   // w x h = OUTPUT size; targetFbo: render there instead of the private FBO
        sv.target = tex->m_type == Render::TEXTURE_EXTERNAL ? GL_TEXTURE_EXTERNAL_OES : GL_TEXTURE_2D;
        glGetIntegerv(GL_CURRENT_PROGRAM, &sv.prog);
        glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &sv.drawFb);
        glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &sv.readFb);
        glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &sv.vao);
        glGetIntegerv(GL_ACTIVE_TEXTURE, &sv.active);
        glGetIntegerv(GL_PIXEL_PACK_BUFFER_BINDING, &sv.packBuf);
        glGetIntegerv(GL_VIEWPORT, sv.vp);
        sv.blend = glIsEnabled(GL_BLEND); sv.scissor = glIsEnabled(GL_SCISSOR_TEST); sv.depth = glIsEnabled(GL_DEPTH_TEST);
        sv.cull = glIsEnabled(GL_CULL_FACE); sv.stencil = glIsEnabled(GL_STENCIL_TEST);
        glActiveTexture(GL_TEXTURE0);
        GLint t2d = 0, text = 0;
        glGetIntegerv(GL_TEXTURE_BINDING_2D, &t2d);
        glGetIntegerv(GL_TEXTURE_BINDING_EXTERNAL_OES, &text);
        sv.tex0 = t2d;
        sv.texExt = text;
        GLint bsr, bdr, bsa, bda;
        glGetIntegerv(GL_BLEND_SRC_RGB, &bsr); glGetIntegerv(GL_BLEND_DST_RGB, &bdr);
        glGetIntegerv(GL_BLEND_SRC_ALPHA, &bsa); glGetIntegerv(GL_BLEND_DST_ALPHA, &bda);
        sv.bsr = bsr; sv.bdr = bdr; sv.bsa = bsa; sv.bda = bda;

        if (targetFbo) {
            glBindFramebuffer(GL_FRAMEBUFFER, targetFbo);
        } else {
        if (!C.fbo || C.w != w || C.h != h) {
            if (C.fbo) glDeleteFramebuffers(1, &C.fbo);
            if (C.out) glDeleteTextures(1, &C.out);
            glGenTextures(1, &C.out);
            glBindTexture(GL_TEXTURE_2D, C.out);
            glTexStorage2D(GL_TEXTURE_2D, 1, GL_RGBA8, w, h);
            glBindTexture(GL_TEXTURE_2D, t2d);
            glGenFramebuffers(1, &C.fbo);
            glBindFramebuffer(GL_FRAMEBUFFER, C.fbo);
            glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, C.out, 0);
            if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE)
                throw std::runtime_error("private FBO incomplete");
            C.w = w; C.h = h;
        }
        glBindFramebuffer(GL_FRAMEBUFFER, C.fbo);
        }
        glViewport(0, 0, w, h);
        glDisable(GL_SCISSOR_TEST); glDisable(GL_DEPTH_TEST); glDisable(GL_CULL_FACE); glDisable(GL_STENCIL_TEST);
        glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
        glClearColor(0.f, 0.f, 0.f, 0.f);
        glClear(GL_COLOR_BUFFER_BIT);
        glEnable(GL_BLEND);
        glBlendFuncSeparate(GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);   // Wayland buffers are premultiplied
        glBindVertexArray(0);
        const Vector2D logical = res->m_current.size;
        const double k = logical.x > 0 ? (double)w / logical.x : 1.0;
        S.subsDrawn = 0;
        drawSurfaceTree(res, Vector2D{0, 0}, k, w, h, 0);
        glBindTexture(GL_TEXTURE_2D, t2d);
        glBindTexture(GL_TEXTURE_EXTERNAL_OES, text);
        glActiveTexture(sv.active);
    }

    void restoreGl(const SavedGl& sv) {
        glBindFramebuffer(GL_DRAW_FRAMEBUFFER, sv.drawFb);
        glBindFramebuffer(GL_READ_FRAMEBUFFER, sv.readFb);
        glBindVertexArray(sv.vao);
        glUseProgram(sv.prog);
        glViewport(sv.vp[0], sv.vp[1], sv.vp[2], sv.vp[3]);
        (sv.blend ? glEnable : glDisable)(GL_BLEND);
        (sv.scissor ? glEnable : glDisable)(GL_SCISSOR_TEST);
        (sv.depth ? glEnable : glDisable)(GL_DEPTH_TEST);
        (sv.cull ? glEnable : glDisable)(GL_CULL_FACE);
        (sv.stencil ? glEnable : glDisable)(GL_STENCIL_TEST);
        glBlendFuncSeparate(sv.bsr, sv.bdr, sv.bsa, sv.bda);
        glBindBuffer(GL_PIXEL_PACK_BUFFER, sv.packBuf);
    }

    // Copy the window's own buffer (isolated from everything the compositor draws) when the client COMMITTED a new one, and deliver it
    // through a 3-deep PBO ring (async, no GPU stall): a read issued now is complete two draws later, so a few extra repaints (`flush`)
    // are requested until nothing is in flight. Change detection is the surface's own `commit` signal - no pixel comparison.
    // EGL dma-buf import entry points (resolved lazily, shared by the zero-copy export and result paths)
    PFNEGLCREATEIMAGEKHRPROC pEglCreateImage = nullptr;
    PFNEGLDESTROYIMAGEKHRPROC pEglDestroyImage = nullptr;
    PFNGLEGLIMAGETARGETTEXTURE2DOESPROC pImageTarget = nullptr;

    // ---- zero-copy export: the composite is rendered straight into a dma-buf slot of the shared memory (EGLImage-backed FBO), then published
    // once a GPU fence has signalled - no PBO, no memcpy. Any failure drops to the PBO path below for good.
    struct ZcExp { EGLImageKHR img = EGL_NO_IMAGE_KHR; GLuint tex = 0, fbo = 0; int w = 0, h = 0; };
    ZcExp g_zExp[3];
    bool g_zcExpOk = getenv("NSPROXY_ZEROCOPY") ? std::string(getenv("NSPROXY_ZEROCOPY")) != "0" : true;
    struct ZPending { uint64_t seq; GLsync fence; int w, h; };
    std::vector<ZPending> g_zPend;

    void zcExpDestroy(ZcExp& z) {
        if (z.fbo) glDeleteFramebuffers(1, &z.fbo);
        if (z.tex) glDeleteTextures(1, &z.tex);
        if (z.img != EGL_NO_IMAGE_KHR && pEglDestroyImage) pEglDestroyImage(eglGetCurrentDisplay(), z.img);
        z = ZcExp{};
    }

    void zcExpDropPending() {
        for (auto& p : g_zPend) if (p.fence) glDeleteSync(p.fence);
        g_zPend.clear();
    }

    GLuint zcExpTarget(int slot, int w, int h) {
        ZcExp& z = g_zExp[slot];
        if (z.fbo && z.w == w && z.h == h)
            return z.fbo;
        if (!pEglCreateImage) {
            pEglCreateImage = (PFNEGLCREATEIMAGEKHRPROC)eglGetProcAddress("eglCreateImageKHR");
            pEglDestroyImage = (PFNEGLDESTROYIMAGEKHRPROC)eglGetProcAddress("eglDestroyImageKHR");
            pImageTarget = (PFNGLEGLIMAGETARGETTEXTURE2DOESPROC)eglGetProcAddress("glEGLImageTargetTexture2DOES");
        }
        if (!pEglCreateImage || !pEglDestroyImage || !pImageTarget)
            throw std::runtime_error("EGL dma-buf import entry points missing");
        zcExpDestroy(z);
        const int fd = ns::Bridge::get().slotDmabuf(false, slot);
        if (fd < 0)
            throw std::runtime_error("udmabuf unavailable");
        const EGLint attr[] = {EGL_WIDTH, w, EGL_HEIGHT, h, EGL_LINUX_DRM_FOURCC_EXT, (EGLint)DRM_FORMAT_ABGR8888,
                               EGL_DMA_BUF_PLANE0_FD_EXT, fd, EGL_DMA_BUF_PLANE0_OFFSET_EXT, 0,
                               EGL_DMA_BUF_PLANE0_PITCH_EXT, (EGLint)nsproxy::pitchFor(w),
                               EGL_DMA_BUF_PLANE0_MODIFIER_LO_EXT, 0, EGL_DMA_BUF_PLANE0_MODIFIER_HI_EXT, 0, EGL_NONE};
        z.img = pEglCreateImage(eglGetCurrentDisplay(), EGL_NO_CONTEXT, EGL_LINUX_DMA_BUF_EXT, nullptr, attr);   // ABGR8888 = bytes R,G,B,A in memory
        if (z.img == EGL_NO_IMAGE_KHR)
            throw std::runtime_error("eglCreateImage(export dma-buf) failed: 0x" + std::to_string(eglGetError()));
        glGenTextures(1, &z.tex);
        GLint prev = 0;
        glGetIntegerv(GL_TEXTURE_BINDING_2D, &prev);
        glBindTexture(GL_TEXTURE_2D, z.tex);
        pImageTarget(GL_TEXTURE_2D, (GLeglImageOES)z.img);
        glBindTexture(GL_TEXTURE_2D, prev);
        GLint pf = 0, df = 0;
        glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &df);
        glGenFramebuffers(1, &z.fbo);
        glBindFramebuffer(GL_FRAMEBUFFER, z.fbo);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, z.tex, 0);
        const GLenum st = glCheckFramebufferStatus(GL_FRAMEBUFFER);
        glBindFramebuffer(GL_FRAMEBUFFER, df);
        (void)pf;
        if (st != GL_FRAMEBUFFER_COMPLETE)
            throw std::runtime_error("dma-buf FBO incomplete: 0x" + std::to_string(st));
        z.w = w; z.h = h;
        glCheck("zero-copy export target");
        ns::Guard::get().log("zero-copy export slot " + std::to_string(slot) + " " + std::to_string(w) + "x" + std::to_string(h));
        return z.fbo;
    }

    void exportZc(SP<CWLSurfaceResource> res, SP<Render::ITexture> tex, int w, int h, const std::chrono::steady_clock::time_point& now) {
        auto& B = ns::Bridge::get();
        auto* hd = B.hdr();
        // size change: what is in flight was rendered at the old size
        if (!g_zPend.empty() && (g_zPend.back().w != w || g_zPend.back().h != h))
            zcExpDropPending();
        // 1) publish the newest frame whose GPU work has finished; older ones are dropped
        int done = -1;
        for (int i = (int)g_zPend.size() - 1; i >= 0; i--) {
            const GLenum r = glClientWaitSync(g_zPend[i].fence, 0, 0);
            if (r == GL_ALREADY_SIGNALED || r == GL_CONDITION_SATISFIED) { done = i; break; }
        }
        if (done >= 0) {
            const auto& p = g_zPend[done];
            const int slot = (int)(p.seq % nsproxy::kSlots);
            hd->exp_w[slot] = p.w;
            hd->exp_h[slot] = p.h;
            __atomic_store_n(&hd->exp_seq, p.seq, __ATOMIC_RELEASE);
            B.notify();
            S.exported++;
            for (int i = 0; i <= done; i++) glDeleteSync(g_zPend[i].fence);
            g_zPend.erase(g_zPend.begin(), g_zPend.begin() + done + 1);
        }
        // 2) a new client buffer -> render it into the next slot
        const bool wantRead = S.commitSeq != S.lastReadCommit && g_zPend.size() < 2 &&
                              now - S.lastExport >= std::chrono::microseconds(1'000'000 / std::max(1, S.maxFps));
        if (wantRead) {
            S.lastExport = now;
            S.lastReadCommit = S.commitSeq;
            S.zSeq = std::max(S.zSeq, __atomic_load_n(&hd->exp_seq, __ATOMIC_RELAXED)) + 1;
            const int slot = (int)(S.zSeq % nsproxy::kSlots);
            const GLuint fbo = zcExpTarget(slot, w, h);
            SavedGl sv{};
            copyClientTexture(res, tex, w, h, sv, fbo);
            GLsync f = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
            glFlush();
            restoreGl(sv);
            glCheck("exportZc");
            g_zPend.push_back(ZPending{S.zSeq, f, w, h});
            S.zcExports++;
        }
        S.inFlight = !g_zPend.empty();
    }

    struct Pending { bool on = false; int age = 0; int w = 0, h = 0; uint64_t commit = 0; };
    Pending g_pend[3];

    void ensureCommitListener() {
        auto win = S.window.lock();
        SP<CWLSurfaceResource> res = (win && win->wlSurface()) ? win->wlSurface()->resource() : nullptr;
        if (res == S.listenedRes.lock())
            return;
        S.commitListener.reset();
        S.listenedRes = res;
        if (res)
            S.commitListener = res->m_events.commit.listen([] { S.commitSeq++; });
    }

    void exportFrame() {
        auto& B = ns::Bridge::get();
        auto win = S.window.lock();
        if (!B.client() || !win || !win->wlSurface()) {
            S.hadClient = false;
            return;
        }
        if (!S.hadClient || S.seenConnect != B.connectSeq()) {   // new pipeline client: send the current picture even if the window is static
            S.hadClient = true;
            S.seenConnect = B.connectSeq();
            S.lastReadCommit = ~0ULL;
        }
        ensureCommitListener();
        auto res = win->wlSurface()->resource();
        if (!res)
            return;
        SP<Render::ITexture> tex = res->m_current.texture;
        if (!tex || !tex->ok() || !tex->m_texID)
            return;
        // native export size = the surface as the compositor sizes it (logical size x buffer scale); with a viewport source rect, that rect in px.
        // (For odd buffer transforms Hyprland keeps the unswapped size and stretches the rotated texture into it - we mirror what is shown.)
        int nw, nh;
        {
            const auto& st = res->m_current;
            const double sc = std::max(1, st.scale);
            nw = (int)std::lround(st.size.x * sc);
            nh = (int)std::lround(st.size.y * sc);
            if (st.viewport.hasSource && st.viewport.source.w > 0 && st.viewport.source.h > 0) {
                nw = (int)std::lround(st.viewport.source.w * sc);
                nh = (int)std::lround(st.viewport.source.h * sc);
            }
            if (nw <= 0 || nh <= 0) { nw = (int)tex->m_size.x; nh = (int)tex->m_size.y; }
            if (nw > (int)nsproxy::kCapW || nh > (int)nsproxy::kCapH)
                return;
        }
        int w = nw, h = nh;   // export size: what the pipeline asked for (never above native), else native
        {
            const uint32_t ww = __atomic_load_n(&B.hdr()->want_w, __ATOMIC_RELAXED), wh = __atomic_load_n(&B.hdr()->want_h, __ATOMIC_RELAXED);
            if (ww >= 16 && wh >= 16 && (int)ww <= nw && (int)wh <= nh) { w = (int)ww; h = (int)wh; }
        }
        if (w != S.lastOutW || h != S.lastOutH) {   // size changed: what is in flight has the wrong size
            S.lastOutW = w; S.lastOutH = h;
            S.lastReadCommit = ~0ULL;
        }
        const size_t bytes = (size_t)w * h * 4;
        const auto now = std::chrono::steady_clock::now();
        const bool coherentClient = (__atomic_load_n(&B.hdr()->client_flags, __ATOMIC_ACQUIRE) & 1) != 0;
        if (!coherentClient && !g_zPend.empty())
            zcExpDropPending();      // switched to the copying path: what is in flight in dma-buf slots is abandoned
        if (g_zcExpOk && coherentClient) {
            try {
                exportZc(res, tex, w, h, now);
                return;
            } catch (const std::exception& e) {   // (GL state is restored by exportZc's callers only on success - do it defensively here)
                g_zcExpOk = false;
                zcExpDropPending();
                for (auto& z : g_zExp) zcExpDestroy(z);
                S.lastReadCommit = ~0ULL;
                ns::Guard::get().log(std::string("zero-copy export unavailable, using the PBO path: ") + e.what());
            }
        }

        SavedGl sv{};
        bool saved = false;
        auto begin = [&] {
            if (saved) return;
            saved = true;
            copyClientTexture(res, tex, w, h, sv);   // C.fbo is now bound (draw+read)
            if (w != S.pboW || h != S.pboH) {
                freePbos();
                for (auto& q : g_pend) q = Pending{};
                glGenBuffers(3, S.pbo);
                for (GLuint b : S.pbo) {
                    glBindBuffer(GL_PIXEL_PACK_BUFFER, b);
                    glBufferData(GL_PIXEL_PACK_BUFFER, (GLsizeiptr)bytes, nullptr, GL_STREAM_READ);
                }
                S.pboW = w; S.pboH = h; S.pboIdx = 0;
            }
        };
        static bool sv_valid = false; (void)sv_valid;

        // 1) a new client buffer since the last read -> issue an async read of it
        const bool wantRead = S.commitSeq != S.lastReadCommit && now - S.lastExport >= std::chrono::microseconds(1'000'000 / std::max(1, S.maxFps));
        if (wantRead) {
            S.lastExport = now;
            S.lastReadCommit = S.commitSeq;
            begin();
            glBindBuffer(GL_PIXEL_PACK_BUFFER, S.pbo[S.pboIdx]);
            glPixelStorei(GL_PACK_ALIGNMENT, 1);
            glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);   // texture memory is top-down: row 0 = image top
            g_pend[S.pboIdx] = Pending{true, 0, w, h, S.commitSeq};
            S.pboIdx = (S.pboIdx + 1) % 3;
        }
        // 2) deliver the newest read that has had time to complete; older ones are dropped
        int best = -1;
        for (int i = 0; i < 3; i++)
            if (g_pend[i].on && g_pend[i].age >= 2 && (best < 0 || g_pend[i].commit > g_pend[best].commit))
                best = i;
        if (best >= 0) {
            begin();
            glBindBuffer(GL_PIXEL_PACK_BUFFER, S.pbo[best]);
            void* p = glMapBufferRange(GL_PIXEL_PACK_BUFFER, 0, (GLsizeiptr)((size_t)g_pend[best].w * g_pend[best].h * 4), GL_MAP_READ_BIT);
            if (p) {
                auto* hd = B.hdr();
                const uint64_t seq = __atomic_load_n(&hd->exp_seq, __ATOMIC_RELAXED) + 1;
                const int slot = (int)(seq % nsproxy::kSlots);
                {   // tight PBO rows -> pitch-aligned slot rows
                    const size_t rowB = (size_t)g_pend[best].w * 4, pitch = nsproxy::pitchFor(g_pend[best].w);
                    uint8_t* dst = B.expSlot(slot);
                    if (rowB == pitch) std::memcpy(dst, p, rowB * g_pend[best].h);
                    else for (int r = 0; r < g_pend[best].h; r++) std::memcpy(dst + r * pitch, static_cast<uint8_t*>(p) + r * rowB, rowB);
                }
                glUnmapBuffer(GL_PIXEL_PACK_BUFFER);
                hd->exp_w[slot] = g_pend[best].w;
                hd->exp_h[slot] = g_pend[best].h;
                __atomic_store_n(&hd->exp_seq, seq, __ATOMIC_RELEASE);
                B.notify();
                S.exported++;
            }
            for (int i = 0; i < 3; i++)
                if (g_pend[i].on && g_pend[i].commit <= g_pend[best].commit)
                    g_pend[i].on = false;
        }
        for (auto& q : g_pend)
            if (q.on) q.age++;
        S.inFlight = g_pend[0].on || g_pend[1].on || g_pend[2].on;
        if (saved) {
            glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
            restoreGl(sv);
            glCheck("exportFrame");
        }
    }

    // ---- zero-copy result upload: the result slots are dma-bufs (udmabuf over the shared memfd) imported as EGLImages ---------------------
    struct ZcImg { EGLImageKHR img = EGL_NO_IMAGE_KHR; GLuint tex = 0; int w = 0, h = 0; };
    ZcImg g_zRes[3];
    bool g_zcOk = getenv("NSPROXY_ZEROCOPY") ? std::string(getenv("NSPROXY_ZEROCOPY")) != "0" : true;

    void zcDestroy(ZcImg& z) {
        if (z.tex) glDeleteTextures(1, &z.tex);
        if (z.img != EGL_NO_IMAGE_KHR && pEglDestroyImage)
            pEglDestroyImage(eglGetCurrentDisplay(), z.img);
        z = ZcImg{};
    }

    GLuint zcResultTexture(int slot, int w, int h) {
        ZcImg& z = g_zRes[slot];
        if (z.tex && z.w == w && z.h == h)
            return z.tex;
        if (!pEglCreateImage) {
            pEglCreateImage = (PFNEGLCREATEIMAGEKHRPROC)eglGetProcAddress("eglCreateImageKHR");
            pEglDestroyImage = (PFNEGLDESTROYIMAGEKHRPROC)eglGetProcAddress("eglDestroyImageKHR");
            pImageTarget = (PFNGLEGLIMAGETARGETTEXTURE2DOESPROC)eglGetProcAddress("glEGLImageTargetTexture2DOES");
        }
        if (!pEglCreateImage || !pEglDestroyImage || !pImageTarget)
            throw std::runtime_error("EGL dma-buf import entry points missing");
        zcDestroy(z);
        const int fd = ns::Bridge::get().slotDmabuf(true, slot);
        if (fd < 0)
            throw std::runtime_error("udmabuf unavailable");
        const EGLint attr[] = {EGL_WIDTH, w, EGL_HEIGHT, h, EGL_LINUX_DRM_FOURCC_EXT, (EGLint)DRM_FORMAT_ARGB8888,
                               EGL_DMA_BUF_PLANE0_FD_EXT, fd, EGL_DMA_BUF_PLANE0_OFFSET_EXT, 0,
                               EGL_DMA_BUF_PLANE0_PITCH_EXT, (EGLint)nsproxy::pitchFor(w),
                               EGL_DMA_BUF_PLANE0_MODIFIER_LO_EXT, 0, EGL_DMA_BUF_PLANE0_MODIFIER_HI_EXT, 0, EGL_NONE};
        z.img = pEglCreateImage(eglGetCurrentDisplay(), EGL_NO_CONTEXT, EGL_LINUX_DMA_BUF_EXT, nullptr, attr);
        if (z.img == EGL_NO_IMAGE_KHR)
            throw std::runtime_error("eglCreateImage(dma-buf) failed: 0x" + std::to_string(eglGetError()));
        glGenTextures(1, &z.tex);
        GLint prev = 0;
        glGetIntegerv(GL_TEXTURE_BINDING_2D, &prev);
        glBindTexture(GL_TEXTURE_2D, z.tex);
        pImageTarget(GL_TEXTURE_2D, (GLeglImageOES)z.img);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        glBindTexture(GL_TEXTURE_2D, prev);
        z.w = w; z.h = h;
        glCheck("zero-copy import");
        ns::Guard::get().log("zero-copy result slot " + std::to_string(slot) + " imported " + std::to_string(w) + "x" + std::to_string(h));
        return z.tex;
    }

    // Draws S.tex into the box (monitor pixels) with rounded corners of radius `radius`, alpha-blended, in the CURRENT framebuffer/scissor
    // (the compositor's damage clipping stays in force). Own shader, so the corner shape matches the window's own rounding and the
    // compositor's border/shadow decorations stay visible around it.
    bool drawRounded(const CBox& box, float radius, float rpow, GLuint zTex = 0) {
        if (C.overFailed)
            return false;
        if (!C.progOver) {
            try {
                static const char* vs = "#version 300 es\nuniform vec4 box;\nuniform vec2 vp;\nout vec2 uv;\nvoid main(){ vec2 c = vec2(float(gl_VertexID & 1), float((gl_VertexID >> 1) & 1)); uv = c;\n"
                                        "vec2 px = box.xy + c * box.zw; gl_Position = vec4(px.x / vp.x * 2.0 - 1.0, px.y / vp.y * 2.0 - 1.0, 0.0, 1.0); }\n";
                static const char* fs = "#version 300 es\nprecision highp float;\nin vec2 uv;\nuniform sampler2D tex;\nuniform vec4 box;\nuniform float radius;\nuniform float rpow;\nout vec4 o;\n"
                                        "void main(){ vec2 px = uv * box.zw; vec2 e = min(px, box.zw - px); float cov = 1.0;\n"
                                        "if (e.x < radius && e.y < radius) { vec2 v = vec2(radius) - e; float dist = pow(pow(v.x, rpow) + pow(v.y, rpow), 1.0 / rpow); cov = clamp(radius - dist + 0.5, 0.0, 1.0); }\n"
                                        "vec4 c = texture(tex, uv); o = c * cov; }\n";
                GLuint v = compile(GL_VERTEX_SHADER, vs), f = compile(GL_FRAGMENT_SHADER, fs);
                GLuint pr = glCreateProgram();
                glAttachShader(pr, v); glAttachShader(pr, f); glLinkProgram(pr);
                glDeleteShader(v); glDeleteShader(f);
                GLint ok = 0;
                glGetProgramiv(pr, GL_LINK_STATUS, &ok);
                if (!ok) { glDeleteProgram(pr); throw std::runtime_error("over shader link failed"); }
                C.progOver = pr;
            } catch (const std::exception& e) {
                C.overFailed = true;
                ns::Guard::get().log(std::string("rounded draw unavailable, falling back to a plain rectangle: ") + e.what());
                return false;
            }
        }
        GLint prog, vao, active, tex0, srcRgb, dstRgb, srcA, dstA, vp[4];
        GLboolean blend = glIsEnabled(GL_BLEND), cull = glIsEnabled(GL_CULL_FACE), depth = glIsEnabled(GL_DEPTH_TEST), stencil = glIsEnabled(GL_STENCIL_TEST);
        GLboolean cmask[4];
        glGetBooleanv(GL_COLOR_WRITEMASK, cmask);
        glGetIntegerv(GL_CURRENT_PROGRAM, &prog);
        glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &vao);
        glGetIntegerv(GL_ACTIVE_TEXTURE, &active);
        glGetIntegerv(GL_VIEWPORT, vp);
        glGetIntegerv(GL_BLEND_SRC_RGB, &srcRgb); glGetIntegerv(GL_BLEND_DST_RGB, &dstRgb);
        glGetIntegerv(GL_BLEND_SRC_ALPHA, &srcA); glGetIntegerv(GL_BLEND_DST_ALPHA, &dstA);
        glActiveTexture(GL_TEXTURE0);
        glGetIntegerv(GL_TEXTURE_BINDING_2D, &tex0);
        glUseProgram(C.progOver);
        glBindVertexArray(0);
        if (zTex) {
            glBindTexture(GL_TEXTURE_2D, zTex);
        } else {
            S.tex->bind();
            // a fresh GL texture defaults to a mipmapped MIN_FILTER and is 'incomplete' (samples black) until the filters are set
            S.tex->setTexParameter(GL_TEXTURE_MIN_FILTER, GL_LINEAR);
            S.tex->setTexParameter(GL_TEXTURE_MAG_FILTER, GL_LINEAR);
            S.tex->setTexParameter(GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            S.tex->setTexParameter(GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        }
        glUniform1i(glGetUniformLocation(C.progOver, "tex"), 0);
        glUniform4f(glGetUniformLocation(C.progOver, "box"), (float)box.x, (float)box.y, (float)box.w, (float)box.h);
        glUniform2f(glGetUniformLocation(C.progOver, "vp"), (float)vp[2], (float)vp[3]);
        glUniform1f(glGetUniformLocation(C.progOver, "rpow"), std::max(1.f, rpow));
        glUniform1f(glGetUniformLocation(C.progOver, "radius"), std::max(0.f, std::min(radius, (float)std::min(box.w, box.h) * 0.5f)));
        glEnable(GL_BLEND);
        glDisable(GL_CULL_FACE); glDisable(GL_DEPTH_TEST); glDisable(GL_STENCIL_TEST);
        glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
        glBlendFuncSeparate(GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
        static bool logged = false;
        if (!logged && getenv("NSPROXY_DEBUG")) {
            logged = true;
            GLint sc[4]; glGetIntegerv(GL_SCISSOR_BOX, sc);
            ns::Guard::get().log("rounded draw: vp " + std::to_string(vp[0]) + "," + std::to_string(vp[1]) + "," + std::to_string(vp[2]) + "," + std::to_string(vp[3]) + " box " + std::to_string(box.x) + "," + std::to_string(box.y) + "," + std::to_string(box.w) + "," + std::to_string(box.h) + " radius " + std::to_string(radius) + " scissor " + std::to_string(glIsEnabled(GL_SCISSOR_TEST)) + ":" + std::to_string(sc[0]) + "," + std::to_string(sc[1]) + "," + std::to_string(sc[2]) + "," + std::to_string(sc[3]) + " tex " + std::to_string(S.tex->m_texID) + " cull " + std::to_string(cull) + " depth " + std::to_string(depth) + " stencil " + std::to_string(stencil) + " mask " + std::to_string(cmask[0]) + std::to_string(cmask[1]) + std::to_string(cmask[2]) + std::to_string(cmask[3]));
        }
        // honour the compositor's damage like its own elements do: one scissored draw per damage rect (monitor pixels, row 0 = top)
        GLboolean scEnabled = glIsEnabled(GL_SCISSOR_TEST);
        GLint scBox[4];
        glGetIntegerv(GL_SCISSOR_BOX, scBox);
        glEnable(GL_SCISSOR_TEST);
        for (const auto& r : g_pHyprRenderer->m_renderData.damage.getRects()) {
            const int x1 = std::max<int>(r.x1, (int)box.x), y1 = std::max<int>(r.y1, (int)box.y);
            const int x2 = std::min<int>(r.x2, (int)(box.x + box.w)), y2 = std::min<int>(r.y2, (int)(box.y + box.h));
            if (x2 <= x1 || y2 <= y1)
                continue;
            glScissor(x1, y1, x2 - x1, y2 - y1);
            glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
        }
        glScissor(scBox[0], scBox[1], scBox[2], scBox[3]);
        (scEnabled ? glEnable : glDisable)(GL_SCISSOR_TEST);
        glBlendFuncSeparate(srcRgb, dstRgb, srcA, dstA);
        (blend ? glEnable : glDisable)(GL_BLEND);
        (cull ? glEnable : glDisable)(GL_CULL_FACE);
        (depth ? glEnable : glDisable)(GL_DEPTH_TEST);
        (stencil ? glEnable : glDisable)(GL_STENCIL_TEST);
        glColorMask(cmask[0], cmask[1], cmask[2], cmask[3]);
        glBindTexture(GL_TEXTURE_2D, tex0);
        glActiveTexture(active);
        glBindVertexArray(vao);
        glUseProgram(prog);
        glCheck("drawRounded");
        return true;
    }

    // Draw the pipeline's newest result over the window - unless the pipeline is not delivering (then the real window shows through).
    void drawOverride(int x, int y, int w, int h, float radius, float rpow) {
        auto& B = ns::Bridge::get();
        if (!B.client())
            return;
        auto* hd = B.hdr();
        if (!__atomic_load_n(&hd->override_on, __ATOMIC_ACQUIRE))
            return;
        uint64_t rs = __atomic_load_n(&hd->res_seq, __ATOMIC_ACQUIRE);
        if (!rs || nowNs() - __atomic_load_n(&hd->res_ns, __ATOMIC_ACQUIRE) > nsproxy::kResultMaxAgeNs)
            return;
        GLuint zTex = 0;
        const bool clientCoherent = (__atomic_load_n(&hd->client_flags, __ATOMIC_ACQUIRE) & 1) != 0;
        if (g_zcOk && clientCoherent) {   // zero-copy: sample the slot's dma-buf directly; any failure drops to the copying path for good
            try {
                const int slot = (int)(rs % nsproxy::kSlots);
                const int tw = (int)hd->res_w[slot], th = (int)hd->res_h[slot];
                if (tw <= 0 || th <= 0 || tw > (int)nsproxy::kCapW || th > (int)nsproxy::kCapH)
                    throw std::runtime_error("result slot has an invalid size");
                zTex = zcResultTexture(slot, tw, th);
                S.lastRes = rs;
                S.zeroCopyDraws++;
            } catch (const std::exception& e) {
                g_zcOk = false;
                for (auto& z : g_zRes) zcDestroy(z);
                ns::Guard::get().log(std::string("zero-copy upload unavailable, using the copying path: ") + e.what());
            }
        }
        if (!zTex)
        if (rs != S.lastRes) {
            const int slot = (int)(rs % nsproxy::kSlots);
            const int tw = (int)hd->res_w[slot], th = (int)hd->res_h[slot];
            if (tw <= 0 || th <= 0 || tw > (int)nsproxy::kCapW || th > (int)nsproxy::kCapH)
                throw std::runtime_error("result slot has an invalid size");
            auto* px = const_cast<uint8_t*>(B.resSlot(slot));
            if (!S.tex || tw != S.texW || th != S.texH) {
                S.tex = makeShared<Render::GL::CGLTexture>(DRM_FORMAT_ARGB8888, px, nsproxy::pitchFor(tw), Vector2D(tw, th));
                S.texW = tw; S.texH = th;
            } else {
                CRegion full{CBox{0, 0, (double)tw, (double)th}};
                S.tex->update(DRM_FORMAT_ARGB8888, px, nsproxy::pitchFor(tw), full);
            }
            S.lastRes = rs;
            glCheck("result upload");
        }
        if (!zTex && !S.tex)
            return;
        if (S.debugRect) {
            Render::GL::CHyprOpenGLImpl::SRectRenderData rd;
            rd.damage = &g_pHyprRenderer->m_renderData.damage;
            Render::GL::g_pHyprOpenGL->renderRect(CBox{(double)x, (double)y, (double)w / 2, (double)h / 2}, CHyprColor{1.0, 0.0, 1.0, 1.0}, rd);
            S.drawn++;
            return;
        }
        // renderTexture() drew nothing from a pass element (needs render state we do not set up); the primitive path works
        if (!drawRounded(CBox{(double)x, (double)y, (double)w, (double)h}, radius, rpow, zTex) && S.tex)
            Render::GL::g_pHyprOpenGL->renderTexturePrimitive(S.tex, CBox{(double)x, (double)y, (double)w, (double)h});
        S.drawn++;
    }

    class CNsElement : public IPassElement {
      public:
        CNsElement(CBox b, float r, float rp) : box(b), radius(r), rpow(rp) {}
        std::vector<UP<IPassElement>> draw() override {
            ns::Guard::get().run("draw", [&] {
                S.draws++;
                auto t0 = std::chrono::steady_clock::now();
                exportFrame();
                auto t1 = std::chrono::steady_clock::now();
                S.exportUs += std::chrono::duration<double, std::micro>(t1 - t0).count(); S.exportN++;
                drawOverride((int)box.x, (int)box.y, (int)box.w, (int)box.h, radius, rpow);
                S.drawUs += std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - t1).count(); S.drawN++;
            });
            return {};
        }
        bool                needsLiveBlur() override { return false; }
        bool                needsPrecomputeBlur() override { return false; }
        const char*         passName() override { return "nsproxy"; }
        ePassElementType    type() override { return EK_CUSTOM; }
        std::optional<CBox> boundingBox() override { return std::nullopt; }
        bool                disableSimplification() override { return true; }
      private:
        CBox  box;
        float radius, rpow;
    };

    // "address:0x..." selects one window. Otherwise candidates are windows whose class equals the name (else contains it); among them a window on a VISIBLE
    // workspace wins (a hidden one is never rendered, so attaching to it yields no frames), then the first in Hyprland's order.
    PHLWINDOW findWindow(const std::string& m) {
        PHLWINDOW best;
        int bestScore = -1;
        for (auto& w : Desktop::windowState()->windows()) {
            if (!w || !w->m_isMapped)
                continue;
            if (m.rfind("address:", 0) == 0) {
                char buf[32];
                snprintf(buf, sizeof buf, "0x%lx", (unsigned long)(uintptr_t)w.get());
                if (m.substr(8) == buf)
                    return w;
                continue;
            }
            const bool exact = w->m_class == m || w->m_initialClass == m;
            const bool part = !exact && (w->m_class.find(m) != std::string::npos || w->m_initialClass.find(m) != std::string::npos);
            if (!exact && !part)
                continue;
            const bool visible = w->m_workspace && w->m_workspace->isVisible();
            const int score = (exact ? 2 : 0) + (visible ? 4 : 0);   // visible beats everything; exact beats substring
            if (score > bestScore) {
                bestScore = score;
                best = w;
            }
        }
        return best;
    }

    void onStage(eRenderStage stage) {
        if (stage == RENDER_BEGIN) {
            S.frame++;
            return;
        }
        if (stage == RENDER_PRE_WINDOW && S.attached && ns::Bridge::get().client()) {
            // Hyprland only repaints damaged regions; elsewhere the framebuffer still holds OUR previous result, and reading that back
            // would feed the pipeline its own output. Force the whole window to be repainted from the client's buffer every frame.
            auto w = g_pHyprRenderer->m_renderData.currentWindow.lock();
            auto mon = g_pHyprRenderer->m_renderData.pMonitor.lock();
            if (w && mon && w == S.window.lock()) {
                CBox b = w->getWindowMainSurfaceBox();
                b.translate(-mon->m_position).scale(mon->m_scale).round();
                g_pHyprRenderer->m_renderData.damage.add(b);
                S.forcedDamage++;
            }
            return;
        }
        if (stage != RENDER_POST_WINDOW || !S.attached)
            return;
        S.seenPost++;
        auto w = g_pHyprRenderer->m_renderData.currentWindow.lock();
        auto mon = g_pHyprRenderer->m_renderData.pMonitor.lock();
        if (!w || !mon || w != S.window.lock() || S.addedFrame == S.frame)
            return;
        S.matched++;
        S.addedFrame = S.frame;   // tiled windows are rendered in two passes (main + popups): only the first one carries us
        CBox b = w->getWindowMainSurfaceBox();
        b.translate(-mon->m_position).scale(mon->m_scale).round();
        S.added++;
        g_pHyprRenderer->m_renderPass.add(makeUnique<CNsElement>(b, (float)(w->rounding() * mon->m_scale), w->roundingPower()));
    }

    SP<CEventLoopTimer> g_timer;
    void armTimer() {
        g_timer = makeShared<CEventLoopTimer>(std::chrono::milliseconds(4), [](SP<CEventLoopTimer> self, void*) {
            ns::Guard::get().run("timer", [&] {
                // a fullscreen window may be put on the screen directly, bypassing the render pass we hook into: keep that off while attached
                static bool blockedByUs = false;   // never clear a block somebody else (e.g. screen sharing) set
                if (g_pHyprRenderer && S.attached) {
                    g_pHyprRenderer->m_directScanoutBlocked = true;
                    blockedByUs = true;
                } else if (g_pHyprRenderer && blockedByUs) {
                    g_pHyprRenderer->m_directScanoutBlocked = false;
                    blockedByUs = false;
                }
                auto* hd = ns::Bridge::get().hdr();
                if (S.attached && hd) {
                    uint64_t rs = __atomic_load_n(&hd->res_seq, __ATOMIC_ACQUIRE);
                    const bool wantFlush = ns::Bridge::get().client() && (S.inFlight || S.commitSeq != S.lastReadCommit || S.seenConnect != ns::Bridge::get().connectSeq() || __atomic_load_n(&hd->exp_seq, __ATOMIC_RELAXED) == 0);
                    if (wantFlush) {
                        if (auto w = S.window.lock())
                            g_pHyprRenderer->damageWindow(w);
                    }
                    if (rs != S.lastDamaged) {   // new result -> the window must be repainted even if its own content did not change
                        S.lastDamaged = rs;
                        if (auto w = S.window.lock())
                            g_pHyprRenderer->damageWindow(w);
                    }
                }
            });
            self->updateTimeout(std::chrono::milliseconds(4));
        }, nullptr);
        g_pEventLoopManager->addTimer(g_timer);
    }

    std::atomic<int> g_injectRender{0};       // self-test: make the next N render callbacks misbehave
    std::atomic<int> g_injectKind{0};
    std::atomic<long> g_renderCalls{0};
    Hyprutils::Signal::CHyprSignalListener g_renderListener;
    SP<SHyprCtlCommand> g_cmd;

    [[maybe_unused]] void misbehave(int kind) {
        switch (kind) {
            case 1: throw std::runtime_error("injected exception");
            case 2: throw 42;
            case 3: { volatile int* p = nullptr; *p = 1; break; }         // SIGSEGV
            case 4: __builtin_trap(); break;                                 // SIGILL (ud2)
            case 5: std::abort();                                           // SIGABRT
            default: break;
        }
    }

    int kindFromName(const std::string& s) {
        if (s == "throw") return 1;
        if (s == "throw_int") return 2;
        if (s == "segv") return 3;
        if (s == "div0" || s == "trap") return 4;
        if (s == "abort") return 5;
        return 0;
    }

    std::string handleCommand(eHyprCtlOutputFormat, std::string request) {
        // request looks like "nsproxy selftest throw" / "nsproxy status" / "nsproxy reset"
        auto& g = ns::Guard::get();
        // status/reset must work while the plugin is disabled, so they run outside the shield (they only touch our own state)
        if (request.find("reset") != std::string::npos) {
            g_injectRender = 0;
            g.reset();
            return "re-armed\n";
        }
        if (request.find("status") != std::string::npos)
            return std::string("state: ") + (g.disabled() ? "DISABLED" : "active") + ", faults: " + std::to_string(g.faults()) +
                   ", render callbacks: " + std::to_string(g_renderCalls.load()) +
                   ", attached: " + (S.attached ? S.match : std::string("no")) + ", client: " + (ns::Bridge::get().client() ? "yes" : "no") +
                   ", post_window: " + std::to_string(S.seenPost) + ", forced: " + std::to_string(S.forcedDamage) + ", matched: " + std::to_string(S.matched) + ", added: " + std::to_string(S.added) + ", draws: " + std::to_string(S.draws) + ", fbH: " + std::to_string(S.lastFbH) + ", tex: " + std::to_string(S.texW) + "x" + std::to_string(S.texH) + " ok=" + (S.tex ? std::to_string((int)S.tex->ok()) : std::string("null")) + " lastRes=" + std::to_string(S.lastRes) + ", avg export " + std::to_string((int)(S.exportN ? S.exportUs / S.exportN : 0)) + " us, avg draw " + std::to_string((int)(S.drawN ? S.drawUs / S.drawN : 0)) + " us, exported: " + std::to_string(S.exported) + ", drawn: " + std::to_string(S.drawn) + (g.lastError().empty() ? "" : ", last: " + g.lastError()) + "\n";

        std::string result = "ok";
        if (request.find("attach") != std::string::npos) {
            auto pos = request.find("attach");
            std::string m = request.substr(pos + 6);
            m.erase(0, m.find_first_not_of(' '));
            if (m.empty()) return "usage: nsproxy attach <window class substring>\n";
            g.run("attach", [&] {
                auto w = findWindow(m);
                if (!w) { result = "no mapped window matches '" + m + "'"; return; }
                S.window = w; S.match = m; S.attached = true; S.lastReadCommit = ~0ULL;
                ns::Bridge::get().start();
                result = "attached to " + w->m_class + " (" + std::to_string((int)w->getWindowMainSurfaceBox().w) + "x" + std::to_string((int)w->getWindowMainSurfaceBox().h) + ") address:0x" + [&]{ char b[32]; snprintf(b, sizeof b, "%lx", (unsigned long)(uintptr_t)w.get()); return std::string(b); }() + (w->m_workspace && w->m_workspace->isVisible() ? " (visible)" : " (HIDDEN workspace)");
            });
            return result + "\n";
        }
        if (request.find("debugrect") != std::string::npos) {
            S.debugRect = !S.debugRect;
            return std::string("debugrect ") + (S.debugRect ? "on" : "off") + "\n";
        }
        if (request.find("detach") != std::string::npos) {
            S.attached = false;
            S.window.reset();
            g_pHyprRenderer->m_directScanoutBlocked = false;
            g_pHyprRenderer->m_renderPass.removeAllOfType("nsproxy");
            return "detached\n";
        }
        g.run("hyprctl", [&] {
            auto pos = request.find("selftest");
            if (pos == std::string::npos) {
                result = "usage: nsproxy status | reset | selftest <throw|throw_int|segv|div0|abort|render_throw|render_segv|...>";
                return;
            }
            std::string what = request.substr(pos + 8);
            what.erase(0, what.find_first_not_of(' '));
            if (what.rfind("render_", 0) == 0) {         // fault inside the real render path, next 3 frames
                g_injectKind = kindFromName(what.substr(7));
                g_injectRender = 3;
                result = "will misbehave in the next render callbacks";
            } else {
                misbehave(kindFromName(what));           // fault inside this very command
                result = "self-test finished without a fault";
            }
        });
        if (g.disabled())
            result = "plugin is disabled: " + g.lastError();
        return result + "\n";
    }
}

APICALL EXPORT std::string PLUGIN_API_VERSION() {
    return HYPRLAND_API_VERSION;
}

APICALL EXPORT PLUGIN_DESCRIPTION_INFO PLUGIN_INIT(HANDLE handle) {
    PHANDLE = handle;
    const std::string want = __hyprland_api_get_hash();
    const std::string have = __hyprland_api_get_client_hash();
    if (want != have) {
        HyprlandAPI::addNotification(PHANDLE, "[nsproxy] version mismatch (compositor " + want + ", plugin " + have + ") - rebuild the plugin",
                                     CHyprColor{1.0, 0.2, 0.2, 1.0}, 8000);
        throw std::runtime_error("[nsproxy] Version mismatch");
    }

    ns::Guard::setLogPath(std::string(getenv("HOME") ? getenv("HOME") : "/tmp") + "/.cache/nsproxy-plugin.log");
    ns::installHandlers();
    ns::Guard::get().setNotify([](const std::string& m) {
        HyprlandAPI::addNotification(PHANDLE, m, CHyprColor{1.0, 0.3, 0.2, 1.0}, 10000);
    });

    g_renderListener = Event::bus()->m_events.render.stage.listen([](eRenderStage stage) {
        ns::Guard::get().run("render", [&] {
            onStage(stage);
            if (stage != RENDER_POST_WINDOWS)
                return;
            g_renderCalls++;
            if (g_injectRender.load() > 0) {
                g_injectRender--;
                misbehave(g_injectKind.load());
            }
        });
    });

    armTimer();
    g_cmd = HyprlandAPI::registerHyprCtlCommand(PHANDLE, SHyprCtlCommand{"nsproxy", false, handleCommand});
    ns::Guard::get().log("plugin loaded");
    return {"nsproxy", "DLSS5 window proxy (fault-shielded)", "ns-dlss-wine", "0.1"};
}

APICALL EXPORT void PLUGIN_EXIT() {
    S.attached = false;
    if (g_pHyprRenderer)
        g_pHyprRenderer->m_directScanoutBlocked = false;
    // our pass elements outlive the frame that queued them: Hyprland would destroy them (virtual dtor in OUR code) after we are unloaded
    if (g_pHyprRenderer)
        g_pHyprRenderer->m_renderPass.removeAllOfType("nsproxy");
    g_renderListener.reset();
    if (g_timer) { g_timer->cancel(); g_pEventLoopManager->removeTimer(g_timer); g_timer.reset(); }
    ns::Bridge::get().stop();
    ns::Guard::get().run("exit-gl", [] { freeClientCopy(); freePbos(); for (auto& z : g_zRes) zcDestroy(z); zcExpDropPending(); for (auto& z : g_zExp) zcExpDestroy(z); });
    S.tex.reset();
    if (g_cmd)
        HyprlandAPI::unregisterHyprCtlCommand(PHANDLE, g_cmd);
    g_cmd.reset();
    ns::removeHandlers();
    ns::Guard::get().log("plugin unloaded");
}
