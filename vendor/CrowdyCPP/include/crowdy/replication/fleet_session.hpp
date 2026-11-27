#pragma once

#include <array>
#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <span>
#include <string>
#include <unordered_map>
#include <vector>

#include "crowdy/replication/connection.hpp"

/// A fleet session: one realtime session an organization's game server opens for itself,
/// carrying the players who are on that server.
///
/// THIS IS FOR AN ORGANIZATION'S OWN SERVERS. A fleet token signs for every player the
/// session carries, so it stays where the organization's org token stays and never reaches a
/// player's device. A game on a player's device uses Connection with that player's own token.
///
/// The game server signs each player in once (`mintManagedPlayerToken`), opens one fleet
/// session (the Application API's `openFleetSession`, which answers the fleet token, its
/// gameTokenId and a realtime server; client.admin().fleetSessions() in
/// crowdy/domains/fleet_sessions.hpp wraps it and builds this class's FleetConfig from the
/// answer), and then:
///
///   FleetSession fleet(config, crypto);
///   fleet.open();
///   fleet.carry({playerToken, playerGameTokenId}, [](const FleetCarryResult& r) { ... });
///   fleet.sendActorUpdate(playerGameTokenId, update);   // once the carry is confirmed
///   fleet.poll();                                       // every tick: handlers fire here
///
/// A player who moves to another of the organization's servers is carried by that server's
/// session; this one hears FleetHandlers::released with FleetReleaseReason::CarriedElsewhere.
/// That release is sent once: it can be lost, or arrive up to about 6 seconds late. The sure
/// sign that this session no longer carries a player is the refusal of its next send for
/// that player (UNAUTHORIZED). Stop sending for a player before you hand them to another
/// server. Around a crossing the new session can receive a few state updates the previous
/// session sent for the player just before: treat state for a player you carry as stale.
///
/// What goes on the wire (realtime servers v0.50.3):
///
///  - For a carried player: the message that player's own session would send, with the
///    tail's game token id the player's and the HMAC keyed with the fleet token. The
///    player's permissions, blocks, age rules, speaker lists, rate budget, idle clock and
///    input log apply to it.
///  - Under the session's own game token id, four messages and no others: its
///    CLIENT_CAPABILITIES (29), which also binds the session to its address, FLEET_CARRY
///    (49), FLEET_RELEASE (50) and its own keepalive (26, which registers no actor). The
///    server answers anything else under that id UNAUTHORIZED, so this class has no way to
///    send it: every other send names a carried player.
///
/// This class makes no Application API call. It takes the token and the server it is given,
/// so a server with its own HTTP client uses it as it is; refreshDue() says when to call
/// `refreshFleetSession`, and applyRefresh() takes the answer.
///
/// Threading is Connection's: by default the session owns one network thread and you call
/// poll() from your game thread; with FleetConfig::transport.manualPump you call pump()
/// yourself. Every send, carry() and release() may be called from any thread and never
/// throws. Every callback and handler fires on the thread that calls poll().
namespace crowdy::replication {

/// A player's app token, as `mintManagedPlayerToken` or `refreshAppToken` answered it.
struct PlayerToken {
  std::string token;  ///< 64 characters
  std::int64_t gameTokenId = 0;
  /// After `refreshAppToken`: the gameTokenId this session carries the same player under
  /// now. 0 (the default) for a player's first carry. With it the player stays carried
  /// throughout and the session switches the player's sends to the new id when the new
  /// FLEET_CARRY leaves (FleetSession::carry). It must name the same player.
  std::int64_t replaces = 0;
};

/// How a carry ended. 0 to 4 are FLEET_CARRY_RESULT's status byte.
enum class FleetCarryStatus : std::uint8_t {
  /// The session carries the player; FleetCarryResult::userId says who they are.
  Carried = 0,
  /// The realtime servers know no live token by that gameTokenId in the session's app: it
  /// expired, the player's sign-in ended, or it is a token of another app. A token made a
  /// moment ago may not have reached the server yet, so the session asks once more before
  /// a carry ends this way.
  NoLiveToken = 1,
  /// The token belongs to another organization's player.
  OtherOrganization = 2,
  /// The carry was refused on its proof: the proof did not hold, its time is more than 30
  /// seconds from the server's (check this machine's clock), or it is a replay (the same
  /// carry sent again after it ended, or one older than the session's last accepted carry
  /// of that player).
  ProofRefused = 3,
  /// There is no room for the carry: the session already carries as many players as it may
  /// (`openFleetSession`'s `maxPlayers`), or the realtime server has no place for it.
  NoRoom = 4,
  /// No answer came before FleetConfig::carryTimeoutMs. Never on the wire.
  TimedOut = 254,
  /// A refusal this build does not know; FleetCarryResult::wireStatus has the byte.
  Refused = 255,
};

inline const char* fleetCarryStatusName(FleetCarryStatus s) {
  switch (s) {
    case FleetCarryStatus::Carried: return "Carried";
    case FleetCarryStatus::NoLiveToken: return "NoLiveToken";
    case FleetCarryStatus::OtherOrganization: return "OtherOrganization";
    case FleetCarryStatus::ProofRefused: return "ProofRefused";
    case FleetCarryStatus::NoRoom: return "NoRoom";
    case FleetCarryStatus::TimedOut: return "TimedOut";
    case FleetCarryStatus::Refused: return "Refused";
  }
  return "?";
}

struct FleetCarryResult {
  std::int64_t playerGameTokenId = 0;
  /// The player's user id when carried; 0 otherwise.
  std::int64_t userId = 0;
  FleetCarryStatus status = FleetCarryStatus::TimedOut;
  /// The status byte the server sent; 255 when none came (TimedOut).
  std::uint8_t wireStatus = 255;

