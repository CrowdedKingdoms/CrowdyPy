// The replication binding's shared types: the provider bridge, event batches and the
// connection wrapper, used by replication_binding.cpp and session_binding.cpp.
#pragma once


#include <nanobind/nanobind.h>
#include <nanobind/ndarray.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/pair.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/tuple.h>
#include <nanobind/stl/vector.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "common.hpp"
#include "crowdy/core/crypto.hpp"
#include "crowdy/core/logger.hpp"
#include "crowdy/media/video_frames.hpp"
#include "crowdy/replication/connection.hpp"

namespace crowdypy {

using namespace crowdy;
using replication::Assignment;
using replication::ConnState;
using replication::TokenInfo;

/// The row type for a connection-state change. Not a wire opcode: the wire never
/// carries 255, so it cannot collide with a notification type.
inline constexpr std::uint8_t kStatusRow = 255;
inline constexpr std::size_t kExtras = 4;

// ------------------------------------------------------------------ arguments

inline std::uint8_t distance_arg(int distance) {
  if (distance < 0 || distance > wire::kMaxDistance)
    raise(Errc::InvalidArgument, "distance is 0-8 chunks");
  return static_cast<std::uint8_t>(distance);
}

inline wire::DecayRate decay_arg(int decay) {
  if (decay < 0 || decay > static_cast<int>(wire::DecayRate::Linear5))
    raise(Errc::InvalidArgument, "decay is 0-5");
  return static_cast<wire::DecayRate>(decay);
}

inline std::int16_t i16_arg(int value, const char* what) {
  if (value < INT16_MIN || value > INT16_MAX)
    raise(Errc::InvalidArgument, std::string(what) + " is an int16");
  return static_cast<std::int16_t>(value);
}

inline std::uint8_t sent(const Result<std::uint8_t>& result, const char* what) {
  if (!result.ok()) raise(result.error(), what);
  return result.value();
}

// ------------------------------------------------------------------ the provider bridge

/// The connection's ISessionProvider. A request from CrowdyCPP (connect(), or
/// housekeeping on the network thread) is queued and the asking thread waits on
/// a condition variable; a Python thread takes the request with next(), runs
/// the GraphQL call, and answers. The asking thread never takes the GIL.
class ProviderBridge final : public replication::ISessionProvider {
 public:
  static constexpr int kAssign = 1;
  static constexpr int kRefresh = 2;

  struct Request {
    std::uint64_t id = 0;
    int kind = kAssign;
    bool hasCurrent = false;
    Assignment current;
  };

  explicit ProviderBridge(std::int64_t timeoutMs) : timeoutMs_(timeoutMs) {}

  Result<Assignment> assignServer() override {
    Reply reply = ask(kAssign, nullptr);
    if (reply.code != Errc::Ok) return reply.code;
    return reply.assignment;
  }

  Result<TokenInfo> refreshToken() override { return refreshToken(nullptr); }

  Result<TokenInfo> refreshToken(const Assignment* current) override {
    Reply reply = ask(kRefresh, current);
    if (reply.code != Errc::Ok) return reply.code;
    return reply.token;
  }

  /// The next request, waiting up to timeoutMs. nullopt on timeout or close.
  std::optional<Request> next(int timeoutMs) {
    std::unique_lock lock(mutex_);
    cv_.wait_for(lock, std::chrono::milliseconds(timeoutMs),
                 [&] { return closed_ || !requests_.empty(); });
    if (closed_ || requests_.empty()) return std::nullopt;
    Request request = requests_.front();
    requests_.pop_front();
    return request;
  }

  void answerAssignment(std::uint64_t id, Assignment assignment) {
    Reply reply;
    reply.assignment = std::move(assignment);
    answer(id, std::move(reply));
  }

  void answerToken(std::uint64_t id, TokenInfo token) {
    Reply reply;
    reply.token = std::move(token);
    answer(id, std::move(reply));
  }

