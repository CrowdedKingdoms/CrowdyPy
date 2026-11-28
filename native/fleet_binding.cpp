// crowdypy._native.replication.FleetSession: CrowdyCPP's replication::FleetSession for Python.
//
// A fleet session is one realtime session an organization's game server opens for itself,
// carrying the players who are on it. The fleet token signs for every one of them, so this
// runs on a server the organization runs and never on a player's device.
//
// It keeps the rules replication_binding.cpp is built around (AGENTS.md, "Performance
// rules"):
//
//  - CrowdyCPP's network thread (or the pump() caller) receives, verifies and queues, and
//    sends the carries that are due. Nothing it runs touches the interpreter. The session is
//    given its token and its server, so it asks Python for nothing: FleetConfig::reassign
//    stays unset, and a move the realtime server orders is read from move_ordered.
//  - Python drains events in batches (FleetSession.poll), as columns it can view without
//    copying, with the carried players each row is for beside them. A carry's result, a
//    release and a refusal are rows of the same batch: there is no callback per carry.
//  - Sends release the GIL; a batch send releases it once for the whole batch.
//  - An asyncio loop is woken through Config::onEventsReady and a socket pair.
#include "crowdy/replication/fleet_session.hpp"
#include "replication.hpp"

namespace crowdypy {
namespace {

using replication::FleetRecipients;

/// The row type of a refusal the session could name a carried player for
/// (FleetHandlers::sendRefused). Not a wire opcode: the wire never carries 254.
constexpr std::uint8_t kSendRefusedRow = 254;
constexpr std::uint8_t kCarryResultRow = static_cast<std::uint8_t>(wire::MessageType::FleetCarryResult);
constexpr std::uint8_t kReleasedRow = static_cast<std::uint8_t>(wire::MessageType::FleetReleased);
constexpr std::uint8_t kSessionReleasedRow =
    static_cast<std::uint8_t>(wire::MessageType::CommandSessionReleased);

/// Events in columns, as a player's connection has them (Rows), and for each row the carried
/// players it is for: `listed` is 1 when the notification arrived inside a FLEET_DELIVERY,
/// whose game token ids are recipients[recipientOffsets[i] .. recipientOffsets[i + 1]], and 0
/// when it arrived plain, for the session as a whole.
///
/// What `extra` holds for the session's own rows: a carry result (playerGameTokenId, userId,
/// status, wireStatus), a release (playerGameTokenId, userId, reason, wireReason), a session
/// release (reason, wireReason), a named refusal (playerGameTokenId, userId, code) with the
/// refused send's sequence, an error (code, verified, keepalive), a status row (state), and a
/// not-present report or its end (verified, playerGameTokenId, the end's reason or 0) with
/// the actor, the chunk and the first refused keepalive's sequence in their own columns.
struct FleetRows {
  Rows rows;
  std::vector<std::uint8_t> listed;
  std::vector<std::uint32_t> recipientOffsets{0};
  std::vector<std::int64_t> recipients;

  std::size_t size() const { return rows.size(); }

  void reserve(std::size_t count, std::size_t bytes) {
    rows.reserve(count, bytes);
    listed.reserve(count);
    recipientOffsets.reserve(count + 1);
  }

  void close(const FleetRecipients& who) {
    listed.push_back(who.forSession() ? 0 : 1);
    recipients.insert(recipients.end(), who.begin(), who.end());
    recipientOffsets.push_back(static_cast<std::uint32_t>(recipients.size()));
  }

  void spatial(const replication::SpatialNotification& n, std::array<std::int64_t, kExtras> extras,
               Bytes body, const FleetRecipients& who) {
    rows.spatial(n, extras, body);
    close(who);
  }

  void channel(wire::MessageType type, const replication::ChannelNotification& c,
               const FleetRecipients& who) {
    rows.channel(type, c);
    close(who);
  }

  /// One of the session's own rows: no app, no chunk, no actor and no payload.
  void own(std::uint8_t type, std::uint8_t sequence, std::array<std::int64_t, kExtras> extras) {
    rows.push(type, 0, {}, nullptr, 0, sequence, extras, {});
    close(FleetRecipients{});
  }

  void error(const replication::GenericError& e) {
    rows.error(e);
    close(FleetRecipients{});
  }

  /// A carried player's not-present report (`end` 0) or its end.
  void presence(const replication::ActorNotPresent& report, std::uint8_t end) {
    rows.presence(report, end);
    close(FleetRecipients{});
  }
};

class FleetBatch {
 public:
  FleetBatch() = default;
  explicit FleetBatch(FleetRows rows)
      : notifications_(std::move(rows.rows)),
        listed_(std::move(rows.listed)),
        offsets_(std::move(rows.recipientOffsets)),
        recipients_(std::move(rows.recipients)) {}

  std::size_t size() const { return notifications_.size(); }
  const NotificationBatch& notifications() const { return notifications_; }
  const std::vector<std::uint8_t>& listed() const { return listed_; }
  const std::vector<std::uint32_t>& offsets() const { return offsets_; }
  const std::vector<std::int64_t>& recipients() const { return recipients_; }

