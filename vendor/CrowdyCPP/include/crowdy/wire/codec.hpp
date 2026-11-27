#pragma once

#include <cstring>
#include <optional>

#include "crowdy/core/bytes.hpp"
#include "crowdy/core/crypto.hpp"
#include "crowdy/core/result.hpp"
#include "crowdy/core/uuid.hpp"
#include "crowdy/wire/protocol.hpp"

/// Zero-copy encoder/decoder for the public Replication API wire protocol
/// (https://docs.crowdedkingdoms.com/replication-api/wire-formats).
///
/// Encoding writes directly into a caller-provided buffer; decoding returns
/// views (spans/pointers) into the datagram buffer — nothing is copied and
/// nothing allocates. Views are only valid while the underlying buffer is.
namespace crowdy::wire {

struct ChunkCoord {
  std::int64_t x = 0;
  std::int64_t y = 0;
  std::int64_t z = 0;
  friend bool operator==(const ChunkCoord&, const ChunkCoord&) = default;
};

// ---------------------------------------------------------------------------
// HMAC (https://docs.crowdedkingdoms.com/replication-api/hmac)
// tag = HMAC-SHA256(key = token64, message = prefix || token64)
// where prefix is every byte before the 32-byte HMAC field.
// ---------------------------------------------------------------------------

/// The 64-octet app-scoped token used as the HMAC key.
struct Token64 {
  std::uint8_t octets[kTokenOctets] = {};

  static std::optional<Token64> fromString(std::string_view s) {
    if (s.size() != kTokenOctets) return std::nullopt;
    Token64 t;
    std::memcpy(t.octets, s.data(), kTokenOctets);
    return t;
  }
  Bytes bytes() const { return Bytes(octets, kTokenOctets); }
};

/// Zero `n` bytes through a volatile pointer, so the compiler may not drop the writes as
/// dead stores. Best effort: it reaches this copy, and no copy a caller or the crypto
/// provider made.
inline void scrub(std::uint8_t* bytes, std::size_t n) noexcept {
  volatile std::uint8_t* p = bytes;
  for (; n > 0; --n) *p++ = 0;
}

/// A Token64 that zeroes its octets when it is destroyed. For a key held only as long as
/// something else lives: FleetSession keeps each carried player's token in one, so the
/// token is cleared when the player's row goes and when the session closes.
struct ScrubbedToken64 {
  Token64 token;

  ScrubbedToken64() = default;
  explicit ScrubbedToken64(const Token64& t) : token(t) {}
  ~ScrubbedToken64() { scrub(token.octets, kTokenOctets); }
  ScrubbedToken64(const ScrubbedToken64&) = delete;
  ScrubbedToken64& operator=(const ScrubbedToken64&) = delete;
  ScrubbedToken64& operator=(const Token64& t) {
    token = t;
    return *this;
  }
  /// Fill from a 64-character token; false, leaving the octets as they are, for any other
  /// length.
  bool assign(std::string_view s) {
    if (s.size() != kTokenOctets) return false;
    std::memcpy(token.octets, s.data(), kTokenOctets);
    return true;
  }
};

/// Compute the spatial HMAC tag over `prefix`. `out` must hold 32 bytes.
///
/// Pass `mac` (from ICrypto::makeHmacSha256 for this token) to sign with a
/// pre-keyed context, which is several times cheaper than the one-shot path and
/// also feeds the two parts straight in rather than concatenating them into a
/// stack buffer. Without it the behaviour is unchanged: same key, same message,
/// same tag.
inline bool spatialHmac(const core::ICrypto& crypto, Bytes prefix, const Token64& token,
                        std::uint8_t* out, const core::IMac* mac = nullptr) {
  if (prefix.size() > kMaxDatagramSize) return false;
  if (mac != nullptr) {
    const Bytes parts[] = {prefix, token.bytes()};
    return mac->compute(parts, 2, out);
  }
  std::uint8_t msg[kMaxDatagramSize + kTokenOctets];
  std::memcpy(msg, prefix.data(), prefix.size());
  std::memcpy(msg + prefix.size(), token.octets, kTokenOctets);
  return crypto.hmacSha256(token.bytes(), Bytes(msg, prefix.size() + kTokenOctets), out);
}

// ---------------------------------------------------------------------------
// Long form spatial messages
// ---------------------------------------------------------------------------

struct LongSpatialParams {
  MessageType type = MessageType::GenericSpatial1;
  std::int64_t appId = 0;
  ChunkCoord chunk;
  std::uint8_t distance = 0;
  DecayRate decay = DecayRate::None;
  core::ActorUuid uuid{};
  Bytes payload;
  std::int64_t gameTokenId = 0;
  std::uint8_t sequence = 0;
};

/// Total encoded size for a signed long-spatial message with this payload.
inline constexpr std::size_t longSpatialSize(std::size_t payloadLen, bool withHmac = true) {
  return kLongSpatialHeaderSize + payloadLen + (withHmac ? kTailWithHmac : kTailNoHmac);
}

/// Encode and sign a long-form spatial message into `out`.
/// Returns the number of bytes written, or an error.
inline Result<std::size_t> encodeLongSpatial(const core::ICrypto& crypto,
                                             const LongSpatialParams& p, const Token64& token,
                                             MutableBytes out, const core::IMac* mac = nullptr) {
  if (!isLongSpatialLayout(static_cast<std::uint8_t>(p.type))) return Errc::InvalidArgument;
  if (p.payload.size() > kMaxLongSpatialPayload) return Errc::InvalidArgument;
  const std::size_t total = longSpatialSize(p.payload.size());
  if (out.size() < total) return Errc::BufferTooSmall;
  const Status cryptoStatus = crypto.availability();
  if (!cryptoStatus.ok()) return cryptoStatus.code;

  std::uint8_t* b = out.data();
  b[offsets::kType] = static_cast<std::uint8_t>(p.type);
  le::writeI64(b + offsets::kAppId, p.appId);
  le::writeI64(b + offsets::kChunkX, p.chunk.x);
  le::writeI64(b + offsets::kChunkY, p.chunk.y);
  le::writeI64(b + offsets::kChunkZ, p.chunk.z);
  b[offsets::kDistance] = p.distance;
  b[offsets::kDecay] = static_cast<std::uint8_t>(p.decay);
  b[offsets::kContainsAuth] = 1;
  std::memcpy(b + offsets::kUuid, p.uuid.data(), kUuidSize);
  if (!p.payload.empty()) std::memcpy(b + offsets::kPayload, p.payload.data(), p.payload.size());

  const std::size_t prefixLen = kLongSpatialHeaderSize + p.payload.size();
  if (!spatialHmac(crypto, Bytes(b, prefixLen), token, b + prefixLen, mac)) return Errc::Malformed;
  le::writeI64(b + prefixLen + kHmacTagSize, p.gameTokenId);
  b[total - 1] = p.sequence;
  return total;
}

/// Parsed view over an inbound long-spatial notification. Payload points into
/// the original datagram buffer.
struct LongSpatialView {
  MessageType type;
  std::int64_t appId;
  ChunkCoord chunk;
  std::uint8_t distance;
  DecayRate decay;
  bool containsAuth;
  const char* uuid;        // 32 bytes, not null-terminated
  Bytes payload;
  std::int64_t epochMillisOrTokenId;  // S->C: epoch millis; C->S: gameTokenId
  std::uint8_t sequence;