  bool carried() const { return status == FleetCarryStatus::Carried; }
};

/// Why the server ended a carry (FLEET_RELEASED's reason byte): 1, 2 and 5 are the only
/// reasons it sends. When the fleet session itself ends, its carries end with no
/// FLEET_RELEASED.
enum class FleetReleaseReason : std::uint8_t {
  /// Another session carries the player now: they moved to another of your servers.
  CarriedElsewhere = 1,
  /// The player's sign-in ended: it was revoked, or it expired and the player was not
  /// carried again with a live token.
  SignInEnded = 2,
  /// No gameplay input from the player for the app's idle timeout. Voice and video do not
  /// count as input.
  Idle = 5,
  /// A reason byte this build does not know; FleetReleased::wireReason has it. The carry
  /// has ended all the same. Never on the wire.
  Unknown = 255,
};

inline const char* fleetReleaseReasonName(FleetReleaseReason r) {
  switch (r) {
    case FleetReleaseReason::CarriedElsewhere: return "CarriedElsewhere";
    case FleetReleaseReason::SignInEnded: return "SignInEnded";
    case FleetReleaseReason::Idle: return "Idle";
    case FleetReleaseReason::Unknown: return "Unknown";
  }
  return "?";
}

struct FleetReleased {
  std::int64_t playerGameTokenId = 0;
  /// The player's user id when this session knew it; 0 otherwise.
  std::int64_t userId = 0;
  FleetReleaseReason reason = FleetReleaseReason::Unknown;
  /// The reason byte the server sent.
  std::uint8_t wireReason = 0;
};

/// Why the realtime server ended the session (COMMAND_SESSION_RELEASED's reason byte).
enum class FleetSessionReleaseReason : std::uint8_t {
  /// The fleet token is no longer authorized: the session was closed through the
  /// Application API, its org token was revoked, or fleet sessions were switched off.
  Deauthorized = 0,
  /// The server holds a newer session of the same token.
  Stale = 1,
  /// The fleet token expired.
  TokenExpired = 2,
  /// The server is shedding load.
  Overload = 3,
  /// The server is moving the session to another realtime server.
  Move = 4,
  /// A reason byte this build does not know; FleetSessionReleased::wireReason has it. The
  /// session has ended all the same. Never on the wire.
  Unknown = 255,
};

inline const char* fleetSessionReleaseReasonName(FleetSessionReleaseReason r) {
  switch (r) {
    case FleetSessionReleaseReason::Deauthorized: return "Deauthorized";
    case FleetSessionReleaseReason::Stale: return "Stale";
    case FleetSessionReleaseReason::TokenExpired: return "TokenExpired";
    case FleetSessionReleaseReason::Overload: return "Overload";
    case FleetSessionReleaseReason::Move: return "Move";
    case FleetSessionReleaseReason::Unknown: return "Unknown";
  }
  return "?";
}

/// FleetHandlers::sessionReleased.
struct FleetSessionReleased {
  FleetSessionReleaseReason reason = FleetSessionReleaseReason::Unknown;
  /// The reason byte the server sent.
  std::uint8_t wireReason = 0;
};

/// FleetHandlers::sendRefused: a verified refusal of one send for a carried player.
///
/// The session changes no carry on a refusal, because most reasons are not about the carry:
///
///  - TOKEN_EXPIRED (32): the send landed just before the PLAYER's token expired. Refresh
///    the player's token (`refreshAppToken`) and carry the player again with
///    PlayerToken::replaces. Without a new carry FLEET_RELEASED, reason 2, follows.
///  - UNAUTHORIZED (7), one of:
///      the player is no longer carried here (released, idle, sign-in ended, carried by
///        another session; that session's FLEET_RELEASED can be lost or arrive up to about
///        6 seconds late, so this refusal is the sign): carry again if you mean to;
///      the send named the player's previous token id after a refreshed token's carry took
///        effect: carry with PlayerToken::replaces and the session names the new id itself;
///      a permission is missing (access, voice, video, text, direct message, voxel write):
///        fix the grant;
///      the region's permissions were still loading, or the actor lookup did not answer in
///        2 seconds: send again;
///      the actor is another player's: do not send under it;
///      a communication rule refused it (a minor's direct message, a scope switched off):
///        nothing to do;
///      the player's access to the app is suspended or ended: stop serving the player.
///  - APP_PAUSED (33): the app is paused; nothing the player did.
///
/// Not refused at all, so never here: a send past the player's rate budget is dropped in
/// silence.
struct FleetSendRefused {
  /// The gameTokenId the refused message named: the carried player's.
  std::int64_t playerGameTokenId = 0;
  /// The player's user id when the session still holds the player; 0 otherwise.
  std::int64_t userId = 0;
  /// The sequence number the send returned.
  std::uint8_t sequence = 0;
  wire::ErrorCode code = wire::ErrorCode::UnknownError;

  /// The player's own token expired: refresh it and carry again with PlayerToken::replaces.
  bool playerTokenExpired() const { return code == wire::ErrorCode::TokenExpired; }
};

/// One row of the carried-players table.
struct FleetPlayer {
  std::int64_t gameTokenId = 0;
  /// 0 until the first carry is confirmed.
  std::int64_t userId = 0;
  /// The server the session is on confirmed a carry: sends for the player are accepted.
  bool carried = false;
  /// A FLEET_CARRY is waiting for its result (the first carry, the carry again on another
  /// realtime server, or a carry with a refreshed token).
  bool pending = false;
};

/// Who a notification is for. It is valid only during the callback.
///
///  - forSession(): the notification arrived plain. It is for the session as a whole: every
///    one of its carried players in range may receive it, and the game server, which knows
///    where its players are, decides which of them see it. State arrives this way, once
///    however many of the session's players are in range.
///  - otherwise: the notification arrived inside a FLEET_DELIVERY, which lists the only
///    carried players who may receive it (1 to 32, by gameTokenId). Deliver it to those
///    players and no others.
class FleetRecipients {
 public:
  /// A notification for the session as a whole.
  FleetRecipients() = default;
  /// A FLEET_DELIVERY's list.
  explicit FleetRecipients(std::span<const std::int64_t> listed) : players_(listed), listed_(true) {}

