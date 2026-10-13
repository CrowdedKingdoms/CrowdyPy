// The voice payload helpers, CrowdyCPP's crowdy/media/voice_frames.hpp, registered on
// crowdypy._native.replication beside the video frames. crowdypy.media checks every
// argument the way CrowdyJS does before it reaches these, so the options CrowdyCPP would
// clamp never get here out of range. Nothing here reads a codec: a frame is opaque.
#include <nanobind/stl/optional.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/tuple.h>

#include <vector>

#include "common.hpp"
#include "crowdy/media/voice_frames.hpp"

namespace crowdypy {
namespace {

namespace media = crowdy::media;
using crowdy::Errc;

nb::bytes blob(const std::vector<std::uint8_t>& v) {
  return nb::bytes(reinterpret_cast<const char*>(v.data()), v.size());
}

media::VoiceHeader header_of(int codec, int seq, std::uint32_t timestamp, int frameMs,
                             int flags) {
  media::VoiceHeader h;
  h.codec = static_cast<std::uint8_t>(codec);
  h.seq = static_cast<std::uint16_t>(seq);
  h.timestamp = timestamp;
  h.frameMs = static_cast<std::uint8_t>(frameMs);
  h.flags = static_cast<std::uint8_t>(flags);
  return h;
}

const char* push_result(media::VoicePushResult result) {
  switch (result) {
    case media::VoicePushResult::Buffered: return "buffered";
    case media::VoicePushResult::Late: return "late";
    case media::VoicePushResult::Duplicate: return "duplicate";
    case media::VoicePushResult::Malformed: return "malformed";
  }
  return "malformed";
}

nb::list playouts(std::vector<media::VoicePlayout> slots) {
  nb::list out;
  for (const auto& s : slots) {
    nb::object frame = s.gap ? nb::none() : nb::object(blob(s.frame));
    out.append(
        nb::make_tuple(s.key, s.seq, s.timestamp, s.codec, s.frameMs, s.flags, s.gap, frame));
  }
  return out;
}

class PyPacketizer {
 public:
  PyPacketizer(int codec, int frameMs, int seq, std::uint32_t timestamp)
      : packetizer_(media::VoicePacketizerOptions{static_cast<std::uint8_t>(codec),
                                                  static_cast<std::uint8_t>(frameMs),
                                                  static_cast<std::uint16_t>(seq), timestamp}) {}

  nb::bytes packetize(nb::handle frame, bool last) {
    BufferView body(frame);
    const auto packet = packetizer_.packetize(body.bytes(), last);
    if (packet.empty()) raise(Errc::InvalidArgument, "the frame does not fit one voice packet");
    return blob(packet);
  }

  void skip(std::uint32_t frames) { packetizer_.skip(frames); }
  int codec() const { return packetizer_.codec(); }
  int frameMs() const { return packetizer_.frameMs(); }
  int nextSeq() const { return packetizer_.nextSeq(); }
  std::uint32_t nextTimestamp() const { return packetizer_.nextTimestamp(); }

 private:
  media::VoicePacketizer packetizer_;
};

class PyJitterBuffer {
 public:
  PyJitterBuffer(std::int64_t targetDelayMs, int maxFrames, std::int64_t resetAfterMs)
      : buffer_(media::VoiceJitterBufferOptions{targetDelayMs, maxFrames, resetAfterMs}) {}

  const char* push(const std::string& key, nb::handle packet, std::int64_t nowMs) {
    BufferView body(packet);
    return push_result(buffer_.push(key, body.bytes(), nowMs));
  }

  nb::list pull(const std::string& key, std::int64_t nowMs) {
    return playouts(buffer_.pull(key, nowMs));
  }

  nb::list poll(std::int64_t nowMs) { return playouts(buffer_.poll(nowMs)); }
  void forget(const std::string& key) { buffer_.forget(key); }
  std::size_t senderCount() const { return buffer_.senderCount(); }
  std::size_t bufferedCount(const std::string& key) const { return buffer_.bufferedCount(key); }
  std::uint64_t late() const { return buffer_.late; }
  std::uint64_t duplicates() const { return buffer_.duplicates; }
  std::uint64_t malformed() const { return buffer_.malformed; }
  std::uint64_t discarded() const { return buffer_.discarded; }
  std::uint64_t gaps() const { return buffer_.gaps; }

