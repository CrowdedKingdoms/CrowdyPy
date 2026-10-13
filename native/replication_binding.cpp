// crowdypy._native.replication: CrowdyCPP's replication Connection for Python.
//
// The rules this file is built around (AGENTS.md, "Performance rules"):
//
//  - CrowdyCPP's network thread (or the pump() caller) receives, verifies and
//    queues. Nothing it runs touches the interpreter: it asks for a server
//    assignment or a token refresh through ProviderBridge, which a Python thread
//    serves, and it wakes an event loop by writing one byte to a socket.
//  - Python drains events in batches (Connection.poll), as columns it can view
//    without copying, never one callback per datagram.
//  - Sends release the GIL; a batch send releases it once for the whole batch.
#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#else
#include <sys/socket.h>
#endif
#include "replication.hpp"

namespace crowdypy {

void write_wake(std::int64_t fd) {
  if (fd < 0) return;
  static const char byte = 1;
#ifdef _WIN32
  (void)::send(static_cast<SOCKET>(fd), &byte, 1, 0);
#else
  int flags = MSG_DONTWAIT;
#ifdef MSG_NOSIGNAL
  flags |= MSG_NOSIGNAL;
#endif
  (void)::send(static_cast<int>(fd), &byte, 1, flags);
#endif
}

void prepare_wake_fd(std::int64_t fd) {
#if defined(SO_NOSIGPIPE)
  if (fd >= 0) {
    const int one = 1;
    (void)::setsockopt(static_cast<int>(fd), SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
  }
#else
  (void)fd;
#endif
}

namespace {

// ------------------------------------------------------------------ video frames

class PyAssembler {
 public:
  explicit PyAssembler(std::int64_t timeoutMs) : assembler_(timeoutMs) {}

  std::optional<nb::tuple> ingest(nb::handle uuid, nb::handle packet, std::int64_t nowMs) {
    const core::ActorUuid id = uuid_from(uuid);
    BufferView body(packet);
    auto frame = assembler_.ingest(id, body.bytes(), nowMs);
    if (!frame) return std::nullopt;
    return frame_tuple(*frame);
  }

  /// Feed every video row of a batch; returns the frames completed, in order.
  nb::list ingestBatch(const NotificationBatch& batch, std::int64_t nowMs) {
    nb::list out;
    const Rows& rows = batch.rows();
    for (std::size_t i = 0; i < rows.size(); ++i) {
      if (rows.type[i] != static_cast<std::uint8_t>(wire::MessageType::ClientVideoNotification))
        continue;
      core::ActorUuid id{};
      std::memcpy(id.data(), batch.uuidOf(i), wire::kUuidSize);
      auto frame = assembler_.ingest(id, batch.payloadOf(i), nowMs);
      if (frame) out.append(frame_tuple(*frame));
    }
    return out;
  }

  std::size_t prune(std::int64_t nowMs) { return assembler_.prune(nowMs); }
  void forget(nb::handle uuid) { assembler_.forget(uuid_from(uuid)); }
  std::size_t pendingCount() const { return assembler_.pendingCount(); }
  std::uint64_t dropped() const { return assembler_.dropped; }
  std::uint64_t abandoned() const { return assembler_.abandoned; }

 private:
  static nb::tuple frame_tuple(const media::AssembledVideoFrame& f) {
    return nb::make_tuple(uuid_str(reinterpret_cast<const char*>(f.uuid.data())), f.frameId,
                          static_cast<int>(f.codec),
                          nb::bytes(reinterpret_cast<const char*>(f.bytes.data()), f.bytes.size()),
                          f.completedAtMs);
  }

  media::VideoFrameAssembler assembler_;
};

}  // namespace

void register_replication(nb::module_& m) {
  m.attr("STATUS_ROW") = kStatusRow;
  nb::dict errc;
  for (int code = 0; code <= static_cast<int>(Errc::WouldBlock); ++code)
    errc[errcName(static_cast<Errc>(code))] = code;
  m.attr("ERRC") = errc;

  nb::class_<ProviderBridge>(m, "ProviderBridge")
      .def(
          "next",
          [](ProviderBridge& b, int timeoutMs) -> std::optional<nb::tuple> {
            std::optional<ProviderBridge::Request> request;
            {
              nb::gil_scoped_release release;
              request = b.next(timeoutMs);
            }
            if (!request) return std::nullopt;
            nb::object current = nb::none();
            if (request->hasCurrent)
              current = nb::make_tuple(request->current.ip4, request->current.ip6,
                                       request->current.clientPort);
            return nb::make_tuple(request->id, request->kind, current);
          },
          nb::arg("timeout_ms"),
          "The next request as (id, kind, current), or None after timeout_ms or close.")
      .def(
          "answer_assignment",
          [](ProviderBridge& b, std::uint64_t id, const std::string& ip4, const std::string& ip6,
             int port) { b.answerAssignment(id, Assignment{ip4, ip6, port}); },
          nb::arg("id"), nb::arg("ip4"), nb::arg("ip6"), nb::arg("client_port"))
      .def(
          "answer_token",
          [](ProviderBridge& b, std::uint64_t id, const std::string& token, std::int64_t gameTokenId,
             std::int64_t expiresAtMs, bool authorized) {
            b.answerToken(id, token_info(token, gameTokenId, expiresAtMs, authorized));
          },
          nb::arg("id"), nb::arg("token"), nb::arg("game_token_id"), nb::arg("expires_at_ms"),
          nb::arg("authorized_on_current_server"))
      .def(
          "answer_error",
          [](ProviderBridge& b, std::uint64_t id, int code) {
            b.answerError(id, static_cast<Errc>(code));
          },
          nb::arg("id"), nb::arg("code") = static_cast<int>(Errc::Rejected))
      .def_prop_ro("closed", &ProviderBridge::closed)
      .def("close", &ProviderBridge::close);
  m.attr("ASSIGN") = ProviderBridge::kAssign;
  m.attr("REFRESH") = ProviderBridge::kRefresh;

  nb::class_<NotificationBatch>(m, "NotificationBatch")
      .def("__len__", &NotificationBatch::size)
      .def_prop_ro(
          "types", [](const NotificationBatch& b) { return column(b.rows().type, b.size(), 1); },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "app_ids", [](const NotificationBatch& b) { return column(b.rows().appId, b.size(), 1); },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "chunks", [](const NotificationBatch& b) { return column(b.rows().chunk, b.size(), 3); },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "uuids",
          [](const NotificationBatch& b) { return column(b.rows().uuid, b.size(), wire::kUuidSize); },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "epoch_ms", [](const NotificationBatch& b) { return column(b.rows().epochMs, b.size(), 1); },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "sequences",
          [](const NotificationBatch& b) { return column(b.rows().sequence, b.size(), 1); },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "extras",
          [](const NotificationBatch& b) { return column(b.rows().extra, b.size(), kExtras); },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "payload_offsets",
          [](const NotificationBatch& b) {
            return column(b.rows().offsets, b.rows().offsets.size(), 1);
          },
          nb::rv_policy::reference_internal)
      .def_prop_ro(
          "payload_data",
          [](const NotificationBatch& b) {
            return column(b.rows().payload, b.rows().payload.size(), 1);
          },
          nb::rv_policy::reference_internal)
      .def(
          "payload",
          [](const NotificationBatch& b, std::size_t i) {
            b.check(i);
            return to_bytes(b.payloadOf(i));
          },
          nb::arg("index"))
      .def(
          "uuid",
          [](const NotificationBatch& b, std::size_t i) {
            b.check(i);
            return uuid_str(b.uuidOf(i));
          },
          nb::arg("index"))
      .def(
          "row",
          [](const NotificationBatch& b, std::size_t i) {
            b.check(i);
            return row_tuple(b, i);
          },
          nb::arg("index"),
          "(type, app_id, (x, y, z), uuid, payload, epoch_ms, sequence, extras)")
      .def("rows",
           [](const NotificationBatch& b) {
             nb::list out;
             for (std::size_t i = 0; i < b.size(); ++i) out.append(row_tuple(b, i));
             return out;
           })
      .def(
          "match",
          [](const NotificationBatch& b, int sequence, nb::handle uuid) {
            if (sequence < 0 || sequence > 255) raise(Errc::InvalidArgument, "sequence is a uint8");
            return b.match(static_cast<std::uint8_t>(sequence), uuid_from(uuid));
          },
          nb::arg("sequence"), nb::arg("uuid"),
          "The row answering a send (its echo, or the error naming its sequence), or -1.");

  nb::class_<PyConnection>(m, "Connection")
      .def(
          "__init__",
          [](PyConnection* self, std::int64_t appId, const std::string& token,
             std::int64_t gameTokenId, std::int64_t expiresAtMs, bool manualPump, bool preferIpv6,
             int sessionReadyWaitMs, std::int64_t refreshLeadMs, bool verifyNotifications,
             bool advertiseCapabilities, std::int64_t advertiseIntervalMs,
             std::int64_t watchdogSilenceMs, std::size_t ringCapacity, int recvBuffer,
             int sendBuffer, bool bundleSends, int bundleWindowMs, std::int64_t providerTimeoutMs) {
            ConnectionOptions o;
            o.appId = appId;
            o.manualPump = manualPump;
            o.preferIpv6 = preferIpv6;
            o.sessionReadyWaitMs = sessionReadyWaitMs;
            o.refreshLeadMs = refreshLeadMs;
            o.verifyNotifications = verifyNotifications;
            o.advertiseCapabilities = advertiseCapabilities;
            o.advertiseIntervalMs = advertiseIntervalMs;
            o.watchdogSilenceMs = watchdogSilenceMs;
            o.ringCapacity = ringCapacity;
            o.socketRecvBufferBytes = recvBuffer;
            o.socketSendBufferBytes = sendBuffer;
            o.bundleSends = bundleSends;
            o.bundleWindowMs = bundleWindowMs;
            new (self) PyConnection(o, token, gameTokenId, expiresAtMs, providerTimeoutMs);
          },
          nb::arg("app_id"), nb::arg("token"), nb::arg("game_token_id"),
          nb::arg("expires_at_ms") = 0, nb::arg("manual_pump") = false,
          nb::arg("prefer_ipv6") = false, nb::arg("session_ready_wait_ms") = 1500,
          nb::arg("refresh_lead_ms") = 5 * 60 * 1000, nb::arg("verify_notifications") = true,
          nb::arg("advertise_capabilities") = true, nb::arg("advertise_interval_ms") = 15 * 1000,
          nb::arg("watchdog_silence_ms") = 0, nb::arg("ring_capacity") = 4096,
          nb::arg("socket_recv_buffer_bytes") = 1 << 20,
          nb::arg("socket_send_buffer_bytes") = 1 << 20, nb::arg("bundle_sends") = true,
          nb::arg("bundle_window_ms") = 1, nb::arg("provider_timeout_ms") = 30000)
      .def_prop_ro("bridge", &PyConnection::bridge, nb::rv_policy::reference_internal)
      .def("connect", &PyConnection::connect)
      .def("disconnect", &PyConnection::disconnect)
      .def_prop_ro("state", &PyConnection::state)
      .def("set_token", &PyConnection::setToken, nb::arg("token"), nb::arg("game_token_id"),
           nb::arg("expires_at_ms") = 0, nb::arg("authorized_on_current_server") = false)
      .def("request_reassignment", &PyConnection::requestReassignment)
      .def("set_wake_fd", &PyConnection::setWakeFd, nb::arg("fd"))
      .def("assignment", &PyConnection::assignment)
      .def("send_spatial", &PyConnection::sendSpatial, nb::arg("type"), nb::arg("x"), nb::arg("y"),
           nb::arg("z"), nb::arg("uuid"), nb::arg("payload"), nb::arg("distance"),
           nb::arg("decay"))
      .def("send_voxel_update", &PyConnection::sendVoxel, nb::arg("x"), nb::arg("y"), nb::arg("z"),
           nb::arg("uuid"), nb::arg("voxel_x"), nb::arg("voxel_y"), nb::arg("voxel_z"),
           nb::arg("voxel_type"), nb::arg("state"), nb::arg("distance"), nb::arg("decay"))
      .def("send_client_event", &PyConnection::sendClientEvent, nb::arg("x"), nb::arg("y"),
           nb::arg("z"), nb::arg("uuid"), nb::arg("event_type"), nb::arg("state"),
           nb::arg("distance"), nb::arg("decay"))
      .def("send_single_actor_message", &PyConnection::sendSingleActor, nb::arg("x"), nb::arg("y"),
           nb::arg("z"), nb::arg("uuid"), nb::arg("payload"))
      .def("send_channel_message", &PyConnection::sendChannel, nb::arg("channel_id"),
           nb::arg("uuid"), nb::arg("payload"))
      .def("send_channel_audio", &PyConnection::sendChannelAudio, nb::arg("channel_id"),
           nb::arg("uuid"), nb::arg("payload"))
      .def("send_ranged_channel_message", &PyConnection::sendRangedChannel, nb::arg("channel_id"),
           nb::arg("uuid"), nb::arg("payload"), nb::arg("x"), nb::arg("y"), nb::arg("z"),
           nb::arg("max_distance"))
      .def("send_heartbeat", &PyConnection::sendHeartbeat, nb::arg("x"), nb::arg("y"), nb::arg("z"),
           nb::arg("uuid"))
      .def("send_video_frame", &PyConnection::sendVideoFrame, nb::arg("x"), nb::arg("y"),
           nb::arg("z"), nb::arg("uuid"), nb::arg("frame"), nb::arg("frame_id"),
           nb::arg("codec"), nb::arg("distance"), nb::arg("decay"))
      .def("send_spatial_batch", &PyConnection::sendSpatialBatch, nb::arg("type"),
           nb::arg("chunks"), nb::arg("uuids"), nb::arg("payload"), nb::arg("offsets").none(),
           nb::arg("stride"), nb::arg("distance"), nb::arg("decay"), nb::arg("flush"))
      .def("flush_sends", &PyConnection::flushSends)
      .def("poll", &PyConnection::poll, nb::arg("max_events") = SIZE_MAX)
      .def("pump", &PyConnection::pump, nb::arg("timeout_ms") = 0)
      .def("stats", &PyConnection::stats)
      .def("drain_logs", &PyConnection::drainLogs);

  nb::class_<PyAssembler>(m, "VideoFrameAssembler")
      .def(nb::init<std::int64_t>(), nb::arg("timeout_ms") = media::kVideoFrameTimeoutMs)
      .def("ingest", &PyAssembler::ingest, nb::arg("uuid"), nb::arg("packet"), nb::arg("now_ms"),
           nb::lock_self())
      .def("ingest_batch", &PyAssembler::ingestBatch, nb::arg("batch"), nb::arg("now_ms"),
           nb::lock_self())
      .def("prune", &PyAssembler::prune, nb::arg("now_ms"), nb::lock_self())
      .def("forget", &PyAssembler::forget, nb::arg("uuid"), nb::lock_self())
      .def_prop_ro("pending_count", &PyAssembler::pendingCount, nb::lock_self())
      .def_prop_ro("dropped", &PyAssembler::dropped, nb::lock_self())
      .def_prop_ro("abandoned", &PyAssembler::abandoned, nb::lock_self());

  m.def(
      "fragment_frame",
      [](nb::handle frame, int frameId, int codec, std::size_t maxBody) {
        if (frameId < 0 || frameId > 0xFFFF) raise(Errc::InvalidArgument, "frame id is a uint16");
        if (codec < 0 || codec > 1) raise(Errc::InvalidArgument, "codec is 0 (JPEG) or 1 (WebP)");
        BufferView body(frame);
        const auto parts = media::fragmentFrame(body.bytes(), static_cast<std::uint16_t>(frameId),
                                                static_cast<media::VideoCodec>(codec), maxBody);
        nb::list out;
        for (const auto& part : parts)
          out.append(nb::bytes(reinterpret_cast<const char*>(part.data()), part.size()));
        return out;
      },
      nb::arg("frame"), nb::arg("frame_id"), nb::arg("codec") = 0,
      nb::arg("max_body") = media::kMaxVideoFragmentBodyBytes);
  m.def(
      "parse_video_fragment_header",
      [](nb::handle packet) -> std::optional<nb::tuple> {
        BufferView body(packet);
        const auto h = media::parseVideoFragmentHeader(body.bytes());
        if (!h) return std::nullopt;
        return nb::make_tuple(h->version, h->codec, h->frameId, h->fragIndex, h->fragCount);
      },
      nb::arg("packet"));
  m.def(
      "is_newer_frame_id",
      [](int a, int b) {
        return media::isNewerFrameId(static_cast<std::uint16_t>(a), static_cast<std::uint16_t>(b));
      },
      nb::arg("a"), nb::arg("b"));
  m.attr("VIDEO_FRAGMENT_HEADER_BYTES") = media::kVideoFragmentHeaderBytes;
  m.attr("MAX_VIDEO_FRAGMENT_BODY_BYTES") = media::kMaxVideoFragmentBodyBytes;
  m.attr("MAX_VIDEO_FRAGMENTS") = media::kMaxVideoFragments;
  m.attr("VIDEO_FRAME_TIMEOUT_MS") = media::kVideoFrameTimeoutMs;
  m.attr("VIDEO_FRAGMENT_VERSION") = media::kVideoFragmentVersion;
}

}  // namespace crowdypy
