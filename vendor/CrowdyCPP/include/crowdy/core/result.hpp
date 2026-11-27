#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <utility>
#include <variant>

/// Exception-free result type used on hot paths (the replication layer never
/// throws). The GraphQL layer uses structured exceptions instead — see
/// crowdy/graphql/errors.hpp.
namespace crowdy {

enum class Errc {
  Ok = 0,
  InvalidArgument,
  BufferTooSmall,
  Malformed,
  HmacMismatch,
  NotConnected,
  SocketError,
  Timeout,
  TokenExpired,
  Rejected,
  Closed,
  CryptoUnavailable,
  /// Transient: the operation could not proceed right now (a full kernel send
  /// buffer is the usual cause) and nothing was transferred. The socket is
  /// healthy — retry shortly. Distinct from SocketError, which is a fault.
  WouldBlock,
  /// The caller cancelled the request (graphql::CancelToken). Not a fault and
  /// not a timeout: nothing went wrong, nobody wanted the answer any more.
  Cancelled,
  /// A response was larger than the client accepts (0.85.0): its body, decompressed,
  /// passed HttpRequest::maxResponseBytes. Nothing of it was parsed.
  ResponseTooLarge,
  /// A FleetSession send named a player the session does not carry now (never carried,
  /// released, carried by another session, or a carry not yet confirmed). Nothing was sent.
  NotCarried,
};

inline const char* errcName(Errc e) {
  switch (e) {
    case Errc::Ok: return "Ok";
    case Errc::InvalidArgument: return "InvalidArgument";
    case Errc::BufferTooSmall: return "BufferTooSmall";
    case Errc::Malformed: return "Malformed";
    case Errc::HmacMismatch: return "HmacMismatch";
    case Errc::NotConnected: return "NotConnected";
    case Errc::SocketError: return "SocketError";
    case Errc::Timeout: return "Timeout";
    case Errc::TokenExpired: return "TokenExpired";
    case Errc::Rejected: return "Rejected";
    case Errc::Closed: return "Closed";
    case Errc::CryptoUnavailable: return "CryptoUnavailable";
    case Errc::WouldBlock: return "WouldBlock";
    case Errc::Cancelled: return "Cancelled";
    case Errc::ResponseTooLarge: return "ResponseTooLarge";
    case Errc::NotCarried: return "NotCarried";
  }
  return "Unknown";
}

/// Result<void> analog: just an error code, Errc::Ok means success.
struct Status {
  Errc code = Errc::Ok;
  constexpr Status() = default;
  constexpr Status(Errc c) : code(c) {}
  constexpr bool ok() const { return code == Errc::Ok; }
  constexpr explicit operator bool() const { return ok(); }
};

/// Minimal expected-like carrier for hot paths.
template <typename T>
class Result {
 public:
  Result(T value) : value_(std::move(value)) {}
  Result(Errc code) : value_(code) {}

  bool ok() const { return std::holds_alternative<T>(value_); }
  explicit operator bool() const { return ok(); }

  T& value() { return std::get<T>(value_); }
  const T& value() const { return std::get<T>(value_); }
  T* operator->() { return &value(); }
  const T* operator->() const { return &value(); }

  Errc error() const { return ok() ? Errc::Ok : std::get<Errc>(value_); }