 private:
  media::VoiceJitterBuffer buffer_;
};

}  // namespace

void register_voice(nb::module_& m) {
  m.attr("VOICE_HEADER_BYTES") = media::kVoiceHeaderBytes;
  m.attr("VOICE_HEADER_VERSION") = media::kVoiceHeaderVersion;
  m.attr("MAX_VOICE_FRAME_BYTES") = media::kMaxVoiceFrameBytes;
  m.attr("VOICE_TARGET_DELAY_MS") = media::kVoiceTargetDelayMs;
  m.attr("VOICE_JITTER_MAX_FRAMES") = media::kVoiceJitterMaxFrames;
  m.attr("VOICE_RESET_AFTER_MS") = media::kVoiceResetAfterMs;

  m.def(
      "encode_voice_header",
      [](int codec, int seq, std::uint32_t timestamp, int frameMs, int flags) {
        const auto head = media::encodeVoiceHeader(header_of(codec, seq, timestamp, frameMs, flags));
        return nb::bytes(reinterpret_cast<const char*>(head.data()), head.size());
      },
      nb::arg("codec"), nb::arg("seq"), nb::arg("timestamp"), nb::arg("frame_ms"),
      nb::arg("flags"));
  m.def(
      "encode_voice_packet",
      [](int codec, int seq, std::uint32_t timestamp, int frameMs, int flags, nb::handle frame) {
        BufferView body(frame);
        if (body.size() > media::kMaxVoiceFrameBytes)
          raise(Errc::InvalidArgument, "the frame does not fit one voice packet");
        return blob(media::encodeVoicePacket(header_of(codec, seq, timestamp, frameMs, flags),
                                             body.bytes()));
      },
      nb::arg("codec"), nb::arg("seq"), nb::arg("timestamp"), nb::arg("frame_ms"),
      nb::arg("flags"), nb::arg("frame"));
  m.def(
      "decode_voice_packet",
      [](nb::handle packet) -> std::optional<nb::tuple> {
        BufferView body(packet);
        const auto parsed = media::decodeVoicePacket(body.bytes());
        if (!parsed) return std::nullopt;
        const auto& h = parsed->header;
        return nb::make_tuple(h.version, h.codec, h.seq, h.timestamp, h.frameMs, h.flags,
                              nb::bytes(reinterpret_cast<const char*>(parsed->frame.data()),
                                        parsed->frame.size()));
      },
      nb::arg("packet"));

  nb::class_<PyPacketizer>(m, "VoicePacketizer")
      .def(nb::init<int, int, int, std::uint32_t>(), nb::arg("codec"), nb::arg("frame_ms"),
           nb::arg("seq"), nb::arg("timestamp"))
      .def("packetize", &PyPacketizer::packetize, nb::arg("frame"), nb::arg("last"),
           nb::lock_self())
      .def("skip", &PyPacketizer::skip, nb::arg("frames"), nb::lock_self())
      .def_prop_ro("codec", &PyPacketizer::codec)
      .def_prop_ro("frame_ms", &PyPacketizer::frameMs)
      .def_prop_ro("next_seq", &PyPacketizer::nextSeq, nb::lock_self())
      .def_prop_ro("next_timestamp", &PyPacketizer::nextTimestamp, nb::lock_self());

  nb::class_<PyJitterBuffer>(m, "VoiceJitterBuffer")
      .def(nb::init<std::int64_t, int, std::int64_t>(), nb::arg("target_delay_ms"),
           nb::arg("max_frames"), nb::arg("reset_after_ms"))
      .def("push", &PyJitterBuffer::push, nb::arg("key"), nb::arg("packet"), nb::arg("now_ms"),
           nb::lock_self())
      .def("pull", &PyJitterBuffer::pull, nb::arg("key"), nb::arg("now_ms"), nb::lock_self())
      .def("poll", &PyJitterBuffer::poll, nb::arg("now_ms"), nb::lock_self())
      .def("forget", &PyJitterBuffer::forget, nb::arg("key"), nb::lock_self())
      .def("buffered_count", &PyJitterBuffer::bufferedCount, nb::arg("key"), nb::lock_self())
      .def_prop_ro("sender_count", &PyJitterBuffer::senderCount, nb::lock_self())
      .def_prop_ro("late", &PyJitterBuffer::late, nb::lock_self())
      .def_prop_ro("duplicates", &PyJitterBuffer::duplicates, nb::lock_self())
      .def_prop_ro("malformed", &PyJitterBuffer::malformed, nb::lock_self())
      .def_prop_ro("discarded", &PyJitterBuffer::discarded, nb::lock_self())
      .def_prop_ro("gaps", &PyJitterBuffer::gaps, nb::lock_self());
}

}  // namespace crowdypy
