#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <string_view>
#include <utility>

#include "crowdy/core/clock.hpp"
#include "crowdy/domains/domain_base.hpp"
#include "crowdy/domains/types.hpp"
#include "crowdy/replication/fleet_session.hpp"

/// client.admin().fleetSessions(): the Application API's part of a fleet session
/// (Application API v2.89.0), for an organization's own game servers.
///
/// A fleet session is one realtime session a game server opens for itself, in which it
/// carries the players who are on it (crowdy/replication/fleet_session.hpp). These three
/// calls open it, keep it and end it. Each takes the organization's ORG TOKEN (`cko_...`,
/// scope `fleet:session`) as the client's bearer, from the server and never from a browser
/// or a player's device: build a client for the server and give it the org token with
/// CrowdyClient::setToken. Leave that client's ClientConfig::tokenStore unset, so the org
/// token is kept only where the server keeps its secrets. The fleet token an answer carries
/// signs the realtime session; it is not a bearer for this API.
///
/// **The org token that opened a session is the one that keeps and ends it.** Another org
/// token of the same organization gets NOT_FOUND from refresh() and `false` from close().
/// Revoking an org token ends every session it opened and closes the hub connections it
/// opened. A session that is neither refreshed nor closed ends at its `expiresAt`, 24 hours
/// after it was opened or last refreshed.
///
/// Refusals (GraphQL `extensions.code`, readers in crowdy/graphql/errors.hpp): the throwing
/// calls raise graphql::CrowdyGraphQLError with them and the async calls deliver them in the
/// outcome's `errors`.
///
/// | Code | Means | Reader |
/// |---|---|---|
/// | `ORG_TOKEN_REQUIRED` | the bearer is not an org token | `orgTokenRequiredOf` |
/// | `ORG_TOKEN_NOT_ALLOWED` | the request came from a browser (it carried an `Origin` header) | `errorWithCode` |
/// | `FLEET_SESSION_LIMIT` | open: the organization holds as many sessions as it may (512 unless an operator set another) | `fleetSessionLimitOf` |
/// | `NOT_FOUND` | refresh: the session expired, was closed, or was opened with another org token | `fleetSessionNotFoundOf` |
/// | `PLATFORM_BUSY` | the platform is shedding load; wait `retryAfterMs` and call again | `platformBusyOf` |
/// | `ORG_LEGAL_ACCEPTANCE_REQUIRED` | an admin has not accepted the current Org-Managed Players Addendum | `orgLegalAcceptanceRequiredOf` |
/// | `FORBIDDEN` | fleet sessions are not switched on for the organization, or the org token lacks the `fleet:session` scope | `errorWithCode` |
/// | `SCOPE_MISSING` | the app is not in the org token's app list | `errorWithCode` |
/// | `FEATURE_DISABLED` | the tier does not serve org-managed players | `errorWithCode` |
///
/// The documents are written here, as PortalAPI's are, because the published schema this
/// SDK generates its operations from does not carry the fields yet.
namespace crowdy::domains {

class FleetSessionsAPI : public DomainBase {
 public:
  using DomainBase::DomainBase;

  using Callback = std::function<void(graphql::GraphQLOutcome, FleetSessionResponse)>;
  using CloseCallback = std::function<void(graphql::GraphQLOutcome, bool closed)>;

