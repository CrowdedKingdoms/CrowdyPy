#include "crowdy/replication/fleet_session.hpp"

#include <algorithm>
#include <atomic>
#include <thread>
#include <utility>

#include "dispatch.hpp"

namespace crowdy::replication {

using wire::MessageType;

namespace {

/// How long the silence check waits for the answer to the capabilities that last left, so
/// that the acknowledgement of the advertisement sent as the timeout passes still counts.
constexpr std::int64_t kAcknowledgementGraceMs = 1'000;

bool sameServer(const Assignment& a, const Assignment& b) {
  return a.ip4 == b.ip4 && a.ip6 == b.ip6 && a.clientPort == b.clientPort;
}

FleetCarryStatus carryStatusOf(std::uint8_t wireStatus) {
  return wireStatus <= static_cast<std::uint8_t>(FleetCarryStatus::NoRoom)
             ? static_cast<FleetCarryStatus>(wireStatus)
             : FleetCarryStatus::Refused;
}

/// The reasons the platform sends are 1, 2 and 5; anything else is Unknown, never a cast.
FleetReleaseReason releaseReasonOf(std::uint8_t wireReason) {
  switch (wireReason) {
    case 1: return FleetReleaseReason::CarriedElsewhere;
    case 2: return FleetReleaseReason::SignInEnded;
    case 5: return FleetReleaseReason::Idle;
    default: return FleetReleaseReason::Unknown;
  }
}

FleetSessionReleaseReason sessionReleaseReasonOf(std::uint8_t wireReason) {
  return wireReason <= 4 ? static_cast<FleetSessionReleaseReason>(wireReason)
                         : FleetSessionReleaseReason::Unknown;
}

/// How long a retired gameTokenId is remembered: a late frame about it arrives within
/// seconds.
constexpr std::int64_t kRetiredForMs = 60'000;

/// The long-spatial frame of a message that names no place and no actor: chunk (0,0,0),
/// distance 0, a zero UUID. CLIENT_CAPABILITIES is sent this way.
SpatialSend controlSend(Bytes payload) {
  SpatialSend p;
  p.chunk = wire::ChunkCoord{};
  p.distance = 0;
  p.decay = wire::DecayRate::None;
  p.payload = payload;
  return p;
}

}  // namespace

/// Where the session's connection finds its realtime server. A fleet session is given its
/// server (openFleetSession's answer) and keeps it; only a move the server asks for goes
/// back to the game server, through FleetConfig::reassign.
class FleetSession::Endpoint final : public ISessionProvider {
 public:
  Endpoint(Assignment server, std::function<Result<Assignment>()> reassign)
      : server_(std::move(server)), reassign_(std::move(reassign)) {}

  Result<Assignment> assignServer() override {
    if (!useHeld_.exchange(false, std::memory_order_acq_rel)) {
      if (reassign_) {
        Result<Assignment> next = reassign_();
        if (!next.ok()) return next.error();
        std::lock_guard lock(mutex_);
        server_ = next.value();
        return server_;
      }
      // Nobody answered the order: the session opens again where it is, and says so until
      // the game server gives it a server (FleetSession::moveOrdered).
      moveOrdered_.store(true, std::memory_order_release);
    }
    std::lock_guard lock(mutex_);
    return server_;
  }

  /// The fleet token is the game server's to refresh (refreshFleetSession); the connection
  /// is configured never to ask.
  Result<TokenInfo> refreshToken() override { return Errc::Rejected; }

  /// The next assignment opens at the server held, without asking the game server.
  void useHeldNext() { useHeld_.store(true, std::memory_order_release); }

  bool moveOrdered() const { return moveOrdered_.load(std::memory_order_acquire); }
  void clearMoveOrdered() { moveOrdered_.store(false, std::memory_order_release); }
  /// The session wants a server from the game server, and stays where it is until then.
  void orderMove() { moveOrdered_.store(true, std::memory_order_release); }
  bool asksGameServer() const { return static_cast<bool>(reassign_); }

  void set(const Assignment& server) {
    std::lock_guard lock(mutex_);
    server_ = server;
  }
  Assignment get() const {
    std::lock_guard lock(mutex_);
    return server_;
  }

 private:
  mutable std::mutex mutex_;
  Assignment server_;
  std::function<Result<Assignment>()> reassign_;
  std::atomic<bool> useHeld_{true};
  std::atomic<bool> moveOrdered_{false};
};

class FleetSession::SendScope {
 public:
  SendScope(FleetSession& session, std::int64_t player) : session_(session) {
    std::lock_guard lock(session.playersMutex_);
    const auto it = session.rowOfLocked(player);
    if (it == session.players_.end() || !it->second.carried) return;  // NotCarried
    if (!session.ready()) {
      gate = Errc::NotConnected;
      return;
    }
    tailId = it->second.sendId;
    // Counted under the table's lock: a refreshed token's FLEET_CARRY, which is sent under
    // the same lock, waits for the count to reach 0 before it switches the player's id.
    session.sendsInFlight_.fetch_add(1, std::memory_order_acq_rel);
    gate = Errc::Ok;
  }
  ~SendScope() {
    if (gate == Errc::Ok) session_.sendsInFlight_.fetch_sub(1, std::memory_order_acq_rel);
  }
  SendScope(const SendScope&) = delete;
  SendScope& operator=(const SendScope&) = delete;

  Errc gate = Errc::NotCarried;
  std::int64_t tailId = 0;