  bool forSession() const { return !listed_; }
  /// The listed players' gameTokenIds; empty when forSession().
  std::span<const std::int64_t> players() const { return players_; }

  auto begin() const { return players_.begin(); }
  auto end() const { return players_.end(); }
  std::size_t size() const { return players_.size(); }
  bool empty() const { return players_.empty(); }
  std::int64_t operator[](std::size_t i) const { return players_[i]; }

 private:
  std::span<const std::int64_t> players_;
  bool listed_ = false;
};

/// Connection's Handlers, each with who the notification is for after its own arguments.
///
///  - State (actor updates and departures, voxel edits, chunk changes, server events)
///    arrives plain, once, when at least one carried player may receive it: forSession().
///    State one of the session's own carried players sent is not sent back, unless the
///    session set FLEET_OWN_STATE and another of its carried players is in range; a voxel
///    edit always reaches the session that sent it.
///  - A client's events (clientEvent) and app-defined spatial messages (genericSpatial) are
///    filtered for each receiver by blocks, age rules and scopes. When none of the session's
///    players in range is excluded they arrive plain: forSession(), every carried player in
///    range. When some are excluded they arrive listing only those who may receive them.
///    When all are excluded nothing arrives.
///  - Communications (spatial voice, text and video, direct messages, channel messages and
///    channel audio) always arrive listing the carried players who may receive them, at
///    most 32 a message. A carried sender's own text lists it; its own voice and video only
///    with the own-media echo.
///
/// All callbacks fire on the thread that calls poll().
struct FleetHandlers {
  std::function<void(const SpatialNotification&, FleetRecipients)> actorUpdate;
  std::function<void(const SpatialNotification&, const wire::VoxelPayloadView&, FleetRecipients)>
      voxelUpdate;
  std::function<void(const SpatialNotification&, FleetRecipients)> audio;
  std::function<void(const SpatialNotification&, FleetRecipients)> video;
  std::function<void(const SpatialNotification&, std::uint8_t reason, FleetRecipients)> actorLeft;
  std::function<void(const SpatialNotification&, std::int64_t updatedAtMs, FleetRecipients)>
      chunkChanged;
  std::function<void(const SpatialNotification&, FleetRecipients)> text;
  std::function<void(const SpatialNotification&, const wire::EventPayloadView&, FleetRecipients)>
      clientEvent;
  std::function<void(const SpatialNotification&, const wire::EventPayloadView&, FleetRecipients)>
      serverEvent;
  std::function<void(const SpatialNotification&, FleetRecipients)> genericSpatial;
  std::function<void(const SpatialNotification&, FleetRecipients)> singleActorMessage;
  std::function<void(const ChannelNotification&, FleetRecipients)> channelMessage;
  std::function<void(const ChannelNotification&, FleetRecipients)> channelAudio;
  /// A refusal of one send, by its sequence number: every error frame the session is sent.
  /// The frame carries a one-byte sequence the whole session shares and names no player;
  /// `sendRefused` below names the player when the session can tell.
  ///
  /// Realtime servers v0.50.3 send a fleet session its refusals inside its signed bundles,
  /// so GenericError::verified is true for them. A lone three-byte frame has no HMAC and
  /// arrives with `verified` false: anyone who can put three bytes on the session's flow
  /// could have sent it, so take it as a hint. Verified or not, an error frame changes
  /// nothing in the session: no carry ends on it, and none makes refreshDue() true. The
  /// signed word that the session itself ended is `sessionReleased`.
  std::function<void(const GenericError&)> genericError;
  /// A VERIFIED refusal of a send for a carried player, when the sequence number it echoes
  /// can only be that player's (FleetSendRefused). After `genericError`, which has fired for
  /// the same frame. The session changes no carry on it: FleetSendRefused says what the
  /// code means and what to do.
  std::function<void(const FleetSendRefused&)> sendRefused;
  /// The realtime server ended the session, in a frame signed with the fleet token
  /// (COMMAND_SESSION_RELEASED, 28, or 45 bound to this connection), for any reason but
  /// idleness: the Application API closed it, its org token was revoked or fleet sessions
  /// were switched off (0), the server lets it go (1, 3, 4), or the fleet token expired (2).
  /// Its carries ended with it, unannounced. When this fires moveOrdered() and refreshDue()
  /// are already true and the session carries nobody: sends return NotCarried. Call
  /// `refreshFleetSession`. NOT_FOUND means the session has ended: open a new one and carry
  /// your players again. An answer goes to applyRefresh(), and the session then carries
  /// every player in its table again. (The idle reason, 5, ends the session as it ends a
  /// player's: ConnState::Inactive, through `status`.)
  std::function<void(const FleetSessionReleased&)> sessionReleased;
  /// Every spatial notification, before the typed handler.
  std::function<void(const SpatialNotification&, FleetRecipients)> any;
  std::function<void(ConnState)> status;
  /// Every carry that ends, after the callback given to carry(): the first carry of a
  /// player, a carry with a refreshed token, and each carry again on another realtime
  /// server.
  std::function<void(const FleetCarryResult&)> carryResult;
  /// The server ended a carry: this session no longer carries the player when this fires.
  /// The server sends it once, so it can be lost, and it can arrive up to about 6 seconds
  /// late: the refusal of the next send for the player is the sure sign (sendRefused). It
  /// does not fire for a carry that a refreshed token's carry replaced
  /// (PlayerToken::replaces). It fires only for a player the session's table held; a FLEET_RELEASED for any other
  /// (one sent again after the player's row went, say) is counted in the statistics and
  /// reports nothing. A FLEET_RELEASED carries nothing that makes it fresh, so a captured
  /// one sent again releases a player the session carries here: when the player is still on
  /// this server, carry them again, and the realtime server answers that carry.
  std::function<void(const FleetReleased&)> released;
};

/// How long after a send a refusal of it can still arrive: the realtime servers hold a send
/// up to 2 seconds before they refuse it. FleetHandlers::sendRefused names a player only
/// when no send for another player used the same sequence number in this time.
inline constexpr std::int64_t kFleetRefusalWindowMs = 3'000;

/// How long a fleet session lets its realtime server say nothing that verifies before it
/// takes the session for gone (FleetConfig::transport.watchdogSilenceMs).
inline constexpr std::int64_t kFleetSilenceTimeoutMs = 45'000;
/// The shortest silence timeout a fleet session keeps; a smaller one that is not 0 is raised.
inline constexpr std::int64_t kFleetMinSilenceTimeoutMs = 3'000;

/// Connection's Config as a fleet session starts from it: the silence watchdog on.
inline Config fleetTransportDefaults() {
  Config transport;
  transport.watchdogSilenceMs = kFleetSilenceTimeoutMs;
  return transport;
}

struct FleetConfig {
  std::int64_t appId = 0;
  /// The fleet token (64 characters), its gameTokenId and when it expires, as
  /// `openFleetSession` answered them. The expiry is kept for token(); the game server
  /// renews with `refreshFleetSession` and calls setToken().
  TokenInfo token;
  /// The realtime server `openFleetSession` answered (`ip4`, `ip6`, `clientPort`).
  Assignment server;

