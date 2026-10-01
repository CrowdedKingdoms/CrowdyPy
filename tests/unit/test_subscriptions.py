"""GraphQL subscriptions over graphql-transport-ws, against a local server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from crowdypy.client import AsyncCrowdyClient
from crowdypy.domains.portal import AppTokenResponse
from crowdypy.errors import CrowdyGraphQLError
from crowdypy.subscriptions import GraphQLSubscriptions


class Server:
    def __init__(self) -> None:
        self.init: dict[str, Any] = {}
        self.cookie: str | None = None
        self.completed: list[str] = []
        self.port = 0

    async def handle(self, ws: ServerConnection) -> None:
        assert ws.subprotocol == "graphql-transport-ws"
        self.cookie = ws.request.headers.get("Cookie") if ws.request else None
        self.init = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "connection_ack"}))
        await ws.send(json.dumps({"type": "ping"}))
        async for raw in ws:
            message = json.loads(raw)
            if message["type"] == "pong":
                continue
            if message["type"] == "complete":
                self.completed.append(message["id"])
                continue
            sid = message["id"]
            query = message["payload"]["query"]
            if "broken" in query:
                await ws.send(
                    json.dumps(
                        {"id": sid, "type": "error", "payload": [{"message": "no such field"}]}
                    )
                )
                continue
            for n in range(2):
                await ws.send(
                    json.dumps({"id": sid, "type": "next", "payload": {"data": {"tick": n}}})
                )
            if "forever" not in query:
                await ws.send(json.dumps({"id": sid, "type": "complete"}))


@pytest.fixture
async def server() -> AsyncIterator[Server]:
    fake = Server()
    async with serve(fake.handle, "127.0.0.1", 0, subprotocols=["graphql-transport-ws"]) as running:  # type: ignore[list-item]
        fake.port = next(iter(running.sockets)).getsockname()[1]
        yield fake


async def test_results_stream_until_the_server_completes(server: Server) -> None:
    client = AsyncCrowdyClient(
        http_url=f"http://127.0.0.1:{server.port}", ws_url=f"ws://127.0.0.1:{server.port}"
    )
    client.set_app_token(
        AppTokenResponse(
            token="tok", game_token_id="1", app_id="7", expires_at="2099-01-01T00:00:00Z"
        )
    )
    client.lb_cookie_store.ingest_set_cookie(["cks_ga=sticky; Path=/"])
    subscriptions = GraphQLSubscriptions.for_client(client)
    assert [data async for data in subscriptions.subscribe("subscription { tick }")] == [
        {"tick": 0},
        {"tick": 1},
    ]
    assert server.init == {
        "type": "connection_init",
        "payload": {"Authorization": "Bearer tok", "appId": "7"},
    }
    assert server.cookie == "cks_ga=sticky"

    with pytest.raises(CrowdyGraphQLError, match="no such field"):
        async for _ in subscriptions.subscribe("subscription { broken }"):
            pass

    async for data in subscriptions.subscribe("subscription { forever }"):
        assert data == {"tick": 0}
        break
    await subscriptions.close()
    assert server.completed  # leaving the loop told the server to stop
    await client.aclose()
