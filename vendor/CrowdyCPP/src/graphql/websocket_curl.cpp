#include "crowdy/graphql/websocket.hpp"

#include <curl/curl.h>

#if LIBCURL_VERSION_NUM < 0x080d00
#error "CrowdyCPP curl WebSockets require libcurl 8.13 or newer"
#endif

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cctype>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <limits>
#include <optional>
#include <string>
#include <thread>
#include <utility>

namespace crowdy::graphql {

namespace {

bool validUtf8(std::string_view input) {
  const auto* bytes = reinterpret_cast<const unsigned char*>(input.data());
  std::size_t i = 0;
  while (i < input.size()) {
    const unsigned char first = bytes[i++];
    if (first <= 0x7fU) continue;
    std::size_t count = 0;
    std::uint32_t value = 0;
    std::uint32_t minimum = 0;
    if (first >= 0xc2U && first <= 0xdfU) {
      count = 1;
      value = static_cast<std::uint32_t>(first & 0x1fU);
      minimum = 0x80U;
    } else if (first >= 0xe0U && first <= 0xefU) {
      count = 2;
      value = static_cast<std::uint32_t>(first & 0x0fU);
      minimum = 0x800U;
    } else if (first >= 0xf0U && first <= 0xf4U) {
      count = 3;
      value = static_cast<std::uint32_t>(first & 0x07U);
      minimum = 0x10000U;
    } else {
      return false;
    }
    if (count > input.size() - i) return false;
    for (std::size_t n = 0; n < count; ++n) {
      const unsigned char next = bytes[i++];
      if ((next & 0xc0U) != 0x80U) return false;
      value = (value << 6U) | static_cast<std::uint32_t>(next & 0x3fU);
    }
    if (value < minimum || value > 0x10ffffU ||
        (value >= 0xd800U && value <= 0xdfffU)) {
      return false;
    }
  }
  return true;
}

std::string safeCloseReason(std::string_view reason) {
  std::string result(reason.substr(0, std::min<std::size_t>(reason.size(), 121)));
  while (!result.empty() && !validUtf8(result)) result.pop_back();
  return result;
}

std::string lowercase(std::string_view value) {
  std::string result(value);
  std::transform(result.begin(), result.end(), result.begin(), [](unsigned char c) {
    return static_cast<char>(std::tolower(c));
  });
  return result;
}

WebSocketError curlError(CURLcode code, std::string_view context,
                         const char* detail = nullptr) {
  WebSocketError error;
  error.kind = WebSocketErrorKind::Connection;
  error.status = Errc::SocketError;
  error.retryable = true;
  if (code == CURLE_OPERATION_TIMEDOUT) {
    error.kind = WebSocketErrorKind::Timeout;
    error.status = Errc::Timeout;
  } else if (code == CURLE_COULDNT_RESOLVE_HOST ||
             code == CURLE_COULDNT_RESOLVE_PROXY) {
    error.kind = WebSocketErrorKind::Resolve;
  } else if (code == CURLE_SSL_CONNECT_ERROR ||
             code == CURLE_PEER_FAILED_VERIFICATION ||
             code == CURLE_SSL_CERTPROBLEM ||
             code == CURLE_SSL_CIPHER) {
    error.kind = WebSocketErrorKind::Tls;
  } else if (code == CURLE_UNSUPPORTED_PROTOCOL ||
             code == CURLE_NOT_BUILT_IN) {
    error.kind = WebSocketErrorKind::Unavailable;
    error.status = Errc::NotConnected;
    error.retryable = false;
  } else if (code == CURLE_ABORTED_BY_CALLBACK) {
    error.kind = WebSocketErrorKind::Cancelled;
    error.status = Errc::Closed;
    error.retryable = false;
  } else if (code != CURLE_COULDNT_CONNECT && code != CURLE_GOT_NOTHING) {
    error.kind = WebSocketErrorKind::Io;
  }
  error.message = std::string(context) + ": " +
                  ((detail && *detail) ? detail : curl_easy_strerror(code));
  return error;
}

WebSocketEvent openEvent() {
  WebSocketEvent event;
  event.kind = WebSocketEventKind::Open;
  return event;
}

WebSocketEvent frameEvent(WebSocketFrameKind kind, std::string payload) {
  WebSocketEvent event;
  event.kind = WebSocketEventKind::Frame;
  event.frame = WebSocketFrame{kind, std::move(payload)};
  return event;
}

WebSocketEvent closeEvent(std::uint16_t code, std::string reason, bool clean) {
  WebSocketEvent event;
  event.kind = WebSocketEventKind::Close;
  event.close = WebSocketCloseInfo{code, std::move(reason), clean};
  return event;
}

WebSocketEvent errorEvent(WebSocketError error) {
  WebSocketEvent event;
  event.kind = WebSocketEventKind::Error;
  event.error = std::move(error);
  return event;
}

class CurlWebSocketConnection final
    : public IWebSocketConnection,
      public std::enable_shared_from_this<CurlWebSocketConnection> {
 public:
  explicit CurlWebSocketConnection(WebSocketConnectRequest request)
      : request_(std::move(request)) {}

  ~CurlWebSocketConnection() override {
    abortRequested_.store(true);
    queueCv_.notify_all();
    if (thread_.joinable()) {
      if (thread_.get_id() == std::this_thread::get_id()) {
        thread_.detach();
      } else {
        thread_.join();
      }
    }
  }

  void start(WebSocketEventCallback callback) override {
    {
      std::lock_guard lock(mutex_);
      if (started_) return;
      started_ = true;
      callback_ = std::move(callback);
    }
    auto self = shared_from_this();
    thread_ = std::thread([self = std::move(self)] { self->run(); });
  }

  Status send(WebSocketFrame frame) override {
    if (frame.payload.size() > request_.maxFrameBytes) {
      return Errc::BufferTooSmall;
    }
    std::lock_guard lock(mutex_);
    if (!started_ || finished_ || closing_) return Errc::NotConnected;
    constexpr std::size_t maxQueuedFrames = 64;
    const std::size_t maxQueuedBytes =
        request_.maxFrameBytes > std::numeric_limits<std::size_t>::max() / 4
            ? std::numeric_limits<std::size_t>::max()
            : request_.maxFrameBytes * 4;
    if (outbound_.size() >= maxQueuedFrames ||
        frame.payload.size() > maxQueuedBytes - queuedBytes_) {
      return Errc::BufferTooSmall;
    }
    queuedBytes_ += frame.payload.size();
    outbound_.push_back(
        Outbound{frame.kind, std::move(frame.payload), false, 1000});
    queueCv_.notify_all();
    return Errc::Ok;
  }

  void close(std::uint16_t code, std::string_view reason) override {
    {
      std::lock_guard lock(mutex_);
      if (finished_ || closing_) return;
      closing_ = true;
      if (!started_ || !opened_) {
        abortRequested_.store(true);
      } else {
        std::string payload;
        const std::string safeReason = safeCloseReason(reason);
        payload.reserve(2 + safeReason.size());
        payload.push_back(static_cast<char>((code >> 8U) & 0xffU));
        payload.push_back(static_cast<char>(code & 0xffU));
        payload += safeReason;
        queuedBytes_ += payload.size();
        outbound_.push_back(
            Outbound{WebSocketFrameKind::Text, std::move(payload), true, code});
      }
    }
    queueCv_.notify_all();
  }

 private:
  struct Outbound {
    WebSocketFrameKind kind;
    std::string payload;
    bool close;
    std::uint16_t closeCode;
    std::size_t offset = 0;
  };

  static std::size_t headerCallback(char* data, std::size_t size,
                                    std::size_t count, void* userdata) {
    const std::size_t length = size * count;
    auto* self = static_cast<CurlWebSocketConnection*>(userdata);
    std::string_view line(data, length);
    constexpr std::string_view header = "sec-websocket-protocol:";
    if (line.size() >= header.size() &&
        lowercase(line.substr(0, header.size())) == header) {
      std::string_view value = line.substr(header.size());
      while (!value.empty() &&
             (value.front() == ' ' || value.front() == '\t')) {
        value.remove_prefix(1);
      }
      while (!value.empty() &&
             (value.back() == '\r' || value.back() == '\n' ||
              value.back() == ' ' || value.back() == '\t')) {
        value.remove_suffix(1);
      }
      self->selectedSubprotocol_ = std::string(value);
    }
    return length;
  }

  static int progressCallback(void* userdata, curl_off_t, curl_off_t,
                              curl_off_t, curl_off_t) {
    return static_cast<CurlWebSocketConnection*>(userdata)
                   ->abortRequested_.load()
               ? 1
               : 0;
  }

  void run() {
    {
      std::lock_guard lock(mutex_);
      if (closing_) {
        finished_ = true;
        emitCloseAfterUnlock_ = true;
      }
    }
    if (emitCloseAfterUnlock_) {
      emit(closeEvent(1000, {}, true));
      return;
    }

    static std::once_flag globalInit;
    std::call_once(globalInit,
                   [] { (void)curl_global_init(CURL_GLOBAL_DEFAULT); });

    CURL* curl = curl_easy_init();
    if (!curl) {
      WebSocketError error;
      error.kind = WebSocketErrorKind::Unavailable;
      error.status = Errc::NotConnected;
      error.message = "Failed to initialize libcurl WebSocket handle";
      error.retryable = false;
      finishWithError(std::move(error));
      return;
    }

    std::array<char, CURL_ERROR_SIZE> errorBuffer{};
    curl_slist* headers = nullptr;
    // No subprotocol requested (the ck-exec gateway speaks none): send no header,
    // rather than an empty one.
    if (!request_.subprotocol.empty()) {
      const std::string protocolHeader =
          "Sec-WebSocket-Protocol: " + request_.subprotocol;
      headers = curl_slist_append(headers, protocolHeader.c_str());
    }
    curl_easy_setopt(curl, CURLOPT_URL, request_.url.c_str());
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_CONNECT_ONLY, 2L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS,
                     std::max<long>(0, request_.connectTimeoutMs));
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_PROTOCOLS_STR, "ws,wss");
    curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, errorBuffer.data());
    curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, headerCallback);
    curl_easy_setopt(curl, CURLOPT_HEADERDATA, this);
    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, progressCallback);
    curl_easy_setopt(curl, CURLOPT_XFERINFODATA, this);

    CURLcode result = curl_easy_perform(curl);
    curl_slist_free_all(headers);
    if (result != CURLE_OK) {
      const WebSocketError error =
          curlError(result, "WebSocket handshake failed", errorBuffer.data());
      curl_easy_cleanup(curl);
      finishWithError(error);
      return;
    }

    long responseCode = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &responseCode);
    if (responseCode != 101) {
      WebSocketError error;
      error.kind = WebSocketErrorKind::Protocol;
      error.status = Errc::Rejected;
      error.message = "WebSocket handshake returned HTTP " +
                      std::to_string(responseCode);
      error.retryable = responseCode >= 500;
      curl_easy_cleanup(curl);
      finishWithError(std::move(error));
      return;
    }
    if (selectedSubprotocol_ != request_.subprotocol) {
      WebSocketError error;
      error.kind = WebSocketErrorKind::Protocol;
      error.status = Errc::Malformed;
      error.message = request_.subprotocol.empty()
                          ? "Server selected a subprotocol that was not requested"
                          : "Server did not negotiate " + request_.subprotocol;
      error.retryable = false;
      curl_easy_cleanup(curl);
      finishWithError(std::move(error));
      return;
    }

    {
      std::lock_guard lock(mutex_);
      opened_ = true;
    }
    emit(openEvent());

    std::optional<Outbound> pending;
    std::array<char, 16 * 1024> receiveBuffer{};
    bool running = true;
    while (running && !abortRequested_.load()) {
      bool didWork = false;
      if (!pending) {
        std::lock_guard lock(mutex_);
        if (!outbound_.empty()) {
          queuedBytes_ -= outbound_.front().payload.size();
          pending = std::move(outbound_.front());
          outbound_.pop_front();
        }
      }

      if (pending) {
        std::size_t sent = 0;
        const unsigned int flags =
            pending->close ? static_cast<unsigned int>(CURLWS_CLOSE)
                           : sendFlags(pending->kind);
        result = curl_ws_send(
            curl, pending->payload.data() + pending->offset,
            pending->payload.size() - pending->offset, &sent, 0, flags);
        pending->offset += sent;
        didWork = sent > 0;
        if (result != CURLE_OK && result != CURLE_AGAIN) {
          finishWithError(curlError(result, "WebSocket send failed"));
          running = false;
        } else if (pending->offset == pending->payload.size()) {
          if (pending->close) {
            emit(closeEvent(pending->closeCode, {}, true));
            running = false;
          }
          pending.reset();
        }
      }
      if (!running) break;

      std::size_t received = 0;
      const curl_ws_frame* metadata = nullptr;
      result = curl_ws_recv(curl, receiveBuffer.data(), receiveBuffer.size(),
                            &received, &metadata);
      if (result == CURLE_OK && metadata) {
        didWork = true;
        if (!consumeChunk(metadata,
                          std::string_view(receiveBuffer.data(), received))) {
          running = false;
        }
      } else if (result == CURLE_GOT_NOTHING) {
        emit(closeEvent(1006, "connection closed", false));
        running = false;
      } else if (result != CURLE_AGAIN) {
        finishWithError(curlError(result, "WebSocket receive failed"));
        running = false;
      }

      if (running && !didWork) {
        std::unique_lock lock(mutex_);
        queueCv_.wait_for(lock, std::chrono::milliseconds(5), [this] {
          return abortRequested_.load() || !outbound_.empty();
        });
      }
    }

    curl_easy_cleanup(curl);
    {
      std::lock_guard lock(mutex_);
      finished_ = true;
      opened_ = false;
    }
  }

  static unsigned int sendFlags(WebSocketFrameKind kind) {
    switch (kind) {
      case WebSocketFrameKind::Text:
        return static_cast<unsigned int>(CURLWS_TEXT);
      case WebSocketFrameKind::Binary:
        return static_cast<unsigned int>(CURLWS_BINARY);
      case WebSocketFrameKind::Ping:
        return static_cast<unsigned int>(CURLWS_PING);
      case WebSocketFrameKind::Pong:
        return static_cast<unsigned int>(CURLWS_PONG);
    }
    return static_cast<unsigned int>(CURLWS_BINARY);
  }

  bool consumeChunk(const curl_ws_frame* metadata, std::string_view chunk) {
    if (metadata->bytesleft < 0) {
      return receiveProtocolFailure("libcurl returned invalid frame metadata");
    }
    const auto bytesLeft = static_cast<std::uint64_t>(metadata->bytesleft);
    const std::size_t available =
        request_.maxFrameBytes > frameBuffer_.size()
            ? request_.maxFrameBytes - frameBuffer_.size()
            : 0;
    if (chunk.size() > available ||
        bytesLeft > static_cast<std::uint64_t>(available - chunk.size())) {
      WebSocketError error;
      error.kind = WebSocketErrorKind::FrameTooLarge;
      error.status = Errc::BufferTooSmall;
      error.message = "WebSocket frame exceeded the configured limit";
      error.retryable = false;
      finishWithError(std::move(error));
      return false;
    }
    if (frameBuffer_.empty()) frameFlags_ = metadata->flags;
    frameBuffer_.append(chunk);
    if (metadata->bytesleft != 0) return true;

    std::string frame = std::move(frameBuffer_);
    frameBuffer_.clear();
    const int flags = frameFlags_;
    frameFlags_ = 0;
    if ((flags & CURLWS_CLOSE) != 0) {
      std::uint16_t code = 1005;
      std::string reason;
      if (frame.size() == 1) {
        return receiveProtocolFailure("Malformed WebSocket close frame");
      }
      if (frame.size() >= 2) {
        const auto first = static_cast<unsigned char>(frame[0]);
        const auto second = static_cast<unsigned char>(frame[1]);
        code = static_cast<std::uint16_t>(
            (static_cast<std::uint16_t>(first) << 8U) |
            static_cast<std::uint16_t>(second));
        reason = frame.substr(2);
      }
      if (!validUtf8(reason)) {
        return receiveProtocolFailure("WebSocket close reason is not UTF-8");
      }
      emit(closeEvent(code, std::move(reason), true));
      return false;
    }
    if ((flags & CURLWS_PING) != 0) {
      emit(frameEvent(WebSocketFrameKind::Ping, std::move(frame)));
      return true;
    }
    if ((flags & CURLWS_PONG) != 0) {
      emit(frameEvent(WebSocketFrameKind::Pong, std::move(frame)));
      return true;
    }

    const WebSocketFrameKind kind =
        (flags & CURLWS_TEXT) != 0 ? WebSocketFrameKind::Text
                                  : WebSocketFrameKind::Binary;
    if (assemblingMessage_ && kind != messageKind_) {
      return receiveProtocolFailure(
          "WebSocket fragmented message changed frame type");
    }
    if (!assemblingMessage_) messageKind_ = kind;
    if (frame.size() > request_.maxFrameBytes - messageBuffer_.size()) {
      WebSocketError error;
      error.kind = WebSocketErrorKind::FrameTooLarge;
      error.status = Errc::BufferTooSmall;
      error.message = "WebSocket message exceeded the configured limit";
      error.retryable = false;
      finishWithError(std::move(error));
      return false;
    }
    messageBuffer_ += frame;
    if ((flags & CURLWS_CONT) != 0) {
      assemblingMessage_ = true;
      return true;
    }

    emit(frameEvent(messageKind_, std::move(messageBuffer_)));
    messageBuffer_.clear();
    assemblingMessage_ = false;
    return true;
  }

  bool receiveProtocolFailure(std::string message) {
    WebSocketError error;
    error.kind = WebSocketErrorKind::Protocol;
    error.status = Errc::Malformed;
    error.message = std::move(message);
    error.retryable = false;
    finishWithError(std::move(error));
    return false;
  }

  void finishWithError(WebSocketError error) {
    {
      std::lock_guard lock(mutex_);
      if (finished_) return;
      finished_ = true;
      opened_ = false;
    }
    emit(errorEvent(std::move(error)));
  }

  void emit(WebSocketEvent event) {
    WebSocketEventCallback callback;
    {
      std::lock_guard lock(mutex_);
      callback = callback_;
    }
    if (callback) callback(std::move(event));
  }

  WebSocketConnectRequest request_;
  std::mutex mutex_;
  std::condition_variable queueCv_;
  WebSocketEventCallback callback_;
  std::deque<Outbound> outbound_;
  std::size_t queuedBytes_ = 0;
  std::thread thread_;
  std::atomic<bool> abortRequested_{false};
  bool started_ = false;
  bool opened_ = false;
  bool closing_ = false;
  bool finished_ = false;
  bool emitCloseAfterUnlock_ = false;
  std::string selectedSubprotocol_;
  std::string frameBuffer_;
  int frameFlags_ = 0;
  std::string messageBuffer_;
  WebSocketFrameKind messageKind_ = WebSocketFrameKind::Text;
  bool assemblingMessage_ = false;
};