  core::ActorUuid uuidArray() const {
    core::ActorUuid u;
    std::memcpy(u.data(), uuid, kUuidSize);
    return u;
  }
};

/// Parse a long-spatial message (no HMAC verification — see
/// verifyLongSpatial). Returns Malformed for short/invalid frames.
inline Result<LongSpatialView> parseLongSpatial(Bytes datagram) {
  if (datagram.size() < kMinLongSpatialNoHmac) return Errc::Malformed;
  const std::uint8_t* b = datagram.data();
  if (!isLongSpatialLayout(b[offsets::kType])) return Errc::Malformed;

  const bool auth = b[offsets::kContainsAuth] != 0;
  const std::size_t tail = auth ? kTailWithHmac : kTailNoHmac;
  if (datagram.size() < kLongSpatialHeaderSize + tail) return Errc::Malformed;

  LongSpatialView v;
  v.type = static_cast<MessageType>(b[offsets::kType]);
  v.appId = le::readI64(b + offsets::kAppId);
  v.chunk = {le::readI64(b + offsets::kChunkX), le::readI64(b + offsets::kChunkY),
             le::readI64(b + offsets::kChunkZ)};
  v.distance = b[offsets::kDistance];
  v.decay = static_cast<DecayRate>(b[offsets::kDecay]);
  v.containsAuth = auth;
  v.uuid = reinterpret_cast<const char*>(b + offsets::kUuid);
  v.payload = datagram.subspan(offsets::kPayload, datagram.size() - kLongSpatialHeaderSize - tail);
  v.epochMillisOrTokenId = le::readI64(b + datagram.size() - kTailNoHmac);
  v.sequence = b[datagram.size() - 1];
  return v;
}

/// Verify the server HMAC on a signed (containsAuth = 1) long-spatial
/// notification, keyed on this client's token. Unsigned messages verify
/// trivially (nothing to check).
inline Status verifyLongSpatial(const core::ICrypto& crypto, Bytes datagram,
                                const Token64& token, const core::IMac* mac = nullptr) {
  if (datagram.size() < kMinLongSpatialNoHmac) return Errc::Malformed;
  if (datagram[offsets::kContainsAuth] == 0) return Errc::Ok;
  const Status cryptoStatus = crypto.availability();
  if (!cryptoStatus.ok()) return cryptoStatus;
  if (datagram.size() < kMinLongSpatialWithHmac) return Errc::Malformed;
  const std::size_t prefixLen = datagram.size() - kTailWithHmac;
  std::uint8_t expected[kHmacTagSize];
  if (!spatialHmac(crypto, datagram.first(prefixLen), token, expected, mac))
    return Errc::Malformed;
  if (!crypto.constantTimeEquals(expected, datagram.data() + prefixLen, kHmacTagSize))
    return Errc::HmacMismatch;
  return Errc::Ok;
}

/// Verify a MESSAGE_BUNDLE_SIGNED (Buddy v0.30.0): the last 32 bytes are
/// HMAC(token, body || token) over everything before them -- the client scheme,
/// at the tail. Members inside carry no HMAC of their own; walk them with
/// forEachMessage after this returns Ok.
inline Status verifySignedBundle(const core::ICrypto& crypto, Bytes datagram,
                                 const Token64& token, const core::IMac* mac = nullptr) {
  if (datagram.size() <= kHmacTagSize + 1 ||
      datagram[0] != static_cast<std::uint8_t>(MessageType::MessageBundleSigned))
    return Errc::Malformed;
  const Status cryptoStatus = crypto.availability();
  if (!cryptoStatus.ok()) return cryptoStatus;
  const std::size_t bodyLen = datagram.size() - kHmacTagSize;
  std::uint8_t expected[kHmacTagSize];
  if (!spatialHmac(crypto, datagram.first(bodyLen), token, expected, mac)) return Errc::Malformed;
  if (!crypto.constantTimeEquals(expected, datagram.data() + bodyLen, kHmacTagSize))
    return Errc::HmacMismatch;
  return Errc::Ok;
}

// ---------------------------------------------------------------------------
// Voxel payload ([2B x][2B y][2B z][2B type][2B stateLen][state...])
// ---------------------------------------------------------------------------

struct VoxelPayloadView {
  std::int16_t x, y, z;
  std::int16_t voxelType;
  Bytes state;
};

inline Result<VoxelPayloadView> parseVoxelPayload(Bytes payload) {
  if (payload.size() < voxel::kFixedSize) return Errc::Malformed;
  const std::uint8_t* b = payload.data();
  VoxelPayloadView v;
  v.x = le::readI16(b + voxel::kXOffset);
  v.y = le::readI16(b + voxel::kYOffset);
  v.z = le::readI16(b + voxel::kZOffset);
  v.voxelType = le::readI16(b + voxel::kTypeOffset);
  const std::uint16_t stateLen = le::readU16(b + voxel::kStateLenOffset);
  if (payload.size() < voxel::kFixedSize + stateLen) return Errc::Malformed;
  v.state = payload.subspan(voxel::kStateOffset, stateLen);
  return v;
}

/// Encode a voxel payload into `out`; returns bytes written.
inline Result<std::size_t> encodeVoxelPayload(std::int16_t x, std::int16_t y, std::int16_t z,
                                              std::int16_t voxelType, Bytes state,
                                              MutableBytes out) {
  const std::size_t total = voxel::kFixedSize + state.size();
  if (out.size() < total) return Errc::BufferTooSmall;
  if (state.size() > 0xffff) return Errc::InvalidArgument;
  std::uint8_t* b = out.data();
  le::writeI16(b + voxel::kXOffset, x);
  le::writeI16(b + voxel::kYOffset, y);
  le::writeI16(b + voxel::kZOffset, z);
  le::writeI16(b + voxel::kTypeOffset, voxelType);
  le::writeU16(b + voxel::kStateLenOffset, static_cast<std::uint16_t>(state.size()));
  if (!state.empty()) std::memcpy(b + voxel::kStateOffset, state.data(), state.size());
  return total;
}

// ---------------------------------------------------------------------------
// Event payload ([2B eventType][state...])
// ---------------------------------------------------------------------------

struct EventPayloadView {
  std::uint16_t eventType;
  Bytes state;
};

inline Result<EventPayloadView> parseEventPayload(Bytes payload) {
  if (payload.size() < kEventTypeSize) return Errc::Malformed;
  return EventPayloadView{le::readU16(payload.data()), payload.subspan(kEventTypeSize)};
}

inline Result<std::size_t> encodeEventPayload(std::uint16_t eventType, Bytes state,
                                              MutableBytes out) {
  const std::size_t total = kEventTypeSize + state.size();
  if (out.size() < total) return Errc::BufferTooSmall;
  le::writeU16(out.data(), eventType);
  if (!state.empty()) std::memcpy(out.data() + kEventTypeSize, state.data(), state.size());
  return total;
}

// ---------------------------------------------------------------------------
// Channel messages
// ---------------------------------------------------------------------------

struct ChannelMessageParams {
  std::int64_t channelId = 0;
  core::ActorUuid uuid{};
  Bytes payload;
  std::int64_t gameTokenId = 0;
  std::uint8_t sequence = 0;
};

inline constexpr std::size_t channelRequestSize(std::size_t payloadLen) {
  return channel::kHeaderSize + payloadLen + channel::kRequestTailSize;
}

/// Encode and sign a channel request of `type` (ChannelMessageRequest or
/// ChannelAudioRequest, which share the layout). The HMAC signs all bytes up to
/// and including containsAuth, concatenated with the token octets.
inline Result<std::size_t> encodeChannelRequest(MessageType type, const core::ICrypto& crypto,
                                                const ChannelMessageParams& p,
                                                const Token64& token, MutableBytes out,
                                                const core::IMac* mac = nullptr) {
  if (type != MessageType::ChannelMessageRequest && type != MessageType::ChannelAudioRequest)
    return Errc::InvalidArgument;
  if (p.payload.size() > channel::kMaxPayload) return Errc::InvalidArgument;
  const std::size_t total = channelRequestSize(p.payload.size());
  if (out.size() < total) return Errc::BufferTooSmall;
  const Status cryptoStatus = crypto.availability();
  if (!cryptoStatus.ok()) return cryptoStatus.code;

  std::uint8_t* b = out.data();
  b[0] = static_cast<std::uint8_t>(type);
  le::writeI64(b + channel::kChannelIdOffset, p.channelId);
  std::memcpy(b + channel::kUuidOffset, p.uuid.data(), kUuidSize);
  le::writeU16(b + channel::kPayloadLenOffset, static_cast<std::uint16_t>(p.payload.size()));
  if (!p.payload.empty())
    std::memcpy(b + channel::kPayloadOffset, p.payload.data(), p.payload.size());

  const std::size_t authOffset = channel::kPayloadOffset + p.payload.size();
  b[authOffset] = 1;  // containsAuth
  const std::size_t prefixLen = authOffset + 1;
  if (!spatialHmac(crypto, Bytes(b, prefixLen), token, b + prefixLen, mac)) return Errc::Malformed;
  le::writeI64(b + prefixLen + kHmacTagSize, p.gameTokenId);
  b[total - 1] = p.sequence;
  return total;
}

/// Encode and sign a CHANNEL_MESSAGE_REQUEST (17).
inline Result<std::size_t> encodeChannelMessage(const core::ICrypto& crypto,
                                                const ChannelMessageParams& p,
                                                const Token64& token, MutableBytes out,
                                                const core::IMac* mac = nullptr) {
  return encodeChannelRequest(MessageType::ChannelMessageRequest, crypto, p, token, out, mac);
}

/// Encode and sign a CHANNEL_AUDIO_REQUEST (35, Buddy v0.37.0): opcode 17's layout and
/// signing with its own type byte. `p.payload` is opaque, typically one voice packet.
inline Result<std::size_t> encodeChannelAudio(const core::ICrypto& crypto,
                                              const ChannelMessageParams& p,
                                              const Token64& token, MutableBytes out,
                                              const core::IMac* mac = nullptr) {
  return encodeChannelRequest(MessageType::ChannelAudioRequest, crypto, p, token, out, mac);
}

struct RangedChannelMessageParams {
  std::int64_t channelId = 0;
  core::ActorUuid uuid{};
  std::int64_t appId = 0;
  ChunkCoord origin;
  std::uint32_t maxDistance = 0;
  Bytes payload;
  std::int64_t gameTokenId = 0;
  std::uint8_t sequence = 0;
};

inline constexpr std::size_t rangedChannelRequestSize(std::size_t payloadLen) {
  return channel_ranged::kHeaderSize + payloadLen + channel::kRequestTailSize;
}

/// Encode and sign a CHANNEL_MESSAGE_RANGED_REQUEST (Buddy v0.35.0). Signed like
/// encodeChannelMessage. InvalidArgument for a payload over channel::kMaxPayload or a
/// maxDistance over channel_ranged::kMaxDistance (the server would refuse either).
inline Result<std::size_t> encodeRangedChannelMessage(const core::ICrypto& crypto,
                                                      const RangedChannelMessageParams& p,
                                                      const Token64& token, MutableBytes out,
                                                      const core::IMac* mac = nullptr) {
  if (p.payload.size() > channel::kMaxPayload) return Errc::InvalidArgument;
  if (p.maxDistance > channel_ranged::kMaxDistance) return Errc::InvalidArgument;
  const std::size_t total = rangedChannelRequestSize(p.payload.size());
  if (out.size() < total) return Errc::BufferTooSmall;
  const Status cryptoStatus = crypto.availability();
  if (!cryptoStatus.ok()) return cryptoStatus.code;

  std::uint8_t* b = out.data();
  b[0] = static_cast<std::uint8_t>(MessageType::ChannelMessageRangedRequest);
  le::writeI64(b + channel::kChannelIdOffset, p.channelId);
  std::memcpy(b + channel::kUuidOffset, p.uuid.data(), kUuidSize);
  le::writeI64(b + channel_ranged::kAppIdOffset, p.appId);
  le::writeI64(b + channel_ranged::kChunkXOffset, p.origin.x);
  le::writeI64(b + channel_ranged::kChunkYOffset, p.origin.y);
  le::writeI64(b + channel_ranged::kChunkZOffset, p.origin.z);
  le::writeU32(b + channel_ranged::kMaxDistanceOffset, p.maxDistance);
  le::writeU16(b + channel_ranged::kPayloadLenOffset, static_cast<std::uint16_t>(p.payload.size()));
  if (!p.payload.empty())
    std::memcpy(b + channel_ranged::kPayloadOffset, p.payload.data(), p.payload.size());

  const std::size_t authOffset = channel_ranged::kPayloadOffset + p.payload.size();
  b[authOffset] = 1;  // containsAuth
  const std::size_t prefixLen = authOffset + 1;
  if (!spatialHmac(crypto, Bytes(b, prefixLen), token, b + prefixLen, mac)) return Errc::Malformed;
  le::writeI64(b + prefixLen + kHmacTagSize, p.gameTokenId);
  b[total - 1] = p.sequence;
  return total;
}

struct ChannelNotificationView {
  std::int64_t channelId;
  const char* senderUuid;  // 32 bytes
  Bytes payload;
  std::int64_t epochMillis;
  std::uint8_t sequence;

