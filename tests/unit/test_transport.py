"""The GraphQL transport: headers, cookies, error classes and the datacenter redirect."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from conftest import MockApi, Sent
from crowdypy._generated import operations as ops
from crowdypy.auth_state import AuthState
from crowdypy.datacenter_redirect import DatacenterMove
from crowdypy.errors import (
    CrowdyAppUnavailableError,
    CrowdyGraphQLError,
    CrowdyHttpError,
    CrowdyNetworkError,
    CrowdyTimeoutError,
    CrowdyUserCodeFaultError,
    player_fault_of,
)
from crowdypy.graphql import AsyncGraphQLClient, graphql_endpoint
from crowdypy.lb_cookie import LbCookieStore


async def test_sends_token_operation_and_no_origin(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    session.set_token("t0k3n")
    api.reply_with_root({"userId": "1"})
    data = await graphql.request(ops.ME)
    assert data == {"me": {"userId": "1"}}
    sent = api.last
    assert sent.operation_name == "Me"
    assert sent.headers["authorization"] == "Bearer t0k3n"
    assert sent.headers["content-type"] == "application/json"
    assert "origin" not in sent.headers
    assert sent.headers["user-agent"].startswith("crowdypy/")


async def test_token_is_read_fresh_on_every_request(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    api.reply_with_root({})
    await graphql.request(ops.ME)
    assert "authorization" not in api.last.headers
    session.set_token("later")
    await graphql.request(ops.ME)
    assert api.last.headers["authorization"] == "Bearer later"


async def test_lb_cookie_is_pinned_and_only_it() -> None:
    api = MockApi(set_cookie=["cks_ga=abc123; Path=/; HttpOnly", "other=nope"])
    api.reply_with_root({})
    jar = LbCookieStore()
    client = AsyncGraphQLClient(
        AuthState(),
        endpoint="https://ck.example.test/graphql",
        lb_cookie_store=jar,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
    )
    await client.request(ops.ME)
    assert jar.get_value() == "abc123"
    await client.request(ops.ME)
    assert api.last.headers["cookie"] == "cks_ga=abc123"


async def test_http_error() -> None:
    api = MockApi(status=502)
    client = AsyncGraphQLClient(
        AuthState(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    )
    with pytest.raises(CrowdyHttpError) as caught:
        await client.request(ops.ME)
    assert caught.value.status == 502


@pytest.mark.parametrize(
    ("extensions", "kind"),
    [
        ({"code": "FORBIDDEN"}, CrowdyGraphQLError),
        ({"code": "APP_UNAVAILABLE", "appId": "7", "retryable": True}, CrowdyAppUnavailableError),
        (
            {"code": "USER_CODE_TOO_SLOW", "blame": "AUTHOR", "retryable": False},
            CrowdyUserCodeFaultError,
        ),
    ],
)
async def test_graphql_error_classes(
    api: MockApi, graphql: AsyncGraphQLClient, extensions: dict[str, Any], kind: type
) -> None:
    api.responder = lambda sent: {"errors": [{"message": "no", "extensions": extensions}]}
    with pytest.raises(CrowdyGraphQLError) as caught:
        await graphql.request(ops.ME)
    assert type(caught.value) is kind
    assert caught.value.code == extensions["code"]


async def test_user_code_fault_attribution(api: MockApi, graphql: AsyncGraphQLClient) -> None:
    api.responder = lambda sent: {
        "errors": [
            {
                "message": "too slow",
                "extensions": {
                    "code": "CIRCUIT_OPEN",
                    "blame": "PLATFORM",
                    "retryable": True,
                    "cause": "watchdog_timeout",
                },
            }
        ]
    }
    with pytest.raises(CrowdyUserCodeFaultError) as caught:
        await graphql.request(ops.ME)
    fault = caught.value.fault
    assert (fault.code, fault.blame, fault.retryable, fault.cause) == (
        "CIRCUIT_OPEN",
        "PLATFORM",
        True,
        "watchdog_timeout",
    )
    assert player_fault_of(caught.value) == fault
    assert (
        player_fault_of(
            {"success": False, "fault": {"code": "BUDGET_EXCEEDED", "blame": "BUDGET"}}
        ).blame
        == "BUDGET"
    )  # type: ignore[union-attr]
    assert player_fault_of({"data": 1}) is None


async def test_app_unavailable_fields(api: MockApi, graphql: AsyncGraphQLClient) -> None:
    api.responder = lambda sent: {
        "errors": [
            {
                "message": "offline",
                "extensions": {"code": "APP_UNAVAILABLE", "appId": "9", "appDatacenter": "va"},
            }
        ]
    }
    with pytest.raises(CrowdyAppUnavailableError) as caught:
        await graphql.request(ops.ME)
    assert (caught.value.app_id, caught.value.app_datacenter, caught.value.retryable) == (
        "9",
        "va",
        True,
    )


def _redirecting(first: dict[str, Any]) -> tuple[MockApi, list[str]]:
    hits: list[str] = []

    def responder(sent: Sent) -> Any:
        hits.append(sent.operation_name or "")
        return first if len(hits) == 1 else {"data": {"me": {"userId": "1"}}}

    return MockApi(responder=responder), hits


WRONG_DC = {
    "errors": [
        {
            "message": "wrong datacenter",
            "extensions": {
                "code": "WRONG_DATACENTER",
                "gameApiUrl": "https://ck-va.example.test",
                "gameApiWsUrl": "wss://ck-va.example.test",
                "appDatacenter": "va",
            },
        }
    ]
}


async def test_wrong_datacenter_moves_once_and_retries() -> None:
    api, hits = _redirecting(WRONG_DC)
    moves: list[DatacenterMove] = []
    client = AsyncGraphQLClient(
        AuthState(),
        endpoint="https://ck.example.test/graphql",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
    )

    def handler(move: DatacenterMove) -> bool:
        moves.append(move)
        client.set_endpoint(graphql_endpoint(move.game_api_url) or move.game_api_url)
        return True

    client.set_wrong_datacenter_handler(handler)
    data = await client.request(ops.ME)
    assert data == {"me": {"userId": "1"}}
    assert len(hits) == 2
    assert moves[0].app_datacenter == "va"
    assert client.endpoint == "https://ck-va.example.test/graphql"


async def test_wrong_datacenter_declined_surfaces_the_error() -> None:
    api, hits = _redirecting(WRONG_DC)
    client = AsyncGraphQLClient(
        AuthState(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        on_wrong_datacenter=lambda move: False,
    )
    with pytest.raises(CrowdyGraphQLError):
        await client.request(ops.ME)
    assert len(hits) == 1


async def test_timeout_and_network_errors() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    for handler, kind in ((timeout, CrowdyTimeoutError), (refused, CrowdyNetworkError)):
        client = AsyncGraphQLClient(
            AuthState(),
            timeout=1.5,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(kind) as caught:
            await client.request(ops.ME)
        if kind is CrowdyTimeoutError:
            assert caught.value.timeout_ms == 1500  # type: ignore[attr-defined]


def test_graphql_endpoint_normalisation() -> None:
    assert graphql_endpoint("https://ck.example.test") == "https://ck.example.test/graphql"
    assert graphql_endpoint("https://ck.example.test/graphql/") == "https://ck.example.test/graphql"
    assert graphql_endpoint("  ") is None
    assert graphql_endpoint(None) is None


def test_default_endpoint_is_the_generated_origin() -> None:
    from crowdypy._default_origin import CROWDY_DEFAULT_HTTP_ORIGIN

    client = AsyncGraphQLClient(AuthState())
    assert client.endpoint == f"{CROWDY_DEFAULT_HTTP_ORIGIN}/graphql"
