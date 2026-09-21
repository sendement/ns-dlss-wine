// SPDX-License-Identifier: MIT
// Fault shield for plugin code running inside the compositor.
//
// Every entry point of the plugin (event callbacks, hyprctl commands, ...) runs through Guard::run():
//   * C++ exceptions are caught and reported instead of unwinding into Hyprland;
//   * SIGSEGV/SIGBUS/SIGFPE/SIGILL/SIGABRT raised INSIDE a guarded section are turned into a report + siglongjmp back
//     (a crash in our own code - null pointer, bad index, failed assert - does not take the compositor down);
//   * a circuit breaker trips (plugin goes idle: no drawing, no hooks acting, the windows look exactly as without it)
//     after 3 exceptions within 10 s, or immediately after a signal fault - `hyprctl nsproxy reset` re-arms it.
// What it cannot do: undo damage a fault already did to memory it does not own (a wild write into Hyprland's heap can
// crash later, outside any guard). Keep the plugin small and let the external process do the heavy lifting.
// Signals raised outside a guarded section are passed on to whoever handled them before us (Hyprland's crash reporter).
#pragma once
#include <atomic>
#include <chrono>
#include <csetjmp>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <deque>
#include <exception>
#include <functional>
#include <mutex>
#include <string>

namespace ns {

struct FaultFrame {
    sigjmp_buf jb;
    volatile sig_atomic_t armed = 0;
    volatile int sig = 0;
    FaultFrame* prev = nullptr;
};
inline thread_local FaultFrame* tlFrame = nullptr;

inline struct sigaction g_prevAct[NSIG];
inline const int kSignals[] = {SIGSEGV, SIGBUS, SIGFPE, SIGILL, SIGABRT};
inline bool g_handlersInstalled = false;

inline void faultHandler(int sig, siginfo_t* si, void* ctx) {
    FaultFrame* f = tlFrame;
    if (f && f->armed) {
        f->sig = sig;
        siglongjmp(f->jb, 1);
    }
    // not ours: chain to the previous handler (Hyprland's crash reporter) / default action
    struct sigaction& p = g_prevAct[sig];
    if (p.sa_flags & SA_SIGINFO) {
        p.sa_sigaction(sig, si, ctx);
    } else if (p.sa_handler != SIG_DFL && p.sa_handler != SIG_IGN) {
        p.sa_handler(sig);
    } else {
        signal(sig, SIG_DFL);
        raise(sig);
    }
}

inline void installHandlers() {
    if (g_handlersInstalled)
        return;
    for (int s : kSignals) {
        struct sigaction a{};
        a.sa_sigaction = faultHandler;
        a.sa_flags = SA_SIGINFO | SA_NODEFER;
        sigemptyset(&a.sa_mask);
        sigaction(s, &a, &g_prevAct[s]);
    }
    g_handlersInstalled = true;
}

inline void removeHandlers() {
    if (!g_handlersInstalled)
        return;
    for (int s : kSignals)
        sigaction(s, &g_prevAct[s], nullptr);
    g_handlersInstalled = false;
}

class Guard {
  public:
    using Notify = std::function<void(const std::string&)>;

    static Guard& get() {
        static Guard g;
        return g;
    }

    void setNotify(Notify n) { notify_ = std::move(n); }
    bool disabled() const { return disabled_.load(); }
    const std::string& lastError() const { return last_; }
    int faults() const { return totalFaults_; }

    void reset() {
        std::lock_guard l(mu_);
        disabled_ = false;
        recent_.clear();
        log("re-armed (hyprctl nsproxy reset)");
    }

    // Runs fn under the shield. Returns true when it completed normally; false when it faulted or the plugin is disabled.
    template <typename F>
    bool run(const char* where, F&& fn) {
        if (disabled_.load())
            return false;
        return runImpl(where, std::function<void()>(std::forward<F>(fn)));
    }

    static void setLogPath(std::string p) { logPath() = std::move(p); }

    void log(const std::string& msg) {
        if (FILE* f = fopen(logPath().c_str(), "a")) {
            char ts[32];
            time_t t = time(nullptr);
            strftime(ts, sizeof ts, "%F %T", localtime(&t));
            fprintf(f, "%s %s\n", ts, msg.c_str());
            fclose(f);
        }
        fprintf(stderr, "[nsproxy] %s\n", msg.c_str());
    }

  private:
    static std::string& logPath() {
        static std::string p = "/tmp/nsproxy-plugin.log";
        return p;
    }

    bool runImpl(const char* where, const std::function<void()>& fn) {
        FaultFrame frame;
        frame.prev = tlFrame;
        tlFrame = &frame;
        if (sigsetjmp(frame.jb, 1) == 0) {
            frame.armed = 1;
            try {
                fn();
            } catch (const std::exception& e) {
                frame.armed = 0;
                tlFrame = frame.prev;
                fault(where, std::string("exception: ") + e.what(), false);
                return false;
            } catch (...) {
                frame.armed = 0;
                tlFrame = frame.prev;
                fault(where, "unknown exception", false);
                return false;
            }
            frame.armed = 0;
            tlFrame = frame.prev;
            return true;
        }
        // arrived here through siglongjmp from faultHandler
        int sig = frame.sig;
        frame.armed = 0;
        tlFrame = frame.prev;
        fault(where, std::string("signal ") + std::to_string(sig) + " (" + strsignal(sig) + ")", true);
        return false;
    }

    void fault(const char* where, const std::string& what, bool hard) {
        std::lock_guard l(mu_);
        totalFaults_++;
        last_ = std::string(where) + ": " + what;
        const auto now = std::chrono::steady_clock::now();
        recent_.push_back(now);
        while (!recent_.empty() && now - recent_.front() > std::chrono::seconds(10))
            recent_.pop_front();
        log("FAULT in " + last_);
        if (hard || recent_.size() >= 3) {
            disabled_ = true;
            const std::string m = "nsproxy: " + last_ + " - plugin disabled, the compositor keeps running (hyprctl nsproxy reset to re-enable)";
            log(m);
            report(m);
        } else {
            report("nsproxy: " + last_);
        }
    }

    void report(const std::string& m) {
        if (!notify_)
            return;
        try {
            notify_(m);
        } catch (...) {
        }
    }

    std::mutex mu_;
    std::deque<std::chrono::steady_clock::time_point> recent_;
    std::atomic<bool> disabled_{false};
    std::string last_;
    int totalFaults_ = 0;
    Notify notify_;
};

} // namespace ns