  /// Ask for the state this session's own carried players sent, when another of its
  /// carried players is in range (ClientCapability::kFleetOwnState). Off by default: a game
  /// server simulates its own players. setFleetOwnState() changes it while connected.
  bool fleetOwnState = false;

  /// A FLEET_CARRY with no result is sent again this often, each time with a new time and
  /// proof (the server may drop a carry unanswered, and the same bytes again would be a
  /// replay once the first was accepted and ended).
  int carryResendMs = 250;
  /// A carry with no result this long after it was first sent ends TimedOut.
  int carryTimeoutMs = 5000;
  /// The session sends at most this many FLEET_CARRY a second, first carries and resends
  /// together, after a burst of carryBurst. The server drops carries past 64 a second (after
  /// a burst of the session's `maxPlayers` + 32) unanswered. A carry that waits for its turn
  /// has not started its timeout.
  int carriesPerSecond = 50;
  /// How many FLEET_CARRY may leave at once before the rate applies. domains::fleetConfigOf
  /// raises it to the session's `maxPlayers`, so a session that moves to another realtime
  /// server can carry all its players again at once.
  int carryBurst = 32;

  /// Called when the session needs a realtime server again after open(): the server asked
  /// it to move (COMMAND_RECONNECT), ended it (COMMAND_SESSION_RELEASED with any reason but
  /// idleness), or said nothing for the silence timeout. Call `refreshFleetSession` without
  /// a `currentServer` and return the server it answers, or an error when it answers
  /// NOT_FOUND (the session then fails, and the game server opens a new one). It runs on
  /// the network thread (the pump() caller with manualPump) and may block. Unset,
  /// moveOrdered() says a server is wanted, and setServer() or applyRefresh() gives it one
  /// later: after a move order the session opens again at the server it has meanwhile, and
  /// after a silence, or when the server ended it, it stays as it is.
  std::function<Result<Assignment>()> reassign;

  /// Socket, bundling and pump options, with the meanings Connection gives them. `appId`
  /// and `token` here are ignored (the two above are used). `refreshLeadMs` is ignored: the
  /// game server refreshes its fleet token itself. `advertiseCapabilities` is always on: the
  /// session's CLIENT_CAPABILITIES is what binds it to its address.
  ///
  /// `watchdogSilenceMs` is the session's silence timeout, and here it is ON BY DEFAULT:
  /// 45,000 (kFleetSilenceTimeoutMs), where a player's Connection has 0. The realtime
  /// servers tell a session that it ended once (FleetHandlers::sessionReleased), and that
  /// one datagram can be lost; a session that was never authorized on its server is sent
  /// nothing at all. Silence is the sign that is left. When nothing the server signed has
  /// arrived for this long, moveOrdered()
  /// and refreshDue() turn true: call `refreshFleetSession`, which answers NOT_FOUND for a
  /// session that has ended. The session keeps its socket meanwhile, so one whose server
  /// was only out of reach goes on when it is heard again. When the refresh answers a
  /// server, the session carries every player again there, the same server included: a
  /// server that was silent may hold no carry. 0 switches the timeout off; a smaller value
  /// than 3,000 is raised to 3,000.
  ///
  /// A live session with nobody in range hears only the acknowledgements of its
  /// capabilities, so `advertiseIntervalMs` (15,000) is held to at most a third of the
  /// silence timeout: the acknowledgements of two advertisements in a row can be lost
  /// before the timeout passes.
  Config transport = fleetTransportDefaults();
};

class FleetSession {
 public:
  /// Connection's statistics, and the session's own.
  struct Stats : Connection::Stats {
    /// Carries answered "no live token" (status 1) that were asked once more before they
    /// settled.
    std::uint64_t noLiveTokenRetries = 0;
    /// FLEET_CARRY_RESULT and FLEET_RELEASED frames that named a gameTokenId the session has
    /// replaced with the same player's newer one (PlayerToken::replaces), or is replacing.
    /// They are late words about the old token and change nothing.
    std::uint64_t replacedIdFrames = 0;
    /// Verified COMMAND_SESSION_RELEASED frames with a reason other than idleness: each
    /// made a refresh due (FleetHandlers::sessionReleased).
    std::uint64_t sessionReleases = 0;
    /// Verified refusals FleetHandlers::sendRefused could name a player for.
    std::uint64_t sendRefusalsNamed = 0;
  };
  using CarryCallback = std::function<void(const FleetCarryResult&)>;