  /// The carried players row `i` is for: a tuple of game token ids, or None when the
  /// notification is for the session as a whole.
  nb::object recipientsOf(std::size_t i) const {
    notifications_.check(i);
    if (!listed_[i]) return nb::none();
    nb::list out;
    for (std::uint32_t at = offsets_[i]; at < offsets_[i + 1]; ++at) out.append(recipients_[at]);
    return nb::tuple(out);
  }

 private:
  NotificationBatch notifications_;
  std::vector<std::uint8_t> listed_;
  std::vector<std::uint32_t> offsets_{0};
  std::vector<std::int64_t> recipients_;
};

struct FleetOptions {
  std::int64_t appId = 0;
  bool manualPump = false;
  bool preferIpv6 = false;
  int sessionReadyWaitMs = 1500;
  std::int64_t advertiseIntervalMs = 15 * 1000;
  bool ownMediaEcho = false;
  bool fleetOwnState = false;
  std::int64_t silenceTimeoutMs = replication::kFleetSilenceTimeoutMs;
  int carryResendMs = 250;
  int carryTimeoutMs = 5000;
  int carriesPerSecond = 50;
  int carryBurst = 32;
  int maxPlayers = 0;
  std::size_t ringCapacity = 4096;
  int socketRecvBufferBytes = 1 << 20;
  int socketSendBufferBytes = 1 << 20;
  bool bundleSends = true;
  int bundleWindowMs = 1;
};

Assignment server_arg(const std::string& ip4, const std::string& ip6, int clientPort) {
  if ((ip4.empty() && ip6.empty()) || clientPort <= 0 || clientPort > 0xFFFF)
    raise(Errc::InvalidArgument, "a realtime server is an address and a port");
  return Assignment{ip4, ip6, clientPort};
}

/// The fleet token, or a player's: exactly 64 characters. Named for what it is in the
/// message, and never echoed.
TokenInfo fleet_token(const std::string& token, std::int64_t gameTokenId, std::int64_t expiresAtMs) {
  if (token.size() != wire::kTokenOctets)
    raise(Errc::InvalidArgument, "a fleet token is exactly 64 characters");
  TokenInfo info;
  info.token = token;
  info.gameTokenId = gameTokenId;
  info.expiresAtEpochMs = expiresAtMs;
  return info;
}

void scrub(std::string& secret) {
  wire::scrub(reinterpret_cast<std::uint8_t*>(secret.data()), secret.size());
}

class PyFleetSession {
 public:
  PyFleetSession(const FleetOptions& options, const std::string& token, std::int64_t gameTokenId,
                 std::int64_t expiresAtMs, const std::string& ip4, const std::string& ip6,
                 int clientPort) {
    replication::FleetConfig config;
    config.appId = options.appId;
    config.token = fleet_token(token, gameTokenId, expiresAtMs);
    config.server = server_arg(ip4, ip6, clientPort);
    config.fleetOwnState = options.fleetOwnState;
    config.carryResendMs = options.carryResendMs;
    config.carryTimeoutMs = options.carryTimeoutMs;
    config.carriesPerSecond = options.carriesPerSecond;
    config.carryBurst = options.carryBurst;
    config.maxPlayers = options.maxPlayers;
    config.transport.manualPump = options.manualPump;
    config.transport.preferIpv6 = options.preferIpv6;
    config.transport.sessionReadyWaitMs = options.sessionReadyWaitMs;
    config.transport.advertiseIntervalMs = options.advertiseIntervalMs;
    config.transport.ownMediaEcho = options.ownMediaEcho;
    config.transport.watchdogSilenceMs = options.silenceTimeoutMs;
    config.transport.ringCapacity = options.ringCapacity;
    config.transport.socketRecvBufferBytes = options.socketRecvBufferBytes;
    config.transport.socketSendBufferBytes = options.socketSendBufferBytes;
    config.transport.bundleSends = options.bundleSends;
    config.transport.bundleWindowMs = options.bundleWindowMs;
    config.transport.onEventsReady = [this] { write_wake(wakeFd_.load(std::memory_order_acquire)); };
    fleet_ = std::make_unique<replication::FleetSession>(std::move(config), core::opensslCrypto(),
                                                         core::systemClock(), logger_);
    fleet_->setHandlers(handlers());
  }

  ~PyFleetSession() { fleet_->close(); }

  void open() {
    Status status;
    {
      nb::gil_scoped_release release;
      status = fleet_->open();
    }
    if (!status.ok()) raise(status.code, "open");
  }

  void close() {
    nb::gil_scoped_release release;
    fleet_->close();
  }

  int state() const { return static_cast<int>(fleet_->state()); }
  std::int64_t appId() const { return fleet_->appId(); }

  void setWakeFd(std::int64_t fd) {
    prepare_wake_fd(fd);
    wakeFd_.store(fd, std::memory_order_release);
  }

  // ----- the token and the server

