#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <shared_mutex>
#include <string>

#include "crowdy/core/bytes.hpp"
#include "crowdy/core/result.hpp"

/// Minimal portable UDP socket (POSIX + Winsock). The socket is connect()ed
/// to the replication server, so recv only returns that server's datagrams
/// (the kernel drops a datagram from any other address or port) and send needs
/// no per-call address.
///
/// Threads: open(), close() and the receives belong to one thread at a time,
/// the network thread. send(), wake(), canWake(), isOpen() and nativeHandle()
/// may be called from any thread while that thread opens or closes the socket:
/// a send either goes out on the descriptor that was open or reports
/// NotConnected, and never touches one that is being closed.
namespace crowdy::replication {

class UdpSocket {
 public:
  UdpSocket() = default;
  ~UdpSocket() { close(); }
  UdpSocket(const UdpSocket&) = delete;
  UdpSocket& operator=(const UdpSocket&) = delete;

  /// Open and connect to host:port. `host` is a literal IPv4 or IPv6 address.
  /// Buffer sizes are hints passed to SO_RCVBUF / SO_SNDBUF; a value <= 0
  /// leaves the OS default in place. Kernels may round up (Linux doubles),
  /// and a request the kernel declines is not an error.
  Status open(const std::string& host, int port, int recvBufferBytes, int sendBufferBytes);
  void close();
  bool isOpen() const { return fd_.load(std::memory_order_acquire) >= 0; }

  /// Send one datagram. Never blocks. Returns Errc::Ok when the datagram was
  /// handed to the kernel, Errc::WouldBlock when the send buffer is full — a
  /// transient condition, nothing was sent, retry shortly — and
  /// Errc::SocketError only for a genuine fault.
  Status send(Bytes datagram);

  /// Receive one datagram into `buffer`, waiting up to timeoutMs (0 = no
  /// wait). Returns the datagram length, 0 on timeout, or an error.
  Result<std::size_t> recv(MutableBytes buffer, int timeoutMs);

  /// Batched receive: fill up to `count` datagrams into equal-sized slots of
  /// `slab` (slotSize bytes each), writing each datagram's length into
  /// `lengths`. Waits up to timeoutMs for the first datagram, then drains
  /// without waiting. Uses recvmmsg(2) on Linux (one syscall per batch);
  /// falls back to a recv loop elsewhere. Returns the number received.
  Result<std::size_t> recvBatch(std::uint8_t* slab, std::size_t slotSize, std::size_t count,
                                std::size_t* lengths, int timeoutMs);

  /// Interrupt a recv()/recvBatch() blocked on another thread so it returns 0
  /// (no datagram) now rather than at its timeout. Used by the send path to
  /// tell the net thread a bundle just opened and its window is ticking.
  /// Returns false when this platform cannot wake a blocked receive (Windows),
  /// in which case callers bound their receive timeout instead.
  bool wake();
  /// True when wake() can interrupt a blocked receive on this socket.
  bool canWake() const;

  /// The operating system's error code (errno; WSAGetLastError on Windows) of the last send
  /// that failed with Errc::SocketError, or 0 when none has since the socket was opened. A
  /// full buffer (Errc::WouldBlock) is not a fault and does not set it. For diagnostics: it
  /// says which fault a SocketError was.
  int lastSendFault() const { return lastSendFault_.load(std::memory_order_relaxed); }

  /// Underlying descriptor (a SOCKET on Windows), or -1 when closed. For
  /// diagnostics and tests — reading socket options back, for instance. The
  /// socket remains owned by this object; do not close or reconfigure it.
  long long nativeHandle() const { return fd_.load(std::memory_order_acquire); }

 private:
  void closeLocked();

  /// Held shared by a send or a wake and exclusively while the descriptors are opened or
  /// closed, so a sender on another thread never uses one that is going away.
  mutable std::shared_mutex gate_;
  std::atomic<long long> fd_{-1};
  std::atomic<int> lastSendFault_{0};
#ifndef _WIN32
  // Self-pipe polled alongside the socket so wake() can end a blocked poll().
  int wakeFds_[2] = {-1, -1};
  void drainWake();
#endif
};

}  // namespace crowdy::replication