  static constexpr std::string_view kOpenFleetSessionDocument =
      R"gql(mutation OpenFleetSession($input: OpenFleetSessionInput!) {
  openFleetSession(input: $input) {
    token
    gameTokenId
    appId
    expiresAt
    maxPlayers
    server {
      serverId
      ip4
      ip6
      clientPort
      status
      clients
    }
  }
})gql";

  static constexpr std::string_view kRefreshFleetSessionDocument =
      R"gql(mutation RefreshFleetSession($input: RefreshFleetSessionInput!) {
  refreshFleetSession(input: $input) {
    token
    gameTokenId
    appId
    expiresAt
    maxPlayers
    server {
      serverId
      ip4
      ip6
      clientPort
      status
      clients
    }
  }
})gql";

  static constexpr std::string_view kCloseFleetSessionDocument =
      R"gql(mutation CloseFleetSession($input: CloseFleetSessionInput!) {
  closeFleetSession(input: $input)
})gql";

  /// Open a fleet session in one of the organization's apps (`openFleetSession`), when the
  /// game server starts. The answer is the session's token and gameTokenId, when it expires
  /// (24 hours), how many players it may carry at once and the realtime server to connect
  /// to: fleetConfigOf() turns it into the wire class's configuration. Open one session for
  /// each game server.
  FleetSessionResponse open(std::string_view appId) const {
    return FleetSessionResponse::fromJson(execUnwrap(kOpenFleetSessionDocument, openVars(appId)));
  }
  void openAsync(std::string_view appId, Callback cb) const {
    execUnwrapAsync(kOpenFleetSessionDocument, openVars(appId), {}, parsed(std::move(cb)));
  }

  /// Keep a fleet session (`refreshFleetSession`): the same token and gameTokenId, its expiry
  /// moved to 24 hours from now. This form names no server, so the answer's `server` is a
  /// new one to connect to: use it after a realtime server told the session to move
  /// (replication::FleetSession::moveOrdered()). With the org token that opened the session.
  FleetSessionResponse refresh(std::string_view appId, std::string_view gameTokenId) const {
    return FleetSessionResponse::fromJson(
        execUnwrap(kRefreshFleetSessionDocument, sessionVars(appId, gameTokenId)));
  }
  void refreshAsync(std::string_view appId, std::string_view gameTokenId, Callback cb) const {
    execUnwrapAsync(kRefreshFleetSessionDocument, sessionVars(appId, gameTokenId), {},
                    parsed(std::move(cb)));
  }

  /// The same, naming the realtime server the session is connected to (the `ip4` and
  /// `clientPort` it was given). The session stays there while that server still serves the
  /// app; otherwise the answer's `server` is another one to connect to.
  FleetSessionResponse refresh(std::string_view appId, std::string_view gameTokenId,
                               std::string_view currentIp4, int currentClientPort) const {
    return FleetSessionResponse::fromJson(execUnwrap(
        kRefreshFleetSessionDocument,
        sessionVars(appId, gameTokenId, currentIp4, currentClientPort)));
  }
  void refreshAsync(std::string_view appId, std::string_view gameTokenId,
                    std::string_view currentIp4, int currentClientPort, Callback cb) const {
    execUnwrapAsync(kRefreshFleetSessionDocument,
                    sessionVars(appId, gameTokenId, currentIp4, currentClientPort), {},
                    parsed(std::move(cb)));
  }

  /// Refresh the session a replication::FleetSession holds: its app, its gameTokenId and the
  /// server it is on, which is left out when the session was told to move. Pass the answer
  /// to applyFleetRefresh().
  FleetSessionResponse refresh(const replication::FleetSession& session) const {
    return FleetSessionResponse::fromJson(
        execUnwrap(kRefreshFleetSessionDocument, sessionVars(session)));
  }
  void refreshAsync(const replication::FleetSession& session, Callback cb) const {
    execUnwrapAsync(kRefreshFleetSessionDocument, sessionVars(session), {},
                    parsed(std::move(cb)));
  }

  /// End a fleet session now (`closeFleetSession`), when its game server stops: the realtime
  /// servers release the players it carries, and it no longer counts toward the
  /// organization's sessions. True when a session was ended; false when there was none by
  /// that id for this org token (already closed, or opened with another org token).
  bool close(std::string_view appId, std::string_view gameTokenId) const {
    return execUnwrap(kCloseFleetSessionDocument, sessionVars(appId, gameTokenId)).asBool();
  }
  void closeAsync(std::string_view appId, std::string_view gameTokenId, CloseCallback cb) const {
    execUnwrapAsync(kCloseFleetSessionDocument, sessionVars(appId, gameTokenId), {},
                    [cb = std::move(cb)](graphql::GraphQLOutcome out) mutable {
                      const bool closed = out.ok() && out.data.asBool();
                      cb(std::move(out), closed);
                    });
  }
  /// End the session a replication::FleetSession holds. Close the wire session too
  /// (replication::FleetSession::close()).
  bool close(const replication::FleetSession& session) const {
    return close(std::to_string(session.appId()), std::to_string(session.token().gameTokenId));
  }
  void closeAsync(const replication::FleetSession& session, CloseCallback cb) const {
    closeAsync(std::to_string(session.appId()), std::to_string(session.token().gameTokenId),
               std::move(cb));
  }

 private:
  static graphql::JVal openVars(std::string_view appId) {
    graphql::JVal vars;
    vars["input"]["appId"] = appId;
    return vars;
  }
  static graphql::JVal sessionVars(std::string_view appId, std::string_view gameTokenId) {
    graphql::JVal vars;
    vars["input"]["appId"] = appId;
    vars["input"]["gameTokenId"] = gameTokenId;
    return vars;
  }
  static graphql::JVal sessionVars(std::string_view appId, std::string_view gameTokenId,
                                   std::string_view currentIp4, int currentClientPort) {
    graphql::JVal vars = sessionVars(appId, gameTokenId);
    vars["input"]["currentServer"]["ip4"] = currentIp4;
    vars["input"]["currentServer"]["clientPort"] = currentClientPort;
    return vars;
  }
  static graphql::JVal sessionVars(const replication::FleetSession& session) {
    const std::string appId = std::to_string(session.appId());
    const std::string gameTokenId = std::to_string(session.token().gameTokenId);
    // A session told to move asks for a new server by naming none.
    if (session.moveOrdered()) return sessionVars(appId, gameTokenId);
    const replication::Assignment server = session.server();
    return sessionVars(appId, gameTokenId, server.ip4, server.clientPort);
  }
  static graphql::GraphQLCallback parsed(Callback cb) {
    return [cb = std::move(cb)](graphql::GraphQLOutcome out) mutable {
      FleetSessionResponse answer;
      if (out.ok()) answer = FleetSessionResponse::fromJson(out.data);
      cb(std::move(out), answer);
    };
  }
};

