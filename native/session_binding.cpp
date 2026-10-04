// crowdypy._native.session: CrowdyCPP's WorldSession over a bound Connection.
//
// The session owns the connection's handlers while it is attached, so every
// notification is applied to the stores natively, on the thread that calls
// tick(). What the stores do not keep (audio, video, text) is forwarded into the
// connection's batch rows. Store callbacks are queued here and handed to Python
// by drain_events() after tick(), so native code never calls into Python.
//
// Nothing tick() does blocks: host election and durable chunk persistence are
// GraphQL work the Python facade does itself (asynchronously in an event loop),
// so the session is built with no durable services and no host heartbeat.
#include "replication.hpp"

#include <map>

#include "crowdy/session/world_session.hpp"

namespace crowdypy {
namespace {

using session::ChunkCoord;

ChunkCoord coord_of(std::int64_t x, std::int64_t y, std::int64_t z) { return ChunkCoord{x, y, z}; }

nb::tuple chunk_tuple(const ChunkCoord& c) { return nb::make_tuple(c.x, c.y, c.z); }

nb::str uuid_text(const core::ActorUuid& u) {
  return uuid_str(reinterpret_cast<const char*>(u.data()));
}

nb::bytes blob(Bytes b) { return to_bytes(b); }

nb::bytes blob(const std::vector<std::uint8_t>& v) {
  return nb::bytes(reinterpret_cast<const char*>(v.data()), v.size());
}

// ------------------------------------------------------------------ queued events

/// One store callback, captured on the tick thread for Python to dispatch.
struct SessionEvent {
  enum Kind : int {
    kJoin = 1,
    kLeave,
    kUpdate,
    kChunkChanged,
    kError,
    kChannelMessage,
    kDirectMessage,
    kEvent,
    kActorLeft,
  };
  int kind = 0;
  std::string lane;  // "" for the default lane
  core::ActorUuid uuid{};
  ChunkCoord chunk{};
  std::int64_t a = 0;  // error code / channel id / event type / leave reason
  std::int64_t b = 0;  // error sequence / from-server flag
  std::int64_t c = 0;  // error send kind
  std::int64_t epochMs = 0;
  std::int64_t receivedAtMs = 0;
  bool hasUuid = true;
  std::vector<std::uint8_t> payload;
};

nb::tuple event_tuple(const SessionEvent& e) {
  nb::object uuid = e.hasUuid ? nb::object(uuid_text(e.uuid)) : nb::none();
  return nb::make_tuple(e.kind, e.lane, uuid, chunk_tuple(e.chunk), e.a, e.b, e.c, e.epochMs,
                        e.receivedAtMs, blob(e.payload));
}

nb::tuple actor_tuple(const session::RemoteActor& actor) {
  nb::list samples;
  for (const auto& s : actor.samples)
    samples.append(nb::make_tuple(blob(s.state.bytes()), s.serverEpochMs, s.receivedAtMs));
  return nb::make_tuple(uuid_text(actor.uuid), chunk_tuple(actor.chunk), actor.lastSeenMs,
                        actor.lastServerEpochMs, samples);
}

// ------------------------------------------------------------------ actor snapshots

/// Every actor of a lane at one moment, in columns: uuid (32 octets), chunk,
/// server epoch of the latest update, and the latest state back to back with
/// offsets. Columns are views that keep the snapshot alive.
struct ActorColumns {
  std::vector<std::uint8_t> uuid;
  std::vector<std::int64_t> chunk;
  std::vector<std::int64_t> epochMs;
  std::vector<std::uint32_t> offsets{0};
  std::vector<std::uint8_t> state;

  std::size_t size() const { return epochMs.size(); }