  core::ActorUuid uuidArray() const {
    core::ActorUuid u;
    std::memcpy(u.data(), senderUuid, kUuidSize);
    return u;
  }
};

/// Parse a channel notification: ChannelMessageNotification (18) or
/// ChannelAudioNotification (36), which share the layout; read `datagram[0]` for which.
inline Result<ChannelNotificationView> parseChannelNotification(Bytes datagram) {
  if (datagram.size() < channel::kMinNotificationSize) return Errc::Malformed;
  const std::uint8_t* b = datagram.data();
  if (b[0] != static_cast<std::uint8_t>(MessageType::ChannelMessageNotification) &&
      b[0] != static_cast<std::uint8_t>(MessageType::ChannelAudioNotification))
    return Errc::Malformed;
  const std::uint16_t payloadLen = le::readU16(b + channel::kPayloadLenOffset);
  if (datagram.size() <
      channel::kHeaderSize + payloadLen + channel::kNotificationTailSize)
    return Errc::Malformed;
  ChannelNotificationView v;
  v.channelId = le::readI64(b + channel::kChannelIdOffset);
  v.senderUuid = reinterpret_cast<const char*>(b + channel::kUuidOffset);
  v.payload = datagram.subspan(channel::kPayloadOffset, payloadLen);
  const std::size_t tailStart = channel::kPayloadOffset + payloadLen;
  v.epochMillis = le::readI64(b + tailStart);
  v.sequence = b[tailStart + 8];
  return v;
}

// ---------------------------------------------------------------------------
// Error + reconnect frames
// ---------------------------------------------------------------------------

struct GenericErrorView {
  std::uint8_t sequence;
  ErrorCode code;
};

inline Result<GenericErrorView> parseGenericError(Bytes datagram) {
  if (datagram.size() < kGenericErrorSize ||
      datagram[0] != static_cast<std::uint8_t>(MessageType::GenericError))
    return Errc::Malformed;
  return GenericErrorView{datagram[1], static_cast<ErrorCode>(datagram[2])};
}

/// Verify a COMMAND_RECONNECT frame: HMAC-SHA256 over the single type byte, keyed on this
/// client's game token, as the server and the game API's UDP proxy compute it (before 0.81.0
/// this appended the token to the message, so no server's frame ever verified). Unverified
/// commands must be ignored.
inline Status verifyCommandReconnect(const core::ICrypto& crypto, Bytes datagram,
                                     const Token64& token) {
  if (datagram.size() != kCommandReconnectSize ||
      datagram[0] != static_cast<std::uint8_t>(MessageType::CommandReconnect))
    return Errc::Malformed;
  const Status cryptoStatus = crypto.availability();
  if (!cryptoStatus.ok()) return cryptoStatus;
  std::uint8_t expected[kHmacTagSize];
  if (!crypto.hmacSha256(token.bytes(), Bytes(datagram.data(), 1), expected))
    return Errc::Malformed;
  if (!crypto.constantTimeEquals(expected, datagram.data() + 1, kHmacTagSize))
    return Errc::HmacMismatch;
  return Errc::Ok;
}

/// Verify a bound control frame (Buddy v0.45.0): `type`, a `signedLen`-byte prefix that ends
/// with the 8-byte nonce, and the HMAC over that prefix. A nonce other than `nonce` (the one
/// this assignment sent) is Rejected: a frame from an earlier assignment.
inline Status verifyBoundControl(const core::ICrypto& crypto, Bytes datagram, MessageType type,
                                 std::size_t signedLen, const Token64& token,
                                 const std::uint8_t (&nonce)[kControlNonceSize]) {
  if (datagram.size() != signedLen + kHmacTagSize ||
      datagram[0] != static_cast<std::uint8_t>(type))
    return Errc::Malformed;
  const Status cryptoStatus = crypto.availability();
  if (!cryptoStatus.ok()) return cryptoStatus;
  std::uint8_t expected[kHmacTagSize];
  if (!crypto.hmacSha256(token.bytes(), Bytes(datagram.data(), signedLen), expected))
    return Errc::Malformed;
  if (!crypto.constantTimeEquals(expected, datagram.data() + signedLen, kHmacTagSize))
    return Errc::HmacMismatch;
  if (!crypto.constantTimeEquals(nonce, datagram.data() + signedLen - kControlNonceSize,
                                 kControlNonceSize))
    return Errc::Rejected;
  return Errc::Ok;
}

/// COMMAND_RECONNECT_BOUND (44): [type][nonce][HMAC over the first 9 bytes].
inline Status verifyCommandReconnectBound(const core::ICrypto& crypto, Bytes datagram,
                                          const Token64& token,
                                          const std::uint8_t (&nonce)[kControlNonceSize]) {
  return verifyBoundControl(crypto, datagram, MessageType::CommandReconnectBound,
                            1 + kControlNonceSize, token, nonce);
}

/// CONTROL_BOUND_ACK (46): [type][nonce][HMAC over the first 9 bytes].
inline Status verifyControlBoundAck(const core::ICrypto& crypto, Bytes datagram,
                                    const Token64& token,
                                    const std::uint8_t (&nonce)[kControlNonceSize]) {
  return verifyBoundControl(crypto, datagram, MessageType::ControlBoundAck,
                            1 + kControlNonceSize, token, nonce);
}

/// COMMAND_SESSION_RELEASED_BOUND (45): [type][reason][nonce][HMAC over the first 10 bytes].
/// Returns the reason byte.
inline Result<std::uint8_t> verifySessionReleasedBound(
    const core::ICrypto& crypto, Bytes datagram, const Token64& token,
    const std::uint8_t (&nonce)[kControlNonceSize]) {
  const Status st = verifyBoundControl(crypto, datagram, MessageType::CommandSessionReleasedBound,
                                       2 + kControlNonceSize, token, nonce);
  if (!st.ok()) return st.code;
  return datagram[1];
}

/// The updatedAt (epoch ms) a ChunkChangedNotification carries, or 0 when its payload is short.
inline std::int64_t chunkChangedUpdatedAtMs(Bytes payload) noexcept {
  return payload.size() >= 8 ? le::readI64(payload.data()) : 0;
}

/// Verify a COMMAND_SESSION_RELEASED frame: HMAC-SHA256 keyed on this client's game
/// token over the two leading bytes (type and reason), so the reason cannot be rewritten
/// in flight. Returns the reason byte; an unverified frame must be ignored.
inline Result<std::uint8_t> verifySessionReleased(const core::ICrypto& crypto, Bytes datagram,
                                                  const Token64& token) {
  if (datagram.size() != kCommandSessionReleasedSize ||
      datagram[0] != static_cast<std::uint8_t>(MessageType::CommandSessionReleased))
    return Errc::Malformed;
  const Status cryptoStatus = crypto.availability();
  if (!cryptoStatus.ok()) return cryptoStatus.code;
  std::uint8_t expected[kHmacTagSize];
  if (!crypto.hmacSha256(token.bytes(), Bytes(datagram.data(), 2), expected))
    return Errc::Malformed;
  if (!crypto.constantTimeEquals(expected, datagram.data() + 2, kHmacTagSize))
    return Errc::HmacMismatch;
  return datagram[1];
}

// ---------------------------------------------------------------------------
// Fleet session messages (protocol.hpp, namespace fleet)
// ---------------------------------------------------------------------------

/// The proof inside a FLEET_CARRY: HMAC-SHA256(key = the player's 64-octet token,
/// message = "ck-fleet-carry" + fleet gameTokenId + player gameTokenId + epoch millis),
/// each number 8 bytes little endian. It shows the game server holds that player's token.
/// `out` must hold 32 bytes.
inline bool fleetCarryProof(const core::ICrypto& crypto, const Token64& playerToken,
                            std::int64_t fleetGameTokenId, std::int64_t playerGameTokenId,
                            std::int64_t epochMillis, std::uint8_t* out) {
  std::uint8_t message[fleet::kCarryProofMessageSize];
  std::memcpy(message, fleet::kCarryProofLabel, fleet::kCarryProofLabelSize);
  le::writeI64(message + fleet::kCarryProofLabelSize, fleetGameTokenId);
  le::writeI64(message + fleet::kCarryProofLabelSize + 8, playerGameTokenId);
  le::writeI64(message + fleet::kCarryProofLabelSize + 16, epochMillis);
  return crypto.hmacSha256(playerToken.bytes(), Bytes(message, sizeof(message)), out);
}

/// Write a FLEET_CARRY app payload, [8B player gameTokenId][8B epoch millis][32B proof], into
/// `out`; returns the 48 bytes written. Send it in the long-spatial layout as the
/// capabilities are sent (chunk (0,0,0), distance 0, no actor UUID), with the fleet
/// session's own gameTokenId in the tail.
inline Result<std::size_t> encodeFleetCarryPayload(const core::ICrypto& crypto,
                                                   const Token64& playerToken,
                                                   std::int64_t fleetGameTokenId,
                                                   std::int64_t playerGameTokenId,
                                                   std::int64_t epochMillis, MutableBytes out) {
  if (out.size() < fleet::kCarryPayloadSize) return Errc::BufferTooSmall;
  const Status cryptoStatus = crypto.availability();
  if (!cryptoStatus.ok()) return cryptoStatus.code;
  std::uint8_t* b = out.data();
  le::writeI64(b + fleet::kCarryPlayerOffset, playerGameTokenId);
  le::writeI64(b + fleet::kCarryEpochOffset, epochMillis);
  if (!fleetCarryProof(crypto, playerToken, fleetGameTokenId, playerGameTokenId, epochMillis,
                       b + fleet::kCarryProofOffset))
    return Errc::Malformed;
  return fleet::kCarryPayloadSize;
}

/// Write a FLEET_RELEASE app payload, [8B player gameTokenId]; returns the 8 bytes written.
inline Result<std::size_t> encodeFleetReleasePayload(std::int64_t playerGameTokenId,
                                                     MutableBytes out) {
  if (out.size() < fleet::kReleasePayloadSize) return Errc::BufferTooSmall;
  le::writeI64(out.data(), playerGameTokenId);
  return fleet::kReleasePayloadSize;
}

/// Verify a fleet session's server frame (51, 52 or 53) in the form that carries its own
/// HMAC: alone in its datagram, or a member of a MESSAGE_BUNDLE (2). The last 32 bytes are
/// HMAC-SHA256(key = the fleet token, message = every byte before them, from the type byte
/// on, followed by the fleet token's 64 octets). Returns the frame without its HMAC, for the
/// parsers below. Malformed for something too short to be a frame and its HMAC;
/// HmacMismatch for a frame the fleet token did not sign, which must be dropped.
///
/// A member of a MESSAGE_BUNDLE_SIGNED (30) carries no HMAC of its own: verify the bundle
/// (verifySignedBundle) and give its members to the parsers as they are.
inline Result<Bytes> verifyFleetFrame(const core::ICrypto& crypto, Bytes message,
                                      const Token64& fleetToken,
                                      const core::IMac* mac = nullptr) {
  if (message.size() <= kHmacTagSize) return Errc::Malformed;
  const std::uint8_t type = message[0];
  if (type != static_cast<std::uint8_t>(MessageType::FleetCarryResult) &&
      type != static_cast<std::uint8_t>(MessageType::FleetReleased) &&
      type != static_cast<std::uint8_t>(MessageType::FleetDelivery))
    return Errc::Malformed;
  const Status cryptoStatus = crypto.availability();
  if (!cryptoStatus.ok()) return cryptoStatus.code;
  const std::size_t frameLen = message.size() - kHmacTagSize;
  std::uint8_t expected[kHmacTagSize];
  if (!spatialHmac(crypto, message.first(frameLen), fleetToken, expected, mac))
    return Errc::Malformed;
  if (!crypto.constantTimeEquals(expected, message.data() + frameLen, kHmacTagSize))
    return Errc::HmacMismatch;
  return message.first(frameLen);
}

struct FleetCarryResultView {
  std::int64_t playerGameTokenId;
  /// 0 unless `status` is 0.
  std::int64_t userId;
  /// 0 carried; 1 no such live token (also a token of another app); 2 another
  /// organization's player; 3 the proof or its time failed (also a replay); 4 the session
  /// carries as many as it may (also no place for the carry).
  std::uint8_t status;
};

/// Parse a FLEET_CARRY_RESULT (51) without its HMAC: [type][8B player gameTokenId][8B user
/// id][1B status], 18 bytes exactly.
inline Result<FleetCarryResultView> parseFleetCarryResult(Bytes frame) {
  if (frame.size() != fleet::kCarryResultSize ||
      frame[0] != static_cast<std::uint8_t>(MessageType::FleetCarryResult))
    return Errc::Malformed;
  const std::uint8_t* b = frame.data();
  return FleetCarryResultView{le::readI64(b + 1), le::readI64(b + 9), b[17]};
}

struct FleetReleasedView {
  std::int64_t playerGameTokenId;
  /// 1 another session carries the player; 2 the player's sign-in ended; 5 no gameplay
  /// input from the player for the app's idle timeout.
  std::uint8_t reason;
};

/// Parse a FLEET_RELEASED (52) without its HMAC: [type][8B player gameTokenId][1B reason],
/// 10 bytes exactly.
inline Result<FleetReleasedView> parseFleetReleased(Bytes frame) {
  if (frame.size() != fleet::kReleasedSize ||
      frame[0] != static_cast<std::uint8_t>(MessageType::FleetReleased))
    return Errc::Malformed;
  const std::uint8_t* b = frame.data();
  return FleetReleasedView{le::readI64(b + 1), b[9]};
}

/// A parsed FLEET_DELIVERY (53). `notification` points into the original buffer.
struct FleetDeliveryView {
  /// How many carried players may receive the notification: 1 to 32.
  std::size_t count;
  /// `count` game token ids, 8 bytes little endian each; read them with recipient().
  const std::uint8_t* recipients;
  /// What a player's own session would have been sent, unsigned: a long-spatial
  /// notification with containsAuth 0 and its 9-byte tail (parseFleetDeliveredSpatial), or a
  /// channel notification 18 or 36, byte for byte (parseFleetDeliveredChannel). Its length
  /// is the frame's minus 2 + 8 x count.
  Bytes notification;