class CurlWebSocketTransport final : public IWebSocketTransport {
 public:
  std::shared_ptr<IWebSocketConnection> createConnection(
      const WebSocketConnectRequest& request) override {
    if (request.url.empty() || request.maxFrameBytes == 0) return nullptr;
    return std::make_shared<CurlWebSocketConnection>(request);
  }
};

bool runtimeHasWebSockets() noexcept {
  const curl_version_info_data* info = curl_version_info(CURLVERSION_NOW);
  // curl 8.13 fixed the fragmented-message flag semantics this backend uses
  // to distinguish continuation frames from complete messages. Refuse a
  // mismatched older runtime even when compiled against newer headers.
  if (!info || info->version_num < 0x080d00 || !info->protocols) return false;
  bool ws = false;
  bool wss = false;
  for (const char* const* protocol = info->protocols; *protocol; ++protocol) {
    const std::string_view name(*protocol);
    ws = ws || name == "ws";
    wss = wss || name == "wss";
  }
  return ws && wss;
}

}  // namespace

std::shared_ptr<IWebSocketTransport> makeCurlWebSocketTransport() {
  if (!runtimeHasWebSockets()) return nullptr;
  return std::make_shared<CurlWebSocketTransport>();
}

bool curlWebSocketTransportAvailable() noexcept {
  return runtimeHasWebSockets();
}

}  // namespace crowdy::graphql