  void add(const session::RemoteActor& actor) {
    uuid.insert(uuid.end(), actor.uuid.begin(), actor.uuid.end());
    chunk.insert(chunk.end(), {actor.chunk.x, actor.chunk.y, actor.chunk.z});
    epochMs.push_back(actor.lastServerEpochMs);
    const Bytes latest = actor.state();
    state.insert(state.end(), latest.begin(), latest.end());
    offsets.push_back(static_cast<std::uint32_t>(state.size()));
  }
};

// ------------------------------------------------------------------ the session

class PySession {
 public:
  PySession(PyConnection& conn, const std::string& appId, const std::string& actorUuid,
            int sendHz, std::int64_t keyframeIntervalMs, std::int64_t heartbeatIntervalMs,
            int distance, int decay, std::int64_t staleAfterMs, int historySize,
            std::int64_t reapIntervalMs, int voxelSendDistance)
      : conn_(conn) {
    session::WorldSessionConfig config;
    config.appId = appId;
    config.actorUuid = actorUuid;
    config.self.sendHz = sendHz;
    config.self.keyframeIntervalMs = keyframeIntervalMs;
    config.self.heartbeatIntervalMs = heartbeatIntervalMs;
    config.self.distance = distance_arg(distance);
    config.self.decay = decay_arg(decay);
    config.actors.staleAfterMs = staleAfterMs;
    config.actors.historySize = historySize;
    config.chunks.writeBackIntervalMs = 0;  // durable write-back is the facade's
    config.chunks.voxelSendDistance = distance_arg(voxelSendDistance);
    config.hostHeartbeatIntervalMs = 0;     // host election is the facade's
    config.reapIntervalMs = reapIntervalMs;
    auto forward = [this](const replication::SpatialNotification& n) {
      conn_.rowsLocked().spatial(n, {0, 0, 0, 0}, n.payload);
    };
    config.onAudio = forward;
    config.onVideo = forward;
    config.onText = forward;
    config.onActorLeft = [this](const core::ActorUuid& uuid, std::uint8_t reason) {
      SessionEvent e;
      e.kind = SessionEvent::kActorLeft;
      e.uuid = uuid;
      e.a = reason;
      events_.push_back(std::move(e));
    };
    {
      std::lock_guard lock(conn_.consumerMutex());
      session_ = std::make_unique<session::WorldSession>(
          conn_.shared(), session::WorldSessionServices{}, std::move(config));
    }
    conn_.attachSession(true);
    watchLane("");
    session_->chunks().onChunkChanged([this](const session::ChunkData& chunk) {
      if (!watchChunks_) return;
      SessionEvent e;
      e.kind = SessionEvent::kChunkChanged;
      e.hasUuid = false;
      e.chunk = chunk.coord;
      events_.push_back(std::move(e));
    });
    session_->errors().onError([this](const session::AttributedError& err) {
      SessionEvent e;
      e.kind = SessionEvent::kError;
      e.hasUuid = err.actorUuid.has_value();
      if (err.actorUuid) e.uuid = *err.actorUuid;
      e.a = static_cast<std::int64_t>(err.code);
      e.b = err.sequence;
      e.c = static_cast<std::int64_t>(err.kind);
      e.receivedAtMs = err.atMs;
      events_.push_back(std::move(e));
    });
    session_->channelInbox().onMessage([this](const session::InboxMessage& m) {
      pushMessage(SessionEvent::kChannelMessage, m);
    });
    session_->directInbox().onMessage([this](const session::InboxMessage& m) {
      pushMessage(SessionEvent::kDirectMessage, m);
    });
  }

  ~PySession() { close(); }

  void close() {
    if (!session_) return;
    {
      std::lock_guard lock(conn_.consumerMutex());
      session_.reset();
    }
    conn_.attachSession(false);
  }

  /// Drive the session: drain the connection into the stores, run the local
  /// actor's send loop, reap stale actors. Returns how many events wait for
  /// drain_events().
  std::size_t tick() {
    std::size_t queued = 0;
    bool forwarded = false;
    {
      nb::gil_scoped_release release;
      std::lock_guard lock(conn_.consumerMutex());
      live().tick();
      queued = events_.size();
      forwarded = conn_.rowsLocked().size() > 0;
    }
    if (forwarded) conn_.wake();  // forwarded media and text wait in the batch rows
    return queued;
  }