  std::int64_t recipient(std::size_t i) const { return le::readI64(recipients + i * 8); }
  bool channel() const {
    return notification[0] == static_cast<std::uint8_t>(MessageType::ChannelMessageNotification) ||
           notification[0] == static_cast<std::uint8_t>(MessageType::ChannelAudioNotification);
  }
};

/// Parse a FLEET_DELIVERY (53) without its HMAC: [type][1B count][count x 8B player
/// gameTokenId][notification]. Malformed for a count of 0 or over
/// fleet::kMaxDeliveryRecipients, a frame shorter than its count says, and a frame with no
/// notification after the list.
inline Result<FleetDeliveryView> parseFleetDelivery(Bytes frame) {
  if (frame.size() < fleet::kDeliveryHeaderSize ||
      frame[0] != static_cast<std::uint8_t>(MessageType::FleetDelivery))
    return Errc::Malformed;
  const std::size_t count = frame[1];
  if (count == 0 || count > fleet::kMaxDeliveryRecipients) return Errc::Malformed;
  const std::size_t listEnd = fleet::kDeliveryHeaderSize + count * 8;
  if (frame.size() <= listEnd) return Errc::Malformed;
  return FleetDeliveryView{count, frame.data() + fleet::kDeliveryHeaderSize,
                           frame.subspan(listEnd)};
}

/// The long-spatial notification inside a FLEET_DELIVERY, read by the parser an ordinary
/// session's notifications go through. Inside a delivery it is always the unsigned form
/// (containsAuth 0, no HMAC of its own, the 9-byte tail): the delivery's own HMAC, or its
/// signed bundle's, is what vouches for it. Malformed for anything else, a signed
/// notification and a client-only request included, so this accepts nothing an ordinary
/// session's path would have had to verify.
inline Result<LongSpatialView> parseFleetDeliveredSpatial(Bytes notification) {
  if (notification.size() < kMinLongSpatialNoHmac) return Errc::Malformed;
  if (!isLongSpatialLayout(notification[0]) || isClientOnlyControlType(notification[0]))
    return Errc::Malformed;
  if (notification[offsets::kContainsAuth] != 0) return Errc::Malformed;
  return parseLongSpatial(notification);
}

/// The channel notification (18 or 36) inside a FLEET_DELIVERY: byte for byte what any
/// session receives, and nothing after it.
inline Result<ChannelNotificationView> parseFleetDeliveredChannel(Bytes notification) {
  auto parsed = parseChannelNotification(notification);
  if (!parsed.ok()) return parsed;
  if (notification.size() !=
      channel::kHeaderSize + parsed->payload.size() + channel::kNotificationTailSize)
    return Errc::Malformed;
  return parsed;
}

// ---------------------------------------------------------------------------
// Bundle framing: [1B type=2]{[2B len LE][full message]}...
//
// The same framing in both directions. The server bundles notifications on
// the downlink; a client may bundle its requests on the uplink. There is no
// count field, every member is a complete signed message, and the wrapper
// carries no auth of its own. Bundles never nest.
// ---------------------------------------------------------------------------

/// Type byte in front of the first length prefix.
constexpr std::size_t kBundleHeaderSize = 1;
/// uint16 little-endian length in front of every member.
constexpr std::size_t kBundleLengthPrefix = 2;
/// Cap on members per datagram, matching the server's reader and writer.
constexpr std::size_t kMaxBundleMembers = 32;
/// Largest single message that can travel inside a bundle at all.
constexpr std::size_t kMaxBundleMemberSize =
    kMaxDatagramSize - kBundleHeaderSize - kBundleLengthPrefix;  // 1229

/// Accumulates complete messages into one MESSAGE_BUNDLE datagram in a caller-
/// owned buffer. Mirrors forEachMessage. Typical use is the client-side send
/// path: append() while messages arrive, then transmit datagram() and reset().
///
///   BundleWriter w(MutableBytes(buf, sizeof(buf)));
///   if (!w.fits(n)) { send(w.datagram()); w.reset(); }
///   w.append(Bytes(msg, n));
///
/// datagram() returns the single member unwrapped when count() == 1 (the
/// 3-byte wrapper buys nothing for one message, and the server does the same
/// on the downlink) and the type-2 frame otherwise.
class BundleWriter {
 public:
  explicit BundleWriter(MutableBytes buffer) : buf_(buffer) { reset(); }