  void answerError(std::uint64_t id, Errc code) {
    Reply reply;
    reply.code = code == Errc::Ok ? Errc::Rejected : code;
    answer(id, std::move(reply));
  }

  /// Fail every waiting and future request with Errc::Closed, so a disconnect
  /// can join a network thread that is waiting on Python.
  void close() {
    std::lock_guard lock(mutex_);
    closed_ = true;
    requests_.clear();
    cv_.notify_all();
  }

  void open() {
    std::lock_guard lock(mutex_);
    closed_ = false;
  }

  bool closed() const {
    std::lock_guard lock(mutex_);
    return closed_;
  }

 private:
  struct Reply {
    Errc code = Errc::Ok;
    Assignment assignment;
    TokenInfo token;
  };

  Reply ask(int kind, const Assignment* current) {
    std::unique_lock lock(mutex_);
    Reply reply;
    if (closed_) {
      reply.code = Errc::Closed;
      return reply;
    }
    const std::uint64_t id = ++nextId_;
    Request request;
    request.id = id;
    request.kind = kind;
    if (current) {
      request.hasCurrent = true;
      request.current = *current;
    }
    requests_.push_back(request);
    waiting_.insert(id);
    cv_.notify_all();
    cv_.wait_for(lock, std::chrono::milliseconds(timeoutMs_),
                 [&] { return closed_ || replies_.count(id) > 0; });
    waiting_.erase(id);
    const auto it = replies_.find(id);
    if (it == replies_.end()) {
      std::erase_if(requests_, [id](const Request& r) { return r.id == id; });
      reply.code = closed_ ? Errc::Closed : Errc::Timeout;
      return reply;
    }
    reply = std::move(it->second);
    replies_.erase(it);
    return reply;
  }

  void answer(std::uint64_t id, Reply reply) {
    std::lock_guard lock(mutex_);
    if (waiting_.count(id) == 0) return;  // the asker gave up (timeout or close)
    replies_[id] = std::move(reply);
    cv_.notify_all();
  }

  const std::int64_t timeoutMs_;
  mutable std::mutex mutex_;
  std::condition_variable cv_;
  std::deque<Request> requests_;
  std::unordered_set<std::uint64_t> waiting_;
  std::unordered_map<std::uint64_t, Reply> replies_;
  std::uint64_t nextId_ = 0;
  bool closed_ = false;
};

// ------------------------------------------------------------------ logging

/// CrowdyCPP's lifecycle log lines, queued for Python to read after a poll. The
/// network thread logs; it may not call into Python, so the lines wait here.
class QueueLogger final : public core::ILogger {
 public:
  bool enabled(core::LogLevel level) const override {
    return level >= core::LogLevel::Info;
  }
  void log(core::LogLevel level, std::string_view message) const override {
    std::lock_guard lock(mutex_);
    if (lines_.size() >= kMaxLines) {
      lines_.pop_front();
      ++dropped_;
    }
    lines_.emplace_back(static_cast<int>(level), std::string(message));
  }
  std::vector<std::pair<int, std::string>> drain() const {
    std::lock_guard lock(mutex_);
    std::vector<std::pair<int, std::string>> out(lines_.begin(), lines_.end());
    lines_.clear();
    if (dropped_ > 0) {
      out.emplace_back(static_cast<int>(core::LogLevel::Warn),
                       std::to_string(dropped_) + " replication log line(s) dropped");
      dropped_ = 0;
    }
    return out;
  }

