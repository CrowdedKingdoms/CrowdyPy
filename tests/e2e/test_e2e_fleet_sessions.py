"""Fleet sessions against a tier: two game servers of one organization, four of its players,
the realtime servers and the Application API for real. Modelled on CrowdyJS's
``test/e2e/fleet-sessions.test.mjs``.

This test plays the organization's servers. It opens two fleet sessions with the org token,
signs four managed players in (the SDK has no wrapper for that on purpose, so the test makes
the GraphQL request itself, as an organization's server does), carries them, sends and
receives for them, sends their keepalives, moves one player from one server to the other,
refreshes a session, refreshes a carried player's token, and ends everything it opened.

It runs only when all three of these are set, so it never runs by accident (the variables
CrowdyJS's and CrowdyCPP's fleet suites read):

- ``CROWDY_FLEET_API_URL``: the Application API's origin.
- ``CROWDY_FLEET_APP_ID``: an app of the organization: managed players and fleet sessions
  switched on, text chat granted.
- ``CROWDY_FLEET_ORG_TOKEN``: an org token (``cko_...``) with the scopes ``players:mint`` and
  ``fleet:session``.

Optional: ``CROWDY_FLEET_HUB_APP_ID`` (also connect to hubs as the server, with
``CROWDY_FLEET_HUB_NODE_TYPE``, ``CROWDY_FLEET_HUB_KEY`` and ``CROWDY_FLEET_HUB_METHOD``, and
with ``CROWDY_FLEET_HUB_ORG_TOKEN`` when the hub's app takes another org token than the
session's), and ``CROWDY_FLEET_CHUNK`` (where the players stand, ``7200,7200,7200`` unless
set).

What it leaves on the tier: four managed players, ``crowdypy-fleet-e2e-0`` to ``-3``, which
every run reuses and none deletes; their sign-ins, which last 10 minutes; and the usage of a
few hundred datagrams. Both fleet sessions are closed and every player is released, whether
the steps passed or not. It changes no setting of the organization or the app.

No token is ever printed. No assertion takes a token as an operand, and a failure of a
request names its code and never its variables.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import pytest

import crowdypy
from crowdypy.errors import CrowdyGraphQLError, fleet_session_not_found_of
from crowdypy.fleet import (
    AsyncFleetSession,
    FleetCarryOutcome,
    FleetCarryResult,
    FleetNotCarriedError,
    FleetPlayerToken,
    FleetReleased,
)
from crowdypy.graphql import graphql_endpoint
from crowdypy.replication import KEEPALIVE_ANSWER_WAIT_MS, ActorNotPresent, ConnState, Notification
from crowdypy.wire import ErrorCode

#: What tests/e2e/conftest.py requires of this module, in place of the CROWDY_E2E_* variables.
E2E_REQUIRES = ("CROWDY_FLEET_API_URL", "CROWDY_FLEET_APP_ID", "CROWDY_FLEET_ORG_TOKEN")

EXTERNAL_IDS = [f"crowdypy-fleet-e2e-{n}" for n in range(4)]
SIGN_IN_SECONDS = 600
DAY_MS = 24 * 3_600_000

# Every call that answers an app token names the user it signs in (Application API v2.90.0).
_MINT = (
    "mutation($i: MintManagedPlayerTokenInput!) "
    "{ mintManagedPlayerToken(input: $i) { token gameTokenId userId appId expiresAt } }"
)
_REFRESH = "mutation { refreshAppToken { token gameTokenId userId appId expiresAt } }"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Player:
    external_id: str
    token: str = field(repr=False)
    game_token_id: int
    uuid: str
    #: The user the sign-in's answer named; the carry's result names the same.
    user_id: int


@dataclass
class GameServer:
    """One fleet session, and everything it was told."""

    name: str
    fleet: AsyncFleetSession
    actors: list[tuple[Notification, tuple[int, ...] | None]] = field(default_factory=list)
    texts: list[tuple[Notification, tuple[int, ...] | None]] = field(default_factory=list)
    errors: list[tuple[int, Any, bool]] = field(default_factory=list)
    keepalive_refusals: list[int] = field(default_factory=list)
    not_present: list[ActorNotPresent] = field(default_factory=list)
    released: list[FleetReleased] = field(default_factory=list)
    carry_results: list[FleetCarryResult] = field(default_factory=list)
    session_released: list[int] = field(default_factory=list)
    moves: int = 0
    failures: list[str] = field(default_factory=list)

    def handlers(self) -> dict[str, Any]:
        def moved() -> None:
            self.moves += 1

        def refused(n: Notification, verified: bool) -> None:
            self.errors.append((n.sequence, n.error_code, verified))
            if n.keepalive:
                self.keepalive_refusals.append(n.sequence)

        return {
            "actor_update": lambda n, players: self.actors.append((n, players)),
            "text": lambda n, players: self.texts.append((n, players)),
            "generic_error": refused,
            "not_present": self.not_present.append,
            "carry_result": self.carry_results.append,
            "released": self.released.append,
            "session_released": self.session_released.append,
            "move_ordered": moved,
            "error": lambda exc: self.failures.append(type(exc).__name__),
        }


async def until(what: str, check: Any, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        await asyncio.sleep(0.02)
    if not check():
        raise AssertionError(f"timed out after {seconds:g} s waiting for {what}")


async def test_fleet_sessions_on_a_tier() -> None:
    api_url = _env("CROWDY_FLEET_API_URL")
    app_id = _env("CROWDY_FLEET_APP_ID")
    endpoint = graphql_endpoint(api_url) or api_url
    chunk = tuple(int(v) for v in _env("CROWDY_FLEET_CHUNK", "7200,7200,7200").split(","))
    assert len(chunk) == 3

    # ---- the Application API, with every request counted ----
    requests = 0

    async def count(request: httpx.Request) -> None:
        nonlocal requests
        requests += 1

    http = httpx.AsyncClient(timeout=60.0, event_hooks={"request": [count]})
    api = crowdypy.AsyncCrowdyClient(http_url=api_url, http_client=http)
    # The org token stays in this process: no token store, and it is never an assertion's
    # operand.
    api.set_token(_env("CROWDY_FLEET_ORG_TOKEN"))

    async def as_bearer(bearer: str, query: str, variables: dict[str, Any] | None = None) -> Any:
        """One GraphQL request with ``bearer``. A refusal names its code, never its input."""
        res = await http.post(
            endpoint,
            json={"query": query, "variables": variables or {}},
            headers={"authorization": f"Bearer {bearer}"},
        )
        body = res.json()
        if body.get("errors"):
            code = (body["errors"][0].get("extensions") or {}).get("code")
            raise AssertionError(f"the Application API refused the request: {code}")
        return body["data"]

    async def sign_in(external_id: str) -> Player:
        """What an organization's server does: sign one of its players in with its org token."""
        data = await as_bearer(
            _env("CROWDY_FLEET_ORG_TOKEN"),
            _MINT,
            {
                "i": {
                    "appId": app_id,
                    "externalId": external_id,
                    "gamertag": external_id,
                    "ageYears": 30,
                    "country": "US",
                    "sessionTtlSeconds": SIGN_IN_SECONDS,
                }
            },
        )
        answer = data["mintManagedPlayerToken"]
        token_length = len(answer["token"])
        assert token_length == 64, "a player's app token is 64 characters"
        assert str(answer["userId"]).isdigit(), "the sign-in names its user"
        return Player(
            external_id,
            answer["token"],
            int(answer["gameTokenId"]),
            secrets.token_hex(16),
            int(answer["userId"]),
        )

    servers: dict[str, GameServer] = {}
    players: list[Player] = []
    carrier: dict[int, GameServer] = {}
    hubs: Any = None

    def update(server: GameServer, i: int, marker: str) -> int:
        return server.fleet.send_actor_update(
            players[i].game_token_id, chunk, players[i].uuid, marker.encode(), decay=0
        )

    def saw_actor(server: GameServer, marker: str) -> list[tuple[int, ...] | None]:
        return [who for n, who in server.actors if n.payload == marker.encode()]

    try:
        # 1. Two game servers each open a fleet session.
        for name in ("X", "Y"):
            opened = await api.fleet_sessions.open(app_id)
            token_length = len(opened.token)
            assert token_length == 64, "the fleet token is 64 characters"
            assert opened.game_token_id.isdigit()
            assert opened.app_id == app_id
            assert opened.max_players is not None
            assert 1 <= opened.max_players <= 1024
            assert opened.server.ip4, "the realtime server has an IPv4 address"
            assert 0 < opened.server.client_port < 65536
            assert opened.expires_at is not None
            lifetime = (
                datetime.fromisoformat(opened.expires_at).timestamp() * 1000 - time.time() * 1000
            )
            assert abs(lifetime - DAY_MS) < 5 * 60_000, "a session lasts 24 hours"
            server = GameServer(name, AsyncFleetSession(opened))
            server.fleet.set_handlers(server.handlers())
            servers[name] = server
            assert "token=<redacted>" in repr(opened)
        x, y = servers["X"], servers["Y"]
        assert x.fleet.game_token_id != y.fleet.game_token_id

        # 2. Four managed players sign in with the org token.
        players.extend([await sign_in(external_id) for external_id in EXTERNAL_IDS])
        assert len({p.game_token_id for p in players}) == 4

        # 3. Both sessions connect, and the realtime server acknowledges them.
        for server in (x, y):
            await server.fleet.connect()
            await until(
                f"{server.name} to be connected",
                lambda server=server: server.fleet.state == ConnState.CONNECTED,
                15,
            )
            await until(
                f"{server.name}'s capabilities to be acknowledged",
                lambda server=server: server.fleet.stats()["bound_control_acks"] >= 1,
                5,
            )
            stats = server.fleet.stats()
            assert (stats["hmac_failures"], stats["malformed"]) == (0, 0)

        # 4. X carries players 0 and 1, Y carries 2 and 3.
        for server, i in ((x, 0), (x, 1), (y, 2), (y, 3)):
            player = players[i]
            result = await server.fleet.carry(FleetPlayerToken(player.token, player.game_token_id))
            assert result.status is FleetCarryOutcome.CARRIED, f"player {i} on {server.name}"
            assert result.player_game_token_id == player.game_token_id
            # The user the sign-in named is the user the realtime server carries: a game
            # server needs no `me` call to know its player.
            assert result.user_id == player.user_id, f"player {i}'s user id"
            assert result.user_id > 0
            carrier[i] = server
            assert server.fleet.carries(player.game_token_id)
        assert len({p.user_id for p in players}) == 4, "four different users"
        assert (x.fleet.carried_count, y.fleet.carried_count) == (2, 2)

        # 5. State from X's player reaches Y once, plain, and does not come back to X.
        for server, i in ((x, 1), (y, 2), (y, 3)):
            update(server, i, f"arrive-{i}")
        await asyncio.sleep(0.3)
        for i, sender, receiver in ((0, x, y), (1, x, y), (2, y, x), (3, y, x)):
            # The first messages to a chunk nobody stood in may be refused while its
            # permissions load, so each is sent until the other server sees it.
            for attempt in range(20):
                if any(n.uuid == players[i].uuid for n, _ in receiver.actors):
                    break
                update(sender, i, f"stand-{i}-{attempt}")
                await asyncio.sleep(0.5)
            assert any(n.uuid == players[i].uuid for n, _ in receiver.actors), (
                f"player {i}'s actor never reached {receiver.name}"
            )
        marker = f"once-{secrets.token_hex(6)}"
        update(x, 0, marker)
        await until("the update on Y", lambda: bool(saw_actor(y, marker)), 5)
        await asyncio.sleep(1.5)
        assert saw_actor(y, marker) == [None], (
            "Y is sent the update once, plain, however many of its players are in range"
        )
        own_ids = {players[0].uuid, players[1].uuid}
        assert not [n for n, _ in x.actors if n.uuid in own_ids], (
            "X was sent none of its own players' state back"
        )

        # 5b. Each carried player's keepalive is accepted where the player has access: none
        # is refused, so no report is made (for realtime servers v0.51.0, which accept a
        # keepalive only where the player's actor update would be).
        for server, i in ((x, 0), (x, 1), (y, 2), (y, 3)):
            server.fleet.send_heartbeat(players[i].game_token_id, chunk, players[i].uuid)
        await asyncio.sleep(KEEPALIVE_ANSWER_WAIT_MS / 1000 + 0.5)
        for server, i in ((x, 0), (x, 1), (y, 2), (y, 3)):
            standing = server.fleet.not_present(players[i].game_token_id, players[i].uuid)
            assert standing is None, f"player {i}'s keepalive was refused on {server.name}"
        for server in (x, y):
            stats = server.fleet.stats()
            assert (stats["keepalives_refused"], stats["not_present_reports"]) == (0, 0)
            assert (server.keepalive_refusals, server.not_present) == ([], [])
            assert stats["messages_sent_by_type"][26] >= 2, "the keepalives left"

        # 6. A spatial text arrives inside a delivery that lists its players.
        text = f"hello-{secrets.token_hex(6)}"
        sequence = x.fleet.send_text_packet(players[0].game_token_id, chunk, players[0].uuid, text)

        def refused() -> bool:
            return any(
                seq == sequence and code == ErrorCode.UNAUTHORIZED for seq, code, _ in x.errors
            )

        def heard(server: GameServer) -> list[tuple[int, ...] | None]:
            return [who for n, who in server.texts if n.payload == text.encode()]

        await until("the text on Y, or its refusal", lambda: bool(heard(y)) or refused(), 8)
        if not heard(y) and refused():
            pytest.fail(
                "SETUP: the realtime server refused the text UNAUTHORIZED. The app's access "
                "tier does not grant text chat (use_text_chat) to these players."
            )
        await until("the text on X", lambda: bool(heard(x)), 5)
        await asyncio.sleep(1.0)
        listed_y = sorted(p for who in heard(y) for p in (who or ()))
        listed_x = sorted(p for who in heard(x) for p in (who or ()))
        assert all(who for who in heard(y) + heard(x)), (
            "a text is never plain: it names its players"
        )
        assert listed_y == sorted([players[2].game_token_id, players[3].game_token_id])
        # A sender's own text lists the sender.
        assert listed_x == sorted([players[0].game_token_id, players[1].game_token_id])

        # 7. A crossing: Y carries player 0, X is told, and no Application API call is made.
        before = requests
        moving = players[0]
        result = await y.fleet.carry(FleetPlayerToken(moving.token, moving.game_token_id))
        assert result.status is FleetCarryOutcome.CARRIED
        assert result.user_id == moving.user_id, "the same user"
        carrier[0] = y

        def told() -> list[FleetReleased]:
            return [r for r in x.released if r.player_game_token_id == moving.game_token_id]

        await until("X to be told the player was released", lambda: bool(told()), 8)
        assert (told()[0].reason, told()[0].user_id) == (1, moving.user_id)
        assert not x.fleet.carries(moving.game_token_id)
        assert y.fleet.carries(moving.game_token_id)
        sent_before = x.fleet.stats()["datagrams_sent"]
        with pytest.raises(FleetNotCarriedError):
            update(x, 0, "after-the-crossing")
        assert x.fleet.stats()["datagrams_sent"] == sent_before, "X sent nothing for the player"
        assert requests == before, "the crossing made no Application API call"
        marker = f"moved-{secrets.token_hex(6)}"
        update(y, 0, marker)
        await until("the moved player's update on X", lambda: bool(saw_actor(x, marker)), 5)
        assert saw_actor(x, marker)[0] is None
        assert y.released == []

        # 8. A refresh keeps the token, the server and the carries.
        before_expiry = x.fleet.expires_at
        before_stats = x.fleet.stats()
        refresh = x.fleet.refresh_input()
        server_now = x.fleet.current_server
        assert refresh["currentServer"] == {
            "ip4": server_now.ip4,
            "clientPort": server_now.client_port,
        }
        assert not x.fleet.refresh_due()
        answer = await api.fleet_sessions.refresh(refresh)
        assert int(answer.game_token_id) == x.fleet.game_token_id
        assert (answer.server.ip4, answer.server.client_port) == (
            server_now.ip4,
            server_now.client_port,
        )
        x.fleet.apply_refresh(answer)
        assert before_expiry is not None
        assert x.fleet.expires_at is not None
        assert x.fleet.expires_at > before_expiry, "a later expiry"
        assert x.fleet.state == ConnState.CONNECTED
        after_stats = x.fleet.stats()
        assert (
            after_stats["messages_sent_by_type"][49] == before_stats["messages_sent_by_type"][49]
        ), "no FLEET_CARRY: the carries stand"
        assert after_stats["messages_sent_by_type"][29] > before_stats["messages_sent_by_type"][29]
        assert x.fleet.carried_count == 1
        marker = f"refreshed-{secrets.token_hex(6)}"
        update(x, 1, marker)
        await until(
            "a carried player's update on Y after the refresh",
            lambda: bool(saw_actor(y, marker)),
            5,
        )
        assert (x.session_released, x.moves) == ([], 0)

        # 9. A carried player's token is refreshed and carried again with no gap. The token is
        # refreshed as the player's own client would, with the app token as the bearer.
        staying = players[1]
        previous_id = staying.game_token_id
        fresh = (await as_bearer(staying.token, _REFRESH))["refreshAppToken"]
        staying.token, staying.game_token_id = fresh["token"], int(fresh["gameTokenId"])
        assert staying.game_token_id != previous_id
        assert int(fresh["userId"]) == staying.user_id, "a refresh names the same user"
        releases_before = len(x.released)
        result = await x.fleet.carry(
            FleetPlayerToken(staying.token, staying.game_token_id, replaces=previous_id)
        )
        assert result.status is FleetCarryOutcome.CARRIED
        assert result.user_id == staying.user_id
        assert x.fleet.carries(staying.game_token_id)
        assert not x.fleet.carries(previous_id)
        assert len(x.released) == releases_before, "no release: the player stayed carried"
        marker = f"new-token-{secrets.token_hex(6)}"
        update(x, 1, marker)
        await until(
            "the player's update under the new token on Y", lambda: bool(saw_actor(y, marker)), 5
        )

        # 10. The game server connects to hubs as itself and calls for a player (optional).
        hub_app = _env("CROWDY_FLEET_HUB_APP_ID")
        if hub_app:
            node_type = _env("CROWDY_FLEET_HUB_NODE_TYPE", "lobby")
            key = _env("CROWDY_FLEET_HUB_KEY")
            method = _env("CROWDY_FLEET_HUB_METHOD", "state")
            hub_token = _env("CROWDY_FLEET_HUB_ORG_TOKEN")
            if hub_token and hub_token != _env("CROWDY_FLEET_ORG_TOKEN"):
                hub_api = crowdypy.AsyncCrowdyClient(http_url=api_url, http_client=http)
                hub_api.set_token(hub_token)
            else:
                hub_api = api
            hubs = await hub_api.exec.connect_as_server(
                hub_app, node_type=node_type, key=key or None
            )
            assert hubs.is_server
            assert not hubs.last_connect_failure().failed()
            await hubs.call(node_type, key, method)
            await hubs.call_as(staying.user_id, node_type, key, method)
            hub_step = f"ran ({node_type} / {key} / {method})"
        else:
            hub_step = "not run (CROWDY_FLEET_HUB_APP_ID is not set)"
        print(f"fleet e2e: the hub step {hub_step}")

        # 11. Every player is released and both sessions are closed.
        for i, server in carrier.items():
            assert server.fleet.release(players[i].game_token_id), f"player {i}'s release left"
        for server in (x, y):
            closing = {"appId": server.fleet.app_id, "gameTokenId": server.fleet.game_token_id}
            assert await api.fleet_sessions.close(closing) is True
            assert await api.fleet_sessions.close(closing) is False, "already closed"
            with pytest.raises(CrowdyGraphQLError) as gone:
                await api.fleet_sessions.refresh(closing)
            assert fleet_session_not_found_of(gone.value)

        # 12. The realtime server sent each session nothing but its own.
        for server in (x, y):
            stats = server.fleet.stats()
            assert (
                stats["wrong_app"],
                stats["unsigned_notifications"],
                stats["hmac_failures"],
            ) == (
                0,
                0,
                0,
            ), f"{server.name} was sent something that was not its own"
            assert server.failures == []
            assert server.not_present == [], f"{server.name}'s players were present throughout"
    finally:
        if hubs is not None:
            await hubs.close()
        for server in servers.values():
            # Best effort: a session the steps already closed answers false, and the steps'
            # own failure is the one to report.
            with contextlib.suppress(Exception):
                await api.fleet_sessions.close(
                    {"appId": server.fleet.app_id, "gameTokenId": server.fleet.game_token_id}
                )
            await server.fleet.close()
        await api.aclose()
        await http.aclose()