  /// The session's own game token id and its expiry (epoch ms, 0: none). Never the token.
  std::pair<std::int64_t, std::int64_t> tokenInfo() const {
    TokenInfo info = fleet_->token();
    scrub(info.token);
    return {info.gameTokenId, info.expiresAtEpochMs};
  }

  std::tuple<std::string, std::string, int> server() const {
    const Assignment a = fleet_->server();
    return {a.ip4, a.ip6, a.clientPort};
  }

  void applyRefresh(const std::string& token, std::int64_t gameTokenId, std::int64_t expiresAtMs,
                    const std::string& ip4, const std::string& ip6, int clientPort) {
    TokenInfo info = fleet_token(token, gameTokenId, expiresAtMs);
    const Assignment to = server_arg(ip4, ip6, clientPort);
    Status status;
    {
      nb::gil_scoped_release release;
      status = fleet_->applyRefresh(info, to);
    }
    scrub(info.token);
    if (!status.ok()) raise(status.code, "applyRefresh");
  }

  bool moveOrdered() const { return fleet_->moveOrdered(); }

  /// What `refreshFleetSession` takes for this session, as (app_id, game_token_id,
  /// current_server): the server is (ip4, ip6, client_port) with exactly one address set,
  /// or None when the session was told to move.
  nb::tuple refreshInput() const {
    const replication::FleetRefreshInput input = fleet_->refreshInput();
    nb::object server = nb::none();
    if (input.currentServer)
      server = nb::make_tuple(input.currentServer->ip4, input.currentServer->ip6,
                              input.currentServer->clientPort);
    return nb::make_tuple(input.appId, input.gameTokenId, server);
  }

  std::size_t tableLimit() const { return fleet_->tableLimit(); }
  void setMaxPlayers(int maxPlayers) { fleet_->setMaxPlayers(maxPlayers); }

  /// The not-present report that stands for one of a carried player's actors, or None.
  std::optional<nb::tuple> notPresent(std::int64_t player, nb::handle uuid) const {
    const auto report = fleet_->notPresent(player, uuid_from(uuid));
    if (!report) return std::nullopt;
    return not_present_tuple(*report);
  }
  bool refreshDue() const { return fleet_->refreshDue(); }

  void setFleetOwnState(bool on) {
    nb::gil_scoped_release release;
    fleet_->setFleetOwnState(on);
  }
  bool fleetOwnState() const { return fleet_->fleetOwnState(); }

  void setOwnMediaEcho(bool on) {
    nb::gil_scoped_release release;
    fleet_->setOwnMediaEcho(on);
  }

  // ----- carrying

  /// Start carrying a player. The result arrives as a row of a later batch (type 51).
  void carry(const std::string& token, std::int64_t gameTokenId, std::int64_t replaces) {
    if (token.size() != wire::kTokenOctets)
      raise(Errc::InvalidArgument, "a player's app token is exactly 64 characters");
    replication::PlayerToken player;
    player.token = token;
    player.gameTokenId = gameTokenId;
    player.replaces = replaces;
    Status status;
    {
      nb::gil_scoped_release release;
      status = fleet_->carry(player);
    }
    scrub(player.token);
    if (!status.ok()) raise(status.code, "carry");
  }

  /// True when the FLEET_RELEASE left; false when the session is not connected (the player
  /// is out of the table either way).
  bool release(std::int64_t gameTokenId) {
    Status status;
    {
      nb::gil_scoped_release release;
      status = fleet_->release(gameTokenId);
    }
    if (status.code == Errc::NotConnected) return false;
    if (!status.ok()) raise(status.code, "release");
    return true;
  }

  bool carries(std::int64_t gameTokenId) const { return fleet_->carries(gameTokenId); }

  std::optional<nb::tuple> player(std::int64_t gameTokenId) const {
    const auto row = fleet_->player(gameTokenId);
    if (!row) return std::nullopt;
    return nb::make_tuple(row->gameTokenId, row->userId, row->carried, row->pending);
  }

  /// Every row of the table as (gameTokenId, userId, carried, pending).
  nb::list players() const {
    nb::list out;
    for (const auto& row : fleet_->players())
      out.append(nb::make_tuple(row.gameTokenId, row.userId, row.carried, row.pending));
    return out;
  }

  std::size_t carriedCount() const { return fleet_->carriedCount(); }

  // ----- sends for a carried player

  int sendSpatial(std::int64_t player, int type, std::int64_t x, std::int64_t y, std::int64_t z,
                  nb::handle uuid, nb::handle payload, int distance, int decay) {
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
      result = sendOne(player, static_cast<wire::MessageType>(type), send);
    }
    return sent(result, "send");
  }

  int sendVoxel(std::int64_t player, std::int64_t x, std::int64_t y, std::int64_t z,
                nb::handle uuid, int vx, int vy, int vz, int voxelType, nb::handle state,
                int distance, int decay) {
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
      result = fleet_->sendVoxelUpdate(player, {x, y, z}, id, ax, ay, az, type, body.bytes(), d, r);
    }
    return sent(result, "sendVoxelUpdate");
  }