 private:
  static constexpr std::size_t kMaxLines = 256;
  mutable std::mutex mutex_;
  mutable std::deque<std::pair<int, std::string>> lines_;
  mutable std::uint64_t dropped_ = 0;
};

// ------------------------------------------------------------------ batches

/// Events in columns. One row per event; what `extra` holds depends on the type:
/// a voxel update (x, y, z, voxelType) with the voxel state as the payload, a
/// client or server event (eventType) with the event state as the payload, an
/// actor-left (reason), a channel message (channelId) with the sender in the uuid
/// column, an error (code) with the failed send's sequence, a status row (state).
struct Rows {
  std::vector<std::uint8_t> type;
  std::vector<std::int64_t> appId;
  std::vector<std::int64_t> chunk;  // 3 per row
  std::vector<std::uint8_t> uuid;   // 32 per row
  std::vector<std::int64_t> epochMs;
  std::vector<std::uint8_t> sequence;
  std::vector<std::int64_t> extra;  // kExtras per row
  std::vector<std::uint32_t> offsets{0};
  std::vector<std::uint8_t> payload;

  std::size_t size() const { return type.size(); }

  void reserve(std::size_t rows, std::size_t bytes) {
    type.reserve(rows);
    appId.reserve(rows);
    chunk.reserve(rows * 3);
    uuid.reserve(rows * wire::kUuidSize);
    epochMs.reserve(rows);
    sequence.reserve(rows);
    extra.reserve(rows * kExtras);
    offsets.reserve(rows + 1);
    payload.reserve(bytes);
  }

  void push(std::uint8_t rowType, std::int64_t app, const wire::ChunkCoord& c, const char* id,
            std::int64_t epoch, std::uint8_t seq, std::array<std::int64_t, kExtras> extras,
            Bytes body) {
    type.push_back(rowType);
    appId.push_back(app);
    chunk.insert(chunk.end(), {c.x, c.y, c.z});
    if (id) {
      uuid.insert(uuid.end(), id, id + wire::kUuidSize);
    } else {
      uuid.insert(uuid.end(), wire::kUuidSize, 0);
    }
    epochMs.push_back(epoch);
    sequence.push_back(seq);
    extra.insert(extra.end(), extras.begin(), extras.end());
    payload.insert(payload.end(), body.begin(), body.end());
    offsets.push_back(static_cast<std::uint32_t>(payload.size()));
  }

  void spatial(const replication::SpatialNotification& n, std::array<std::int64_t, kExtras> extras,
               Bytes body) {
    push(static_cast<std::uint8_t>(n.type), n.appId, n.chunk, n.uuid, n.epochMillis, n.sequence,
         extras, body);
  }
};

template <typename T>
using Column = nb::ndarray<nb::memview, const T, nb::c_contig>;

/// A view of one column that keeps the batch alive (rv_policy::reference_internal).
template <typename T>
inline Column<T> column(const std::vector<T>& values, std::size_t rows, std::size_t width) {
  static const T empty{};
  const T* data = values.empty() ? &empty : values.data();
  if (width == 1) {
    const std::size_t shape[1] = {rows};
    return Column<T>(data, 1, shape, nb::handle());
  }
  const std::size_t shape[2] = {rows, width};
  return Column<T>(data, 2, shape, nb::handle());
}

class NotificationBatch {
 public:
  NotificationBatch() = default;
  explicit NotificationBatch(Rows rows) : rows_(std::move(rows)) {}

  std::size_t size() const { return rows_.size(); }
  const Rows& rows() const { return rows_; }

  void check(std::size_t i) const {
    if (i >= rows_.size()) throw nb::index_error("row out of range");
  }

  Bytes payloadOf(std::size_t i) const {
    const std::uint32_t begin = rows_.offsets[i];
    const std::uint32_t end = rows_.offsets[i + 1];
    return Bytes(rows_.payload.data() + begin, end - begin);
  }

  const char* uuidOf(std::size_t i) const {
    return reinterpret_cast<const char*>(rows_.uuid.data() + i * wire::kUuidSize);
  }