 private:
  std::variant<T, Errc> value_;
};

/// The longest the SDK waits because a server asked it to, in milliseconds (0.95.0). The
/// platform's own hints (`retryAfterMs` on a GraphQL refusal, "retry in N ms" from a hub
/// gateway) run from 200 ms to 10 s. 30 s is three times the longest of them, so a hint the
/// platform sends is never shortened and a longer one has room, and it is half of the 60 s a
/// single API request may take by default, so no hint holds a retry longer than a request
/// itself can. Without a ceiling, whoever answers for the server decides how long a connect,
/// a build wait or a hub's calls stand still.
inline constexpr std::int64_t kMaxRetryAfterWaitMs = 30'000;

/// What the SDK waits on a server's hint of `hintMs`: nothing for a hint of 0 or below, the
/// hint up to kMaxRetryAfterWaitMs, and kMaxRetryAfterWaitMs for anything longer. Every wait
/// the SDK makes on a server's word goes through this; use it before you wait on a hint
/// yourself (graphql::platformBusyOf, GraphQLErrorDetail::retryAfterMs).
inline constexpr std::int64_t retryWaitMs(std::int64_t hintMs) noexcept {
  return hintMs <= 0 ? 0 : hintMs > kMaxRetryAfterWaitMs ? kMaxRetryAfterWaitMs : hintMs;
}

/// The most of a server's text the SDK puts in a log line or a failure message.
inline constexpr std::size_t kServerTextMaxChars = 500;

/// Text a server sent, made fit to log or to put in a message (0.95.0): it is not the SDK's
/// own, so it is taken as untrusted. Every control character and every run of whitespace
/// becomes one space (a line break in it cannot start a log line of its own), the ends are
/// trimmed, and it is cut at `maxChars` bytes, never inside a UTF-8 character.
inline std::string serverText(std::string_view text, std::size_t maxChars = kServerTextMaxChars) {
  std::string out;
  out.reserve(text.size() < maxChars ? text.size() : maxChars);
  bool cut = false;
  for (const char c : text) {
    const auto byte = static_cast<unsigned char>(c);
    const bool blank = byte <= 0x20 || byte == 0x7f;
    if (blank && (out.empty() || out.back() == ' ')) continue;
    if (out.size() >= maxChars) {
      cut = true;
      break;
    }
    out.push_back(blank ? ' ' : c);
  }
  if (cut) {
    // The cut may have left the first bytes of a UTF-8 character: take them off.
    std::size_t lead = out.size();
    std::size_t following = 0;
    while (lead > 0 && following < 3 && (static_cast<unsigned char>(out[lead - 1]) & 0xC0) == 0x80) {
      --lead;
      ++following;
    }
    if (lead > 0) {
      const auto first = static_cast<unsigned char>(out[lead - 1]);
      const std::size_t length = first >= 0xF0 ? 4 : first >= 0xE0 ? 3 : first >= 0xC0 ? 2 : 1;
      if (length > 1 && following + 1 < length) out.resize(lead - 1);
    }
  }
  while (!out.empty() && out.back() == ' ') out.pop_back();
  return out;
}

/// Why the Application API did not do what a connection asked of it (0.95.0): a realtime
/// server assignment (replication::AssignmentFailure) or a hub connection's connect call
/// (domains::ExecConnectFailure).
///
/// The call itself answers one Errc for every such failure. This keeps what the API said:
/// its refusal code and, when it named one, how long it asked the caller to wait.
/// PLATFORM_BUSY with a `retryAfterMs` means "wait that long and ask again"; any other code
/// is a refusal that the same call meets again until its cause is dealt with.
inline constexpr std::size_t kApiFailureCodeMaxChars = 64;

struct ApiFailure {
  /// The Errc the call answered; Errc::Ok when the last one succeeded, or none was made.
  Errc error = Errc::Ok;
  /// The Application API's refusal code (GraphQL `extensions.code`), such as "PLATFORM_BUSY";
  /// empty when the failure carried none (no answer of the API's at all, an answer that
  /// names no server, a socket that would not open). At most kApiFailureCodeMaxChars.
  std::string code;
  /// How long the Application API asked the caller to wait before asking again, in
  /// milliseconds; 0 when it named no wait. The SDK fills it through retryWaitMs(), so it is
  /// never below 0 or above kMaxRetryAfterWaitMs (30 s), whatever the answer said.
  std::int64_t retryAfterMs = 0;
  /// The Application API's message, or the transport's: a server's text, as serverText()
  /// leaves it (one line, at most kServerTextMaxChars). Show it; do not act on it.
  std::string message;

  bool failed() const { return error != Errc::Ok; }
  /// The platform was shedding load: wait `retryAfterMs`, then ask again.
  bool platformBusy() const { return code == "PLATFORM_BUSY"; }
};

/// `why` with its server-given parts bounded as the SDK's own are: the code and the message
/// through serverText(), the wait through retryWaitMs(). For a failure that a session
/// provider or a dialer of your own filled in.
inline ApiFailure bounded(ApiFailure why) {
  why.code = serverText(why.code, kApiFailureCodeMaxChars);
  why.message = serverText(why.message);
  why.retryAfterMs = retryWaitMs(why.retryAfterMs);
  return why;
}

}  // namespace crowdy
