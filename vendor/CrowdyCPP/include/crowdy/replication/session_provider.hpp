#pragma once

#include "crowdy/core/result.hpp"
#include "crowdy/replication/types.hpp"

namespace crowdy::replication {

/// Bridges the replication client to the GraphQL plane without a compile
/// dependency on it. CrowdyClient installs an implementation backed by
/// serverStatus().serverWithLeastClients() and portal().refresh(); tests can
/// stub it.
///
/// Implementations may block (they run HTTP requests) and are called from
/// the replication client's connect path (net thread in owned-thread mode).
class ISessionProvider {
 public:
  virtual ~ISessionProvider() = default;

  /// Assign (or re-assign) a replication server. Side effect on the Game
  /// API: installs this client's UDP session on the chosen server.
  virtual Result<Assignment> assignServer() = 0;

  /// assignServer(), also saying why when it fails (0.95.0): the Application API's refusal
  /// code and `retryAfterMs`, which a caller needs to tell "busy, try again in a moment"
  /// from a refusal. The connection calls this one. The default forwards to assignServer()
  /// and reports its error alone, so existing providers keep compiling.
  virtual Result<Assignment> assignServerOrSayWhy(AssignmentFailure& why) {
    Result<Assignment> assigned = assignServer();
    if (!assigned.ok()) why.error = assigned.error();
    return assigned;
  }

  /// Rotate the app-scoped token (refreshAppToken). Returns the new token
  /// material; the implementation must also update its GraphQL bearer.
  ///
  /// `current` names the replication server the client is connected to (null
  /// when it has none). An implementation that passes it to the Game API as
  /// `currentServer` gets the new token installed THERE and sets
  /// `TokenInfo::authorizedOnCurrentServer`; the connection then keeps its
  /// socket and switches tokens instead of re-placing. The default forwards to
  /// the older no-argument form so existing providers keep compiling; they
  /// simply never report the token as authorized, and the connection re-assigns
  /// after every refresh as before.
  virtual Result<TokenInfo> refreshToken(const Assignment* current) {
    (void)current;
    return refreshToken();
  }
  virtual Result<TokenInfo> refreshToken() = 0;
};

}  // namespace crowdy::replication