  /// The row of the self-echo for `sequence` from `uuid`, or of the GENERIC_ERROR
  /// answering that sequence; -1 when neither is in this batch.
  std::int64_t match(std::uint8_t sequence, const core::ActorUuid& uuid) const {
    for (std::size_t i = 0; i < rows_.size(); ++i) {
      if (rows_.sequence[i] != sequence) continue;
      const auto type = static_cast<wire::MessageType>(rows_.type[i]);
      if (type == wire::MessageType::GenericError) return static_cast<std::int64_t>(i);
      if (rows_.type[i] == kStatusRow || type == wire::MessageType::ChannelMessageNotification)
        continue;
      if (std::memcmp(uuidOf(i), uuid.data(), wire::kUuidSize) == 0)
        return static_cast<std::int64_t>(i);
    }
    return -1;
  }

 private:
  Rows rows_;
};

inline nb::str uuid_str(const char* id) {
  std::size_t len = wire::kUuidSize;
  while (len > 0 && id[len - 1] == '\0') --len;
  return nb::str(id, len);
}

inline nb::tuple row_tuple(const NotificationBatch& b, std::size_t i) {
  const Rows& r = b.rows();
  const std::int64_t* c = r.chunk.data() + i * 3;
  const std::int64_t* e = r.extra.data() + i * kExtras;
  return nb::make_tuple(r.type[i], r.appId[i], nb::make_tuple(c[0], c[1], c[2]),
                        uuid_str(b.uuidOf(i)), to_bytes(b.payloadOf(i)), r.epochMs[i],
                        r.sequence[i], nb::make_tuple(e[0], e[1], e[2], e[3]));
}

// ------------------------------------------------------------------ the connection

// Defined in replication_binding.cpp, so this header pulls in no platform socket headers.
void write_wake(std::int64_t fd);
void prepare_wake_fd(std::int64_t fd);

struct ConnectionOptions {
  std::int64_t appId = 0;
  bool manualPump = false;
  bool preferIpv6 = false;
  int sessionReadyWaitMs = 1500;
  std::int64_t refreshLeadMs = 5 * 60 * 1000;
  bool verifyNotifications = true;
  bool advertiseCapabilities = true;
  std::int64_t advertiseIntervalMs = 15 * 1000;
  std::int64_t watchdogSilenceMs = 0;
  std::size_t ringCapacity = 4096;
  int socketRecvBufferBytes = 1 << 20;
  int socketSendBufferBytes = 1 << 20;
  bool bundleSends = true;
  int bundleWindowMs = 1;
};

inline TokenInfo token_info(const std::string& token, std::int64_t gameTokenId, std::int64_t expiresAtMs,
                     bool authorized) {
  if (token.size() != wire::kTokenOctets)
    raise(Errc::InvalidArgument, "an app token is exactly 64 characters");
  TokenInfo info;
  info.token = token;
  info.gameTokenId = gameTokenId;
  info.expiresAtEpochMs = expiresAtMs;
  info.authorizedOnCurrentServer = authorized;
  return info;
}

class PyConnection {
 public:
  PyConnection(ConnectionOptions options, const std::string& token, std::int64_t gameTokenId,
               std::int64_t expiresAtMs, std::int64_t providerTimeoutMs)
      : bridge_(std::make_shared<ProviderBridge>(providerTimeoutMs)) {
    replication::Config config;
    config.appId = options.appId;
    config.token = token_info(token, gameTokenId, expiresAtMs, false);
    config.manualPump = options.manualPump;
    config.preferIpv6 = options.preferIpv6;
    config.sessionReadyWaitMs = options.sessionReadyWaitMs;
    config.refreshLeadMs = options.refreshLeadMs;
    config.verifyNotifications = options.verifyNotifications;
    config.advertiseCapabilities = options.advertiseCapabilities;
    config.advertiseIntervalMs = options.advertiseIntervalMs;
    config.watchdogSilenceMs = options.watchdogSilenceMs;
    config.ringCapacity = options.ringCapacity;
    config.socketRecvBufferBytes = options.socketRecvBufferBytes;
    config.socketSendBufferBytes = options.socketSendBufferBytes;
    config.bundleSends = options.bundleSends;
    config.bundleWindowMs = options.bundleWindowMs;
    config.onEventsReady = [this] { write_wake(wakeFd_.load(std::memory_order_acquire)); };
    connection_ = std::make_shared<replication::Connection>(config, bridge_, core::opensslCrypto(),
                                                            core::systemClock(), logger_);
    connection_->setHandlers(handlers());
  }