  int sendClientEvent(std::int64_t player, std::int64_t x, std::int64_t y, std::int64_t z,
                      nb::handle uuid, int eventType, nb::handle state, int distance, int decay) {
    if (eventType < 0 || eventType > 0xFFFF) raise(Errc::InvalidArgument, "event type is a uint16");
    const core::ActorUuid id = uuid_from(uuid);
    const std::uint8_t d = distance_arg(distance);
    const wire::DecayRate r = decay_arg(decay);
    BufferView body(state);
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = fleet_->sendClientEvent(player, {x, y, z}, id,
                                       static_cast<std::uint16_t>(eventType), body.bytes(), d, r);
    }
    return sent(result, "sendClientEvent");
  }

  int sendSingleActor(std::int64_t player, std::int64_t x, std::int64_t y, std::int64_t z,
                      nb::handle uuid, nb::handle payload) {
    const core::ActorUuid id = uuid_from(uuid);
    BufferView body(payload);
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = fleet_->sendSingleActorMessage(player, {x, y, z}, id, body.bytes());
    }
    return sent(result, "sendSingleActorMessage");
  }

  int sendChannel(std::int64_t player, std::int64_t channelId, nb::handle uuid,
                  nb::handle payload) {
    const core::ActorUuid id = uuid_from(uuid);
    BufferView body(payload);
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = fleet_->sendChannelMessage(player, channelId, id, body.bytes());
    }
    return sent(result, "sendChannelMessage");
  }

  int sendChannelAudio(std::int64_t player, std::int64_t channelId, nb::handle uuid,
                       nb::handle payload) {
    const core::ActorUuid id = uuid_from(uuid);
    BufferView body(payload);
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = fleet_->sendChannelAudio(player, channelId, id, body.bytes());
    }
    return sent(result, "sendChannelAudio");
  }

  int sendRangedChannel(std::int64_t player, std::int64_t channelId, nb::handle uuid,
                        nb::handle payload, std::int64_t x, std::int64_t y, std::int64_t z,
                        std::uint32_t maxDistance) {
    const core::ActorUuid id = uuid_from(uuid);
    BufferView body(payload);
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = fleet_->sendRangedChannelMessage(player, channelId, id, body.bytes(), {x, y, z},
                                                maxDistance);
    }
    return sent(result, "sendRangedChannelMessage");
  }

  int sendHeartbeat(std::int64_t player, std::int64_t x, std::int64_t y, std::int64_t z,
                    nb::handle uuid) {
    const core::ActorUuid id = uuid_from(uuid);
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = fleet_->sendHeartbeat(player, {x, y, z}, id);
    }
    return sent(result, "sendHeartbeat");
  }

  std::size_t sendVideoFrame(std::int64_t player, std::int64_t x, std::int64_t y, std::int64_t z,
                             nb::handle uuid, nb::handle frame, int frameId, int codec,
                             int distance, int decay) {
    if (frameId < 0 || frameId > 0xFFFF) raise(Errc::InvalidArgument, "frame id is a uint16");
    if (codec < 0 || codec > 1) raise(Errc::InvalidArgument, "codec is 0 (JPEG) or 1 (WebP)");
    const core::ActorUuid id = uuid_from(uuid);
    const std::uint8_t d = distance_arg(distance);
    const wire::DecayRate r = decay_arg(decay);
    BufferView body(frame);
    Result<std::size_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = fleet_->sendVideoFrame(player, {x, y, z}, id, body.bytes(),
                                      static_cast<std::uint16_t>(frameId),
                                      static_cast<std::uint8_t>(codec), d, r);
    }
    if (!result.ok()) raise(result.error(), "sendVideoFrame");
    return result.value();
  }

  /// Many spatial sends of one type in one call, each for its own carried player: `players`
  /// is int64 (n), and the other columns are PyConnection::sendSpatialBatch's. The GIL is
  /// released once. A row whose player the session does not carry now is skipped and its
  /// index returned; any other refusal stops the batch and raises, with the count so far in
  /// the message. Returns (accepted, the skipped rows).
  std::pair<std::size_t, std::vector<std::size_t>> sendSpatialBatch(
      int type, nb::handle players, nb::handle chunks, nb::handle uuids, nb::handle payload,
      nb::handle offsets, std::size_t stride, int distance, int decay, bool flush) {
    BufferView playerView(players);
    BufferView chunkView(chunks);
    BufferView uuidView(uuids);
    BufferView bodyView(payload);
    if (playerView.size() % sizeof(std::int64_t) != 0)
      raise(Errc::InvalidArgument, "players are int64 game token ids");
    const std::size_t n = playerView.size() / sizeof(std::int64_t);
    if (chunkView.size() != n * 3 * sizeof(std::int64_t))
      raise(Errc::InvalidArgument, "chunks are int64 triples, one per player");
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
    std::vector<std::size_t> skipped;
    Errc failure = Errc::Ok;
    {
      nb::gil_scoped_release release;
      const auto* who = playerView.bytes().data();
      const auto* c = chunkView.bytes().data();
      const auto* ids = uuidView.bytes().data();
      const auto* body = bodyView.bytes().data();
      const auto* off = offsetView ? offsetView->bytes().data() : nullptr;
      replication::SpatialSend send;
      send.distance = d;
      send.decay = r;
      for (std::size_t i = 0; i < n; ++i) {
        std::int64_t player = 0;
        std::memcpy(&player, who + i * sizeof(player), sizeof(player));
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
        const Result<std::uint8_t> result = sendOne(player, kind, send);
        if (result.ok()) {
          ++accepted;
        } else if (result.error() == Errc::NotCarried) {
          skipped.push_back(i);
        } else {
          failure = result.error();
          break;
        }
      }
      if (flush && failure == Errc::Ok) (void)fleet_->flushSends();
    }
    if (failure != Errc::Ok)
      raise(failure, "batch stopped after " + std::to_string(accepted) + " send(s)");
    return {accepted, std::move(skipped)};
  }

  void setSpeakers(std::int64_t player, int voiceMode, const std::vector<std::string>& voice,
                   int videoMode, const std::vector<std::string>& video) {
    replication::SpeakerList v = speaker_list(voiceMode, voice);
    replication::SpeakerList w = speaker_list(videoMode, video);
    Status status;
    {
      nb::gil_scoped_release release;
      status = fleet_->setSpeakers(player, std::move(v), std::move(w));
    }
    if (!status.ok()) raise(status.code, "setSpeakers");
  }

  // ----- the session's own sends

  int sendSessionHeartbeat() {
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = fleet_->sendHeartbeat();
    }
    return sent(result, "sendHeartbeat");
  }

  int sendCapabilities() {
    Result<std::uint8_t> result = Errc::InvalidArgument;
    {
      nb::gil_scoped_release release;
      result = fleet_->sendCapabilities();
    }
    return sent(result, "sendCapabilities");
  }

  void flushSends() {
    Status status;
    {
      nb::gil_scoped_release release;
      status = fleet_->flushSends();
    }
    if (!status.ok() && status.code != Errc::WouldBlock) raise(status.code, "flushSends");
  }

  // ----- receive

  FleetBatch poll(std::size_t maxEvents) {
    FleetRows rows;
    {
      nb::gil_scoped_release release;
      std::lock_guard lock(consumer_);
      fleet_->poll(maxEvents);
      rows = std::move(rows_);
      rows_ = FleetRows{};
      rows_.reserve(std::max<std::size_t>(rows.size(), 64),
                    std::max<std::size_t>(rows.rows.payload.size(), 4096));
    }
    return FleetBatch(std::move(rows));
  }

  std::size_t pump(int timeoutMs) {
    nb::gil_scoped_release release;
    std::lock_guard lock(pumper_);
    return fleet_->pump(timeoutMs);
  }

  nb::dict stats() const {
    const auto s = fleet_->stats();
    nb::dict d = stats_dict(s);
    d["no_live_token_retries"] = s.noLiveTokenRetries;
    d["replaced_id_frames"] = s.replacedIdFrames;
    d["session_releases"] = s.sessionReleases;
    d["send_refusals_named"] = s.sendRefusalsNamed;
    d["carries_refused_table_full"] = s.carriesRefusedTableFull;
    return d;
  }

  std::uint64_t sessionEpoch() const { return fleet_->sessionEpoch(); }
  std::vector<std::pair<int, std::string>> drainLogs() const { return logger_.drain(); }

 private:
  Result<std::uint8_t> sendOne(std::int64_t player, wire::MessageType type,
                               const replication::SpatialSend& send) {
    switch (type) {
      case wire::MessageType::ActorUpdateRequest: return fleet_->sendActorUpdate(player, send);
      case wire::MessageType::ClientAudioPacket: return fleet_->sendAudio(player, send);
      case wire::MessageType::ClientVideoPacket: return fleet_->sendVideo(player, send);
      case wire::MessageType::ClientTextPacket: return fleet_->sendText(player, send);
      case wire::MessageType::GenericSpatial1: return fleet_->sendGenericSpatial(player, send);
      default: return Errc::InvalidArgument;
    }
  }

  /// Every handler fills rows_ on the thread that calls poll(), which holds consumer_.
  replication::FleetHandlers handlers() {
    replication::FleetHandlers h;
    auto plain = [this](const replication::SpatialNotification& n, FleetRecipients who) {
      rows_.spatial(n, {0, 0, 0, 0}, n.payload, who);
    };
    h.actorUpdate = plain;
    h.audio = plain;
    h.video = plain;
    h.text = plain;
    h.genericSpatial = plain;
    h.singleActorMessage = plain;
    h.voxelUpdate = [this](const replication::SpatialNotification& n,
                           const wire::VoxelPayloadView& v, FleetRecipients who) {
      rows_.spatial(n, {v.x, v.y, v.z, v.voxelType}, v.state, who);
    };
    h.actorLeft = [this](const replication::SpatialNotification& n, std::uint8_t reason,
                         FleetRecipients who) {
      rows_.spatial(n, {reason, 0, 0, 0}, n.payload, who);
    };
    h.chunkChanged = [this](const replication::SpatialNotification& n, std::int64_t updatedAtMs,
                            FleetRecipients who) {
      rows_.spatial(n, {updatedAtMs, 0, 0, 0}, n.payload, who);
    };
    auto event = [this](const replication::SpatialNotification& n,
                        const wire::EventPayloadView& e, FleetRecipients who) {
      rows_.spatial(n, {e.eventType, 0, 0, 0}, e.state, who);
    };
    h.clientEvent = event;
    h.serverEvent = event;
    h.channelMessage = [this](const replication::ChannelNotification& c, FleetRecipients who) {
      rows_.channel(wire::MessageType::ChannelMessageNotification, c, who);
    };
    h.channelAudio = [this](const replication::ChannelNotification& c, FleetRecipients who) {
      rows_.channel(wire::MessageType::ChannelAudioNotification, c, who);
    };
    h.genericError = [this](const replication::GenericError& e) { rows_.error(e); };
    h.notPresent = [this](const replication::ActorNotPresent& report) {
      rows_.presence(report, 0);
    };
    h.notPresentEnded = [this](const replication::ActorNotPresent& report,
                               replication::NotPresentEnd end) {
      rows_.presence(report, static_cast<std::uint8_t>(end));
    };
    h.sendRefused = [this](const replication::FleetSendRefused& r) {
      rows_.own(kSendRefusedRow, r.sequence,
                {r.playerGameTokenId, r.userId, static_cast<std::int64_t>(r.code), 0});
    };
    h.sessionReleased = [this](const replication::FleetSessionReleased& r) {
      rows_.own(kSessionReleasedRow, 0,
                {static_cast<std::int64_t>(r.reason), r.wireReason, 0, 0});
    };
    h.status = [this](ConnState s) {
      rows_.own(kStatusRow, 0, {static_cast<std::int64_t>(s), 0, 0, 0});
    };
    h.carryResult = [this](const replication::FleetCarryResult& r) {
      rows_.own(kCarryResultRow, 0,
                {r.playerGameTokenId, r.userId, static_cast<std::int64_t>(r.status), r.wireStatus});
    };
    h.released = [this](const replication::FleetReleased& r) {
      rows_.own(kReleasedRow, 0,
                {r.playerGameTokenId, r.userId, static_cast<std::int64_t>(r.reason), r.wireReason});
    };
    return h;
  }

  QueueLogger logger_;
  std::atomic<std::int64_t> wakeFd_{-1};
  std::mutex consumer_;  // poll() is single-consumer
  std::mutex pumper_;    // and pump() single-producer, with or without a GIL
  FleetRows rows_;       // filled by the handlers, under consumer_
  /// Declared last: destroyed first, so its network thread has stopped before the logger and
  /// the wake descriptor it uses go.
  std::unique_ptr<replication::FleetSession> fleet_;
};

}  // namespace