  FleetSession(FleetConfig config, const core::ICrypto& crypto,
               const core::IClock& clock = core::systemClock(),
               const core::ILogger& logger = core::defaultLogger());
  ~FleetSession();

  FleetSession(const FleetSession&) = delete;
  FleetSession& operator=(const FleetSession&) = delete;

  /// Open the socket to FleetConfig::server. After it returns the state is Connecting until
  /// the session-ready wait elapses, then Connected (the status handler fires from poll()).
  /// Once connected the session sends its CLIENT_CAPABILITIES, which binds it to its
  /// address, and then a FLEET_CARRY for every player in its table: a session that is
  /// opened again holds no carry on the server. InvalidArgument for a token that is not 64
  /// characters.
  Status open();
  /// Stop the network thread and close the socket; state -> Closed. The carried table is
  /// emptied and a carry still waiting reports nothing. This does not end the session on
  /// the platform: that is the Application API's `closeFleetSession`.
  void close();

  ConnState state() const { return connection_->state(); }

  /// Replace the handler set (call before open() or between polls).
  void setHandlers(FleetHandlers handlers);

  /// Take a refreshed fleet token (`refreshFleetSession`: the same token and id, a new
  /// expiry). Nothing is sent and the carries stand. A different token or id is taken too:
  /// that is another session to the server, which holds no carry of it, so the session
  /// sends its CLIENT_CAPABILITIES and carries every player again. InvalidArgument for a
  /// token that is not 64 characters.
  Status setToken(const TokenInfo& token);
  TokenInfo token() const;

  /// Move to another realtime server (`refreshFleetSession` answered a different one). The
  /// new server holds no carry. The session reconnects there, sends its
  /// CLIENT_CAPABILITIES, then a new FLEET_CARRY for every player in its table, paced
  /// (FleetConfig::carriesPerSecond), and counts each as carried again only when its result
  /// arrives: until then sends for the player return NotCarried. The server it left sends no
  /// FLEET_RELEASED for them. Naming the server the session is on does nothing.
  /// InvalidArgument without an address or a port.
  Status setServer(const Assignment& server);
  /// The server the session is on, or will open at.
  Assignment server() const;
  /// The app the session is in (FleetConfig::appId).
  std::int64_t appId() const { return config_.appId; }

  /// Take a `refreshFleetSession` answer in one call: the new expiry, and the answer's
  /// server.
  ///
  ///  - The server the session is on: the carries stand. The session sends its
  ///    CLIENT_CAPABILITIES again (the refresh authorized the token again, and this is what
  ///    lets the server follow the session to a new address) and no FLEET_CARRY.
  ///  - Another server: as setServer(). Every player is carried again there.
  ///
  /// It answers a move order (moveOrdered() is false afterwards). InvalidArgument, with
  /// nothing changed, for a token that is not 64 characters or a server without an address
  /// or a port. domains::applyFleetRefresh() calls this with the Application API's answer.
  Status applyRefresh(const TokenInfo& token, const Assignment& server);

  /// True when the session needs a server from the game server and no FleetConfig::reassign
  /// answered: the realtime server told it to move (COMMAND_RECONNECT, 22 or 44, signed
  /// with the fleet token), ended it (COMMAND_SESSION_RELEASED, 28 or 45, with any reason
  /// but idleness; FleetHandlers::sessionReleased), or said nothing that verifies for the
  /// silence timeout. Call `refreshFleetSession` without a `currentServer` and apply the
  /// answer; setServer and applyRefresh clear it. No error frame sets it, TOKEN_EXPIRED
  /// included: that one is about a carried player's token.
  bool moveOrdered() const;
  /// True when it is time to call `refreshFleetSession`: a move was ordered (moveOrdered()),
  /// or less than half of the token's lifetime is left, counted from when this session was
  /// given the token to its `expiresAtEpochMs` (12 hours into a 24-hour token). False for a
  /// token with no expiry. The library makes no call itself; ask this from your own loop.
  bool refreshDue() const;

  /// Start or stop asking for the state this session's own carried players sent
  /// (ClientCapability::kFleetOwnState). Advertised at once when connected and it changed;
  /// the starting value is FleetConfig::fleetOwnState. Any thread.
  void setFleetOwnState(bool on);
  bool fleetOwnState() const noexcept;
  /// Connection::setOwnMediaEcho for this session; the starting value is
  /// FleetConfig::transport.ownMediaEcho.
  void setOwnMediaEcho(bool on) { connection_->setOwnMediaEcho(on); }

  // ----- Carrying -------------------------------------------------------------

