#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <utility>
#include <vector>

#include "crowdy/core/uuid.hpp"
#include "crowdy/wire/codec.hpp"
#include "crowdy/wire/protocol.hpp"

namespace crowdy::replication {

/// App-scoped token material needed for native UDP: the 64-octet token is
/// the HMAC key, the gameTokenId goes in every message tail, and expiry
/// drives proactive refresh.
struct TokenInfo {
  std::string token;               ///< 64-character app-scoped token
  std::int64_t gameTokenId = 0;
  std::int64_t expiresAtEpochMs = 0;  ///< 0 = non-expiring
  /// True when the Game API installed THIS token on the replication server the
  /// client is already connected to (`refreshAppToken(currentServer)` answered
  /// `authorizedServer`, ck-api v1.83.7+). A Buddy silently drops datagrams for a
  /// token it was never told about, so when this is false after a refresh the
  /// connection must re-assign -- which is what every refresh did before 2026-09-06.
  bool authorizedOnCurrentServer = false;
};

/// A replication-server assignment (from the Game API's
/// serverWithLeastClients, which also installs the UDP session server-side).
struct Assignment {
  std::string ip4;
  std::string ip6;
  int clientPort = 0;
};

/// The address a connection opens its socket to for an assignment: the `ip6` when
/// `preferIpv6` (Config::preferIpv6) and the server has one, otherwise the `ip4`, and the
/// `ip6` when the server has no `ip4` (0.95.0; until then a server with an `ip6` alone was
/// refused unless preferIpv6 was set). Empty for a server with neither.
inline const std::string& connectAddress(const Assignment& server, bool preferIpv6) {
  if (preferIpv6 && !server.ip6.empty()) return server.ip6;
  return server.ip4.empty() ? server.ip6 : server.ip4;
}

/// Why a server assignment did not happen (Connection::lastAssignmentFailure(), 0.95.0):
/// the Application API's refusal code and its `retryAfterMs`, which connect()'s one Errc
/// cannot carry. See ApiFailure.
using AssignmentFailure = ApiFailure;

enum class ConnState : std::uint8_t {
  Idle = 0,
  Connecting,    ///< assigning a server / waiting for session-ready
  Connected,
  Reconnecting,  ///< re-assigning after COMMAND_RECONNECT / TOKEN_EXPIRED / watchdog
  Failed,
  Closed,
  /// The server released the session because the player sent no gameplay input for
  /// the app's idle timeout (15 minutes unless the app sets its own). Final: nothing
  /// reassigns until connect() is called, which the game does on the next input.
  Inactive,
};

inline const char* connStateName(ConnState s) {
  switch (s) {
    case ConnState::Idle: return "Idle";
    case ConnState::Connecting: return "Connecting";
    case ConnState::Connected: return "Connected";
    case ConnState::Reconnecting: return "Reconnecting";
    case ConnState::Failed: return "Failed";
    case ConnState::Closed: return "Closed";
    case ConnState::Inactive: return "Inactive";
  }
  return "?";
}

/// A change to one kind of speaker list for Connection::setSpeakers (Buddy v0.49.0).
struct SpeakerList {
  wire::SpeakerMode mode = wire::SpeakerMode::Keep;
  std::vector<core::ActorUuid> names;  ///< SpeakerMode::Only; at most wire::kMaxSpeakers

  /// Leave this kind's list as it is.
  static SpeakerList keep() { return {}; }
  /// Receive this kind from everyone in range again.
  static SpeakerList everyone() { return {wire::SpeakerMode::Everyone, {}}; }
  /// Receive this kind only from these actors (from no one when empty).
  static SpeakerList only(std::vector<core::ActorUuid> actors) {
    return {wire::SpeakerMode::Only, std::move(actors)};
  }
};

/// How many datagrams a connection reads from its socket at a time, and the most it reads
/// in one receive pass (one Connection::pump(), or one turn of the network thread) before
/// it does its other work and reads again. Connection::disconnect() waits for the batch in
/// hand at most, whatever is still arriving.
inline constexpr std::size_t kReceiveBatchDatagrams = 16;
inline constexpr std::size_t kReceivePassDatagrams = 1'024;

/// The least time between two fresh tokens a player's Connection asks its session provider
/// for because of TOKEN_EXPIRED frames that nothing verified (GenericError::verified).
inline constexpr std::int64_t kTokenCheckIntervalMs = 30'000;

struct Config {
  std::int64_t appId = 0;
  TokenInfo token;

  /// Manual pump mode: no SDK threads; you call pump() (network work) and
  /// poll() (callback dispatch) yourself. Default: the SDK owns a net thread
  /// and you only call poll().
  bool manualPump = false;