  /// Capacity of the underlying buffer, at most kMaxDatagramSize is useful.
  std::size_t capacity() const { return buf_.size(); }
  std::size_t count() const { return count_; }
  bool empty() const { return count_ == 0; }
  /// Bytes the type-2 frame would occupy right now (header included).
  std::size_t size() const { return offset_; }

  /// True if a message of `len` bytes can be appended without exceeding the
  /// buffer or the member cap. A zero-length message never fits.
  bool fits(std::size_t len) const {
    if (len == 0 || len > 0xFFFF || count_ >= kMaxBundleMembers) return false;
    return offset_ + kBundleLengthPrefix + len <= buf_.size();
  }

  /// Append one complete message. Returns false, writing nothing, if it does
  /// not fit (see fits()).
  bool append(Bytes message) {
    if (!fits(message.size())) return false;
    le::writeU16(buf_.data() + offset_, static_cast<std::uint16_t>(message.size()));
    std::memcpy(buf_.data() + offset_ + kBundleLengthPrefix, message.data(), message.size());
    offset_ += kBundleLengthPrefix + message.size();
    ++count_;
    return true;
  }

  /// The bytes to put on the wire: empty when nothing was appended, the lone
  /// member itself when count() == 1, the type-2 frame otherwise.
  Bytes datagram() const {
    if (count_ == 0) return Bytes();
    if (count_ == 1) {
      const std::size_t len = le::readU16(buf_.data() + kBundleHeaderSize);
      return Bytes(buf_.data() + kBundleHeaderSize + kBundleLengthPrefix, len);
    }
    return Bytes(buf_.data(), offset_);
  }