  ~PyConnection() {
    bridge_->close();
    connection_->disconnect();
  }

  ProviderBridge& bridge() { return *bridge_; }

  void connect() {
    bridge_->open();
    Status status;
    {
      nb::gil_scoped_release release;
      status = connection_->connect();
    }
    if (!status.ok()) raise(status.code, "connect");
  }

  void disconnect() {
    bridge_->close();
    nb::gil_scoped_release release;
    connection_->disconnect();
  }

  int state() const { return static_cast<int>(connection_->state()); }

  void setToken(const std::string& token, std::int64_t gameTokenId, std::int64_t expiresAtMs,
                bool authorized) {
    connection_->setToken(token_info(token, gameTokenId, expiresAtMs, authorized));
  }

  void requestReassignment() { connection_->requestReassignment(); }

  void setWakeFd(std::int64_t fd) {
    prepare_wake_fd(fd);
    wakeFd_.store(fd, std::memory_order_release);
  }

  std::tuple<std::string, std::string, int> assignment() const {
    const Assignment a = connection_->assignmentSnapshot();
    return {a.ip4, a.ip6, a.clientPort};
  }

  // ----- sends

  int sendSpatial(int type, std::int64_t x, std::int64_t y, std::int64_t z, nb::handle uuid,
                  nb::handle payload, int distance, int decay) {
    replication::SpatialSend send;
    send.chunk = {x, y, z};
    send.uuid = uuid_from(uuid);
    send.distance = distance_arg(distance);
    send.decay = decay_arg(decay);
    BufferView body(payload);
    send.payload = body.bytes();
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = sendOne(static_cast<wire::MessageType>(type), send);
    }
    return sent(result, "send");
  }