  nb::list drainEvents() {
    std::vector<SessionEvent> events;
    {
      std::lock_guard lock(conn_.consumerMutex());
      events.swap(events_);
    }
    nb::list out;
    for (const auto& e : events) out.append(event_tuple(e));
    return out;
  }

  void setWatchUpdates(bool on) { watchUpdates_ = on; }
  void setWatchChunks(bool on) { watchChunks_ = on; }

  // ----- the local actor

  nb::str actorUuid() { return uuid_text([&] {
      std::lock_guard lock(conn_.consumerMutex());
      return live().actorUuid();
    }()); }

  nb::tuple selfChunk() {
    return chunk_tuple([&] {
      std::lock_guard lock(conn_.consumerMutex());
      return live().self().chunk();
    }());
  }

  bool selfJoined() { return [&] {
      std::lock_guard lock(conn_.consumerMutex());
      return live().self().joined();
    }(); }

  void join(std::int64_t x, std::int64_t y, std::int64_t z, nb::handle state) {
    BufferView body(state);
    Status status;
    {
      nb::gil_scoped_release release;
      std::lock_guard lock(conn_.consumerMutex());
      status = live().join(coord_of(x, y, z), body.bytes());
      recordSelfSend();
    }
    if (!status.ok()) raise(status.code, "join");
  }

  void moveTo(std::int64_t x, std::int64_t y, std::int64_t z) {
    Status status;
    {
      nb::gil_scoped_release release;
      std::lock_guard lock(conn_.consumerMutex());
      status = live().self().moveTo(coord_of(x, y, z));
      recordSelfSend();
    }
    if (!status.ok()) raise(status.code, "moveTo");
  }

  void setChunk(std::int64_t x, std::int64_t y, std::int64_t z) {
    std::lock_guard lock(conn_.consumerMutex());
    live().self().setChunk(coord_of(x, y, z));
  }

  void refresh() {
    Status status;
    {
      nb::gil_scoped_release release;
      std::lock_guard lock(conn_.consumerMutex());
      status = live().self().refresh();
      recordSelfSend();
    }
    if (!status.ok()) raise(status.code, "refresh");
  }

  void setState(nb::handle state) {
    BufferView body(state);
    if (body.size() > session::kMaxStateBytes)
      raise(Errc::InvalidArgument, "an actor state is at most 256 bytes");
    std::lock_guard lock(conn_.consumerMutex());
    live().self().setState(body.bytes());
  }

  nb::bytes selfState() {
    const session::StateBlob state = [&] {
      std::lock_guard lock(conn_.consumerMutex());
      return live().self().state();
    }();
    return blob(state.bytes());
  }

  int selfStatus() { return static_cast<int>([&] {
      std::lock_guard lock(conn_.consumerMutex());
      return live().self().status();
    }()); }

  std::optional<nb::tuple> lastAck() {
    auto ack = [&] {
      std::lock_guard lock(conn_.consumerMutex());
      return live().self().lastAck();
    }();
    if (!ack) return std::nullopt;
    return nb::make_tuple(ack->sequence, ack->serverEpochMs, ack->receivedAtMs,
                          blob(ack->state.bytes()));
  }

  std::optional<nb::tuple> lastSent() {
    auto sent = [&] {
      std::lock_guard lock(conn_.consumerMutex());
      return live().self().lastSent();
    }();
    if (!sent) return std::nullopt;
    nb::object sequence = sent->sequence ? nb::object(nb::int_(*sent->sequence)) : nb::none();
    return nb::make_tuple(blob(sent->state.bytes()), chunk_tuple(sent->chunk), sequence,
                          sent->sentAtMs, static_cast<int>(sent->reason));
  }

