#include <curl/curl.h>

#include <mutex>

#include "crowdy/graphql/errors.hpp"
#include "crowdy/graphql/http.hpp"

namespace crowdy::graphql {

namespace {

/// The body as it arrives, decompressed, and where it stops.
struct BodySink {
  std::string body;
  std::size_t max = kDefaultMaxResponseBytes;
  bool tooLarge = false;
};

std::size_t writeCallback(char* data, std::size_t size, std::size_t nmemb, void* userdata) {
  auto* sink = static_cast<BodySink*>(userdata);
  const std::size_t n = size * nmemb;
  // A compressed body can expand far past what was sent: count what libcurl hands over.
  if (n > sink->max - sink->body.size()) {
    sink->tooLarge = true;
    return 0;  // CURLE_WRITE_ERROR
  }
  sink->body.append(data, n);
  return n;
}

/// Requests and redirects use http or https only, and a redirect from https stays on it.
void limitProtocols(CURL* curl, const std::string& url) {
  const bool secure = url.rfind("https://", 0) == 0;
#if LIBCURL_VERSION_NUM >= 0x075500  // 7.85.0
  curl_easy_setopt(curl, CURLOPT_PROTOCOLS_STR, "http,https");
  curl_easy_setopt(curl, CURLOPT_REDIR_PROTOCOLS_STR, secure ? "https" : "http,https");
#else
  curl_easy_setopt(curl, CURLOPT_PROTOCOLS, static_cast<long>(CURLPROTO_HTTP | CURLPROTO_HTTPS));
  curl_easy_setopt(curl, CURLOPT_REDIR_PROTOCOLS,
                   static_cast<long>(secure ? CURLPROTO_HTTPS : (CURLPROTO_HTTP | CURLPROTO_HTTPS)));
#endif
  curl_easy_setopt(curl, CURLOPT_MAXREDIRS, 5L);
}

/// libcurl calls this about once a second even while the transfer is idle, so a
/// cancelled request ends within a second, waiting on the server or not.
int cancelCallback(void* userdata, curl_off_t, curl_off_t, curl_off_t, curl_off_t) {
  return static_cast<const CancelToken*>(userdata)->cancelled() ? 1 : 0;
}

/// libcurl transport with a shared in-memory cookie jar (sticky
/// load-balancer cookies persist across requests within one transport).
class CurlTransport final : public IHttpTransport {
 public:
  CurlTransport() {
    static std::once_flag globalInit;
    std::call_once(globalInit, [] { curl_global_init(CURL_GLOBAL_DEFAULT); });
    share_ = curl_share_init();
    curl_share_setopt(share_, CURLSHOPT_SHARE, CURL_LOCK_DATA_COOKIE);
    curl_share_setopt(share_, CURLSHOPT_LOCKFUNC, lockCb);
    curl_share_setopt(share_, CURLSHOPT_UNLOCKFUNC, unlockCb);
    curl_share_setopt(share_, CURLSHOPT_USERDATA, this);
  }

  ~CurlTransport() override {
    if (share_) curl_share_cleanup(share_);
  }

  HttpResponse send(const HttpRequest& request) override {
    HttpOutcome outcome = perform(request);
#ifndef CROWDY_NO_EXCEPTIONS
    if (outcome.status.code == Errc::Timeout) {
      throw CrowdyTimeoutError(outcome.errorMessage);
    }
    if (outcome.status.code == Errc::Cancelled) {
      throw CrowdyCancelledError(outcome.errorMessage);
    }
    if (outcome.status.code == Errc::ResponseTooLarge) {
      throw CrowdyProtocolError(outcome.errorMessage);
    }
    if (!outcome.status.ok()) {
      throw CrowdyNetworkError(outcome.errorMessage);
    }
#endif
    return outcome.status.ok() ? std::move(outcome.response) : HttpResponse{};
  }

  HttpOutcome sendOutcome(const HttpRequest& request) noexcept override {
    return perform(request);
  }

 private:
  HttpOutcome perform(const HttpRequest& request) noexcept {
    HttpOutcome outcome;
    if (request.cancel && request.cancel->cancelled()) {
      outcome.status = Errc::Cancelled;
      outcome.errorMessage = "HTTP request cancelled: " + request.url;
      return outcome;
    }
    CURL* curl = curl_easy_init();
    if (!curl) {
      outcome.status = Errc::SocketError;
      outcome.errorMessage = "failed to initialize HTTP handle";
      return outcome;
    }

    BodySink sink;
    sink.max = request.maxResponseBytes;
    curl_slist* headers = nullptr;
    for (const auto& [k, v] : request.headers) {
      headers = curl_slist_append(headers, (k + ": " + v).c_str());
    }

    curl_easy_setopt(curl, CURLOPT_URL, request.url.c_str());
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    if (request.method == "POST") {
      curl_easy_setopt(curl, CURLOPT_POSTFIELDS, request.body.c_str());
      curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(request.body.size()));
    } else {
      curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, request.method.c_str());
      if (!request.body.empty()) {
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, request.body.c_str());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(request.body.size()));
      }
    }
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &sink);
    // Refused before the body is read when the server declares a larger one.
    curl_easy_setopt(curl, CURLOPT_MAXFILESIZE_LARGE, static_cast<curl_off_t>(sink.max));
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, request.timeoutMs);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    limitProtocols(curl, request.url);
    curl_easy_setopt(curl, CURLOPT_COOKIEFILE, "");  // enable the cookie engine
    curl_easy_setopt(curl, CURLOPT_SHARE, share_);
    curl_easy_setopt(curl, CURLOPT_ACCEPT_ENCODING, "");
    if (request.cancel) {
      curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
      curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, cancelCallback);
      curl_easy_setopt(curl, CURLOPT_XFERINFODATA, &*request.cancel);
    }

    const CURLcode rc = curl_easy_perform(curl);
    long status = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);

    if (rc == CURLE_OPERATION_TIMEDOUT) {
      outcome.status = Errc::Timeout;
      outcome.errorMessage = "HTTP request timed out: " + request.url;
      return outcome;
    }
    if (rc == CURLE_ABORTED_BY_CALLBACK) {
      outcome.status = Errc::Cancelled;
      outcome.errorMessage = "HTTP request cancelled: " + request.url;
      return outcome;
    }
    if (sink.tooLarge || rc == CURLE_FILESIZE_EXCEEDED) {
      outcome.status = Errc::ResponseTooLarge;
      outcome.errorMessage = "HTTP response larger than " + std::to_string(sink.max) +
                             " bytes: " + request.url;
      return outcome;
    }
    if (rc != CURLE_OK) {
      outcome.status = Errc::SocketError;
      outcome.errorMessage =
          std::string("HTTP request failed: ") + curl_easy_strerror(rc);
      return outcome;
    }
    outcome.status = Errc::Ok;
    outcome.response =
        HttpResponse{static_cast<int>(status), std::move(sink.body)};
    return outcome;
  }

  static void lockCb(CURL*, curl_lock_data, curl_lock_access, void* userdata) {
    static_cast<CurlTransport*>(userdata)->shareMutex_.lock();
  }
  static void unlockCb(CURL*, curl_lock_data, void* userdata) {
    static_cast<CurlTransport*>(userdata)->shareMutex_.unlock();
  }

  CURLSH* share_ = nullptr;
  std::mutex shareMutex_;
};

}  // namespace

std::shared_ptr<IHttpTransport> makeCurlTransport() { return std::make_shared<CurlTransport>(); }

}  // namespace crowdy::graphql
