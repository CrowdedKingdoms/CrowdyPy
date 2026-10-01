"""``client.udp``: the native connection behind CrowdyJS's UdpAPI names, with the Game API
mocked and a fake replication server on 127.0.0.1."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from crowdypy import wire
from crowdypy.client import AsyncCrowdyClient
from crowdypy.datacenter_redirect import DatacenterMove
from crowdypy.domains.portal import AppTokenResponse
from crowdypy.errors import CrowdyReplicationError
from crowdypy.replication import ConnState
from crowdypy.sync import CrowdyClient
from crowdypy.wire import DecayRate, MessageType

TOKEN = "t" * 64
FRESH = "f" * 64
ME = "a" * 32
OPTIONS = {"session_ready_wait_ms": 0, "advertise_capabilities": False, "bundle_window_ms": 0}


def app_token(token: str = TOKEN) -> AppTokenResponse:
    return AppTokenResponse(
        token=token, game_token_id="42", app_id="7", expires_at="2099-01-01T00:00:00Z"
    )


class World:
    """The Game API (GraphQL over httpx's mock transport) and one replication server."""

    def __init__(self) -> None:
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(3.0)
        self.port = self.udp.getsockname()[1]
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.pending: list[wire.LongSpatialMessage] = []
        self.channel: list[bytes] = []
        self.client: Any = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = httpx.Request("POST", "http://x", content=request.content).read()
        import json

        payload = json.loads(body)
        name = payload.get("operationName") or ""
        with self.lock:
            self.calls.append((name, request.headers.get("authorization", "")))
        if name == "ServerWithLeastClients":
            data = {
                "serverWithLeastClients": {"ip4": "127.0.0.1", "ip6": None, "clientPort": self.port}
            }
        elif name.startswith("RefreshAppToken"):
            data = {
                "refreshAppToken": {
                    "token": FRESH, "gameTokenId": "43", "appId": "7",
                    "expiresAt": "2099-01-01T00:00:00Z", "authorizedServer": None,
                }
            }  # fmt: skip
        else:
            data = {}
        return httpx.Response(200, json={"data": data})

    def count(self, name: str) -> int:
        with self.lock:
            return sum(1 for n, _ in self.calls if n.startswith(name))

    def pump_once(self) -> None:
        """Read one datagram: bundles unpacked, channel messages kept apart, heartbeats skipped."""
        data, self.client = self.udp.recvfrom(2048)
        for message in wire.split_datagram(data):
            if message[0] == MessageType.CHANNEL_MESSAGE_REQUEST:
                self.channel.append(message)
                continue
            parsed = wire.parse_long_spatial(message)
            if parsed.type != MessageType.CLIENT_ACTOR_HEARTBEAT:
                self.pending.append(parsed)

    def recv(self) -> wire.LongSpatialMessage:
        """The next spatial message the client sent."""
        while not self.pending:
            self.pump_once()
        return self.pending.pop(0)

    def echo(self, sent: wire.LongSpatialMessage, token: str = TOKEN) -> None:
        """Answer a send with its notification, as the server's fan-out includes the sender."""
        kind = {
            MessageType.ACTOR_UPDATE_REQUEST: MessageType.ACTOR_UPDATE_NOTIFICATION,
            MessageType.VOXEL_UPDATE_REQUEST: MessageType.VOXEL_UPDATE_NOTIFICATION,
        }[MessageType(sent.type)]
        self.udp.sendto(
            wire.encode_long_spatial(
                token, kind, sent.app_id, sent.chunk, sent.uuid, sent.payload,
                distance=sent.distance, game_token_id=1_700_000_000_000, sequence=sent.sequence,
            ),
            self.client,
        )  # fmt: skip

    def close(self) -> None:
        self.udp.close()


@pytest.fixture
def world() -> Iterator[World]:
    fake = World()
    yield fake
    fake.close()


def async_client(world: World) -> AsyncCrowdyClient:
    return AsyncCrowdyClient(
        http_url="https://ck.example.test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(world.handle)),
    )


async def until(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


async def test_connect_assigns_through_the_game_api_and_sends_with_defaults(world: World) -> None:
    client = async_client(world)
    status = await client.udp.connect(app_token(), **OPTIONS)
    assert status.server_ip4 == "127.0.0.1"
    assert status.server_client_port == world.port
    assert world.calls[0] == ("ServerWithLeastClients", f"Bearer {TOKEN}")
    assert client.get_token() == TOKEN
    await until(
        lambda: (
            client.udp.connection is not None and client.udp.connection.state == ConnState.CONNECTED
        )
    )
    assert (await client.udp.connection_status()).connected

    await client.udp.send_actor_update((1, 2, 3), ME, b"pose")
    client.udp.flush_sends()
    actor = await asyncio.to_thread(world.recv)
    assert (actor.type, actor.distance, actor.decay) == (
        MessageType.ACTOR_UPDATE_REQUEST,
        8,
        DecayRate.EXPONENTIAL,
    )

    await client.udp.send_text_packet((1, 2, 3), ME, "hi")
    await client.udp.send_voxel_update((1, 2, 3), ME, (1, 2, 3), 9)
    client.udp.flush_sends()
    text = await asyncio.to_thread(world.recv)
    voxel = await asyncio.to_thread(world.recv)
    assert (text.type, text.distance, text.decay, text.payload) == (
        MessageType.CLIENT_TEXT_PACKET,
        8,
        0,
        b"hi",
    )
    assert (voxel.type, voxel.distance, voxel.decay) == (MessageType.VOXEL_UPDATE_REQUEST, 8, 0)
    await client.aclose()
    assert client.udp.connection is None


async def test_connect_without_an_app_token_says_what_it_needs(world: World) -> None:
    client = async_client(world)
    with pytest.raises(CrowdyReplicationError, match="app token"):
        await client.udp.connect()
    with pytest.raises(CrowdyReplicationError) as caught:
        await client.udp.send_actor_update((0, 0, 0), ME)
    assert caught.value.code == "NotConnected"


async def test_a_datacenter_move_reassigns_the_udp_session(world: World) -> None:
    client = async_client(world)
    await client.udp.connect(app_token(), **OPTIONS)
    assert world.count("ServerWithLeastClients") == 1
    assert client.move_to_datacenter(DatacenterMove(game_api_url="https://ck-va.example.test"))
    await until(lambda: world.count("ServerWithLeastClients") == 2)
    await client.aclose()


async def test_a_gameplay_refresh_quiesces_and_resumes_the_connection(world: World) -> None:
    client = async_client(world)
    seen: list[Any] = []
    client.udp.subscribe({"status": seen.append})
    await client.udp.connect(app_token(), **OPTIONS)
    connection = client.udp.connection
    assert connection is not None
    refreshed = await client.refresh_gameplay_token()
    assert refreshed.token == FRESH
    assert client.app_token is refreshed
    assert client.get_token() == FRESH
    assert world.count("RefreshAppToken") == 1
    assert world.count("ServerWithLeastClients") == 2
    assert client.udp.connection is connection  # the same connection, resumed
    await client.udp.send_actor_update((0, 0, 0), ME, b"x")
    client.udp.flush_sends()
    sent = await asyncio.to_thread(world.recv)
    assert sent.epoch_or_token_id == 43  # signed with the fresh token's id
    await until(lambda: ConnState.CONNECTED in [e.state for e in seen])
    await client.aclose()


def test_the_blocking_client_has_the_same_udp(world: World) -> None:
    client = CrowdyClient(
        http_url="https://ck.example.test",
        http_client=httpx.Client(transport=httpx.MockTransport(world.handle)),
    )
    status = client.udp.connect(app_token(), **OPTIONS)
    assert status.server_client_port == world.port
    client.udp.send_client_event((4, 5, 6), ME, 3, b"st")
    client.udp.flush_sends()
    event = world.recv()
    assert (event.type, event.chunk, event.distance) == (
        MessageType.CLIENT_EVENT_NOTIFICATION,
        (4, 5, 6),
        8,
    )
    assert (int.from_bytes(event.payload[:2], "little"), event.payload[2:]) == (3, b"st")
    client.close()


async def test_grid_sends_check_the_box_then_use_udp(world: World) -> None:
    from crowdypy import GridBox, GridChunk, GridScopeError

    client = async_client(world)
    await client.udp.connect(app_token(), **OPTIONS)
    grid = client.grid("7", "5", GridBox(GridChunk(0, 0, 0), GridChunk(2, 2, 2)))
    with pytest.raises(GridScopeError):
        await grid.send.actor_update((3, 0, 0), ME, b"x")
    await grid.send.actor_update((1, 1, 1), ME, b"x")
    await grid.channels.send("9", ME, b"hi")
    client.udp.flush_sends()
    actor = await asyncio.to_thread(world.recv)
    assert (actor.type, actor.chunk) == (MessageType.ACTOR_UPDATE_REQUEST, (1, 1, 1))
    while not world.channel:  # it may have left in a datagram of its own
        await asyncio.to_thread(world.pump_once)
    assert len(world.channel) == 1
    await client.aclose()


async def test_a_world_actor_remembers_its_chunk(world: World) -> None:
    client = async_client(world)
    await client.udp.connect(app_token(), **OPTIONS)
    actor = client.world("7").actor(uuid=ME)
    with pytest.raises(ValueError, match="join a chunk"):
        await actor.send_state(b"x")

    joining = asyncio.create_task(actor.join((1, 2, 3), b"hello"))
    sent = await asyncio.to_thread(world.recv)
    assert (sent.chunk, sent.payload, sent.decay) == ((1, 2, 3), b"hello", DecayRate.EXPONENTIAL)
    world.echo(sent)
    echo = await asyncio.wait_for(joining, 3)
    assert (echo.uuid, echo.payload) == (ME, b"hello")
    assert actor.chunk == (1, 2, 3)

    other = "c" * 32
    await actor.send_to_actor(other, b"dm", (4, 5, 6))
    client.udp.flush_sends()
    direct = await asyncio.to_thread(world.recv)
    assert (direct.type, direct.chunk, direct.uuid) == (
        MessageType.SINGLE_ACTOR_MESSAGE,
        (4, 5, 6),
        other.encode(),
    )
    await client.aclose()