 private:
  FleetSession& session_;
};

FleetSession::FleetSession(FleetConfig config, const core::ICrypto& crypto,
                           const core::IClock& clock, const core::ILogger& logger)
    : config_(std::move(config)),
      crypto_(crypto),
      clock_(clock),
      endpoint_(std::make_shared<Endpoint>(config_.server, config_.reassign)) {
  // The session's CLIENT_CAPABILITIES binds it to its address, so it is not an option here.
  config_.transport.advertiseCapabilities = true;
  // An idle session hears only the acknowledgements of its capabilities, so three
  // advertisements fit in the silence timeout.
  std::int64_t& silence = config_.transport.watchdogSilenceMs;
  if (silence > 0) {
    silence = std::max(silence, kFleetMinSilenceTimeoutMs);
    config_.transport.advertiseIntervalMs =
        std::min(config_.transport.advertiseIntervalMs, silence / 3);
  } else {
    silence = 0;
  }
  Config transport = config_.transport;
  transport.appId = config_.appId;
  transport.token = config_.token;
  transport.refreshLeadMs = 0;
  tokenSinceEpochMs_.store(clock_.epochMillis(), std::memory_order_release);
  connection_ = std::make_unique<Connection>(std::move(transport), endpoint_, crypto_, clock_,
                                             logger);

  auto hooks = std::make_unique<Connection::FleetHooks>();
  hooks->message = [this](Bytes message) { onFleetMessage(message); };
  hooks->tick = [this] { onTick(); };
  hooks->advertised = [this] { onAdvertised(); };
  hooks->sessionReleased = [this](std::uint8_t reason) { onSessionReleased(reason); };
  hooks->sent = [this](std::uint8_t sequence, std::int64_t player) { onSent(sequence, player); };
  hooks->capabilities.store(config_.fleetOwnState ? wire::ClientCapability::kFleetOwnState : 0u,
                            std::memory_order_release);
  connection_->fleet_ = std::move(hooks);

  // Whatever arrives outside a FLEET_DELIVERY is for the session as a whole.
  Handlers plain;
  plain.any = [this](const SpatialNotification& n) {
    detail::dispatchSpatial(active_, n, FleetRecipients{});
  };
  plain.channelMessage = [this](const ChannelNotification& n) {
    if (active_.channelMessage) active_.channelMessage(n, FleetRecipients{});
  };
  plain.channelAudio = [this](const ChannelNotification& n) {
    if (active_.channelAudio) active_.channelAudio(n, FleetRecipients{});
  };
  plain.genericError = [this](const GenericError& e) { onGenericError(e); };
  plain.status = [this](ConnState s) {
    onStatus(s);
    if (active_.status) active_.status(s);
  };
  connection_->setHandlers(std::move(plain));
}

FleetSession::~FleetSession() { connection_->disconnect(); }

Status FleetSession::open() {
  const ConnState s = state();
  if (s != ConnState::Connected && s != ConnState::Connecting) {
    endpoint_->useHeldNext();
    // A session that starts anew holds no carry on the server, whichever server it is.
    sessionRestarted_.store(true, std::memory_order_release);
  }
  return connection_->connect();
}

void FleetSession::close() {
  connection_->disconnect();
  std::lock_guard lock(playersMutex_);
  players_.clear();
  aliases_.clear();
  retired_.clear();
  carriesHeld_ = false;
  attemptsPending_ = false;
}

FleetSession::Stats FleetSession::stats() const {
  Stats stats;
  static_cast<Connection::Stats&>(stats) = connection_->stats();
  stats.noLiveTokenRetries = noLiveTokenRetries_.load(std::memory_order_relaxed);
  stats.replacedIdFrames = replacedIdFrames_.load(std::memory_order_relaxed);
  stats.sessionReleases = sessionReleases_.load(std::memory_order_relaxed);
  stats.sendRefusalsNamed = sendRefusalsNamed_.load(std::memory_order_relaxed);
  return stats;
}

void FleetSession::setHandlers(FleetHandlers handlers) {
  std::lock_guard lock(handlersMutex_);
  handlers_ = std::move(handlers);
}

void FleetSession::onStatus(ConnState state) {
  // The session ended (an idle release, or a reassignment that failed): its carries ended
  // with it, unannounced. The rows wait to be carried again when it is opened again.
  if (state != ConnState::Inactive && state != ConnState::Failed) return;
  std::lock_guard lock(playersMutex_);
  recarryAllLocked();
}

bool FleetSession::takeToken(const TokenInfo& token) {
  const auto key = wire::Token64::fromString(token.token);
  const Connection::Credentials before = connection_->credentials();
  connection_->setToken(token);
  tokenSinceEpochMs_.store(clock_.epochMillis(), std::memory_order_release);
  return before.gameTokenId != token.gameTokenId ||
         (key && std::memcmp(before.token.octets, key->octets, wire::kTokenOctets) != 0);
}

Status FleetSession::setToken(const TokenInfo& token) {
  if (token.token.size() != wire::kTokenOctets) return Errc::InvalidArgument;
  if (!takeToken(token)) return Errc::Ok;  // a refresh: the same session, its carries stand
  // Another token or id is another session to the server, which holds no carry of it:
  // capabilities under the new id first, then every player again.
  {
    std::lock_guard lock(playersMutex_);
    recarryAllLocked();
    carriesHeld_ = false;
  }
  if (state() == ConnState::Connected) {
    (void)connection_->sendCapabilities();
    std::lock_guard lock(playersMutex_);
    driveCarriesLocked(clock_.monotonicMillis());
  }
  return Errc::Ok;
}

TokenInfo FleetSession::token() const {
  std::lock_guard lock(connection_->tokenMutex_);
  return connection_->token_;
}

Status FleetSession::setServer(const Assignment& server) {
  bool advertised = false;
  return place(server, advertised);
}

Status FleetSession::place(const Assignment& server, bool& advertised) {
  const std::string& host =
      (config_.transport.preferIpv6 && !server.ip6.empty()) ? server.ip6 : server.ip4;
  if (host.empty() || server.clientPort <= 0) return Errc::InvalidArgument;
  // The game server has named the session's server: an order to move is answered, whether
  // or not the answer is the server it is on. The server named has the silence timeout to
  // be heard from.
  const bool afterSilence = carriesLost_.exchange(false, std::memory_order_acq_rel);
  connection_->lastVerifiedMs_.store(clock_.monotonicMillis(), std::memory_order_relaxed);
  endpoint_->clearMoveOrdered();
  if (sameServer(endpoint_->get(), server)) {
    if (afterSilence) {
      // The server ended the session, or said nothing for the silence timeout: it holds
      // none of the carries, or may hold none (it was restarted, say). The capabilities
      // first, which bind the session again, then every player again.
      if (state() == ConnState::Connected) {
        (void)connection_->sendCapabilities();
        advertised = true;
      }
      std::lock_guard lock(playersMutex_);
      recarryAllLocked();
      carriesHeld_ = false;
    }
    return Errc::Ok;
  }
  endpoint_->set(server);
  const ConnState s = state();
  if (s == ConnState::Connected || s == ConnState::Connecting || s == ConnState::Reconnecting) {
    endpoint_->useHeldNext();
    connection_->requestReassignment();
  }
  return Errc::Ok;
}

Assignment FleetSession::server() const { return endpoint_->get(); }

Status FleetSession::applyRefresh(const TokenInfo& token, const Assignment& server) {
  // Both are checked before either is taken, so a bad answer changes nothing.
  if (token.token.size() != wire::kTokenOctets) return Errc::InvalidArgument;
  const std::string& host =
      (config_.transport.preferIpv6 && !server.ip6.empty()) ? server.ip6 : server.ip4;
  if (host.empty() || server.clientPort <= 0) return Errc::InvalidArgument;
  const bool sameSession = token.gameTokenId == connection_->credentials().gameTokenId;
  const bool stays = sameServer(endpoint_->get(), server);
  const Status taken = setToken(token);
  if (!taken.ok()) return taken;
  bool advertised = false;
  const Status placed = place(server, advertised);
  // The same session on the same server: its carries stand. The refresh authorized the
  // token there again, and the capabilities sent now are what let the server follow the
  // session to the address it has. No FLEET_CARRY. (Another server, or another session,
  // sends its capabilities and its carries once it is connected.)
  if (placed.ok() && stays && sameSession && !advertised && state() == ConnState::Connected)
    (void)connection_->sendCapabilities();
  return placed;
}

bool FleetSession::moveOrdered() const { return endpoint_->moveOrdered(); }

bool FleetSession::refreshDue() const {
  if (endpoint_->moveOrdered()) return true;
  const std::int64_t expiresAt = token().expiresAtEpochMs;
  if (expiresAt <= 0) return false;
  const std::int64_t since = tokenSinceEpochMs_.load(std::memory_order_acquire);
  return clock_.epochMillis() >= since + (expiresAt - since) / 2;
}

void FleetSession::setFleetOwnState(bool on) {
  constexpr std::uint32_t bit = wire::ClientCapability::kFleetOwnState;
  auto& capabilities = connection_->fleet_->capabilities;
  const std::uint32_t before = on ? capabilities.fetch_or(bit, std::memory_order_acq_rel)
                                  : capabilities.fetch_and(~bit, std::memory_order_acq_rel);
  if (((before & bit) != 0) == on) return;
  if (state() == ConnState::Connected) (void)connection_->sendCapabilities();
}

bool FleetSession::fleetOwnState() const noexcept {
  return (connection_->fleet_->capabilities.load(std::memory_order_acquire) &
          wire::ClientCapability::kFleetOwnState) != 0;
}

// ---------------------------------------------------------------------------
// Carrying
// ---------------------------------------------------------------------------

void FleetSession::beginAttempt(Player& player) {
  player.attempt = true;
  player.nextSendMs = 0;
  player.deadlineMs = 0;  // its timeout starts when it is first sent
  player.sends = 0;
  player.refusals = 0;
  player.noLiveToken = Player::NoLiveToken::None;
}

FleetSession::Table::iterator FleetSession::rowOfLocked(std::int64_t id) {
  auto it = players_.find(id);
  if (it != players_.end()) return it;
  const auto alias = aliases_.find(id);
  return alias == aliases_.end() ? players_.end() : players_.find(alias->second);
}

FleetSession::Table::const_iterator FleetSession::rowOfLocked(std::int64_t id) const {
  auto it = players_.find(id);
  if (it != players_.end()) return it;
  const auto alias = aliases_.find(id);
  return alias == aliases_.end() ? players_.end() : players_.find(alias->second);
}

void FleetSession::retireLocked(std::int64_t id) {
  const std::int64_t now = clock_.monotonicMillis();
  if (retired_.size() >= 64) {
    for (auto it = retired_.begin(); it != retired_.end();) {
      it = now - it->second > kRetiredForMs ? retired_.erase(it) : std::next(it);
    }
  }
  retired_[id] = now;
}

bool FleetSession::retiredLocked(std::int64_t id) const {
  const auto it = retired_.find(id);
  return it != retired_.end() && clock_.monotonicMillis() - it->second <= kRetiredForMs;
}

void FleetSession::dropPreviousLocked(Player& player, std::int64_t id) {
  if (player.previousId != 0) {
    aliases_.erase(player.previousId);
    retireLocked(player.previousId);
    player.previousId = 0;
    wire::scrub(player.previousKey.token.octets, wire::kTokenOctets);
  }
  player.previousEndedSet = false;
  player.sendId = id;
}

FleetSession::Table::iterator FleetSession::supersedeLocked(Table::iterator row,
                                                           const PlayerToken& refreshed) {
  const std::int64_t from = row->first;
  const std::int64_t id = refreshed.gameTokenId;
  Player& player = row->second;
  if (!player.carried) {
    // Its own carry was never confirmed: the row simply takes the new token.
    dropPreviousLocked(player, id);
    retireLocked(from);
  } else if (player.previousId != 0) {
    // A newer token while the last replacement still waited: the id that waited is dropped,
    // and the player's sends name the id the server confirmed until the new carry leaves.
    retireLocked(from);
    player.sendId = player.previousId;
  } else {
    // The carry under `from` stands, and sends keep naming it, until the FLEET_CARRY for the
    // new id leaves.
    player.previousId = from;
    player.previousKey = player.key.token;
    player.previousEndedSet = false;
  }
  if (player.previousId != 0) aliases_[player.previousId] = id;
  auto node = players_.extract(row);
  node.key() = id;
  const auto moved = players_.insert(std::move(node)).position;
  (void)moved->second.key.assign(refreshed.token);
  beginAttempt(moved->second);
  return moved;
}

Status FleetSession::carry(const PlayerToken& player, CarryCallback done) {
  if (player.token.size() != wire::kTokenOctets) return Errc::InvalidArgument;
  if (player.gameTokenId == connection_->credentials().gameTokenId) return Errc::InvalidArgument;
  const ConnState s = state();
  if (s != ConnState::Connected && s != ConnState::Connecting && s != ConnState::Reconnecting)
    return Errc::NotConnected;

  std::lock_guard lock(playersMutex_);
  auto it = players_.find(player.gameTokenId);
  if (it == players_.end() && player.replaces != 0 && player.replaces != player.gameTokenId) {
    // The same player with a refreshed token: the row moves to the new id at once, and the
    // player stays carried.
    const auto old = rowOfLocked(player.replaces);
    if (old != players_.end()) it = supersedeLocked(old, player);
  } else {
    if (it == players_.end()) {
      it = players_.try_emplace(player.gameTokenId).first;
      it->second.sendId = player.gameTokenId;
    }
    // Straight from the caller's string into the row: no copy of the token is left behind.
    (void)it->second.key.assign(player.token);
    beginAttempt(it->second);
  }
  if (it == players_.end()) {
    // `replaces` named nobody this session holds: a first carry.
    it = players_.try_emplace(player.gameTokenId).first;
    it->second.sendId = player.gameTokenId;
    (void)it->second.key.assign(player.token);
    beginAttempt(it->second);
  }
  Player& entry = it->second;
  entry.order = ++carryOrder_;
  entry.done = std::move(done);
  attemptsPending_ = true;
  // Before the session is connected, when the rate has no turn left, and while the server
  // has ended the session, the carry waits for the pass that can send it.
  if (ready() && !carriesHeld_) {
    const std::int64_t now = clock_.monotonicMillis();
    if (takeCarryTurnLocked(now)) sendCarryLocked(player.gameTokenId, entry, now);
  }
  return Errc::Ok;
}

void FleetSession::sendCarryLocked(std::int64_t id, Player& player, std::int64_t nowMono) {
  // One read of the session's id for the proof and the tail, so a token change between
  // them cannot make the two disagree. The time and the proof are new at every send: the
  // same bytes again are a replay once the first was accepted and its carry ended. Two
  // sends in one millisecond would be the same bytes, so the second is dated the next one.
  const std::int64_t fleetId = connection_->credentials().gameTokenId;
  const std::int64_t when = std::max(clock_.epochMillis(), player.lastCarryMs + 1);
  player.lastCarryMs = when;
  std::uint8_t payload[wire::fleet::kCarryPayloadSize];
  const auto n = wire::encodeFleetCarryPayload(crypto_, player.key.token, fleetId, id, when,
                                               MutableBytes(payload, sizeof(payload)));
  // A refreshed token's carry: from the moment it leaves, the player's sends name the new
  // id (the server holds them behind the carry), and one naming the old id after the carry
  // took effect would be refused. So no send for a player is between its check and its
  // transmission while this one leaves: they are counted under the lock this holds.
  const bool switches = player.previousId != 0 && player.sendId != id;
  if (switches) {
    while (sendsInFlight_.load(std::memory_order_acquire) != 0) std::this_thread::yield();
  }
  // A send that fails (a full send buffer, a socket between two servers) is tried again at
  // the next interval like one that was lost. Only a carry that left can be answered, so
  // only those count.
  if (n.ok() && connection_
                    ->sendLongSpatial(MessageType::FleetCarry,
                                      controlSend(Bytes(payload, n.value())), &fleetId)
                    .ok()) {
    ++player.sends;
    if (switches) player.sendId = id;
    if (player.noLiveToken == Player::NoLiveToken::Waiting)
      player.noLiveToken = Player::NoLiveToken::Retried;
  }
  player.nextSendMs = nowMono + std::max(1, config_.carryResendMs);
  if (player.deadlineMs == 0) player.deadlineMs = nowMono + std::max(0, config_.carryTimeoutMs);
}

bool FleetSession::takeCarryTurnLocked(std::int64_t nowMono) {
  // The server drops carries past 64 a second unanswered; the session keeps under that.
  const double burst = static_cast<double>(std::max(1, config_.carryBurst));
  const double rate = static_cast<double>(std::max(1, config_.carriesPerSecond));
  if (!carryTurnsPrimed_) {
    carryTurnsPrimed_ = true;
    carryTurns_ = burst;
  } else if (nowMono > carryTurnsAtMs_) {
    carryTurns_ = std::min(burst, carryTurns_ + static_cast<double>(nowMono - carryTurnsAtMs_) * rate / 1000.0);
  }
  carryTurnsAtMs_ = nowMono;
  if (carryTurns_ < 1.0) return false;
  carryTurns_ -= 1.0;
  return true;
}

void FleetSession::driveCarriesLocked(std::int64_t nowMono) {
  if (!attemptsPending_ || carriesHeld_) return;
  dueScratch_.clear();
  bool any = false;
  for (const auto& [id, player] : players_) {
    if (!player.attempt) continue;
    any = true;
    // Past its deadline a carry is poll()'s to report; it is not sent again.
    if (player.deadlineMs != 0 && nowMono >= player.deadlineMs) continue;
    if (nowMono >= player.nextSendMs) dueScratch_.emplace_back(player.nextSendMs, id);
  }
  attemptsPending_ = any;
  if (dueScratch_.empty()) return;
  // A carry that never left (0) goes before one that is being sent again, and of those the
  // one that has waited longest, so the rate starves no player.
  std::sort(dueScratch_.begin(), dueScratch_.end());
  for (const auto& due : dueScratch_) {
    if (!takeCarryTurnLocked(nowMono)) break;
    sendCarryLocked(due.second, players_.find(due.second)->second, nowMono);
  }
}

void FleetSession::recarryAllLocked() {
  for (auto& [id, player] : players_) {
    // A refreshed token's carry that waited: the server this goes to holds neither carry,
    // so the player is carried there with the new token and the old id is finished.
    if (player.previousId != 0) dropPreviousLocked(player, id);
    if (!player.attempt && !player.carried) continue;
    // Carried again only when its own result says so.
    player.carried = false;
    beginAttempt(player);
    attemptsPending_ = true;
  }
}

void FleetSession::checkSilence() {
  const std::int64_t timeout = config_.transport.watchdogSilenceMs;
  if (timeout <= 0 || state() != ConnState::Connected) return;
  // One sign for one silence: the game server has a refresh due already.
  if (endpoint_->moveOrdered()) return;
  const std::int64_t now = clock_.monotonicMillis();
  const std::int64_t heard = connection_->lastVerifiedMs_.load(std::memory_order_relaxed);
  if (now - heard <= timeout) return;
  // The capabilities that just left are still owed their acknowledgement.
  if (now - connection_->lastCapsMs_ < kAcknowledgementGraceMs) return;
  if (connection_->logger_.enabled(core::LogLevel::Warn)) {
    connection_->logger_.log(
        core::LogLevel::Warn,
        "fleet session: the realtime server has sent nothing that verifies for " +
            std::to_string(now - heard) + " ms; a refresh is due");
  }
  // The server may be gone, or may have ended the session: it may hold no carry when it,
  // or another, is heard again.
  carriesLost_.store(true, std::memory_order_release);
  if (endpoint_->asksGameServer()) {
    sessionRestarted_.store(true, std::memory_order_release);
    connection_->requestReassignment();
  } else {
    endpoint_->orderMove();
  }
}

void FleetSession::onTick() {
  checkSilence();
  if (!ready()) return;
  const std::uint64_t epoch = connection_->sessionEpoch();
  std::lock_guard lock(playersMutex_);
  if (epoch != carriedEpoch_) {
    // The session is connected again, and its capabilities have left. On the realtime
    // server its carries were made on they stand (a refresh is what lets that server
    // follow the session to its new address). Another server holds none of them, and
    // neither does any server for a session that was opened again: every player is
    // carried again, and the server left sends no FLEET_RELEASED for them.
    const Assignment now = connection_->assignmentSnapshot();
    const bool restarted = sessionRestarted_.exchange(false, std::memory_order_acq_rel);
    if (restarted || !carriedServerKnown_ || !sameServer(now, carriedServer_)) {
      recarryAllLocked();
      carriesHeld_ = false;
    }
    carriedServer_ = now;
    carriedServerKnown_ = true;
    carriedEpoch_ = epoch;
  }
  driveCarriesLocked(clock_.monotonicMillis());
}

Status FleetSession::release(std::int64_t playerGameTokenId) {
  // While a refreshed token's carry waits the server may hold the player under either id.
  std::int64_t key = playerGameTokenId, previous = 0;
  {
    std::lock_guard lock(playersMutex_);
    const auto it = rowOfLocked(playerGameTokenId);
    if (it != players_.end()) {
      key = it->first;
      previous = it->second.previousId;
      if (previous != 0) aliases_.erase(previous);
      players_.erase(it);
    }
  }
  if (!ready()) return Errc::NotConnected;
  const Status sent = sendRelease(key);
  if (previous != 0) (void)sendRelease(previous);
  return sent;
}

Status FleetSession::sendRelease(std::int64_t id) {
  std::uint8_t payload[wire::fleet::kReleasePayloadSize];
  const auto n = wire::encodeFleetReleasePayload(id, MutableBytes(payload, sizeof(payload)));
  if (!n.ok()) return n.error();
  const auto sent = connection_->sendLongSpatial(MessageType::FleetRelease,
                                                 controlSend(Bytes(payload, n.value())));
  return sent.ok() ? Status(Errc::Ok) : Status(sent.error());
}

bool FleetSession::ready() const {
  return state() == ConnState::Connected &&
         advertisedEpoch_.load(std::memory_order_acquire) == connection_->sessionEpoch();
}

bool FleetSession::carries(std::int64_t playerGameTokenId) const {
  std::lock_guard lock(playersMutex_);
  const auto it = rowOfLocked(playerGameTokenId);
  return it != players_.end() && it->second.carried;
}

std::optional<FleetPlayer> FleetSession::player(std::int64_t playerGameTokenId) const {
  std::lock_guard lock(playersMutex_);
  const auto it = rowOfLocked(playerGameTokenId);
  if (it == players_.end()) return std::nullopt;
  return FleetPlayer{it->first, it->second.userId, it->second.carried, it->second.attempt};
}

std::vector<FleetPlayer> FleetSession::players() const {
  std::lock_guard lock(playersMutex_);
  std::vector<FleetPlayer> out;
  out.reserve(players_.size());
  for (const auto& [id, player] : players_)
    out.push_back(FleetPlayer{id, player.userId, player.carried, player.attempt});
  return out;
}

std::size_t FleetSession::carriedCount() const {
  std::lock_guard lock(playersMutex_);
  std::size_t n = 0;
  for (const auto& [id, player] : players_) {
    (void)id;
    if (player.carried) ++n;
  }
  return n;
}

// ---------------------------------------------------------------------------
// Sends for a carried player
// ---------------------------------------------------------------------------

Result<std::uint8_t> FleetSession::sendActorUpdate(std::int64_t player, const SpatialSend& p) {
  const SendScope scope(*this, player);
  if (scope.gate != Errc::Ok) return scope.gate;
  return connection_->sendLongSpatial(MessageType::ActorUpdateRequest, p, &scope.tailId);
}

Result<std::uint8_t> FleetSession::sendVoxelUpdate(std::int64_t player,
                                                   const wire::ChunkCoord& chunk,
                                                   const core::ActorUuid& uuid, std::int16_t x,
                                                   std::int16_t y, std::int16_t z,
                                                   std::int16_t voxelType, Bytes voxelState,
                                                   std::uint8_t distance, wire::DecayRate decay) {
  const SendScope scope(*this, player);
  if (scope.gate != Errc::Ok) return scope.gate;
  return connection_->sendVoxelUpdateAs(&scope.tailId, chunk, uuid, x, y, z, voxelType, voxelState,
                                        distance, decay);
}

Result<std::uint8_t> FleetSession::sendAudio(std::int64_t player, const SpatialSend& p) {
  const SendScope scope(*this, player);
  if (scope.gate != Errc::Ok) return scope.gate;
  return connection_->sendLongSpatial(MessageType::ClientAudioPacket, p, &scope.tailId);
}

Result<std::uint8_t> FleetSession::sendVideo(std::int64_t player, const SpatialSend& p) {
  const SendScope scope(*this, player);
  if (scope.gate != Errc::Ok) return scope.gate;
  return connection_->sendLongSpatial(MessageType::ClientVideoPacket, p, &scope.tailId);
}

Result<std::size_t> FleetSession::sendVideoFrame(std::int64_t player,
                                                 const wire::ChunkCoord& chunk,
                                                 const core::ActorUuid& uuid, Bytes frame,
                                                 std::uint16_t frameId, std::uint8_t codec,
                                                 std::uint8_t distance, wire::DecayRate decay) {
  const SendScope scope(*this, player);
  if (scope.gate != Errc::Ok) return scope.gate;
  return connection_->sendVideoFrameAs(&scope.tailId, chunk, uuid, frame, frameId, codec, distance,
                                       decay);
}

Result<std::uint8_t> FleetSession::sendText(std::int64_t player, const SpatialSend& p) {
  const SendScope scope(*this, player);
  if (scope.gate != Errc::Ok) return scope.gate;
  return connection_->sendLongSpatial(MessageType::ClientTextPacket, p, &scope.tailId);
}

Result<std::uint8_t> FleetSession::sendClientEvent(std::int64_t player,
                                                   const wire::ChunkCoord& chunk,
                                                   const core::ActorUuid& uuid,
                                                   std::uint16_t eventType, Bytes state,
                                                   std::uint8_t distance, wire::DecayRate decay) {
  const SendScope scope(*this, player);
  if (scope.gate != Errc::Ok) return scope.gate;
  return connection_->sendClientEventAs(&scope.tailId, chunk, uuid, eventType, state, distance, decay);
}

Result<std::uint8_t> FleetSession::sendGenericSpatial(std::int64_t player, const SpatialSend& p) {
  const SendScope scope(*this, player);
  if (scope.gate != Errc::Ok) return scope.gate;
  return connection_->sendLongSpatial(MessageType::GenericSpatial1, p, &scope.tailId);
}

Result<std::uint8_t> FleetSession::sendSingleActorMessage(std::int64_t player,
                                                          const wire::ChunkCoord& targetChunk,
                                                          const core::ActorUuid& targetUuid,
                                                          Bytes payload) {
  const SendScope scope(*this, player);
  if (scope.gate != Errc::Ok) return scope.gate;
  return connection_->sendSingleActorMessageAs(&scope.tailId, targetChunk, targetUuid, payload);
}

Result<std::uint8_t> FleetSession::sendChannelMessage(std::int64_t player,
                                                      std::int64_t channelId,
                                                      const core::ActorUuid& uuid,
                                                      Bytes payload) {
  const SendScope scope(*this, player);
  if (scope.gate != Errc::Ok) return scope.gate;
  return connection_->sendChannelRequest(MessageType::ChannelMessageRequest, channelId, uuid,
                                         payload, &scope.tailId);
}

Result<std::uint8_t> FleetSession::sendChannelAudio(std::int64_t player, std::int64_t channelId,
                                                    const core::ActorUuid& uuid, Bytes payload) {
  const SendScope scope(*this, player);
  if (scope.gate != Errc::Ok) return scope.gate;
  return connection_->sendChannelRequest(MessageType::ChannelAudioRequest, channelId, uuid,
                                         payload, &scope.tailId);
}

Result<std::uint8_t> FleetSession::sendRangedChannelMessage(std::int64_t player,
                                                            std::int64_t channelId,
                                                            const core::ActorUuid& uuid,
                                                            Bytes payload,
                                                            const wire::ChunkCoord& origin,
                                                            std::uint32_t maxDistance) {
  const SendScope scope(*this, player);
  if (scope.gate != Errc::Ok) return scope.gate;
  return connection_->sendRangedChannelMessageAs(&scope.tailId, channelId, uuid, payload, origin,
                                                 maxDistance);
}

Result<std::uint8_t> FleetSession::sendHeartbeat(std::int64_t player,
                                                 const wire::ChunkCoord& chunk,
                                                 const core::ActorUuid& uuid) {
  const SendScope scope(*this, player);
  if (scope.gate != Errc::Ok) return scope.gate;
  return connection_->sendHeartbeatAs(&scope.tailId, chunk, uuid);
}

Result<std::uint8_t> FleetSession::sendHeartbeat() {
  if (!ready()) return Errc::NotConnected;
  return connection_->sendLongSpatial(MessageType::ClientActorHeartbeat, controlSend(Bytes()));
}

Status FleetSession::setSpeakers(std::int64_t player, SpeakerList voice, SpeakerList video) {
  if (voice.names.size() > wire::kMaxSpeakers || video.names.size() > wire::kMaxSpeakers)
    return Errc::InvalidArgument;
  std::lock_guard lock(playersMutex_);
  const auto it = rowOfLocked(player);
  if (it == players_.end() || !it->second.carried) return Errc::NotCarried;
  Player& entry = it->second;
  const std::int64_t tail = entry.sendId;
  if (voice.mode != wire::SpeakerMode::Keep)
    entry.voiceSpeakers = voice.mode == wire::SpeakerMode::Only
                              ? std::optional<std::vector<core::ActorUuid>>(voice.names)
                              : std::nullopt;
  if (video.mode != wire::SpeakerMode::Keep)
    entry.videoSpeakers = video.mode == wire::SpeakerMode::Only
                              ? std::optional<std::vector<core::ActorUuid>>(video.names)
                              : std::nullopt;
  if (!ready()) return Errc::Ok;  // they follow the session's next capabilities
  // One kind a message, as Connection sends them: 32 names of each would not fit one datagram.
  if (voice.mode != wire::SpeakerMode::Keep) {
    const auto sent = connection_->sendSpeakerLists(voice, SpeakerList::keep(), &tail);
    if (!sent.ok()) return sent.error();
  }
  if (video.mode != wire::SpeakerMode::Keep) {
    const auto sent = connection_->sendSpeakerLists(SpeakerList::keep(), video, &tail);
    if (!sent.ok()) return sent.error();
  }
  return Errc::Ok;
}

void FleetSession::sendSpeakersLocked(std::int64_t id, const Player& player) {
  (void)id;
  const std::int64_t tail = player.sendId;
  if (player.voiceSpeakers)
    (void)connection_->sendSpeakerLists(SpeakerList::only(*player.voiceSpeakers),
                                        SpeakerList::keep(), &tail);
  if (player.videoSpeakers)
    (void)connection_->sendSpeakerLists(SpeakerList::keep(),
                                        SpeakerList::only(*player.videoSpeakers), &tail);
}

void FleetSession::onAdvertised() {
  // This session's capabilities have left: from here its other messages may.
  advertisedEpoch_.store(connection_->sessionEpoch(), std::memory_order_release);
  // The lists live with the session on the server, as the capabilities do. A player being
  // carried again gets theirs when that carry is confirmed.
  std::lock_guard lock(playersMutex_);
  for (const auto& [id, player] : players_) {
    if (player.carried && !player.attempt) sendSpeakersLocked(id, player);
  }
}

// ---------------------------------------------------------------------------
// Receive path (the poll() thread)
// ---------------------------------------------------------------------------

std::size_t FleetSession::poll(std::size_t maxEvents) {
  {
    std::lock_guard lock(handlersMutex_);
    active_ = handlers_;
  }
  const std::size_t dispatched = connection_->poll(maxEvents);
  expireCarries();
  return dispatched;
}

void FleetSession::report(const FleetCarryResult& result, const CarryCallback& done) {
  if (done) done(result);
  if (active_.carryResult) active_.carryResult(result);
}

void FleetSession::onFleetMessage(Bytes message) {
  if (message.empty()) return;
  switch (static_cast<MessageType>(message[0])) {
    case MessageType::FleetCarryResult:
      if (const auto result = wire::parseFleetCarryResult(message); result.ok())
        onCarryResult(result.value());
      break;
    case MessageType::FleetReleased:
      if (const auto released = wire::parseFleetReleased(message); released.ok())
        onReleased(released.value());
      break;
    case MessageType::FleetDelivery:
      if (const auto delivery = wire::parseFleetDelivery(message); delivery.ok())
        onDelivery(delivery.value());
      break;
    case MessageType::CommandSessionReleasedBound:
      // The receive path verified it and told the session at once (onSessionReleased); this
      // is the same frame's turn on the poll() thread, for the handler.
      if (message.size() >= 2 && active_.sessionReleased)
        active_.sessionReleased(FleetSessionReleased{sessionReleaseReasonOf(message[1]), message[1]});
      break;
    default:
      break;
  }
}

void FleetSession::onSessionReleased(std::uint8_t reason) {
  (void)reason;
  sessionReleases_.fetch_add(1, std::memory_order_relaxed);
  // The server answers this session no more, and its carries ended unannounced. Every row
  // waits to be carried again where the game server places the session next; until then
  // nothing is carried, and no FLEET_CARRY leaves for a server that would not answer it.
  carriesLost_.store(true, std::memory_order_release);
  {
    std::lock_guard lock(playersMutex_);
    for (auto& [id, player] : players_) {
      if (player.previousId != 0) dropPreviousLocked(player, id);
      player.carried = false;
      beginAttempt(player);
    }
    carriesHeld_ = true;
    attemptsPending_ = !players_.empty();
  }
  if (endpoint_->asksGameServer()) {
    sessionRestarted_.store(true, std::memory_order_release);
    connection_->requestReassignment();
  } else {
    endpoint_->orderMove();
  }
}

void FleetSession::onSent(std::uint8_t sequence, std::int64_t player) {
  const std::int64_t now = std::max<std::int64_t>(clock_.monotonicMillis(), 1);
  std::lock_guard lock(sentMutex_);
  SentAs& slot = sentBySequence_[sequence];
  if (slot.player != player) slot.otherAtMs = slot.atMs;
  slot.player = player;
  slot.atMs = now;
}

std::int64_t FleetSession::refusedPlayer(std::uint8_t sequence) {
  const std::int64_t now = clock_.monotonicMillis();
  std::lock_guard lock(sentMutex_);
  const SentAs& slot = sentBySequence_[sequence];
  // The last message under this number was a carried player's, it left within the time a
  // refusal can take, and nothing for anyone else left under the number in that time.
  if (slot.player == 0 || slot.atMs == 0 || now - slot.atMs > kFleetRefusalWindowMs) return 0;
  if (slot.otherAtMs != 0 && now - slot.otherAtMs <= kFleetRefusalWindowMs) return 0;
  return slot.player;
}

void FleetSession::onGenericError(const GenericError& error) {
  if (active_.genericError) active_.genericError(error);
  // Only a refusal the server signed names a player: an unsigned frame could carry any
  // sequence number. Neither changes a carry.
  if (!error.verified) return;
  const std::int64_t player = refusedPlayer(error.sequence);
  if (player == 0) return;
  FleetSendRefused refused;
  refused.playerGameTokenId = player;
  refused.sequence = error.sequence;
  refused.code = error.code;
  {
    std::lock_guard lock(playersMutex_);
    const auto it = rowOfLocked(player);
    if (it != players_.end()) refused.userId = it->second.userId;
  }
  sendRefusalsNamed_.fetch_add(1, std::memory_order_relaxed);
  if (active_.sendRefused) active_.sendRefused(refused);
}

void FleetSession::onCarryResult(const wire::FleetCarryResultView& view) {
  FleetCarryResult result;
  result.playerGameTokenId = view.playerGameTokenId;
  result.wireStatus = view.status;
  result.status = carryStatusOf(view.status);
  CarryCallback done;
  std::optional<FleetReleased> ended;    // the old carry ended while a refused replacement waited
  std::int64_t releaseOnServer = 0;      // an id whose carry the server holds for nobody here
  {
    std::lock_guard lock(playersMutex_);
    // A late answer about a token the player's newer one replaced, or is replacing.
    if (aliases_.count(view.playerGameTokenId) != 0 ||
        (retiredLocked(view.playerGameTokenId) && players_.count(view.playerGameTokenId) == 0)) {
      replacedIdFrames_.fetch_add(1, std::memory_order_relaxed);
      return;
    }
    auto it = players_.find(view.playerGameTokenId);
    // Nothing waits: a second answer to a carry that was sent again, or an answer for a
    // player released since.
    if (it == players_.end() || !it->second.attempt) return;
    Player& entry = it->second;
    if (result.status == FleetCarryStatus::ProofRefused && ++entry.refusals < entry.sends) {
      // An answer does not say which carry it answers, and a carry that left has not been
      // answered yet. This status 3 can be for an earlier one that reached the server after
      // a later one was accepted (a replay, while the server carries the player), so it
      // settles nothing. The latest carry's answer is a status 3 for certain only when
      // every carry that left has had one. After the first, one more carry leaves now with
      // a new time and proof; the resends go on, and the deadline ends the carry
      // ProofRefused if nothing else answers.
      if (entry.refusals == 1) {
        entry.nextSendMs = 0;
        if (ready()) driveCarriesLocked(clock_.monotonicMillis());
      }
      return;
    }
    if (result.status == FleetCarryStatus::NoLiveToken &&
        entry.noLiveToken != Player::NoLiveToken::Retried) {
      // A token minted or refreshed a moment ago may not have reached this server yet: the
      // carry is asked once more, a resend interval from now, with a new time and proof.
      // Answers to the carries that left before that one settle nothing.
      if (entry.noLiveToken == Player::NoLiveToken::None) {
        entry.noLiveToken = Player::NoLiveToken::Waiting;
        entry.nextSendMs = clock_.monotonicMillis() + std::max(1, config_.carryResendMs);
        noLiveTokenRetries_.fetch_add(1, std::memory_order_relaxed);
      }
      return;
    }
    done = std::move(entry.done);
    entry.done = nullptr;
    if (!result.carried()) {
      if (entry.previousId == 0) {
        players_.erase(it);
      } else {
        // The refreshed token's carry was refused: the carry under the old id stands, and
        // the player's sends name the old id again.
        const std::int64_t previous = entry.previousId;
        aliases_.erase(previous);
        retireLocked(view.playerGameTokenId);
        auto node = players_.extract(it);
        node.key() = previous;
        it = players_.insert(std::move(node)).position;
        Player& row = it->second;
        row.key = row.previousKey.token;
        wire::scrub(row.previousKey.token.octets, wire::kTokenOctets);
        row.previousId = 0;
        row.sendId = previous;
        row.attempt = false;
        row.nextSendMs = 0;
        row.deadlineMs = 0;
        // Unless the server said meanwhile that it ended: then the player is carried no more.
        if (row.previousEndedSet || !row.carried) {
          if (row.carried) {
            ended = FleetReleased{previous, row.userId, releaseReasonOf(row.previousEnded),
                                  row.previousEnded};
          }
          players_.erase(it);
        } else {
          row.previousEndedSet = false;
        }
      }
    } else {
      result.userId = view.userId;
      if (entry.previousId != 0) {
        // The new carry took effect: the old id names nothing now, and late frames about it
        // are ignored.
        const std::int64_t previous = entry.previousId;
        const bool anotherPlayer = entry.userId != 0 && entry.userId != view.userId;
        dropPreviousLocked(entry, view.playerGameTokenId);
        if (anotherPlayer) {
          // `replaces` named another player. That player's carry still stands on the server
          // under the old id, with nobody here to send for it: it is let go.
          retired_.erase(previous);
          entry.voiceSpeakers.reset();
          entry.videoSpeakers.reset();
          releaseOnServer = previous;
        }
      }
      entry.attempt = false;
      entry.carried = true;
      entry.userId = view.userId;
      entry.nextSendMs = 0;
      entry.deadlineMs = 0;
      // The same user under a token carried earlier: this carry replaced that one on the
      // server, so its entry goes and its speaker lists come along.
      for (auto other = players_.begin(); other != players_.end();) {
        Player& old = other->second;
        if (other->first == view.playerGameTokenId || view.userId == 0 ||
            old.userId != view.userId || old.order >= entry.order) {
          ++other;
          continue;
        }
        if (!entry.voiceSpeakers) entry.voiceSpeakers = std::move(old.voiceSpeakers);
        if (!entry.videoSpeakers) entry.videoSpeakers = std::move(old.videoSpeakers);
        if (old.previousId != 0) aliases_.erase(old.previousId);
        other = players_.erase(other);
      }
      if (ready()) sendSpeakersLocked(view.playerGameTokenId, entry);
    }
  }
  if (releaseOnServer != 0) {
    if (connection_->logger_.enabled(core::LogLevel::Warn)) {
      connection_->logger_.log(core::LogLevel::Warn,
                               "fleet session: carry's `replaces` named game token id " +
                                   std::to_string(releaseOnServer) +
                                   ", which is another player's; that player was released");
    }
    if (ready()) (void)sendRelease(releaseOnServer);
  }
  report(result, done);
  if (ended && active_.released) active_.released(*ended);
}

void FleetSession::onReleased(const wire::FleetReleasedView& view) {
  FleetReleased released;
  released.playerGameTokenId = view.playerGameTokenId;
  released.reason = releaseReasonOf(view.reason);
  released.wireReason = view.reason;
  {
    std::lock_guard lock(playersMutex_);
    if (const auto alias = aliases_.find(view.playerGameTokenId); alias != aliases_.end()) {
      // The carry under the old id ended while the refreshed token's carry waits. The player
      // is not dropped: the new carry's answer decides, and remembers this if it is refused.
      const auto waiting = players_.find(alias->second);
      if (waiting != players_.end()) {
        waiting->second.previousEnded = view.reason;
        waiting->second.previousEndedSet = true;
      }
      replacedIdFrames_.fetch_add(1, std::memory_order_relaxed);
      return;
    }
    const auto it = players_.find(view.playerGameTokenId);
    if (it == players_.end()) {
      // A late word about a token the player's newer one replaced; or a player the table
      // does not hold: the session carries nobody by that id, so there is nothing to end
      // and nothing to report (a frame sent again after its row went lands here). The
      // receive path has counted it.
      if (retiredLocked(view.playerGameTokenId))
        replacedIdFrames_.fetch_add(1, std::memory_order_relaxed);
      return;
    }
    released.userId = it->second.userId;
    // A carry still waiting keeps its row until its own result or deadline; the player
    // is not carried meanwhile.
    if (it->second.attempt) it->second.carried = false;
    else players_.erase(it);
  }
  if (active_.released) active_.released(released);
}

void FleetSession::onDelivery(const wire::FleetDeliveryView& view) {
  // "Only these players": the list is the whole of who may receive the notification.
  std::int64_t ids[wire::fleet::kMaxDeliveryRecipients];
  for (std::size_t i = 0; i < view.count; ++i) ids[i] = view.recipient(i);
  const FleetRecipients recipients(std::span<const std::int64_t>(ids, view.count));

  // The notification is the unsigned form of what a player's own session is sent; the
  // receive path has verified the frame around it and parsed it once already.
  if (view.channel()) {
    const auto ch = wire::parseFleetDeliveredChannel(view.notification);
    if (!ch.ok()) return;
    const ChannelNotification n{ch->channelId, ch->senderUuid, ch->payload, ch->epochMillis,
                                ch->sequence};
    const bool audio = view.notification[0] ==
                       static_cast<std::uint8_t>(MessageType::ChannelAudioNotification);
    if (audio && active_.channelAudio) active_.channelAudio(n, recipients);
    if (!audio && active_.channelMessage) active_.channelMessage(n, recipients);
    return;
  }
  const auto v = wire::parseFleetDeliveredSpatial(view.notification);
  if (!v.ok()) return;
  const SpatialNotification n{v->type,    v->appId,                v->chunk,   v->uuid,
                              v->payload, v->epochMillisOrTokenId, v->sequence};
  detail::dispatchSpatial(active_, n, recipients);
}

void FleetSession::expireCarries() {
  std::vector<std::pair<FleetCarryResult, CarryCallback>> expired;
  std::vector<std::int64_t> releaseOnServer;
  {
    std::lock_guard lock(playersMutex_);
    if (!attemptsPending_) return;
    const std::int64_t now = clock_.monotonicMillis();
    for (auto it = players_.begin(); it != players_.end();) {
      Player& player = it->second;
      if (!player.attempt || player.deadlineMs == 0 || now < player.deadlineMs) {
        ++it;
        continue;
      }
      FleetCarryResult result;
      result.playerGameTokenId = it->first;
      // Out of attempts. A carry whose only answers were status 3 ends as that, one that
      // was asked again after a status 1 and not answered ends as that, and one that
      // nothing answered ends TimedOut.
      if (player.refusals > 0) {
        result.status = FleetCarryStatus::ProofRefused;
        result.wireStatus = static_cast<std::uint8_t>(FleetCarryStatus::ProofRefused);
      } else if (player.noLiveToken != Player::NoLiveToken::None) {
        result.status = FleetCarryStatus::NoLiveToken;
        result.wireStatus = static_cast<std::uint8_t>(FleetCarryStatus::NoLiveToken);
      } else {
        result.status = FleetCarryStatus::TimedOut;
      }
      if (player.previousId != 0) {
        // A refreshed token's carry that nothing answered: the session cannot know which of
        // the two ids the server holds the player under, so it lets both go.
        aliases_.erase(player.previousId);
        retireLocked(player.previousId);
        releaseOnServer.push_back(it->first);
        releaseOnServer.push_back(player.previousId);
      }
      expired.emplace_back(result, std::move(player.done));
      it = players_.erase(it);
    }
  }
  if (!releaseOnServer.empty() && ready()) {
    for (const std::int64_t id : releaseOnServer) (void)sendRelease(id);
  }
  for (const auto& [result, done] : expired) report(result, done);
}

}  // namespace crowdy::replication