  /// Start carrying a player. Sends FLEET_CARRY now when connected (once connected
  /// otherwise) and again every FleetConfig::carryResendMs, each time with a new time and
  /// proof, until FLEET_CARRY_RESULT arrives or FleetConfig::carryTimeoutMs passes: the
  /// server may leave a carry unanswered. All the session's carries together leave at most
  /// FleetConfig::carriesPerSecond a second after a burst of carryBurst, a carry that never
  /// left before one that is being sent again. `done` then fires once from poll(), followed
  /// by FleetHandlers::carryResult. Sends for the player are accepted from the moment the
  /// result is Carried.
  ///
  /// A result does not say which send it answers. A status 3 (ProofRefused) settles the
  /// carry when every FLEET_CARRY sent for it has been answered status 3, the carry's only
  /// send included: then the latest one's answer is a refusal for certain. While a send is
  /// still unanswered, a status 3 may answer an earlier send that reached the server after
  /// a later one was accepted (a replay), so it settles nothing: after the first, the
  /// session sends once more at once, with a new time and proof, and goes on resending.
  /// The carry then ends as whatever else answers it, or ProofRefused when the timeout
  /// passes.
  ///
  /// A status 1 (NoLiveToken) is asked about once more: a token minted or refreshed a
  /// moment ago may not have reached the realtime server yet. The session sends the carry
  /// again FleetConfig::carryResendMs later, with a new time and proof, and a status 1 that
  /// arrives after that settles the carry (Stats::noLiveTokenRetries counts the retries).
  ///
  /// AFTER `refreshAppToken` FOR A CARRIED PLAYER, call it with the new token and
  /// `replaces` set to the gameTokenId the player is carried under:
  ///
  ///   fleet.carry({newToken, newGameTokenId, oldGameTokenId}, done);
  ///
  /// The player stays carried throughout, with their speaker lists: the carry made with the
  /// old token stands until the new one takes effect, and FleetHandlers::released does not
  /// fire. From the moment the new FLEET_CARRY leaves, every send for the player names the
  /// new id on the wire, whichever of the two ids you pass to the send; the realtime server
  /// holds those sends behind the carry. (A send that named the old id after the new carry
  /// took effect would be refused.) When the result is Carried, the old id names nothing in
  /// this session: sends for it return NotCarried, and a late FLEET_RELEASED or
  /// FLEET_CARRY_RESULT about it is ignored and counted (Stats::replacedIdFrames). When the
  /// new carry is refused, the player stays carried under the old id and its sends name the
  /// old id again. When it times out, the session cannot know which id the server holds: it
  /// sends FLEET_RELEASE for both and the player is carried no more.
  ///
  /// Without `replaces` the session learns that two tokens are one player only from the
  /// new carry's result: until then sends for the new id return NotCarried and sends for
  /// the old one name the old id. When that carry is confirmed it replaces the entry the
  /// same user had under the older token, whose gameTokenId this session then refuses.
  ///
  /// A carry that is refused or times out leaves the player not carried under that
  /// gameTokenId. A carry repeated before its result reports once, to the newer callback.
  ///
  /// InvalidArgument for a token that is not 64 characters or a gameTokenId that is the
  /// session's own; NotConnected before open() and after close().
  Status carry(const PlayerToken& player, CarryCallback done = {});

  /// The player left the game: sends FLEET_RELEASE and takes them out of the carried table
  /// at once (for both ids while a refreshed token's carry waits). Their actors leave as when a session ends. A carry still waiting for the
  /// player reports nothing. The message is sent whether or not the table had the player.
  /// It has no answer and is safe to repeat; one that is lost leaves the carry until the
  /// player idles out or is carried elsewhere. NotConnected, with the player out of the
  /// table and nothing sent, while the session is not connected.
  Status release(std::int64_t playerGameTokenId);

  /// True when the server confirmed a carry of this gameTokenId and nothing ended it.
  /// While a refreshed token's carry waits (PlayerToken::replaces), true for both ids.
  bool carries(std::int64_t playerGameTokenId) const;
  std::optional<FleetPlayer> player(std::int64_t playerGameTokenId) const;
  /// Every row of the table: carried players and carries still waiting.
  std::vector<FleetPlayer> players() const;
  /// How many players the server confirmed and nothing ended.
  std::size_t carriedCount() const;

  // ----- Sends for a carried player -------------------------------------------
  //
  // Connection's sends with the carried player first: the tail carries that player's
  // gameTokenId. Each returns Errc::NotCarried, with nothing sent, for a gameTokenId this
  // session does not carry now, and Errc::NotConnected while the session is not connected
  // (its CLIENT_CAPABILITIES leaves first).

  Result<std::uint8_t> sendActorUpdate(std::int64_t player, const SpatialSend& p);
  Result<std::uint8_t> sendVoxelUpdate(std::int64_t player, const wire::ChunkCoord& chunk,
                                       const core::ActorUuid& uuid, std::int16_t x,
                                       std::int16_t y, std::int16_t z, std::int16_t voxelType,
                                       Bytes voxelState, std::uint8_t distance = 8,
                                       wire::DecayRate decay = wire::DecayRate::None);
  Result<std::uint8_t> sendAudio(std::int64_t player, const SpatialSend& p);
  Result<std::uint8_t> sendVideo(std::int64_t player, const SpatialSend& p);
  Result<std::size_t> sendVideoFrame(std::int64_t player, const wire::ChunkCoord& chunk,
                                     const core::ActorUuid& uuid, Bytes frame,
                                     std::uint16_t frameId, std::uint8_t codec = 0,
                                     std::uint8_t distance = 1,
                                     wire::DecayRate decay = wire::DecayRate::None);
  Result<std::uint8_t> sendText(std::int64_t player, const SpatialSend& p);
  Result<std::uint8_t> sendClientEvent(std::int64_t player, const wire::ChunkCoord& chunk,
                                       const core::ActorUuid& uuid, std::uint16_t eventType,
                                       Bytes state, std::uint8_t distance = 8,
                                       wire::DecayRate decay = wire::DecayRate::None);
  Result<std::uint8_t> sendGenericSpatial(std::int64_t player, const SpatialSend& p);
  Result<std::uint8_t> sendSingleActorMessage(std::int64_t player,
                                              const wire::ChunkCoord& targetChunk,
                                              const core::ActorUuid& targetUuid, Bytes payload);
  Result<std::uint8_t> sendChannelMessage(std::int64_t player, std::int64_t channelId,
                                          const core::ActorUuid& uuid, Bytes payload);
  Result<std::uint8_t> sendChannelAudio(std::int64_t player, std::int64_t channelId,
                                        const core::ActorUuid& uuid, Bytes payload);
  Result<std::uint8_t> sendRangedChannelMessage(std::int64_t player, std::int64_t channelId,
                                                const core::ActorUuid& uuid, Bytes payload,
                                                const wire::ChunkCoord& origin,
                                                std::uint32_t maxDistance);
  /// Idle keep-alive for one of the player's actors. Send every 2 seconds or so while the
  /// actor is idle, so its presence never lapses.
  Result<std::uint8_t> sendHeartbeat(std::int64_t player, const wire::ChunkCoord& chunk,
                                     const core::ActorUuid& uuid);
  /// The speakers whose spatial voice and video the player receives (Connection::setSpeakers).
  /// Each carried player has their own lists. A changed kind is sent at once when connected,
  /// and every list again with each capabilities advertisement and after the player is
  /// carried again. InvalidArgument over wire::kMaxSpeakers names.
  Status setSpeakers(std::int64_t player, SpeakerList voice, SpeakerList video);

