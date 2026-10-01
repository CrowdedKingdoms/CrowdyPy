"""A server may move this client only within its own estate, because the token follows.

A WRONG_DATACENTER refusal and a re-discovery answer both name the endpoint to move to.
One compromised instance must not be able to walk clients, and their bearer tokens, onto
an origin outside the platform's estate (``crowdypy.estate.is_same_estate``).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from crowdypy.client import AsyncCrowdyClient
from crowdypy.errors import CrowdyGraphQLError
from crowdypy.rediscover import Endpoint

ORIGIN = "https://ck.example.test"


def wrong_datacenter(http_url: str, ws_url: str | None = None) -> dict[str, Any]:
    extensions = {"code": "WRONG_DATACENTER", "gameApiUrl": http_url, "appDatacenter": "va"}
    if ws_url:
        extensions["gameApiWsUrl"] = ws_url
    return {"errors": [{"message": "wrong datacenter", "extensions": extensions}]}


def client_answering(first: dict[str, Any]) -> tuple[AsyncCrowdyClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = first if len(seen) == 1 else {"data": {"me": {"userId": "1"}}}
        return httpx.Response(200, json=body)

    client = AsyncCrowdyClient(
        http_url=ORIGIN, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    client.set_token("session-token")
    return client, seen


async def test_a_move_inside_the_estate_is_followed_with_one_retry() -> None:
    client, seen = client_answering(
        wrong_datacenter("https://ck-va.example.test", "wss://ck-va.example.test")
    )
    assert await client.users.me() == {"userId": "1"}
    assert [r.url.host for r in seen] == ["ck.example.test", "ck-va.example.test"]
    assert client.graphql_endpoint == "https://ck-va.example.test/graphql"
    assert client.ws_endpoint == "wss://ck-va.example.test/graphql"


@pytest.mark.parametrize(
    ("http_url", "ws_url"),
    [
        ("https://ck.attacker.example", None),
        ("https://example.test.attacker.example", None),
        ("https://attacker.example@ck.example.test.attacker.example", None),
        ("https://ck-va.example.test", "wss://ck.attacker.example"),
    ],
)
async def test_a_move_outside_the_estate_is_refused(http_url: str, ws_url: str | None) -> None:
    client, seen = client_answering(wrong_datacenter(http_url, ws_url))
    with pytest.raises(CrowdyGraphQLError) as caught:
        await client.users.me()
    assert caught.value.code == "WRONG_DATACENTER"
    assert len(seen) == 1, "no retry, so the token was not sent anywhere else"
    assert client.graphql_endpoint == f"{ORIGIN}/graphql"


async def test_rediscovery_moves_only_within_the_estate() -> None:
    answers = iter(
        [
            Endpoint("https://ck.attacker.example", None),
            Endpoint("https://ck-ap.example.test", "wss://ck.attacker.example"),
            Endpoint("https://ck-ap.example.test", "wss://ck-ap.example.test"),
        ]
    )

    async def rediscover(app_id: str | None) -> Endpoint | None:
        return next(answers)

    client = AsyncCrowdyClient(http_url=ORIGIN, rediscover=rediscover)
    assert await client.rediscover_endpoint("42") is False
    assert await client.rediscover_endpoint("42") is False
    assert client.graphql_endpoint == f"{ORIGIN}/graphql"
    assert await client.rediscover_endpoint("42") is True
    assert client.graphql_endpoint == "https://ck-ap.example.test/graphql"
    assert client.ws_endpoint == "wss://ck-ap.example.test/graphql"
    await client.aclose()