  int sendVoxel(std::int64_t x, std::int64_t y, std::int64_t z, nb::handle uuid, int vx, int vy,
                int vz, int voxelType, nb::handle state, int distance, int decay) {
    const core::ActorUuid id = uuid_from(uuid);
    const std::int16_t ax = i16_arg(vx, "voxel x");
    const std::int16_t ay = i16_arg(vy, "voxel y");
    const std::int16_t az = i16_arg(vz, "voxel z");
    const std::int16_t type = i16_arg(voxelType, "voxel type");
    const std::uint8_t d = distance_arg(distance);
    const wire::DecayRate r = decay_arg(decay);
    BufferView body(state);
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = connection_->sendVoxelUpdate({x, y, z}, id, ax, ay, az, type, body.bytes(), d, r);
    }
    return sent(result, "sendVoxelUpdate");
  }

  int sendClientEvent(std::int64_t x, std::int64_t y, std::int64_t z, nb::handle uuid,
                      int eventType, nb::handle state, int distance, int decay) {
    if (eventType < 0 || eventType > 0xFFFF) raise(Errc::InvalidArgument, "event type is a uint16");
    const core::ActorUuid id = uuid_from(uuid);
    const std::uint8_t d = distance_arg(distance);
    const wire::DecayRate r = decay_arg(decay);
    BufferView body(state);
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = connection_->sendClientEvent({x, y, z}, id, static_cast<std::uint16_t>(eventType),
                                            body.bytes(), d, r);
    }
    return sent(result, "sendClientEvent");
  }

  int sendSingleActor(std::int64_t x, std::int64_t y, std::int64_t z, nb::handle uuid,
                      nb::handle payload) {
    const core::ActorUuid id = uuid_from(uuid);
    BufferView body(payload);
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = connection_->sendSingleActorMessage({x, y, z}, id, body.bytes());
    }
    return sent(result, "sendSingleActorMessage");
  }

  int sendChannel(std::int64_t channelId, nb::handle uuid, nb::handle payload) {
    const core::ActorUuid id = uuid_from(uuid);
    BufferView body(payload);
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = connection_->sendChannelMessage(channelId, id, body.bytes());
    }
    return sent(result, "sendChannelMessage");
  }

  int sendHeartbeat(std::int64_t x, std::int64_t y, std::int64_t z, nb::handle uuid) {
    const core::ActorUuid id = uuid_from(uuid);
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = connection_->sendHeartbeat({x, y, z}, id);
    }
    return sent(result, "sendHeartbeat");
  }

  std::size_t sendVideoFrame(std::int64_t x, std::int64_t y, std::int64_t z, nb::handle uuid,
                             nb::handle frame, int frameId, int codec, int distance, int decay) {
    if (frameId < 0 || frameId > 0xFFFF) raise(Errc::InvalidArgument, "frame id is a uint16");
    if (codec < 0 || codec > 1) raise(Errc::InvalidArgument, "codec is 0 (JPEG) or 1 (WebP)");
    const core::ActorUuid id = uuid_from(uuid);
    const std::uint8_t d = distance_arg(distance);
    const wire::DecayRate r = decay_arg(decay);
    BufferView body(frame);
    Result<std::size_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = connection_->sendVideoFrame({x, y, z}, id, body.bytes(),
                                           static_cast<std::uint16_t>(frameId),
                                           static_cast<std::uint8_t>(codec), d, r);
    }
    if (!result.ok()) raise(result.error(), "sendVideoFrame");
    return result.value();
  }

  /// Many spatial sends of one type in one call: `chunks` is int64 (n, 3),
  /// `uuids` n x 32 octets, `payload` the bodies back to back and `offsets` int64
  /// (n + 1) delimiting them, or `stride` > 0 for fixed-size bodies. The GIL is
  /// released once; the pending bundle is flushed at the end when `flush`.
  /// Returns how many were accepted; a refusal stops the batch and raises, with
  /// the count so far in the message.
  std::size_t sendSpatialBatch(int type, nb::handle chunks, nb::handle uuids, nb::handle payload,
                               nb::handle offsets, std::size_t stride, int distance, int decay,
                               bool flush) {
    BufferView chunkView(chunks);
    BufferView uuidView(uuids);
    BufferView bodyView(payload);
    if (chunkView.size() % (3 * sizeof(std::int64_t)) != 0)
      raise(Errc::InvalidArgument, "chunks are int64 triples");
    const std::size_t n = chunkView.size() / (3 * sizeof(std::int64_t));
    if (uuidView.size() != n * wire::kUuidSize)
      raise(Errc::InvalidArgument, "uuids are 32 octets per row");
    std::optional<BufferView> offsetView;
    if (!offsets.is_none()) {
      offsetView.emplace(offsets);
      if (offsetView->size() != (n + 1) * sizeof(std::int64_t))
        raise(Errc::InvalidArgument, "offsets are int64, one more than the rows");
    } else if (stride == 0 ? bodyView.size() != 0 : bodyView.size() != n * stride) {
      raise(Errc::InvalidArgument, "payload is not rows x stride octets");
    }
    const std::uint8_t d = distance_arg(distance);
    const wire::DecayRate r = decay_arg(decay);
    const auto kind = static_cast<wire::MessageType>(type);

    std::size_t accepted = 0;
    Errc failure = Errc::Ok;
    {
      nb::gil_scoped_release release;
      const auto* c = reinterpret_cast<const std::uint8_t*>(chunkView.bytes().data());
      const auto* ids = uuidView.bytes().data();
      const auto* body = bodyView.bytes().data();
      const auto* off = offsetView ? offsetView->bytes().data() : nullptr;
      replication::SpatialSend send;
      send.distance = d;
      send.decay = r;
      for (std::size_t i = 0; i < n; ++i) {
        std::int64_t xyz[3];
        std::memcpy(xyz, c + i * sizeof(xyz), sizeof(xyz));
        send.chunk = {xyz[0], xyz[1], xyz[2]};
        std::memcpy(send.uuid.data(), ids + i * wire::kUuidSize, wire::kUuidSize);
        std::size_t begin = i * stride;
        std::size_t end = begin + stride;
        if (off) {
          std::int64_t pair[2];
          std::memcpy(pair, off + i * sizeof(std::int64_t), sizeof(pair));
          if (pair[0] < 0 || pair[1] < pair[0] ||
              static_cast<std::size_t>(pair[1]) > bodyView.size()) {
            failure = Errc::InvalidArgument;
            break;
          }
          begin = static_cast<std::size_t>(pair[0]);
          end = static_cast<std::size_t>(pair[1]);
        }
        send.payload = Bytes(body + begin, end - begin);
        Result<std::uint8_t> result = sendOne(kind, send);
        if (!result.ok()) {
          failure = result.error();
          break;
        }
        ++accepted;
      }
      if (flush && failure == Errc::Ok) (void)connection_->flushSends();
    }
    if (failure != Errc::Ok)
      raise(failure, "batch stopped after " + std::to_string(accepted) + " send(s)");
    return accepted;
  }

  void flushSends() {
    Status status;
    {
      nb::gil_scoped_release release;
      status = connection_->flushSends();
    }
    if (!status.ok() && status.code != Errc::WouldBlock) raise(status.code, "flushSends");
  }

  // ----- receive

  NotificationBatch poll(std::size_t maxEvents) {
    Rows rows;
    {
      nb::gil_scoped_release release;
      std::lock_guard lock(consumer_);
      // An attached WorldSession is the consumer: its tick() drains the queue
      // into the stores and forwards what they do not keep into rows_.
      if (!sessionAttached_) connection_->poll(maxEvents);
      rows = std::move(rows_);
      rows_ = Rows{};
      rows_.reserve(std::max<std::size_t>(rows.size(), 64), std::max<std::size_t>(rows.payload.size(), 4096));
    }
    return NotificationBatch(std::move(rows));
  }

  std::size_t pump(int timeoutMs) {
    nb::gil_scoped_release release;
    std::lock_guard lock(pumper_);
    return connection_->pump(timeoutMs);
  }

  nb::dict stats() const {
    const auto s = connection_->stats();
    nb::dict d;
    d["datagrams_sent"] = s.datagramsSent;
    d["datagrams_received"] = s.datagramsReceived;
    d["messages_sent"] = s.messagesSent;
    d["messages_received"] = s.messagesReceived;
    d["bytes_sent"] = s.bytesSent;
    d["bytes_received"] = s.bytesReceived;
    d["bundles_sent"] = s.bundlesSent;
    d["sends_deferred"] = s.sendsDeferred;
    d["sends_failed"] = s.sendsFailed;
    d["messages_dropped"] = s.messagesDropped;
    d["hmac_failures"] = s.hmacFailures;
    d["signed_bundles_received"] = s.signedBundlesReceived;
    d["malformed"] = s.malformed;
    d["ring_dropped"] = s.ringDropped;
    d["reconnects"] = s.reconnects;
    d["last_server_epoch_ms"] = s.lastServerEpochMs;
    nb::dict sentByType;
    nb::dict receivedByType;
    for (std::size_t i = 0; i < s.messagesSentByType.size(); ++i) {
      if (s.messagesSentByType[i]) sentByType[nb::int_(i)] = s.messagesSentByType[i];
      if (s.messagesReceivedByType[i]) receivedByType[nb::int_(i)] = s.messagesReceivedByType[i];
    }
    d["messages_sent_by_type"] = sentByType;
    d["messages_received_by_type"] = receivedByType;
    return d;
  }

  std::vector<std::pair<int, std::string>> drainLogs() const { return logger_.drain(); }

  // ----- for an attached WorldSession (session_binding.cpp)

  std::shared_ptr<replication::Connection> shared() const { return connection_; }
  std::mutex& consumerMutex() { return consumer_; }
  /// The pending rows; the caller holds consumerMutex().
  Rows& rowsLocked() { return rows_; }
  void wake() const { write_wake(wakeFd_.load(std::memory_order_acquire)); }

  /// While a session is attached it owns the connection's handlers; detaching
  /// puts the row-building handlers back.
  void attachSession(bool attached) {
    std::lock_guard lock(consumer_);
    sessionAttached_ = attached;
    if (!attached) connection_->setHandlers(handlers());
  }

 private:
  Result<std::uint8_t> sendOne(wire::MessageType type, const replication::SpatialSend& send) {
    switch (type) {
      case wire::MessageType::ActorUpdateRequest: return connection_->sendActorUpdate(send);
      case wire::MessageType::ClientAudioPacket: return connection_->sendAudio(send);
      case wire::MessageType::ClientVideoPacket: return connection_->sendVideo(send);
      case wire::MessageType::ClientTextPacket: return connection_->sendText(send);
      case wire::MessageType::GenericSpatial1: return connection_->sendGenericSpatial(send);
      default: return Errc::InvalidArgument;
    }
  }

  replication::Handlers handlers() {
    replication::Handlers h;
    auto plain = [this](const replication::SpatialNotification& n) {
      rows_.spatial(n, {0, 0, 0, 0}, n.payload);
    };
    h.actorUpdate = plain;
    h.audio = plain;
    h.video = plain;
    h.text = plain;
    h.genericSpatial = plain;
    h.singleActorMessage = plain;
    h.voxelUpdate = [this](const replication::SpatialNotification& n, const wire::VoxelPayloadView& v) {
      rows_.spatial(n, {v.x, v.y, v.z, v.voxelType}, v.state);
    };
    h.actorLeft = [this](const replication::SpatialNotification& n, std::uint8_t reason) {
      rows_.spatial(n, {reason, 0, 0, 0}, n.payload);
    };
    auto event = [this](const replication::SpatialNotification& n, const wire::EventPayloadView& e) {
      rows_.spatial(n, {e.eventType, 0, 0, 0}, e.state);
    };
    h.clientEvent = event;
    h.serverEvent = event;
    h.channelMessage = [this](const replication::ChannelNotification& c) {
      rows_.push(static_cast<std::uint8_t>(wire::MessageType::ChannelMessageNotification), 0, {},
                 c.senderUuid, c.epochMillis, c.sequence, {c.channelId, 0, 0, 0}, c.payload);
    };
    h.genericError = [this](const replication::GenericError& e) {
      rows_.push(static_cast<std::uint8_t>(wire::MessageType::GenericError), 0, {}, nullptr, 0,
                 e.sequence, {static_cast<std::int64_t>(e.code), 0, 0, 0}, {});
    };
    h.status = [this](ConnState s) {
      rows_.push(kStatusRow, 0, {}, nullptr, 0, 0, {static_cast<std::int64_t>(s), 0, 0, 0}, {});
    };
    return h;
  }

  QueueLogger logger_;
  std::shared_ptr<ProviderBridge> bridge_;
  std::shared_ptr<replication::Connection> connection_;
  std::atomic<std::int64_t> wakeFd_{-1};
  bool sessionAttached_ = false;  // under consumer_
  std::mutex consumer_;  // poll() is single-consumer
  std::mutex pumper_;    // and pump() single-producer, with or without a GIL
  Rows rows_;            // filled by the handlers, under consumer_
};

}  // namespace crowdypy