  /// Opcode of the first appended member (0 when empty). Lets a caller decide
  /// whether the lone member it is about to send unwrapped needs special
  /// handling.
  std::uint8_t firstType() const {
    if (count_ == 0) return 0;
    return buf_.data()[kBundleHeaderSize + kBundleLengthPrefix];
  }

  void reset() {
    if (!buf_.empty()) buf_.data()[0] = static_cast<std::uint8_t>(MessageType::MessageBundle);
    offset_ = kBundleHeaderSize;
    count_ = 0;
  }

 private:
  MutableBytes buf_;
  std::size_t offset_ = kBundleHeaderSize;
  std::size_t count_ = 0;
};

/// Invoke `fn(Bytes)` for each message in the datagram. A MESSAGE_BUNDLE
/// ([1B type=2]{[2B len LE][msg]}...) yields each member; any other datagram
/// yields itself once. Returns Malformed if a bundle is truncated (members
/// before the truncation are still delivered).
template <typename Fn>
inline Status forEachMessage(Bytes datagram, Fn&& fn) {
  if (datagram.empty()) return Errc::Malformed;
  if (datagram[0] == static_cast<std::uint8_t>(MessageType::MessageBundleSigned)) {
    // The caller has verified the tail (verifySignedBundle); walk what precedes it.
    if (datagram.size() <= kHmacTagSize + 1) return Errc::Malformed;
    datagram = datagram.first(datagram.size() - kHmacTagSize);
  } else if (datagram[0] != static_cast<std::uint8_t>(MessageType::MessageBundle)) {
    fn(datagram);
    return Errc::Ok;
  }
  std::size_t offset = 1;
  while (offset < datagram.size()) {
    if (offset + 2 > datagram.size()) return Errc::Malformed;
    const std::uint16_t msgLen = le::readU16(datagram.data() + offset);
    offset += 2;
    if (offset + msgLen > datagram.size()) return Errc::Malformed;
    fn(datagram.subspan(offset, msgLen));
    offset += msgLen;
  }
  return Errc::Ok;
}

}  // namespace crowdy::wire