  std::optional<nb::tuple> lastError() {
    auto err = [&] {
      std::lock_guard lock(conn_.consumerMutex());
      return live().self().lastError();
    }();
    if (!err) return std::nullopt;
    nb::object server = err->serverCode ? nb::object(nb::int_(static_cast<int>(*err->serverCode)))
                                        : nb::none();
    nb::object sequence = err->sequence ? nb::object(nb::int_(*err->sequence)) : nb::none();
    return nb::make_tuple(errcName(err->status.code), server, sequence, err->receivedAtMs);
  }

  // ----- remote actors

  std::size_t actorCount(const std::string& lane) {
    std::lock_guard lock(conn_.consumerMutex());
    return lane.empty() ? live().actors().size() : laneOf(lane).size();
  }

  std::optional<nb::tuple> actor(const std::string& lane, nb::handle uuid) {
    const core::ActorUuid id = uuid_from(uuid);
    std::lock_guard lock(conn_.consumerMutex());
    const session::RemoteActor* found =
        lane.empty() ? live().actors().find(id) : laneOf(lane).find(id);
    if (!found) return std::nullopt;
    return actor_tuple(*found);
  }

  nb::list actors(const std::string& lane) {
    std::lock_guard lock(conn_.consumerMutex());
    nb::list out;
    auto all = lane.empty() ? live().actors().list() : laneOf(lane).list();
    for (const auto* a : all) out.append(actor_tuple(*a));
    return out;
  }

  ActorColumns actorColumns(const std::string& lane) {
    ActorColumns columns;
    std::lock_guard lock(conn_.consumerMutex());
    auto all = lane.empty() ? live().actors().list() : laneOf(lane).list();
    columns.uuid.reserve(all.size() * wire::kUuidSize);
    for (const auto* a : all) columns.add(*a);
    return columns;
  }

  bool removeActor(nb::handle uuid) {
    const core::ActorUuid id = uuid_from(uuid);
    std::lock_guard lock(conn_.consumerMutex());
    return live().actors().remove(id);
  }

  void reap() {
    std::lock_guard lock(conn_.consumerMutex());
    live().actors().reap(core::systemClock().monotonicMillis());
  }

  void clearActors(const std::string& lane) {
    std::lock_guard lock(conn_.consumerMutex());
    if (lane.empty()) {
      live().actors().clear();
    } else {
      laneOf(lane).clear();
    }
  }

  std::uint64_t actorRevision(const std::string& lane) {
    std::lock_guard lock(conn_.consumerMutex());
    return lane.empty() ? live().actors().revision() : laneOf(lane).revision();
  }

  /// A named lane: actors whose latest state satisfies
  /// `(state[tagOffset] & tagMask) == tagValue` (every actor when tagOffset < 0).
  /// The filter runs natively, per update.
  void addLane(const std::string& name, int tagOffset, int tagMask, int tagValue,
               std::int64_t staleAfterMs, int historySize) {
    if (name.empty()) raise(Errc::InvalidArgument, "a lane needs a name");
    std::lock_guard lock(conn_.consumerMutex());
    session::RemoteActorLane::Options options;
    if (tagOffset >= 0) {
      const auto offset = static_cast<std::size_t>(tagOffset);
      const auto mask = static_cast<std::uint8_t>(tagMask);
      const auto value = static_cast<std::uint8_t>(tagValue);
      options.filter = [offset, mask, value](const replication::SpatialNotification& n) {
        return n.payload.size() > offset && (n.payload[offset] & mask) == value;
      };
    }
    options.staleAfterMs = staleAfterMs;
    options.historySize = historySize;
    auto& lane = live().actors().lane(name, std::move(options));
    lanes_[name] = &lane;
    watchLane(name);
  }

  // ----- chunks

  std::optional<nb::tuple> chunk(std::int64_t x, std::int64_t y, std::int64_t z) {
    std::lock_guard lock(conn_.consumerMutex());
    const session::ChunkData* c = live().chunks().find(coord_of(x, y, z));
    if (!c) return std::nullopt;
    nb::dict states;
    for (const auto& [index, state] : c->voxelStates)
      states[nb::int_(index)] = nb::make_tuple(state.voxelType, blob(state.state));
    return nb::make_tuple(
        nb::bytes(reinterpret_cast<const char*>(c->voxels.data()), c->voxels.size()), states,
        c->hydratedAtMs);
  }

