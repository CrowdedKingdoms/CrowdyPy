"""Fleet sessions for an organization's own game servers (``client.fleet_sessions``, also
``client.admin.fleet_sessions``). Application API v2.89.0.

A fleet session is one realtime session a game server opens for itself, in which it carries
the players who are on it. A player still signs in once, when they join the game (the
``mintManagedPlayerToken`` mutation). A player who moves to another of the organization's
servers is carried by that server, with no call to the Application API. The three calls here
open a session, keep it and end it; the realtime session itself is in :mod:`crowdypy.fleet`.

**These calls take the organization's org token, on a server the organization runs.** Build a
client for the server and give it the org token (``cko_...``, scope ``fleet:session``) with
``client.set_token(org_token)``, without a ``token_store``. An org token and a fleet token
never go to a player's device or into a browser page: the API refuses a request that carries
an ``Origin`` header, and these methods refuse before any request is made where Python runs in
a browser page or on a phone.

**The org token that opened a session is the one that keeps it and ends it.** Another org
token of the same organization gets ``NOT_FOUND`` from :meth:`FleetSessionsAPI.refresh` and
``False`` from :meth:`FleetSessionsAPI.close`. Revoking an org token ends every fleet session
and every hub connection it opened. A session that is neither refreshed nor closed ends at its
``expires_at``, 24 hours after it was opened or last refreshed.

Refusals arrive as :class:`crowdypy.errors.CrowdyGraphQLError`; branch on its ``code``, with
the readers in :mod:`crowdypy.errors`:

- ``ORG_TOKEN_REQUIRED``: the bearer is not an org token (``org_token_required_of``).
- ``ORG_TOKEN_NOT_ALLOWED``: the request carried an ``Origin`` header.
- ``FLEET_SESSION_LIMIT``, on ``open``: the organization holds as many sessions as it may, 512
  unless an operator set another number (``fleet_session_limit_of``).
- ``NOT_FOUND``, on ``refresh``: the session expired, was closed, or was opened with another
  org token (``fleet_session_not_found_of``).
- ``PLATFORM_BUSY``: the platform could not start the call in time; wait ``retry_after_ms``,
  then call again (``platform_busy_of``).
- ``ORG_LEGAL_ACCEPTANCE_REQUIRED``: an admin has not accepted the current Org-Managed Players
  Addendum (``org_legal_acceptance_required_of``).
- ``FORBIDDEN``: fleet sessions are not switched on for the organization, or the org token
  lacks the ``fleet:session`` scope.
- ``SCOPE_MISSING``: the app is not in the org token's app list.
- ``FEATURE_DISABLED``: the tier does not serve org-managed players.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import msgspec

from crowdypy._generated import inputs
from crowdypy._generated import operations as ops
from crowdypy._server_only import assert_organization_server
from crowdypy.domains._base import Domain
from crowdypy.utils import bigint

__all__ = ["FleetServer", "FleetSessionAnswer", "FleetSessionsAPI"]


class FleetServer(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """The realtime server a fleet session connects to."""

    ip4: str | None = None
    ip6: str | None = None
    client_port: int
    server_id: str | None = None
    status: str | None = None
    clients: int | None = None


class FleetSessionAnswer(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A fleet session as the Application API answers it (``openFleetSession`` and
    ``refreshFleetSession``).

    **Keep the token on the server.** It signs for every player the session carries. It never
    goes to a player's device, and the Application API does not accept it as a bearer. The
    repr never shows it.
    """

    #: The session's token: 64 characters, the key the game server signs the session's
    #: realtime messages with.
    token: str
    #: The token's id: what ``refresh`` and ``close`` name the session by.
    game_token_id: str
    app_id: str
    #: When the session ends unless it is refreshed first (ISO 8601): 24 hours after it was
    #: opened or last refreshed.
    expires_at: str | None = None
    #: How many players the session may carry at once.
    max_players: int | None = None
    #: The realtime server to connect to.
    server: FleetServer

    def __repr__(self) -> str:
        return (
            f"FleetSessionAnswer(app_id={self.app_id!r}, game_token_id={self.game_token_id!r}, "
            f"expires_at={self.expires_at!r}, max_players={self.max_players!r}, "
            f"server={self.server!r}, token=<redacted>)"
        )


def _named(input: Any) -> Any:
    """A mapping's ``appId`` and ``gameTokenId`` as the decimal strings a BigInt travels as,
    so a session's own integers (``fleet.app_id``, ``fleet.game_token_id``) are taken as they
    are. A generated input struct is sent as given."""
    if not isinstance(input, Mapping):
        return input
    named = dict(input)
    for field in ("appId", "gameTokenId"):
        if field in named:
            named[field] = bigint(named[field])
    return named


class FleetSessionsAPI(Domain):
    """Open, keep and end fleet sessions with the organization's org token."""

    async def open(self, app_id: str | int) -> FleetSessionAnswer:
        """Open a fleet session in one of the organization's apps (``openFleetSession``), when
        the game server starts. Open one session for each game server. The answer carries the
        session's token, its ``game_token_id``, when it expires (24 hours away), how many
        players it may carry at once and the realtime server to connect to; give it to the
        session in :mod:`crowdypy.fleet`."""
        assert_organization_server("fleet_sessions.open")
        payload = await self._request(ops.OPEN_FLEET_SESSION, {"input": {"appId": bigint(app_id)}})
        return msgspec.convert(payload, FleetSessionAnswer)

    async def refresh(
        self, input: inputs.RefreshFleetSessionInput | Mapping[str, Any]
    ) -> FleetSessionAnswer:
        """Keep a fleet session (``refreshFleetSession``): the same token and ``game_token_id``,
        with ``expires_at`` moved to 24 hours from now. Call it with the org token that opened
        the session, before ``expires_at``. The session's ``refresh_due()`` says when, and its
        ``refresh_input()`` builds this input (``appId``, ``gameTokenId``, ``currentServer``).

        Name the realtime server the session is connected to (``currentServer``, the ``ip4``
        and ``clientPort`` it was given) and the session stays there while that server still
        serves the app. Leave it out, or name one that no longer serves, and the answer's
        ``server`` is another one to connect to: do that after a realtime server told the
        session to move.

        Raises :class:`crowdypy.errors.CrowdyGraphQLError` with ``NOT_FOUND`` for a session
        that expired, was closed, or was opened with another org token
        (:func:`crowdypy.errors.fleet_session_not_found_of`): open a new one."""
        assert_organization_server("fleet_sessions.refresh")
        payload = await self._request(ops.REFRESH_FLEET_SESSION, {"input": _named(input)})
        return msgspec.convert(payload, FleetSessionAnswer)

    async def close(self, input: inputs.CloseFleetSessionInput | Mapping[str, Any]) -> bool:
        """End a fleet session now (``closeFleetSession``), when its game server stops. The
        players it carries are released (their actors leave, as when a player's own session
        ends) and it no longer counts toward the organization's sessions. Call it with the
        org token that opened the session. ``True`` when a session was ended, and ``False``
        when there was none by that id for this org token: already closed, or opened with
        another org token."""
        assert_organization_server("fleet_sessions.close")
        return bool(await self._request(ops.CLOSE_FLEET_SESSION, {"input": _named(input)}))