  /// Prefer the IPv6 assignment address. Default IPv4 (most reachable).
  bool preferIpv6 = false;

  /// Wait after server assignment before the session accepts traffic
  /// (session install is asynchronous server-side).
  int sessionReadyWaitMs = 1500;

  /// Refresh the app token this many ms before expiresAt (0 disables).
  std::int64_t refreshLeadMs = 5 * 60 * 1000;

  /// Verify server HMACs on inbound long-spatial notifications and drop
  /// mismatches (recommended; disable only for benchmarking).
  bool verifyNotifications = true;

  /// Tell the server what this client can read (CLIENT_CAPABILITIES, Buddy
  /// v0.30.0) once the session is ready and every advertiseIntervalMs after,
  /// so downlink bundles arrive with one HMAC instead of one per member. A
  /// server older than v0.30.0 ignores it. The repeat covers a token refresh
  /// or a server-side migration, which reset the server's record silently.
  bool advertiseCapabilities = true;
  std::int64_t advertiseIntervalMs = 15 * 1000;

  /// Ask for this session's own spatial voice and video back
  /// (ClientCapability::kOwnMediaEcho), for a game that confirms capture by
  /// playing back what came round. From Buddy v0.48.0 a speaker is not sent
  /// its own ClientAudioPacket2 or ClientVideoPacket2 otherwise; an older
  /// server always sends it. Rides the capabilities advertisement, so it
  /// needs advertiseCapabilities.
  bool ownMediaEcho = false;

  /// Silent-drop watchdog: if >0 and we have received traffic before, going
  /// this long with sends flowing but nothing received triggers reassignment.
  /// Disabled by default (a lone player legitimately receives nothing).
  std::int64_t watchdogSilenceMs = 0;

  /// An idle release (COMMAND_SESSION_RELEASED, reason Idle) that arrives within this
  /// long of this client's own gameplay input reassigns instead of ending the session,
  /// and counts in Stats::idleReleasesAfterInput. A genuine idle release cannot follow
  /// input the server counted, because the platform's shortest idle timeout is 60 s;
  /// a frame captured from an earlier release and sent again can. Gameplay input is
  /// what the server counts: actor updates whose payload changed, voxel edits, text,
  /// events, generic spatial and single-actor messages, audio, video, and channel
  /// publishes; never heartbeats or capability notices. 0 makes every verified idle
  /// release final.
  std::int64_t idleReleaseGuardMs = 60 * 1000;

  /// A verified legacy COMMAND_RECONNECT (22) reassigns at most once per assignment, and
  /// only this long after the last one it honoured; the window doubles with each honoured
  /// one up to 10 minutes, and goes back to this after 10 minutes without one. A 22 signs
  /// fixed bytes, so one captured earlier verifies again for the game token's life; the
  /// rest are dropped and count in Stats::legacyReconnectsSuppressed. The bound form (44)
  /// is not limited. 0 honours every verified 22, as before 0.83.0. Once a server
  /// acknowledges the bound forms on an assignment (CONTROL_BOUND_ACK, Buddy v0.46.0), every
  /// legacy 22 and 28 on it is dropped (Stats::legacyControlDropped).
  std::int64_t legacyReconnectWindowMs = 30 * 1000;

  /// Capacity (events) of the net->game notification ring.
  std::size_t ringCapacity = 4096;

  /// Kernel receive buffer size hint.
  int socketRecvBufferBytes = 1 << 20;

  /// Kernel send buffer size hint. Raise it for a client that emits many
  /// entities in one frame: the send buffer is what absorbs a burst, and when
  /// it fills, sends return Errc::WouldBlock until it drains.
  int socketSendBufferBytes = 1 << 20;

  /// Pack outbound messages into MESSAGE_BUNDLE datagrams (the server does the
  /// same on the downlink). A send() call appends its signed message to the
  /// pending bundle; the bundle goes on the wire when bundleWindowMs has passed
  /// since it was opened, when the next message would not fit (1232 bytes / 32
  /// members), on flushSends(), on the *AndWait sends, and on disconnect. A
  /// lone message is sent unwrapped, so a client that sends one message per
  /// window puts exactly the bytes it always did on the wire.
  ///
  /// Requires a replication server that accepts client bundles (Buddy
  /// v0.27.0+). Set false to send every message as its own datagram, which is
  /// what the SDK did before 0.37.0.
  bool bundleSends = true;

