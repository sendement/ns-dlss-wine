// SPDX-License-Identifier: MIT
// Shared memory + unix socket to the external pipeline process. Runs its own thread; never touches compositor state.
#pragma once
#include <atomic>
#include <cerrno>
#include <cstring>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>

#include <fcntl.h>
#include <poll.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/udmabuf.h>

#include "guard.hpp"
#include "proto.hpp"

namespace ns {

class Bridge {
  public:
    static Bridge& get() {
        static Bridge b;
        return b;
    }

    void start() {
        if (running_)
            return;
        const uint64_t slot = (uint64_t)nsproxy::kCapW * nsproxy::kCapH * 4;
        total_ = 4096 + 2 * nsproxy::kSlots * slot;
        memfd_ = memfd_create("nsproxy-shm", MFD_CLOEXEC | MFD_ALLOW_SEALING);
        if (memfd_ < 0 || ftruncate(memfd_, (off_t)total_) != 0)
            throw std::runtime_error(std::string("memfd: ") + strerror(errno));
        fcntl(memfd_, F_ADD_SEALS, F_SEAL_SHRINK | F_SEAL_GROW);   // udmabuf requires a memfd that cannot shrink
        void* p = mmap(nullptr, total_, PROT_READ | PROT_WRITE, MAP_SHARED, memfd_, 0);
        if (p == MAP_FAILED)
            throw std::runtime_error("mmap failed");
        base_ = static_cast<uint8_t*>(p);
        hdr_ = reinterpret_cast<nsproxy::Header*>(base_);
        std::memset(hdr_, 0, sizeof *hdr_);
        hdr_->magic = nsproxy::kMagic;
        hdr_->version = nsproxy::kVersion;
        hdr_->cap_w = nsproxy::kCapW;
        hdr_->cap_h = nsproxy::kCapH;
        hdr_->slot_bytes = slot;
        hdr_->exp_off = 4096;
        hdr_->res_off = 4096 + nsproxy::kSlots * slot;

        const char* rd = getenv("XDG_RUNTIME_DIR");
        path_ = std::string(rd ? rd : "/tmp") + "/nsproxy.sock";
        listen_ = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        sockaddr_un a{};
        a.sun_family = AF_UNIX;
        std::strncpy(a.sun_path, path_.c_str(), sizeof a.sun_path - 1);
        unlink(path_.c_str());
        if (bind(listen_, reinterpret_cast<sockaddr*>(&a), sizeof a) != 0 || listen(listen_, 1) != 0)
            throw std::runtime_error(std::string("socket: ") + strerror(errno));
        running_ = true;
        thread_ = std::thread([this] { loop(); });
    }

    void stop() {
        if (!running_)
            return;
        running_ = false;
        if (thread_.joinable())
            thread_.join();
        dropClient();
        close(listen_);
        unlink(path_.c_str());
        for (int i = 0; i < nsproxy::kSlots; i++) {
            if (resDma_[i] > 0) close(resDma_[i]);
            if (expDma_[i] > 0) close(expDma_[i]);
            resDma_[i] = expDma_[i] = 0;
        }
        if (udmabuf_ > 0) close(udmabuf_);
        udmabuf_ = 0;
        munmap(base_, total_);
        close(memfd_);
        base_ = nullptr;
        hdr_ = nullptr;
    }

    // A dma-buf over one slot of the shared memory (udmabuf), for zero-copy GL import. -1 when /dev/udmabuf is unavailable. The fd is cached.
    int slotDmabuf(bool result, int i) {
        int& cached = (result ? resDma_ : expDma_)[i];
        if (cached != 0)
            return cached;
        if (udmabuf_ == 0)
            udmabuf_ = open("/dev/udmabuf", O_RDWR | O_CLOEXEC);
        if (udmabuf_ < 0) {
            return cached = -1;
        }
        udmabuf_create c{};
        c.memfd = (uint32_t)memfd_;
        c.flags = UDMABUF_FLAGS_CLOEXEC;
        c.offset = (result ? hdr_->res_off : hdr_->exp_off) + (uint64_t)i * hdr_->slot_bytes;
        c.size = hdr_->slot_bytes;
        int fd = ioctl(udmabuf_, UDMABUF_CREATE, &c);
        return cached = (fd >= 0 ? fd : -1);
    }