void register_fleet(nb::module_& m) {
  m.attr("FLEET_SEND_REFUSED_ROW") = kSendRefusedRow;
  m.attr("FLEET_CARRY_RESULT_ROW") = kCarryResultRow;
  m.attr("FLEET_RELEASED_ROW") = kReleasedRow;
  m.attr("FLEET_SESSION_RELEASED_ROW") = kSessionReleasedRow;
  m.attr("FLEET_CARRY_TIMED_OUT") = static_cast<int>(replication::FleetCarryStatus::TimedOut);
  m.attr("FLEET_CARRY_REFUSED") = static_cast<int>(replication::FleetCarryStatus::Refused);
  m.attr("FLEET_CARRY_TABLE_FULL") = static_cast<int>(replication::FleetCarryStatus::TableFull);
  m.attr("FLEET_TABLE_MARGIN") = replication::kFleetTableMargin;
  m.attr("FLEET_MAX_PLAYERS_CEILING") = replication::kFleetMaxPlayersCeiling;
  m.def(
      "fleet_current_server",
      [](const std::string& ip4, const std::string& ip6, int clientPort,
         bool preferIpv6) -> std::optional<nb::tuple> {
        const auto current =
            replication::fleetCurrentServer(Assignment{ip4, ip6, clientPort}, preferIpv6);
        if (!current) return std::nullopt;
        return nb::make_tuple(current->ip4, current->ip6, current->clientPort);
      },
      nb::arg("ip4"), nb::arg("ip6"), nb::arg("client_port"), nb::arg("prefer_ipv6") = false,
      "refreshFleetSession's currentServer for a server, as (ip4, ip6, client_port) with "
      "exactly one address set; None for a server with no address or no port.");
  m.attr("FLEET_SILENCE_TIMEOUT_MS") = replication::kFleetSilenceTimeoutMs;
  m.attr("FLEET_MIN_SILENCE_TIMEOUT_MS") = replication::kFleetMinSilenceTimeoutMs;
  m.attr("FLEET_REFUSAL_WINDOW_MS") = replication::kFleetRefusalWindowMs;

  nb::class_<FleetBatch>(m, "FleetBatch")
      .def("__len__", &FleetBatch::size)
      .def_prop_ro("notifications", &FleetBatch::notifications, nb::rv_policy::reference_internal,
                   "The rows as a NotificationBatch: the columns a player's connection has.")
      .def_prop_ro(
          "listed", [](const FleetBatch& b) { return column(b.listed(), b.size(), 1); },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "recipient_offsets",
          [](const FleetBatch& b) { return column(b.offsets(), b.offsets().size(), 1); },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "recipient_ids",
          [](const FleetBatch& b) { return column(b.recipients(), b.recipients().size(), 1); },
          nb::rv_policy::reference_internal)
      .def("recipients", &FleetBatch::recipientsOf, nb::arg("index"),
           "The carried players row `index` is for, or None for the session as a whole.")
      .def("rows", [](const FleetBatch& b) {
        nb::list out;
        for (std::size_t i = 0; i < b.size(); ++i)
          out.append(nb::make_tuple(row_tuple(b.notifications(), i), b.recipientsOf(i)));
        return out;
      });

  nb::class_<PyFleetSession>(m, "FleetSession")
      .def(
          "__init__",
          [](PyFleetSession* self, std::int64_t appId, const std::string& token,
             std::int64_t gameTokenId, std::int64_t expiresAtMs, const std::string& ip4,
             const std::string& ip6, int clientPort, bool manualPump, bool preferIpv6,
             int sessionReadyWaitMs, std::int64_t advertiseIntervalMs, bool ownMediaEcho,
             bool fleetOwnState, std::int64_t silenceTimeoutMs, int carryResendMs,
             int carryTimeoutMs, int carriesPerSecond, int carryBurst, std::size_t ringCapacity,
             int recvBuffer, int sendBuffer, bool bundleSends, int bundleWindowMs,
             int maxPlayers) {
            FleetOptions o;
            o.appId = appId;
            o.manualPump = manualPump;
            o.preferIpv6 = preferIpv6;
            o.sessionReadyWaitMs = sessionReadyWaitMs;
            o.advertiseIntervalMs = advertiseIntervalMs;
            o.ownMediaEcho = ownMediaEcho;
            o.fleetOwnState = fleetOwnState;
            o.silenceTimeoutMs = silenceTimeoutMs;
            o.carryResendMs = carryResendMs;
            o.carryTimeoutMs = carryTimeoutMs;
            o.carriesPerSecond = carriesPerSecond;
            o.carryBurst = carryBurst;
            o.ringCapacity = ringCapacity;
            o.socketRecvBufferBytes = recvBuffer;
            o.socketSendBufferBytes = sendBuffer;
            o.bundleSends = bundleSends;
            o.bundleWindowMs = bundleWindowMs;
            o.maxPlayers = maxPlayers;
            new (self) PyFleetSession(o, token, gameTokenId, expiresAtMs, ip4, ip6, clientPort);
          },
          nb::arg("app_id"), nb::arg("token"), nb::arg("game_token_id"),
          nb::arg("expires_at_ms"), nb::arg("ip4"), nb::arg("ip6"), nb::arg("client_port"),
          nb::arg("manual_pump") = false, nb::arg("prefer_ipv6") = false,
          nb::arg("session_ready_wait_ms") = 1500, nb::arg("advertise_interval_ms") = 15 * 1000,
          nb::arg("own_media_echo") = false, nb::arg("fleet_own_state") = false,
          nb::arg("silence_timeout_ms") = replication::kFleetSilenceTimeoutMs,
          nb::arg("carry_resend_ms") = 250, nb::arg("carry_timeout_ms") = 5000,
          nb::arg("carries_per_second") = 50, nb::arg("carry_burst") = 32,
          nb::arg("ring_capacity") = 4096, nb::arg("socket_recv_buffer_bytes") = 1 << 20,
          nb::arg("socket_send_buffer_bytes") = 1 << 20, nb::arg("bundle_sends") = true,
          nb::arg("bundle_window_ms") = 1, nb::arg("max_players") = 0)
      .def("open", &PyFleetSession::open)
      .def("close", &PyFleetSession::close)
      .def_prop_ro("state", &PyFleetSession::state)
      .def_prop_ro("app_id", &PyFleetSession::appId)
      .def("set_wake_fd", &PyFleetSession::setWakeFd, nb::arg("fd"))
      .def("token_info", &PyFleetSession::tokenInfo,
           "(game_token_id, expires_at_ms). The token itself is never returned.")
      .def("server", &PyFleetSession::server)
      .def("apply_refresh", &PyFleetSession::applyRefresh, nb::arg("token"),
           nb::arg("game_token_id"), nb::arg("expires_at_ms"), nb::arg("ip4"), nb::arg("ip6"),
           nb::arg("client_port"))
      .def_prop_ro("move_ordered", &PyFleetSession::moveOrdered)
      .def("refresh_input", &PyFleetSession::refreshInput,
           "(app_id, game_token_id, current_server): the server as (ip4, ip6, client_port) "
           "with one address set, or None after a move was ordered.")
      .def_prop_ro("table_limit", &PyFleetSession::tableLimit)
      .def("set_max_players", &PyFleetSession::setMaxPlayers, nb::arg("max_players"))
      .def("not_present", &PyFleetSession::notPresent, nb::arg("player"), nb::arg("uuid"))
      .def("refresh_due", &PyFleetSession::refreshDue)
      .def("set_fleet_own_state", &PyFleetSession::setFleetOwnState, nb::arg("on"))
      .def_prop_ro("fleet_own_state", &PyFleetSession::fleetOwnState)
      .def("set_own_media_echo", &PyFleetSession::setOwnMediaEcho, nb::arg("on"))
      .def("carry", &PyFleetSession::carry, nb::arg("token"), nb::arg("game_token_id"),
           nb::arg("replaces") = 0)
      .def("release", &PyFleetSession::release, nb::arg("game_token_id"))
      .def("carries", &PyFleetSession::carries, nb::arg("game_token_id"))
      .def("player", &PyFleetSession::player, nb::arg("game_token_id"))
      .def("players", &PyFleetSession::players)
      .def_prop_ro("carried_count", &PyFleetSession::carriedCount)
      .def("send_spatial", &PyFleetSession::sendSpatial, nb::arg("player"), nb::arg("type"),
           nb::arg("x"), nb::arg("y"), nb::arg("z"), nb::arg("uuid"), nb::arg("payload"),
           nb::arg("distance"), nb::arg("decay"))
      .def("send_voxel_update", &PyFleetSession::sendVoxel, nb::arg("player"), nb::arg("x"),
           nb::arg("y"), nb::arg("z"), nb::arg("uuid"), nb::arg("voxel_x"), nb::arg("voxel_y"),
           nb::arg("voxel_z"), nb::arg("voxel_type"), nb::arg("state"), nb::arg("distance"),
           nb::arg("decay"))
      .def("send_client_event", &PyFleetSession::sendClientEvent, nb::arg("player"),
           nb::arg("x"), nb::arg("y"), nb::arg("z"), nb::arg("uuid"), nb::arg("event_type"),
           nb::arg("state"), nb::arg("distance"), nb::arg("decay"))
      .def("send_single_actor_message", &PyFleetSession::sendSingleActor, nb::arg("player"),
           nb::arg("x"), nb::arg("y"), nb::arg("z"), nb::arg("uuid"), nb::arg("payload"))
      .def("send_channel_message", &PyFleetSession::sendChannel, nb::arg("player"),
           nb::arg("channel_id"), nb::arg("uuid"), nb::arg("payload"))
      .def("send_channel_audio", &PyFleetSession::sendChannelAudio, nb::arg("player"),
           nb::arg("channel_id"), nb::arg("uuid"), nb::arg("payload"))
      .def("send_ranged_channel_message", &PyFleetSession::sendRangedChannel, nb::arg("player"),
           nb::arg("channel_id"), nb::arg("uuid"), nb::arg("payload"), nb::arg("x"),
           nb::arg("y"), nb::arg("z"), nb::arg("max_distance"))
      .def("send_heartbeat", &PyFleetSession::sendHeartbeat, nb::arg("player"), nb::arg("x"),
           nb::arg("y"), nb::arg("z"), nb::arg("uuid"))
      .def("send_video_frame", &PyFleetSession::sendVideoFrame, nb::arg("player"), nb::arg("x"),
           nb::arg("y"), nb::arg("z"), nb::arg("uuid"), nb::arg("frame"), nb::arg("frame_id"),
           nb::arg("codec"), nb::arg("distance"), nb::arg("decay"))
      .def("send_spatial_batch", &PyFleetSession::sendSpatialBatch, nb::arg("type"),
           nb::arg("players"), nb::arg("chunks"), nb::arg("uuids"), nb::arg("payload"),
           nb::arg("offsets").none(), nb::arg("stride"), nb::arg("distance"), nb::arg("decay"),
           nb::arg("flush"))
      .def("set_speakers", &PyFleetSession::setSpeakers, nb::arg("player"),
           nb::arg("voice_mode"), nb::arg("voice"), nb::arg("video_mode"), nb::arg("video"))
      .def("send_session_heartbeat", &PyFleetSession::sendSessionHeartbeat)
      .def("send_capabilities", &PyFleetSession::sendCapabilities)
      .def("flush_sends", &PyFleetSession::flushSends)
      .def("poll", &PyFleetSession::poll, nb::arg("max_events") = SIZE_MAX)
      .def("pump", &PyFleetSession::pump, nb::arg("timeout_ms") = 0)
      .def("stats", &PyFleetSession::stats)
      .def_prop_ro("session_epoch", &PyFleetSession::sessionEpoch)
      .def("drain_logs", &PyFleetSession::drainLogs);
}

}  // namespace crowdypy