/// The fleet token of an answer as the wire class takes it: the token, its gameTokenId and
/// its expiry in epoch millis. An empty token when the answer is not a usable one (a token
/// that is not 64 characters, or a gameTokenId that is not a decimal int64), as a failed
/// call's default answer is.
inline replication::TokenInfo fleetTokenOf(const FleetSessionResponse& answer) {
  replication::TokenInfo info;
  const auto id = answer.gameTokenIdInt64();
  if (answer.token.size() != wire::kTokenOctets || !id) return info;
  info.token = answer.token;
  info.gameTokenId = *id;
  info.expiresAtEpochMs =
      answer.expiresAt.empty()
          ? 0
          : core::parseIso8601Millis(answer.expiresAt.data(), answer.expiresAt.size());
  return info;
}

/// The realtime server of an answer as the wire class takes it.
inline replication::Assignment fleetServerOf(const FleetSessionResponse& answer) {
  return replication::Assignment{answer.server.ip4, answer.server.ip6, answer.server.clientPort};
}

/// The configuration of a replication::FleetSession for a session `openFleetSession`
/// answered: `base` (your transport options, handlers of a move, carry timings) with the
/// app, the fleet token and the realtime server taken from the answer, and `carryBurst`
/// raised to the answer's `maxPlayers`.
///
///   auto opened = client.admin().fleetSessions().open(appId);
///   replication::FleetSession fleet(fleetConfigOf(opened).value(), crypto);
///   fleet.open();
///
/// Errc::InvalidArgument for an answer without a usable token, app or server.
inline Result<replication::FleetConfig> fleetConfigOf(const FleetSessionResponse& opened,
                                                      replication::FleetConfig base = {}) {
  const replication::TokenInfo token = fleetTokenOf(opened);
  const auto appId = opened.appIdInt64();
  const replication::Assignment server = fleetServerOf(opened);
  if (token.token.empty() || !appId || (server.ip4.empty() && server.ip6.empty()) ||
      server.clientPort <= 0)
    return Errc::InvalidArgument;
  base.appId = *appId;
  base.token = token;
  base.server = server;
  // The server lets a session send its `maxPlayers` + 32 carries at once, so one that moves
  // to another realtime server can carry all its players again without waiting on the rate.
  if (opened.maxPlayers > base.carryBurst) base.carryBurst = opened.maxPlayers;
  return base;
}

/// Give a live session the answer of `refreshFleetSession`: the new expiry, and the realtime
/// server. When the server is the one the session is on, its carries stand: the session
/// sends its CLIENT_CAPABILITIES again and no FLEET_CARRY. When it is another one, the
/// session reconnects there, sends its CLIENT_CAPABILITIES and carries every player again
/// (each is carried again only when its result arrives).
///
///   if (fleet.refreshDue())
///     applyFleetRefresh(fleet, client.admin().fleetSessions().refresh(fleet));
///
/// Errc::InvalidArgument, with nothing changed, for an answer that is not usable or is
/// another session's (a different app or gameTokenId): a refresh keeps the token and its id.
inline Status applyFleetRefresh(replication::FleetSession& session,
                                const FleetSessionResponse& refreshed) {
  const replication::TokenInfo token = fleetTokenOf(refreshed);
  if (token.token.empty() || refreshed.appIdInt64() != session.appId() ||
      token.gameTokenId != session.token().gameTokenId)
    return Errc::InvalidArgument;
  return session.applyRefresh(token, fleetServerOf(refreshed));
}

}  // namespace crowdy::domains