  /// How long a pending bundle may wait for more messages before it is
  /// flushed. The net thread (or pump()) flushes on expiry; the sending thread
  /// also flushes an expired bundle before appending to it. With manualPump
  /// the effective window is bounded by how often you call pump(): call
  /// flushSends() at the end of a frame if you need the datagram out sooner.
  /// 0 flushes on the very next net-thread pass / pump() with no deliberate
  /// wait, so sends made between two passes still share a datagram. Ignored
  /// when bundleSends is false.
  int bundleWindowMs = 1;
  /// Called when notifications are waiting for poll(): at most once between
  /// two poll() calls, from the thread that queued the first of them (the net
  /// thread, or the pump() caller), and again from poll() itself when a
  /// maxEvents bound left events queued. It exists so an event loop can sleep
  /// until there is something to dispatch instead of polling on a timer:
  /// write to a wake descriptor and return. It must not block, throw, or call
  /// back into this connection.
  std::function<void()> onEventsReady;
};

/// SpatialSend::distance left unset: the send uses its message type's default, 1 chunk for
/// voice (sendAudio) and video (sendVideo) and 8 for the rest, as CrowdyJS and CrowdyPy do.
inline constexpr std::uint8_t kTypeDefaultDistance = 0xFF;

/// Parameters for a spatial send. Payload bytes are copied into the send
/// buffer during the call and need not outlive it.
struct SpatialSend {
  wire::ChunkCoord chunk;
  core::ActorUuid uuid{};
  Bytes payload;
  /// Fan-out radius in chunks, 0 to 8 (the server clamps larger values). Unset, it is the
  /// message type's default (kTypeDefaultDistance); before 0.88.0 it was 8 for every type.
  std::uint8_t distance = kTypeDefaultDistance;
  wire::DecayRate decay = wire::DecayRate::None;
};

/// One dispatched notification. Spans point into the poll buffer and are
/// valid only during the callback — copy anything you keep.
struct SpatialNotification {
  wire::MessageType type;
  std::int64_t appId;
  wire::ChunkCoord chunk;
  const char* uuid;  ///< 32 bytes, not null-terminated
  Bytes payload;
  std::int64_t epochMillis;
  std::uint8_t sequence;