  nb::list chunkCoords() {
    std::lock_guard lock(conn_.consumerMutex());
    nb::list out;
    for (const auto* c : live().chunks().list()) out.append(chunk_tuple(c->coord));
    return out;
  }

  int voxelTypeAt(std::int64_t x, std::int64_t y, std::int64_t z, int vx, int vy, int vz) {
    std::lock_guard lock(conn_.consumerMutex());
    return live().chunks().voxelTypeAt(coord_of(x, y, z), vx, vy, vz);
  }

  std::optional<nb::tuple> voxelStateAt(std::int64_t x, std::int64_t y, std::int64_t z, int vx,
                                        int vy, int vz) {
    std::lock_guard lock(conn_.consumerMutex());
    const session::VoxelState* s = live().chunks().voxelStateAt(coord_of(x, y, z), vx, vy, vz);
    if (!s) return std::nullopt;
    return nb::make_tuple(s->voxelType, blob(s->state));
  }

  int setVoxel(std::int64_t x, std::int64_t y, std::int64_t z, int vx, int vy, int vz,
               int voxelType, nb::handle state) {
    BufferView body(state);
    const std::int16_t type = i16_arg(voxelType, "voxel type");
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      std::lock_guard lock(conn_.consumerMutex());
      auto& s = live();
      result = s.chunks().setVoxel(coord_of(x, y, z), vx, vy, vz, type, body.bytes(), s.actorUuid());
      if (result.ok()) s.errors().recordSend(result.value(), session::SendKind::VoxelUpdate, s.actorUuid());
    }
    return sent(result, "setVoxel");
  }

  /// Insert a chunk the facade loaded from the durable store, or generated.
  void seed(std::int64_t x, std::int64_t y, std::int64_t z, nb::handle voxels) {
    BufferView body(voxels);
    if (body.size() != session::kChunkVolume)
      raise(Errc::InvalidArgument, "a chunk is 4096 voxel octets");
    std::array<std::uint8_t, session::kChunkVolume> grid{};
    std::memcpy(grid.data(), body.bytes().data(), grid.size());
    std::lock_guard lock(conn_.consumerMutex());
    live().chunks().seed(coord_of(x, y, z), grid);
  }

  std::size_t pruneBeyond(std::int64_t x, std::int64_t y, std::int64_t z, int distance) {
    std::lock_guard lock(conn_.consumerMutex());
    return live().chunks().pruneBeyond(coord_of(x, y, z), distance);
  }

  std::uint64_t chunkRevision() {
    std::lock_guard lock(conn_.consumerMutex());
    return live().chunks().revision();
  }

  std::size_t chunkCount() {
    std::lock_guard lock(conn_.consumerMutex());
    return live().chunks().size();
  }

  // ----- errors

  nb::list recentErrors(std::size_t limit) {
    std::lock_guard lock(conn_.consumerMutex());
    nb::list out;
    for (const auto& e : live().errors().recent(limit)) out.append(error_tuple(e));
    return out;
  }

  std::uint64_t errorTotal() {
    std::lock_guard lock(conn_.consumerMutex());
    return live().errors().total();
  }

  void clearErrors() {
    std::lock_guard lock(conn_.consumerMutex());
    live().errors().clear();
  }

  // ----- inboxes

  /// The retained messages, oldest first (optionally one channel's). Draining
  /// takes every message, whatever `channelId` says.
  nb::list inbox(bool channel, std::optional<std::int64_t> channelId, bool drain) {
    std::lock_guard lock(conn_.consumerMutex());
    auto& box = channel ? live().channelInbox() : live().directInbox();
    nb::list out;
    auto add = [&out](const session::InboxMessage& m) {
      out.append(nb::make_tuple(m.channelId, uuid_text(m.senderUuid), blob(m.payload),
                                m.serverEpochMs, m.receivedAtMs));
    };
    if (drain) {
      for (const auto& m : box.drain()) add(m);
    } else {
      for (const auto* m : box.messages(channelId)) add(*m);
    }
    return out;
  }