  // ----- The session's own sends ----------------------------------------------

  // The four messages the server takes under the session's own gameTokenId are these two,
  // carry() and release().

  /// A keep-alive of the session itself (26): no actor, chunk (0,0,0), the fleet session's
  /// own gameTokenId. It registers no actor. The capabilities advertisement already goes out
  /// every FleetConfig::transport.advertiseIntervalMs; this is for a server that wants one
  /// sooner. NotConnected while the session is not connected.
  Result<std::uint8_t> sendHeartbeat();
  /// CLIENT_CAPABILITIES (29) now, under the session's own gameTokenId; never under a
  /// player's, which the server refuses. Sent automatically when connected and every
  /// advertiseIntervalMs.
  Result<std::uint8_t> sendCapabilities() { return connection_->sendCapabilities(); }
  /// Connection::flushSends: put the pending outbound bundle on the wire now.
  Status flushSends() { return connection_->flushSends(); }

  // ----- Receive-side ----------------------------------------------------------

  /// Drain pending notifications and dispatch them to the handlers on the calling thread,
  /// then report the carries whose deadline passed. Returns the number of events dispatched.
  std::size_t poll(std::size_t maxEvents = SIZE_MAX);
  /// Manual-pump mode only: receive datagrams, resend waiting carries and run lifecycle
  /// housekeeping without blocking longer than timeoutMs. Returns the datagrams processed.
  std::size_t pump(int timeoutMs = 0) { return connection_->pump(timeoutMs); }

  /// Connection's counters for this session's socket. messagesSentByType[49] and [50] count
  /// carries and releases; messagesReceivedByType[51], [52] and [53] count their answers.
  Stats stats() const;
  /// How many times this session has started: open() and every reconnection count one.
  std::uint64_t sessionEpoch() const { return connection_->sessionEpoch(); }

 private:
  class Endpoint;

  struct Player {
    /// The player's key, for the proof of a resend or a carry again. Zeroed when the row
    /// goes (a release, a refusal, close(), the session's destruction).
    wire::ScrubbedToken64 key;
    std::int64_t userId = 0;
    bool carried = false;
    bool attempt = false;         ///< a FLEET_CARRY waits for its result
    std::int64_t nextSendMs = 0;  ///< monotonic; 0: send at the next pass
    std::int64_t deadlineMs = 0;  ///< monotonic; 0 until the attempt's first send
    std::int64_t lastCarryMs = 0; ///< the time in the last FLEET_CARRY; the next is later
    int sends = 0;                ///< FLEET_CARRYs that left in this attempt
    /// Status 3 answers this attempt has had. It ends ProofRefused when they are as many as
    /// its sends (every carry that left was refused), or at its deadline when there is one.
    int refusals = 0;
    /// A status 1 is asked about once more: Waiting while that carry is due, Retried once
    /// it left. Only a status 1 that comes after it settles the carry.
    enum class NoLiveToken : std::uint8_t { None, Waiting, Retried } noLiveToken = NoLiveToken::None;
    std::uint64_t order = 0;      ///< which carry() call made or last renewed the entry
    CarryCallback done;
    /// The gameTokenId this player's sends name on the wire: the row's own, except while a
    /// refreshed token's carry has not left yet, when it is still `previousId`.
    std::int64_t sendId = 0;
    /// While a refreshed token's carry waits (PlayerToken::replaces): the id and the key the
    /// player is carried under on the server, which a refused carry goes back to; and the
    /// reason of a FLEET_RELEASED for that id that arrived meanwhile (0: none).
    std::int64_t previousId = 0;
    wire::ScrubbedToken64 previousKey;
    std::uint8_t previousEnded = 0;
    bool previousEndedSet = false;
    std::optional<std::vector<core::ActorUuid>> voiceSpeakers;
    std::optional<std::vector<core::ActorUuid>> videoSpeakers;
  };

