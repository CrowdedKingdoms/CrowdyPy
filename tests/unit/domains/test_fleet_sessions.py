"""Fleet sessions on the Application API (CrowdyJS 18.40.0, Application API v2.89.0): the three
calls an organization's game server makes with its org token, the hub server connection, and
the refusals they answer.

An org token and a fleet token stay on a server the organization runs, so the SDK wraps the
calls a server makes and still no org-token sign-in of a player.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from websockets.asyncio.server import ServerConnection, serve

import crowdypy
from conftest import MockApi
from crowdypy import AsyncCrowdyClient, _server_only
from crowdypy.auth_state import AuthState
from crowdypy.domains.exec import CrowdyExecError, ExecAPI, ExecEndpoint
from crowdypy.domains.fleet_sessions import FleetServer, FleetSessionAnswer, FleetSessionsAPI
from crowdypy.errors import (
    FLEET_SESSION_LIMIT_CODE,
    NOT_FOUND_CODE,
    ORG_TOKEN_REQUIRED_CODE,
    PLATFORM_BUSY_CODE,
    CrowdyError,
    CrowdyGraphQLError,
    CrowdyPlatformBusy,
    fleet_session_limit_of,
    fleet_session_not_found_of,
    org_legal_acceptance_required_of,
    org_token_required_of,
    platform_busy_of,
    retry_after_ms_of,
)
from crowdypy.graphql import AsyncGraphQLClient
from crowdypy.inputs import CloseFleetSessionInput, CurrentServerInput, RefreshFleetSessionInput
from crowdypy.sync import CrowdyClient

ORG_TOKEN = "cko_" + "o" * 60
FLEET_TOKEN = "f" * 64

ANSWER: dict[str, Any] = {
    "token": FLEET_TOKEN,
    "gameTokenId": "9001",
    "appId": "7",
    "expiresAt": "2026-11-27T21:08:00.000Z",
    "maxPlayers": 256,
    "server": {
        "serverId": "rt-1",
        "ip4": "203.0.113.9",
        "ip6": None,
        "clientPort": 9000,
        "status": "ReadyForClients",
        "clients": 12,
    },
}


def refused(**extensions: Any) -> CrowdyGraphQLError:
    return CrowdyGraphQLError([{"message": "refused", "extensions": extensions}])


async def test_open_sends_the_app_with_the_org_token_as_the_bearer(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    session.set_token(ORG_TOKEN)
    api.reply_with_root(ANSWER)
    opened = await FleetSessionsAPI(graphql).open(7)
    assert api.last.operation_name == "OpenFleetSession"
    assert "openFleetSession(input: $input)" in api.last.query
    assert api.last.variables == {"input": {"appId": "7"}}
    assert api.last.headers["authorization"] == f"Bearer {ORG_TOKEN}"
    assert "origin" not in api.last.headers  # the API refuses an org token sent with one
    assert opened == FleetSessionAnswer(
        token=FLEET_TOKEN,
        game_token_id="9001",
        app_id="7",
        expires_at="2026-11-27T21:08:00.000Z",
        max_players=256,
        server=FleetServer(
            server_id="rt-1",
            ip4="203.0.113.9",
            ip6=None,
            client_port=9000,
            status="ReadyForClients",
            clients=12,
        ),
    )


async def test_an_answer_never_shows_its_token(api: MockApi, graphql: AsyncGraphQLClient) -> None:
    api.reply_with_root(ANSWER)
    opened = await FleetSessionsAPI(graphql).open("7")
    for text in (repr(opened), str(opened), f"{opened}"):
        assert FLEET_TOKEN not in text
        assert "token=<redacted>" in text
    assert "game_token_id='9001'" in repr(opened)


async def test_refresh_names_the_session_and_the_server_it_is_on(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    api.reply_with_root({**ANSWER, "expiresAt": "2026-11-28T21:08:00.000Z"})
    fleet = FleetSessionsAPI(graphql)
    kept = await fleet.refresh(
        {
            "appId": "7",
            "gameTokenId": "9001",
            "currentServer": {"ip4": "203.0.113.9", "clientPort": 9000},
        }
    )
    assert api.last.operation_name == "RefreshFleetSession"
    assert api.last.variables == {
        "input": {
            "appId": "7",
            "gameTokenId": "9001",
            "currentServer": {"ip4": "203.0.113.9", "clientPort": 9000},
        }
    }
    assert kept.expires_at == "2026-11-28T21:08:00.000Z"

    # After a move order the session names no server; ids given as integers travel as the
    # decimal strings a BigInt is.
    await fleet.refresh({"appId": 7, "gameTokenId": 9_007_199_254_740_993})
    assert api.last.variables == {"input": {"appId": "7", "gameTokenId": "9007199254740993"}}

    # The generated input struct is sent as it is.
    await fleet.refresh(
        RefreshFleetSessionInput(
            app_id="7",
            game_token_id="9001",
            current_server=CurrentServerInput(ip4="203.0.113.9", client_port=9000),
        )
    )
    assert api.last.variables == {
        "input": {
            "appId": "7",
            "gameTokenId": "9001",
            "currentServer": {"ip4": "203.0.113.9", "clientPort": 9000},
        }
    }


async def test_close_answers_whether_a_session_was_ended(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    fleet = FleetSessionsAPI(graphql)
    api.reply_with_root(True)
    assert await fleet.close({"appId": 7, "gameTokenId": 9001}) is True
    assert api.last.operation_name == "CloseFleetSession"
    assert api.last.variables == {"input": {"appId": "7", "gameTokenId": "9001"}}
    # Already closed, or opened with another org token.
    api.reply_with_root(False)
    assert await fleet.close(CloseFleetSessionInput(app_id="7", game_token_id="9001")) is False


async def test_a_refused_refresh_is_read_as_not_found(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    api.responder = lambda sent: {
        "errors": [{"message": "no such session", "extensions": {"code": "NOT_FOUND"}}]
    }
    with pytest.raises(CrowdyGraphQLError) as raised:
        await FleetSessionsAPI(graphql).refresh({"appId": "7", "gameTokenId": "9001"})
    assert raised.value.code == NOT_FOUND_CODE
    assert fleet_session_not_found_of(raised.value)
    assert not fleet_session_limit_of(raised.value)


def test_the_refusals_are_read_by_their_code() -> None:
    assert org_token_required_of(refused(code=ORG_TOKEN_REQUIRED_CODE))
    assert not org_token_required_of(refused(code="ORG_TOKEN_NOT_ALLOWED"))
    assert fleet_session_limit_of(refused(code=FLEET_SESSION_LIMIT_CODE))
    assert not fleet_session_limit_of(refused(code="FORBIDDEN"))
    assert fleet_session_not_found_of(refused(code="NOT_FOUND"))
    assert not fleet_session_not_found_of(refused(code="FORBIDDEN"))
    assert org_legal_acceptance_required_of(refused(code="ORG_LEGAL_ACCEPTANCE_REQUIRED"))
    # A raw error entry is read as a raised error is, and anything else is not a refusal.
    assert fleet_session_limit_of({"extensions": {"code": "FLEET_SESSION_LIMIT"}})
    for reader in (org_token_required_of, fleet_session_limit_of, fleet_session_not_found_of):
        assert not reader(RuntimeError("boom"))
        assert not reader(None)
    assert (ORG_TOKEN_REQUIRED_CODE, FLEET_SESSION_LIMIT_CODE, NOT_FOUND_CODE) == (
        "ORG_TOKEN_REQUIRED",
        "FLEET_SESSION_LIMIT",
        "NOT_FOUND",
    )


def test_platform_busy_carries_the_wait_the_api_named() -> None:
    assert PLATFORM_BUSY_CODE == "PLATFORM_BUSY"
    busy = refused(code="PLATFORM_BUSY", retryAfterMs=750, retryable=True, blame="PLATFORM")
    assert platform_busy_of(busy) == CrowdyPlatformBusy(retry_after_ms=750.0)
    assert platform_busy_of(refused(code="PLATFORM_BUSY")) == CrowdyPlatformBusy(0.0)
    assert platform_busy_of(refused(code="FORBIDDEN", retryAfterMs=750)) is None
    assert platform_busy_of(RuntimeError("boom")) is None
    # The one reader of extensions.retryAfterMs: a raised error, or one raw entry.
    assert retry_after_ms_of(busy) == 750.0
    assert retry_after_ms_of({"extensions": {"retryAfterMs": 20.5}}) == 20.5
    for bad in (0, -5, "750", True, None, float("inf"), float("nan")):
        assert retry_after_ms_of({"extensions": {"retryAfterMs": bad}}) == 0.0
    assert retry_after_ms_of(RuntimeError("boom")) == 0.0
    # The SDK's own read-again rule reads the same field.
    from crowdypy import _dropped_read

    assert _dropped_read.retry_after_ms_of is retry_after_ms_of


def test_the_refusal_readers_and_codes_are_package_names() -> None:
    for name in (
        "ORG_TOKEN_REQUIRED_CODE",
        "FLEET_SESSION_LIMIT_CODE",
        "NOT_FOUND_CODE",
        "PLATFORM_BUSY_CODE",
        "CrowdyPlatformBusy",
        "org_token_required_of",
        "fleet_session_limit_of",
        "fleet_session_not_found_of",
        "platform_busy_of",
        "FleetSessionsAPI",
        "FleetSessionAnswer",
        "FleetServer",
    ):
        assert name in crowdypy.__all__, name
        assert getattr(crowdypy, name) is not None


def http(api: MockApi) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(api.handler))


async def test_the_client_exposes_the_three_calls_and_no_player_sign_in(api: MockApi) -> None:
    client = AsyncCrowdyClient(http_url="https://ck.example.test", http_client=http(api))
    assert isinstance(client.fleet_sessions, FleetSessionsAPI)
    assert client.admin.fleet_sessions is client.fleet_sessions
    for name in ("open", "refresh", "close"):
        assert callable(getattr(client.fleet_sessions, name))
    # The org-token sign-in of a player is still not wrapped: an org token stays on the
    # organization's server.
    assert not hasattr(client.fleet_sessions, "mint")
    assert not hasattr(client.managed_players, "mint")
    client.set_token(ORG_TOKEN)
    api.reply_with_root(ANSWER)
    assert (await client.fleet_sessions.open(7)).game_token_id == "9001"
    assert api.last.headers["authorization"] == f"Bearer {ORG_TOKEN}"
    await client.aclose()


def test_the_blocking_client_makes_the_same_calls(api: MockApi) -> None:
    client = CrowdyClient(
        http_url="https://ck.example.test",
        http_client=httpx.Client(transport=httpx.MockTransport(api.handler)),
    )
    client.set_token(ORG_TOKEN)
    api.reply_with_root(ANSWER)
    opened = client.fleet_sessions.open(7)
    # One class in both clients: what the blocking client answers is what the async one does.
    assert isinstance(opened, FleetSessionAnswer)
    assert opened.server.client_port == 9000
    assert client.admin.fleet_sessions is client.fleet_sessions
    kept = client.fleet_sessions.refresh({"appId": 7, "gameTokenId": 9001})
    assert kept.token == FLEET_TOKEN
    assert api.last.variables == {"input": {"appId": "7", "gameTokenId": "9001"}}
    api.reply_with_root(True)
    assert client.fleet_sessions.close({"appId": 7, "gameTokenId": 9001}) is True
    client.close()


# ---- an org token is used on a server the organization runs ----


@pytest.mark.parametrize(
    ("platform", "refusal"),
    [
        ("linux", None),
        ("darwin", None),
        ("win32", None),
        ("emscripten", "this is Python running in a browser page or its worker"),
        ("android", "this is a player's device (Android)"),
        ("ios", "this is a player's device (iOS)"),
    ],
)
def test_which_runtimes_may_use_an_org_token(platform: str, refusal: str | None) -> None:
    assert _server_only.organization_server_refusal(platform) == refusal


async def test_the_calls_refuse_before_any_request_where_python_is_not_a_server(
    api: MockApi, graphql: AsyncGraphQLClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_server_only, "_runtime_platform", lambda: "emscripten")
    fleet = FleetSessionsAPI(graphql)
    exec_api = ExecAPI(graphql)
    calls = [
        ("fleet_sessions.open", lambda: fleet.open(7)),
        ("fleet_sessions.refresh", lambda: fleet.refresh({"appId": "7", "gameTokenId": "1"})),
        ("fleet_sessions.close", lambda: fleet.close({"appId": "7", "gameTokenId": "1"})),
        ("exec.server_endpoint", lambda: exec_api.server_endpoint(7)),
        ("exec.connect_as_server", lambda: exec_api.connect_as_server(7)),
    ]
    for what, call in calls:
        with pytest.raises(CrowdyError) as raised:
            await call()
        message = raised.value.message
        assert message.startswith(f"{what} takes your organization's org token")
        assert "never goes into a browser page or a game client" in message
        assert "Call it from a server your organization runs." in message
    assert api.sent == []


# ---- the hub connection for a game server (execConnectAsServer, ck-exec 0.25.0) ----


async def test_server_endpoint_asks_for_a_server_connect_token(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    session.set_token(ORG_TOKEN)
    api.reply_with_root(
        {
            "gatewayUrl": "wss://exec.example.test",
            "token": "server-connect-token",
            "host": "exec-1",
            "expiresAt": "2026-11-26T21:09:00.000Z",
        }
    )
    endpoint = await ExecAPI(graphql).server_endpoint(7, node_type="lobby", key="eu")
    assert api.last.operation_name == "ExecConnectAsServer"
    assert "execConnectAsServer(appId: $appId" in api.last.query
    assert api.last.variables == {"appId": "7", "nodeType": "lobby", "key": "eu"}
    assert api.last.headers["authorization"] == f"Bearer {ORG_TOKEN}"
    assert isinstance(endpoint, ExecEndpoint)
    assert (endpoint.gateway_url, endpoint.host) == ("wss://exec.example.test", "exec-1")
    assert endpoint.expires_at == "2026-11-26T21:09:00.000Z"
    assert "server-connect-token" not in repr(endpoint)

    await ExecAPI(graphql).server_endpoint("7")
    assert api.last.variables == {"appId": "7"}


async def test_connect_as_server_refuses_a_gateway_off_the_estate_without_dialing(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    api.reply_with_root(
        {"gatewayUrl": "wss://gw.example.org", "token": "secret-token", "host": "h"}
    )
    with pytest.raises(CrowdyExecError) as raised:
        await ExecAPI(graphql).connect_as_server(7)
    assert raised.value.status == "Unavailable"
    assert "refusing the gateway wss://gw.example.org" in raised.value.message
    assert "secret-token" not in raised.value.message


async def test_connect_as_server_opens_a_server_connection_and_redials_with_a_new_token() -> None:
    frames: list[bytes] = []
    tokens: list[str] = []

    async def gateway(ws: ServerConnection) -> None:
        path = ws.request.path if ws.request is not None else ""
        tokens.append(path.split("token=", 1)[-1])
        async for message in ws:
            assert isinstance(message, bytes)
            frames.append(message)
            await ws.send(b"\x81" + message[1:5] + b"\x00\xc3")  # Ok, msgpack true

    async with serve(gateway, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        minted = 0

        def answer(sent: Any) -> Any:
            nonlocal minted
            minted += 1
            return {
                "data": {
                    "execConnectAsServer": {
                        "gatewayUrl": f"ws://127.0.0.1:{port}",
                        "token": f"server-token-{minted}",
                        "host": "exec-1",
                        "expiresAt": "2026-11-26T21:09:00.000Z",
                    }
                }
            }

        api = MockApi(responder=answer)
        client = AsyncCrowdyClient(http_url="http://127.0.0.1:1", http_client=http(api))
        client.set_token(ORG_TOKEN)
        hubs = await client.exec.connect_as_server(7, call_timeout=3)
        assert hubs.is_server
        assert hubs.host == "exec-1"
        assert api.last.operation_name == "ExecConnectAsServer"
        assert api.last.variables == {"appId": "7"}
        assert tokens == ["server-token-1"]

        assert await hubs.call("lobby", "", "report", {"players": 2}) is True
        assert frames[-1][0] == 0x01  # the server's own call
        assert await hubs.call_as(77, "player", "p-77", "grant", {"gold": 10}) is True
        assert frames[-1][:13] == b"\x05" + (2).to_bytes(4, "little") + (77).to_bytes(8, "little")

        # A reconnect asks the Application API for a fresh server connect token.
        await hubs._redial(hubs._ws)
        assert tokens == ["server-token-1", "server-token-2"]
        assert await hubs.call_as("77", "player", "p-77", "grant") is True
        await hubs.close()
        await client.aclose()