  nb::list inboxChannels() {
    std::lock_guard lock(conn_.consumerMutex());
    nb::list out;
    for (auto id : live().channelInbox().channels()) out.append(id);
    return out;
  }

  void clearInbox(bool channel) {
    std::lock_guard lock(conn_.consumerMutex());
    (channel ? live().channelInbox() : live().directInbox()).clear();
  }

  // ----- events

  void watchEvent(int eventType) {
    if (eventType < 0 || eventType > 0xFFFF) raise(Errc::InvalidArgument, "event type is a uint16");
    std::lock_guard lock(conn_.consumerMutex());
    live().events().on(static_cast<std::uint16_t>(eventType),
                       [this](const session::EventRouter::Event& ev) {
                         SessionEvent e;
                         e.kind = SessionEvent::kEvent;
                         e.uuid = ev.senderUuid;
                         e.chunk = ev.chunk;
                         e.a = ev.eventType;
                         e.b = ev.fromServer ? 1 : 0;
                         e.epochMs = ev.serverEpochMs;
                         e.receivedAtMs = ev.receivedAtMs;
                         e.payload = ev.state;
                         events_.push_back(std::move(e));
                       });
  }

  std::optional<nb::tuple> lastEvent(int eventType) {
    std::lock_guard lock(conn_.consumerMutex());
    const auto* ev = live().events().lastEvent(static_cast<std::uint16_t>(eventType));
    if (!ev) return std::nullopt;
    return nb::make_tuple(ev->eventType, ev->fromServer, uuid_text(ev->senderUuid),
                          chunk_tuple(ev->chunk), blob(ev->state), ev->serverEpochMs,
                          ev->receivedAtMs);
  }

 private:
  session::WorldSession& live() {
    if (!session_) raise(Errc::Closed, "the world session is closed");
    return *session_;
  }

  /// A send made between ticks is attributed now, not on the next tick: that tick
  /// drains first, and an error for it may already be waiting. The caller holds
  /// the consumer lock.
  void recordSelfSend() {
    auto& s = live();
    if (auto seq = s.self().lastSequence())
      s.errors().recordSend(*seq, session::SendKind::ActorUpdate, s.actorUuid());
  }

  session::RemoteActorLane& laneOf(const std::string& name) {
    const auto it = lanes_.find(name);
    if (it == lanes_.end()) raise(Errc::InvalidArgument, "no lane named " + name);
    return *it->second;
  }

  static nb::tuple error_tuple(const session::AttributedError& e) {
    nb::object uuid = e.actorUuid ? nb::object(uuid_text(*e.actorUuid)) : nb::none();
    return nb::make_tuple(static_cast<int>(e.code), e.sequence, static_cast<int>(e.kind), uuid,
                          e.atMs);
  }

  void watchLane(const std::string& lane) {
    auto push = [this, lane](int kind, const session::RemoteActor& actor) {
      SessionEvent e;
      e.kind = kind;
      e.lane = lane;
      e.uuid = actor.uuid;
      e.chunk = actor.chunk;
      e.epochMs = actor.lastServerEpochMs;
      e.receivedAtMs = actor.lastSeenMs;
      const Bytes state = actor.state();
      e.payload.assign(state.begin(), state.end());
      events_.push_back(std::move(e));
    };
    if (lane.empty()) {
      auto& store = live().actors();
      store.onJoin([push](const session::RemoteActor& a) { push(SessionEvent::kJoin, a); });
      store.onLeave([push](const session::RemoteActor& a) { push(SessionEvent::kLeave, a); });
      store.onUpdate([this, push](const session::RemoteActor& a) {
        if (watchUpdates_) push(SessionEvent::kUpdate, a);
      });
    } else {
      auto& l = laneOf(lane);
      l.onJoin([push](const session::RemoteActor& a) { push(SessionEvent::kJoin, a); });
      l.onLeave([push](const session::RemoteActor& a) { push(SessionEvent::kLeave, a); });
      l.onUpdate([this, push](const session::RemoteActor& a) {
        if (watchUpdates_) push(SessionEvent::kUpdate, a);
      });
    }
  }