  /// Connected, with this session's CLIENT_CAPABILITIES on its way: nothing else leaves
  /// before the message that binds the session to its address.
  bool ready() const;
  /// The silence timeout (network thread, every housekeeping pass): nothing the server
  /// signed for FleetConfig::transport.watchdogSilenceMs makes a refresh due, once.
  void checkSilence();
  /// setServer(), saying whether it sent the capabilities itself.
  Status place(const Assignment& server, bool& advertised);
  /// One send for a carried player, from the check that it may leave to its transmission.
  /// `gate` is NotCarried for a player the session does not carry, NotConnected while it
  /// is not ready, and Ok when the message may leave under `tailId`, the gameTokenId the
  /// player's sends name now. While one is open a refreshed token's FLEET_CARRY waits for
  /// it, so no message names the old id after that carry has left.
  class SendScope;
  using Table = std::unordered_map<std::int64_t, Player>;
  /// The row a gameTokenId names: its own, or the one a waiting replacement moved it to.
  Table::iterator rowOfLocked(std::int64_t id);
  Table::const_iterator rowOfLocked(std::int64_t id) const;
  /// carry() with PlayerToken::replaces: the row moves to the refreshed token's id.
  Table::iterator supersedeLocked(Table::iterator row, const PlayerToken& player);
  /// `id` was replaced by the same player's newer token: late frames naming it are ignored.
  void retireLocked(std::int64_t id);
  bool retiredLocked(std::int64_t id) const;
  /// The row stops being a replacement: it is carried, or to be carried, under its own id.
  void dropPreviousLocked(Player& player, std::int64_t id);
  /// A new attempt for the row: nothing sent yet, no refusal seen.
  static void beginAttempt(Player& player);
  void sendCarryLocked(std::int64_t id, Player& player, std::int64_t nowMono);
  /// One FLEET_CARRY's worth of the session's rate, when there is one. playersMutex_ held.
  bool takeCarryTurnLocked(std::int64_t nowMono);
  /// Send the FLEET_CARRYs that are due, as the rate allows. playersMutex_ held.
  void driveCarriesLocked(std::int64_t nowMono);
  void sendSpeakersLocked(std::int64_t id, const Player& player);
  /// The server holds no carry of this session: every row is to be carried again.
  void recarryAllLocked();
  /// setToken's work; true when the token or its id is another one.
  bool takeToken(const TokenInfo& token);
  void onStatus(ConnState state);
  void onTick();
  void onAdvertised();
  void onFleetMessage(Bytes message);
  void onCarryResult(const wire::FleetCarryResultView& result);
  void onReleased(const wire::FleetReleasedView& released);
  void onDelivery(const wire::FleetDeliveryView& delivery);
  /// A verified COMMAND_SESSION_RELEASED that is not the idle one. Network thread.
  void onSessionReleased(std::uint8_t reason);
  /// A message left under `sequence` for the carried player `player` (0: the session's own).
  void onSent(std::uint8_t sequence, std::int64_t player);
  /// The carried player a verified refusal's sequence number can only be, or 0.
  std::int64_t refusedPlayer(std::uint8_t sequence);
  void onGenericError(const GenericError& error);
  /// FLEET_RELEASE for `id`, under the session's own id.
  Status sendRelease(std::int64_t id);
  void expireCarries();
  void report(const FleetCarryResult& result, const CarryCallback& done);

  FleetConfig config_;
  const core::ICrypto& crypto_;
  const core::IClock& clock_;
  std::shared_ptr<Endpoint> endpoint_;

  mutable std::mutex handlersMutex_;
  FleetHandlers handlers_;
  /// The handler set of the poll() in progress (poll thread only).
  FleetHandlers active_;

  mutable std::mutex playersMutex_;
  std::unordered_map<std::int64_t, Player> players_;
  std::uint64_t carryOrder_ = 0;
  /// False when no row waits for a carry result, so a pass need not walk the table.
  bool attemptsPending_ = false;
  /// The session epoch the table was last reconciled with.
  std::uint64_t carriedEpoch_ = 0;
  /// The realtime server the table's carries were made on. A new epoch on the same server
  /// leaves them standing; on another, every player is carried again.
  Assignment carriedServer_;
  bool carriedServerKnown_ = false;
  /// The FLEET_CARRY rate: turns left, and when they were last counted (monotonic ms).
  double carryTurns_ = 0;
  std::int64_t carryTurnsAtMs_ = 0;
  bool carryTurnsPrimed_ = false;
  std::vector<std::pair<std::int64_t, std::int64_t>> dueScratch_;  ///< (nextSendMs, id)
  /// A player's previous gameTokenId, while the carry that replaces it waits: id -> row key.
  std::unordered_map<std::int64_t, std::int64_t> aliases_;
  /// Ids a newer token of the same player replaced, and when (monotonic ms): a late
  /// FLEET_CARRY_RESULT or FLEET_RELEASED about one is ignored. Kept for a minute.
  std::unordered_map<std::int64_t, std::int64_t> retired_;
  /// The realtime server ended the session: no FLEET_CARRY leaves until the game server
  /// places the session again (setServer, applyRefresh, another token, open()).
  bool carriesHeld_ = false;
  /// Sends for carried players between their check and their transmission (SendScope).
  std::atomic<int> sendsInFlight_{0};
  /// For each sequence number: the carried player the last message under it was for (0:
  /// the session's own), when it left, and when a message for anyone else last left under
  /// it (monotonic ms; 0: never). What FleetHandlers::sendRefused names a player from.
  struct SentAs {
    std::int64_t player = 0;
    std::int64_t atMs = 0;
    std::int64_t otherAtMs = 0;
  };
  std::mutex sentMutex_;
  std::array<SentAs, 256> sentBySequence_{};
  std::atomic<std::uint64_t> noLiveTokenRetries_{0};
  std::atomic<std::uint64_t> replacedIdFrames_{0};
  std::atomic<std::uint64_t> sessionReleases_{0};
  std::atomic<std::uint64_t> sendRefusalsNamed_{0};
  /// open() started a session anew (after Idle, Closed, Failed or Inactive): the server
  /// holds none of the table's carries, whichever server it is.
  std::atomic<bool> sessionRestarted_{false};
  /// The silence timeout passed, or the server ended the session: the server the game
  /// server next places the session on may hold none of its carries, the one it is on
  /// included.
  std::atomic<bool> carriesLost_{false};
  /// The session epoch whose capabilities have been advertised (0: none yet).
  std::atomic<std::uint64_t> advertisedEpoch_{0};
  /// Epoch millis when the session was last given its token: where refreshDue() counts the
  /// token's lifetime from.
  std::atomic<std::int64_t> tokenSinceEpochMs_{0};

  /// Declared last: destroyed first, so its network thread has stopped before anything
  /// its hooks touch goes.
  std::unique_ptr<Connection> connection_;
};

}  // namespace crowdy::replication
