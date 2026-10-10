// The wire codec, exposed for tools, tests and custom transports. The replication
// client uses the same functions internally without crossing into Python.

#include <nanobind/stl/string.h>
#include <nanobind/stl/tuple.h>
#include <nanobind/stl/vector.h>

#include <vector>

#include "common.hpp"
#include "crowdy/core/crypto.hpp"

namespace crowdypy {
namespace {

using crowdy::Bytes;
using crowdy::Errc;
using crowdy::MutableBytes;
namespace wire = crowdy::wire;

const crowdy::core::ICrypto& crypto() { return crowdy::core::opensslCrypto(); }

nb::bytes spatial_hmac(nb::handle token_obj, nb::handle prefix_obj) {
  const auto token = token_from(token_obj);
  BufferView prefix(prefix_obj);
  if (prefix.size() > wire::kMaxDatagramSize)
    raise(Errc::InvalidArgument, "the prefix is longer than a datagram");
  std::uint8_t tag[wire::kHmacTagSize];
  if (!wire::spatialHmac(crypto(), prefix.bytes(), token, tag))
    raise(Errc::CryptoUnavailable, "HMAC-SHA256 failed");
  return to_bytes(Bytes(tag, sizeof(tag)));
}

nb::bytes encode_long_spatial(nb::handle token_obj, std::uint8_t type, std::int64_t app_id,
                              std::int64_t chunk_x, std::int64_t chunk_y, std::int64_t chunk_z,
                              std::uint8_t distance, std::uint8_t decay, nb::handle uuid_obj,
                              nb::handle payload_obj, std::int64_t game_token_id,
                              std::uint8_t sequence) {
  const auto token = token_from(token_obj);
  BufferView payload(payload_obj);
  wire::LongSpatialParams p;
  p.type = static_cast<wire::MessageType>(type);
  p.appId = app_id;
  p.chunk = {chunk_x, chunk_y, chunk_z};
  p.distance = distance;
  p.decay = static_cast<wire::DecayRate>(decay);
  p.uuid = uuid_from(uuid_obj);
  p.payload = payload.bytes();
  p.gameTokenId = game_token_id;
  p.sequence = sequence;
  std::uint8_t buf[wire::kMaxDatagramSize];
  auto n = wire::encodeLongSpatial(crypto(), p, token, MutableBytes(buf, sizeof(buf)));
  if (!n.ok()) raise(n.error(), "cannot encode this long-spatial message");
  return to_bytes(Bytes(buf, n.value()));
}

nb::tuple parse_long_spatial(nb::handle datagram_obj) {
  BufferView datagram(datagram_obj);
  auto v = wire::parseLongSpatial(datagram.bytes());
  if (!v.ok()) raise(v.error(), "not a long-spatial message");
  return nb::make_tuple(static_cast<int>(v->type), v->appId, v->chunk.x, v->chunk.y, v->chunk.z,
                        v->distance, static_cast<int>(v->decay), v->containsAuth,
                        uuid_bytes(v->uuid), to_bytes(v->payload), v->epochMillisOrTokenId,
                        v->sequence);
}

std::string verify_long_spatial(nb::handle token_obj, nb::handle datagram_obj) {
  const auto token = token_from(token_obj);
  BufferView datagram(datagram_obj);
  return crowdy::errcName(wire::verifyLongSpatial(crypto(), datagram.bytes(), token).code);
}

std::string verify_signed_bundle(nb::handle token_obj, nb::handle datagram_obj) {
  const auto token = token_from(token_obj);
  BufferView datagram(datagram_obj);
  return crowdy::errcName(wire::verifySignedBundle(crypto(), datagram.bytes(), token).code);
}

std::string verify_command_reconnect(nb::handle token_obj, nb::handle datagram_obj) {
  const auto token = token_from(token_obj);
  BufferView datagram(datagram_obj);
  return crowdy::errcName(wire::verifyCommandReconnect(crypto(), datagram.bytes(), token).code);
}

nb::bytes encode_channel_request(wire::MessageType type, nb::handle token_obj,
                                 std::int64_t channel_id, nb::handle uuid_obj,
                                 nb::handle payload_obj, std::int64_t game_token_id,
                                 std::uint8_t sequence) {
  const auto token = token_from(token_obj);
  BufferView payload(payload_obj);
  wire::ChannelMessageParams p;
  p.channelId = channel_id;
  p.uuid = uuid_from(uuid_obj);
  p.payload = payload.bytes();
  p.gameTokenId = game_token_id;
  p.sequence = sequence;
  std::uint8_t buf[wire::kMaxDatagramSize];
  auto n = wire::encodeChannelRequest(type, crypto(), p, token, MutableBytes(buf, sizeof(buf)));
  if (!n.ok()) raise(n.error(), "cannot encode this channel request");
  return to_bytes(Bytes(buf, n.value()));
}

nb::bytes encode_channel_message(nb::handle token_obj, std::int64_t channel_id,
                                 nb::handle uuid_obj, nb::handle payload_obj,
                                 std::int64_t game_token_id, std::uint8_t sequence) {
  return encode_channel_request(wire::MessageType::ChannelMessageRequest, token_obj, channel_id,
                                uuid_obj, payload_obj, game_token_id, sequence);
}

nb::bytes encode_channel_audio(nb::handle token_obj, std::int64_t channel_id,
                               nb::handle uuid_obj, nb::handle payload_obj,
                               std::int64_t game_token_id, std::uint8_t sequence) {
  return encode_channel_request(wire::MessageType::ChannelAudioRequest, token_obj, channel_id,
                                uuid_obj, payload_obj, game_token_id, sequence);
}

nb::bytes encode_ranged_channel_message(nb::handle token_obj, std::int64_t channel_id,
                                        nb::handle uuid_obj, nb::handle payload_obj,
                                        std::int64_t app_id, std::int64_t chunk_x,
                                        std::int64_t chunk_y, std::int64_t chunk_z,
                                        std::uint32_t max_distance, std::int64_t game_token_id,
                                        std::uint8_t sequence) {
  const auto token = token_from(token_obj);
  BufferView payload(payload_obj);
  wire::RangedChannelMessageParams p;
  p.channelId = channel_id;
  p.uuid = uuid_from(uuid_obj);
  p.appId = app_id;
  p.origin = {chunk_x, chunk_y, chunk_z};
  p.maxDistance = max_distance;
  p.payload = payload.bytes();
  p.gameTokenId = game_token_id;
  p.sequence = sequence;
  std::uint8_t buf[wire::kMaxDatagramSize];
  auto n = wire::encodeRangedChannelMessage(crypto(), p, token, MutableBytes(buf, sizeof(buf)));
  if (!n.ok()) raise(n.error(), "cannot encode this ranged channel message");
  return to_bytes(Bytes(buf, n.value()));
}

nb::tuple parse_channel_notification(nb::handle datagram_obj) {
  BufferView datagram(datagram_obj);
  auto v = wire::parseChannelNotification(datagram.bytes());
  if (!v.ok()) raise(v.error(), "not a channel notification");
  return nb::make_tuple(v->channelId, uuid_bytes(v->senderUuid), to_bytes(v->payload),
                        v->epochMillis, v->sequence);
}

nb::tuple parse_generic_error(nb::handle datagram_obj) {
  BufferView datagram(datagram_obj);
  auto v = wire::parseGenericError(datagram.bytes());
  if (!v.ok()) raise(v.error(), "not a generic error frame");
  return nb::make_tuple(v->sequence, static_cast<int>(v->code));
}

nb::bytes encode_voxel_payload(std::int16_t x, std::int16_t y, std::int16_t z,
                               std::int16_t voxel_type, nb::handle state_obj) {
  BufferView state(state_obj);
  std::uint8_t buf[wire::kMaxDatagramSize];
  auto n = wire::encodeVoxelPayload(x, y, z, voxel_type, state.bytes(),
                                    MutableBytes(buf, sizeof(buf)));
  if (!n.ok()) raise(n.error(), "cannot encode this voxel payload");
  return to_bytes(Bytes(buf, n.value()));
}

nb::tuple parse_voxel_payload(nb::handle payload_obj) {
  BufferView payload(payload_obj);
  auto v = wire::parseVoxelPayload(payload.bytes());
  if (!v.ok()) raise(v.error(), "not a voxel payload");
  return nb::make_tuple(v->x, v->y, v->z, v->voxelType, to_bytes(v->state));
}

nb::bytes encode_event_payload(std::uint16_t event_type, nb::handle state_obj) {
  BufferView state(state_obj);
  std::uint8_t buf[wire::kMaxDatagramSize];
  auto n = wire::encodeEventPayload(event_type, state.bytes(), MutableBytes(buf, sizeof(buf)));
  if (!n.ok()) raise(n.error(), "cannot encode this event payload");
  return to_bytes(Bytes(buf, n.value()));
}

nb::bytes bundle(const std::vector<nb::handle>& messages) {
  std::uint8_t buf[wire::kMaxDatagramSize];
  wire::BundleWriter writer(MutableBytes(buf, sizeof(buf)));
  for (const auto& message : messages) {
    BufferView view(message);
    if (!writer.append(view.bytes()))
      raise(Errc::BufferTooSmall,
            "these messages do not fit one datagram (1232 bytes, 32 members)");
  }
  return to_bytes(writer.datagram());
}

std::vector<nb::bytes> split_datagram(nb::handle datagram_obj) {
  BufferView datagram(datagram_obj);
  std::vector<nb::bytes> out;
  const auto status = wire::forEachMessage(datagram.bytes(), [&](Bytes member) {
    out.push_back(to_bytes(member));
  });
  if (!status.ok()) raise(status.code, "truncated bundle");
  return out;
}

}  // namespace

void register_wire(nb::module_& m) {
  m.attr("MAX_DATAGRAM_SIZE") = wire::kMaxDatagramSize;
  m.attr("MAX_LONG_SPATIAL_PAYLOAD") = wire::kMaxLongSpatialPayload;
  m.attr("LONG_SPATIAL_HEADER_SIZE") = wire::kLongSpatialHeaderSize;
  m.attr("UUID_SIZE") = wire::kUuidSize;
  m.attr("HMAC_TAG_SIZE") = wire::kHmacTagSize;
  m.attr("TOKEN_OCTETS") = wire::kTokenOctets;
  m.attr("MAX_BUNDLE_MEMBERS") = wire::kMaxBundleMembers;
  m.attr("MAX_BUNDLE_MEMBER_SIZE") = wire::kMaxBundleMemberSize;
  m.attr("MAX_CHANNEL_PAYLOAD") = wire::channel::kMaxPayload;
  m.attr("CHANNEL_RANGED_MAX_DISTANCE") = wire::channel_ranged::kMaxDistance;
  m.attr("VOXEL_STATE_MAX_BYTES") = wire::voxel::kMaxStateSize;
  m.attr("MAX_DISTANCE") = wire::kMaxDistance;
  m.attr("CLIENT_CAPABILITIES_ALL") = wire::ClientCapability::kAll;

  m.def("spatial_hmac", &spatial_hmac, nb::arg("token"), nb::arg("prefix"));
  m.def("encode_long_spatial", &encode_long_spatial, nb::arg("token"), nb::arg("type"),
        nb::arg("app_id"), nb::arg("chunk_x"), nb::arg("chunk_y"), nb::arg("chunk_z"),
        nb::arg("distance"), nb::arg("decay"), nb::arg("uuid"), nb::arg("payload"),
        nb::arg("game_token_id"), nb::arg("sequence"));
  m.def("parse_long_spatial", &parse_long_spatial, nb::arg("datagram"));
  m.def("verify_long_spatial", &verify_long_spatial, nb::arg("token"), nb::arg("datagram"));
  m.def("verify_signed_bundle", &verify_signed_bundle, nb::arg("token"), nb::arg("datagram"));
  m.def("verify_command_reconnect", &verify_command_reconnect, nb::arg("token"),
        nb::arg("datagram"));
  m.def("encode_channel_message", &encode_channel_message, nb::arg("token"),
        nb::arg("channel_id"), nb::arg("uuid"), nb::arg("payload"), nb::arg("game_token_id"),
        nb::arg("sequence"));
  m.def("encode_channel_audio", &encode_channel_audio, nb::arg("token"), nb::arg("channel_id"),
        nb::arg("uuid"), nb::arg("payload"), nb::arg("game_token_id"), nb::arg("sequence"));
  m.def("encode_ranged_channel_message", &encode_ranged_channel_message, nb::arg("token"),
        nb::arg("channel_id"), nb::arg("uuid"), nb::arg("payload"), nb::arg("app_id"),
        nb::arg("chunk_x"), nb::arg("chunk_y"), nb::arg("chunk_z"), nb::arg("max_distance"),
        nb::arg("game_token_id"), nb::arg("sequence"));
  m.def("parse_channel_notification", &parse_channel_notification, nb::arg("datagram"));
  m.def("parse_generic_error", &parse_generic_error, nb::arg("datagram"));
  m.def("encode_voxel_payload", &encode_voxel_payload, nb::arg("x"), nb::arg("y"),
        nb::arg("z"), nb::arg("voxel_type"), nb::arg("state"));
  m.def("parse_voxel_payload", &parse_voxel_payload, nb::arg("payload"));
  m.def("encode_event_payload", &encode_event_payload, nb::arg("event_type"), nb::arg("state"));
  m.def("bundle", &bundle, nb::arg("messages"));
  m.def("split_datagram", &split_datagram, nb::arg("datagram"));
}

}  // namespace crowdypy