  void pushMessage(int kind, const session::InboxMessage& m) {
    SessionEvent e;
    e.kind = kind;
    e.uuid = m.senderUuid;
    e.a = m.channelId;
    e.epochMs = m.serverEpochMs;
    e.receivedAtMs = m.receivedAtMs;
    e.payload = m.payload;
    events_.push_back(std::move(e));
  }

  PyConnection& conn_;
  std::unique_ptr<session::WorldSession> session_;
  std::map<std::string, session::RemoteActorLane*> lanes_;
  std::vector<SessionEvent> events_;  // under conn_.consumerMutex()
  std::atomic<bool> watchUpdates_{false};
  std::atomic<bool> watchChunks_{false};
};

}  // namespace

void register_session(nb::module_& m) {
  m.attr("JOIN") = static_cast<int>(SessionEvent::kJoin);
  m.attr("LEAVE") = static_cast<int>(SessionEvent::kLeave);
  m.attr("UPDATE") = static_cast<int>(SessionEvent::kUpdate);
  m.attr("CHUNK_CHANGED") = static_cast<int>(SessionEvent::kChunkChanged);
  m.attr("ERROR") = static_cast<int>(SessionEvent::kError);
  m.attr("CHANNEL_MESSAGE") = static_cast<int>(SessionEvent::kChannelMessage);
  m.attr("DIRECT_MESSAGE") = static_cast<int>(SessionEvent::kDirectMessage);
  m.attr("EVENT") = static_cast<int>(SessionEvent::kEvent);
  m.attr("ACTOR_LEFT") = static_cast<int>(SessionEvent::kActorLeft);
  m.attr("MAX_STATE_BYTES") = session::kMaxStateBytes;
  m.attr("CHUNK_VOLUME") = session::kChunkVolume;

  nb::class_<ActorColumns>(m, "ActorColumns")
      .def("__len__", &ActorColumns::size)
      .def_prop_ro(
          "uuids",
          [](const ActorColumns& c) { return column(c.uuid, c.size(), wire::kUuidSize); },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "chunks", [](const ActorColumns& c) { return column(c.chunk, c.size(), 3); },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "epoch_ms", [](const ActorColumns& c) { return column(c.epochMs, c.size(), 1); },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "state_offsets",
          [](const ActorColumns& c) { return column(c.offsets, c.offsets.size(), 1); },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "state_data",
          [](const ActorColumns& c) { return column(c.state, c.state.size(), 1); },
          nb::rv_policy::reference_internal);

  nb::class_<PySession>(m, "WorldSession")
      .def(nb::init<PyConnection&, const std::string&, const std::string&, int, std::int64_t,
                    std::int64_t, int, int, std::int64_t, int, std::int64_t, int>(),
           nb::arg("connection"), nb::arg("app_id"), nb::arg("actor_uuid") = "",
           nb::arg("send_hz") = 5, nb::arg("keyframe_interval_ms") = 3000,
           nb::arg("heartbeat_interval_ms") = 2000, nb::arg("distance") = 8, nb::arg("decay") = 1,
           nb::arg("stale_after_ms") = 12000, nb::arg("history_size") = 2,
           nb::arg("reap_interval_ms") = 1000, nb::arg("voxel_send_distance") = 8,
           nb::keep_alive<1, 2>())
      .def("close", &PySession::close)
      .def("tick", &PySession::tick)
      .def("drain_events", &PySession::drainEvents)
      .def("watch_updates", &PySession::setWatchUpdates, nb::arg("on"))
      .def("watch_chunks", &PySession::setWatchChunks, nb::arg("on"))
      .def_prop_ro("actor_uuid", &PySession::actorUuid)
      .def("self_chunk", &PySession::selfChunk)
      .def("self_joined", &PySession::selfJoined)
      .def("join", &PySession::join, nb::arg("x"), nb::arg("y"), nb::arg("z"), nb::arg("state"))
      .def("move_to", &PySession::moveTo, nb::arg("x"), nb::arg("y"), nb::arg("z"))
      .def("set_chunk", &PySession::setChunk, nb::arg("x"), nb::arg("y"), nb::arg("z"))
      .def("refresh", &PySession::refresh)
      .def("set_state", &PySession::setState, nb::arg("state"))
      .def("self_state", &PySession::selfState)
      .def("self_status", &PySession::selfStatus)
      .def("last_ack", &PySession::lastAck)
      .def("last_sent", &PySession::lastSent)
      .def("last_error", &PySession::lastError)
      .def("actor_count", &PySession::actorCount, nb::arg("lane") = "")
      .def("actor", &PySession::actor, nb::arg("lane"), nb::arg("uuid"))
      .def("actors", &PySession::actors, nb::arg("lane") = "")
      .def("actor_columns", &PySession::actorColumns, nb::arg("lane") = "")
      .def("remove_actor", &PySession::removeActor, nb::arg("uuid"))
      .def("reap", &PySession::reap)
      .def("clear_actors", &PySession::clearActors, nb::arg("lane") = "")
      .def("actor_revision", &PySession::actorRevision, nb::arg("lane") = "")
      .def("add_lane", &PySession::addLane, nb::arg("name"), nb::arg("tag_offset") = -1,
           nb::arg("tag_mask") = 0xFF, nb::arg("tag_value") = 0,
           nb::arg("stale_after_ms") = 12000, nb::arg("history_size") = 2)
      .def("chunk", &PySession::chunk, nb::arg("x"), nb::arg("y"), nb::arg("z"))
      .def("chunk_coords", &PySession::chunkCoords)
      .def("voxel_type_at", &PySession::voxelTypeAt, nb::arg("x"), nb::arg("y"), nb::arg("z"),
           nb::arg("vx"), nb::arg("vy"), nb::arg("vz"))
      .def("voxel_state_at", &PySession::voxelStateAt, nb::arg("x"), nb::arg("y"), nb::arg("z"),
           nb::arg("vx"), nb::arg("vy"), nb::arg("vz"))
      .def("set_voxel", &PySession::setVoxel, nb::arg("x"), nb::arg("y"), nb::arg("z"),
           nb::arg("vx"), nb::arg("vy"), nb::arg("vz"), nb::arg("voxel_type"), nb::arg("state"))
      .def("seed", &PySession::seed, nb::arg("x"), nb::arg("y"), nb::arg("z"), nb::arg("voxels"))
      .def("prune_beyond", &PySession::pruneBeyond, nb::arg("x"), nb::arg("y"), nb::arg("z"),
           nb::arg("distance"))
      .def("chunk_revision", &PySession::chunkRevision)
      .def("chunk_count", &PySession::chunkCount)
      .def("recent_errors", &PySession::recentErrors, nb::arg("limit") = SIZE_MAX)
      .def("error_total", &PySession::errorTotal)
      .def("clear_errors", &PySession::clearErrors)
      .def("inbox", &PySession::inbox, nb::arg("channel"), nb::arg("channel_id").none(),
           nb::arg("drain"))
      .def("inbox_channels", &PySession::inboxChannels)
      .def("clear_inbox", &PySession::clearInbox, nb::arg("channel"))
      .def("watch_event", &PySession::watchEvent, nb::arg("event_type"))
      .def("last_event", &PySession::lastEvent, nb::arg("event_type"));
}

}  // namespace crowdypy