  core::ActorUuid uuidArray() const {
    core::ActorUuid u;
    std::memcpy(u.data(), uuid, wire::kUuidSize);
    return u;
  }
};

struct ChannelNotification {
  std::int64_t channelId;
  const char* senderUuid;  ///< 32 bytes
  Bytes payload;
  std::int64_t epochMillis;
  std::uint8_t sequence;
};

struct GenericError {
  std::uint8_t sequence;
  wire::ErrorCode code;
  /// True when the frame arrived inside a MESSAGE_BUNDLE_SIGNED whose HMAC verified. An
  /// error frame ([3][sequence][code]) carries no HMAC of its own, so one that arrives any
  /// other way is the word of whoever could put a datagram on the session's flow, and the
  /// realtime servers send a player's session every error frame alone. Verified or not, a
  /// frame settles the awaited send whose sequence number it names and reaches this handler.
  /// A FleetSession changes no other state on an unverified one. A Connection reassigns on
  /// a verified TOKEN_EXPIRED; on an unverified one it asks its session provider for a fresh
  /// token (at most once every 30 seconds, and only when Config::refreshLeadMs is not 0) and
  /// does what the provider's answer says (0.95.0; until then it reassigned on either).
  bool verified = false;
  /// The frame answers a keepalive (CLIENT_ACTOR_HEARTBEAT, Connection::sendHeartbeat): the
  /// last message this connection sent under `sequence` was one, within
  /// kKeepaliveAnswerWaitMs (0.95.0). UNAUTHORIZED for a keepalive means the actor is not
  /// present where the keepalive named, and the realtime servers answer every keepalive
  /// there the same way, every few seconds. The connection reports that once, through
  /// Handlers::notPresent; a handler that reads every frame can tell the repeats by this.
  bool keepalive = false;
};

/// How long a keepalive's refusal may take to arrive, in milliseconds. A keepalive that has
/// had no refusal for this long was accepted: the realtime servers answer an accepted
/// keepalive with nothing and a refused one at once. It is longer than the 2 seconds between
/// two keepalives (LocalActorStore's default), so one lost refusal does not read as
/// acceptance.
inline constexpr std::int64_t kKeepaliveAnswerWaitMs = 3'000;

/// A not-present report ends by itself when no keepalive was sent for the actor there for
/// this long: whoever sent them stopped keeping the actor present.
inline constexpr std::int64_t kNotPresentIdleMs = 30'000;

/// An actor's keepalive was answered UNAUTHORIZED: the actor is not present at `chunk`
/// (0.95.0, for realtime servers v0.51.0). From v0.51.0 a keepalive is accepted only where
/// the player's actor update would be, so where the player has no `access` every keepalive
/// is refused: other players there are not told of the actor, and the session is sent
/// nothing of the place. The session stays connected. Keep sending keepalives at the same
/// rate: never faster, and never stop. Once access is granted the next keepalive is
/// accepted within about 6 seconds, with no new sign-in.
///
/// One report stands per actor. It is made once, at the first refused keepalive
/// (Handlers::notPresent), stays readable (Connection::notPresent), and ends once
/// (Handlers::notPresentEnded) when a keepalive or an update there is accepted, when the
/// actor's next keepalive or update names another chunk, or when its keepalives stop.
struct ActorNotPresent {
  core::ActorUuid uuid{};
  /// The place: the chunk the refused keepalive named.
  wire::ChunkCoord chunk;
  /// The sequence number of the first refused keepalive.
  std::uint8_t sequence = 0;
  /// GenericError::verified of that refusal. The realtime servers send a player's session
  /// every refusal alone and unsigned, so on a player's Connection this is false, and the
  /// report changes nothing else in the connection: take it as what to show the player. A
  /// fleet session reports only refusals that verified.
  bool verified = false;
  /// A fleet session only: the carried player whose keepalive it was (the game token id
  /// the keepalive was sent for). 0 on a player's own Connection.
  std::int64_t playerGameTokenId = 0;
};

/// Why a not-present report ended.
enum class NotPresentEnd : std::uint8_t {
  /// A keepalive for the actor there went kKeepaliveAnswerWaitMs without a refusal, or the
  /// actor's own update there came back from the server: the actor is present.
  Accepted = 1,
  /// The actor's next keepalive or update named another chunk. Nothing is known of the new
  /// place yet: a refused keepalive there makes a new report.
  Moved = 2,
  /// No keepalive was sent for the actor there for kNotPresentIdleMs.
  Stopped = 3,
};

inline constexpr const char* notPresentEndName(NotPresentEnd end) noexcept {
  switch (end) {
    case NotPresentEnd::Accepted: return "Accepted";
    case NotPresentEnd::Moved: return "Moved";
    case NotPresentEnd::Stopped: return "Stopped";
  }
  return "Unknown";
}

/// Notification handlers, mirroring CrowdyJS's UdpNotificationHandlers.
/// All callbacks fire on the thread that calls poll().
struct Handlers {
  std::function<void(const SpatialNotification&)> actorUpdate;
  std::function<void(const SpatialNotification&, const wire::VoxelPayloadView&)> voxelUpdate;
  std::function<void(const SpatialNotification&)> audio;
  /// One webcam video FRAGMENT (payload = 6-byte header + frame slice). Feed the
  /// payload to a media::VideoFrameAssembler to get whole frames.
  std::function<void(const SpatialNotification&)> video;
  /// The server stopped considering an actor present (uuid = the actor, chunk =
  /// its last chunk). Emitted once; a later actorUpdate for the uuid is a rejoin.
  /// `reason`: 0 STALE (1 reserved).
  std::function<void(const SpatialNotification&, std::uint8_t reason)> actorLeft;
  /// A chunk near you was replaced as a whole (a hub's or mod's world.set_chunk, Buddy
  /// v0.45.0): read it again. `chunk` is the chunk; `updatedAtMs` its new updatedAt, so a
  /// copy at least that new is current.
  std::function<void(const SpatialNotification&, std::int64_t updatedAtMs)> chunkChanged;
  std::function<void(const SpatialNotification&)> text;
  std::function<void(const SpatialNotification&, const wire::EventPayloadView&)> clientEvent;
  std::function<void(const SpatialNotification&, const wire::EventPayloadView&)> serverEvent;
  std::function<void(const SpatialNotification&)> genericSpatial;
  std::function<void(const SpatialNotification&)> singleActorMessage;
  std::function<void(const ChannelNotification&)> channelMessage;
  /// Channel audio (opcode 36, Buddy v0.37.0) from another member of a channel you
  /// belong to; `payload` is opaque (one voice packet with the SDK voice helpers).
  std::function<void(const ChannelNotification&)> channelAudio;
  std::function<void(const GenericError&)> genericError;
  /// An actor's keepalive was refused UNAUTHORIZED: the actor is not present at that place
  /// (ActorNotPresent, 0.95.0). Once per report, after `genericError` for the same frame;
  /// the refusals that follow every few seconds reach `genericError` only.
  std::function<void(const ActorNotPresent&)> notPresent;
  /// That report ended (NotPresentEnd). Once per report.
  std::function<void(const ActorNotPresent&, NotPresentEnd)> notPresentEnded;
  /// Every spatial notification, before the typed handler.
  std::function<void(const SpatialNotification&)> any;
  std::function<void(ConnState)> status;
};

}  // namespace crowdy::replication