    nsproxy::Header* hdr() { return hdr_; }
    uint8_t* expSlot(int i) { return base_ + hdr_->exp_off + (uint64_t)i * hdr_->slot_bytes; }
    const uint8_t* resSlot(int i) const { return base_ + hdr_->res_off + (uint64_t)i * hdr_->slot_bytes; }
    bool client() const { return client_.load() >= 0; }
    uint64_t connectSeq() const { return connectSeq_.load(); }   // bumps on every new pipeline connection

    void notify() {
        std::lock_guard l(mu_);
        int c = client_.load();
        if (c >= 0)
            (void)send(c, "x", 1, MSG_DONTWAIT | MSG_NOSIGNAL);
    }

  private:
    void dropClient() {
        std::lock_guard l(mu_);
        int c = client_.exchange(-1);
        if (c >= 0)
            close(c);
        if (hdr_)
            __atomic_store_n(&hdr_->override_on, 0u, __ATOMIC_RELEASE);
    }

    void accept1() {
        int c = accept4(listen_, nullptr, nullptr, SOCK_CLOEXEC);
        if (c < 0)
            return;
        std::string msg = "NSPX1 " + std::to_string(total_) + "\n";
        char ctl[CMSG_SPACE(sizeof(int))]{};
        iovec iov{msg.data(), msg.size()};
        msghdr mh{};
        mh.msg_iov = &iov;
        mh.msg_iovlen = 1;
        mh.msg_control = ctl;
        mh.msg_controllen = sizeof ctl;
        cmsghdr* cm = CMSG_FIRSTHDR(&mh);
        cm->cmsg_level = SOL_SOCKET;
        cm->cmsg_type = SCM_RIGHTS;
        cm->cmsg_len = CMSG_LEN(sizeof(int));
        std::memcpy(CMSG_DATA(cm), &memfd_, sizeof(int));
        if (sendmsg(c, &mh, MSG_NOSIGNAL) < 0) {
            close(c);
            return;
        }
        dropClient();                 // a new client replaces the old one
        __atomic_store_n(&hdr_->res_seq, 0ull, __ATOMIC_RELEASE);
        __atomic_store_n(&hdr_->client_flags, 0u, __ATOMIC_RELEASE);   // the new client declares its capabilities itself
        int fl = fcntl(c, F_GETFL);
        fcntl(c, F_SETFL, fl | O_NONBLOCK);
        std::lock_guard l(mu_);
        client_ = c;
        connectSeq_++;
        Guard::get().log("pipeline client connected");
    }

    void loop() {
        Guard::get().run("bridge-thread", [&] {
            while (running_) {
                pollfd fds[2] = {{listen_, POLLIN, 0}, {client_.load(), POLLIN, 0}};
                int n = poll(fds, client_.load() >= 0 ? 2 : 1, 200);
                if (n <= 0)
                    continue;
                if (fds[0].revents & POLLIN)
                    accept1();
                if (fds[1].revents & (POLLIN | POLLHUP | POLLERR)) {
                    char buf[64];
                    ssize_t r = recv(client_.load(), buf, sizeof buf, MSG_DONTWAIT);
                    if (r == 0 || (r < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) {
                        dropClient();
                        Guard::get().log("pipeline client gone - the window shows normally again");
                    }
                }
            }
        });
    }

    std::mutex mu_;
    std::atomic<bool> running_{false};
    std::atomic<int> client_{-1};
    std::atomic<uint64_t> connectSeq_{0};
    std::thread thread_;
    int listen_ = -1, memfd_ = -1, udmabuf_ = 0;
    int resDma_[nsproxy::kSlots] = {0, 0, 0}, expDma_[nsproxy::kSlots] = {0, 0, 0};
    uint64_t total_ = 0;
    uint8_t* base_ = nullptr;
    nsproxy::Header* hdr_ = nullptr;
    std::string path_;
};

} // namespace ns
