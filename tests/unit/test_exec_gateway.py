"""The ck-exec gateway connection: the frame codec against the platform's shared fixture, and
both connections against a local WebSocket gateway."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import msgspec
import pytest
from websockets.asyncio.server import ServerConnection, serve

from crowdypy.domains.exec import CrowdyExecError, ExecEndpoint
from crowdypy.exec_gateway import (
    AsyncExecConnection,
    ExecCall,
    ExecConnection,
    ExecPing,
    ExecPong,
    ExecPush,
    ExecReply,
    ExecSubscribe,
    decode_exec_frame,
    encode_exec_frame,
)

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "exec-client-frames.json").read_text(
        encoding="utf-8"
    )
)


def client_frame(spec: dict[str, Any]) -> ExecCall | ExecSubscribe | ExecPing:
    kind = spec["kind"]
    if kind == "call":
        payload = bytes.fromhex(spec["payloadHex"])
        return ExecCall(spec["rid"], spec["nodeType"], spec["key"], spec["method"], payload)
    if kind in ("subscribe", "unsubscribe"):
        return ExecSubscribe(
            spec["rid"], spec["nodeType"], spec["key"], spec["topic"], kind == "unsubscribe"
        )
    return ExecPing(spec["nonce"])


@pytest.mark.parametrize("case", FIXTURE["client"], ids=lambda c: c["name"])
def test_client_frames_encode_as_the_fixture(case: dict[str, Any]) -> None:
    assert encode_exec_frame(client_frame(case["frame"])).hex() == case["hex"]


@pytest.mark.parametrize("case", FIXTURE["server"], ids=lambda c: c["name"])
def test_server_frames_decode_as_the_fixture(case: dict[str, Any]) -> None:
    frame = decode_exec_frame(bytes.fromhex(case["hex"]))
    spec = case["frame"]
    if spec["kind"] == "reply":
        assert frame == ExecReply(spec["rid"], spec["status"], bytes.fromhex(spec["payloadHex"]))
    elif spec["kind"] == "push":
        assert frame == ExecPush(
            spec["nodeType"], spec["key"], spec["topic"], bytes.fromhex(spec["payloadHex"])
        )
    else:
        assert frame == ExecPong(spec["nonce"])


def test_a_malformed_frame_is_bad_request() -> None:
    for data in (b"\x81\x01", b"\x99", b"\x82\x05ab"):
        with pytest.raises(CrowdyExecError) as caught:
            decode_exec_frame(data)
        assert caught.value.status == "BadRequest"


class Gateway:
    """A gateway that answers calls by method name. ``moved`` sends every call to the other
    gateway once; ``deny`` closes the handshake with 4401."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.port = 0
        self.tokens: list[str] = []
        self.moved = False
        self.deny = False

    async def handle(self, ws: ServerConnection) -> None:
        path = ws.request.path if ws.request is not None else ""
        self.tokens.append(path.split("token=", 1)[-1])
        if self.deny:
            await ws.close(4401, "token expired")
            return
        async for message in ws:
            assert isinstance(message, bytes)
            tag = message[0]
            rid = int.from_bytes(message[1:5], "little")
            if tag == 0x04:
                await ws.send(b"\x84" + message[1:5])
                continue
            node_len = message[5]
            node = message[6 : 6 + node_len].decode()
            at = 6 + node_len
            key_len = int.from_bytes(message[at : at + 2], "little")
            key = message[at + 2 : at + 2 + key_len].decode()
            at += 2 + key_len
            name_len = message[at]
            name = message[at + 1 : at + 1 + name_len].decode()
            payload = message[at + 1 + name_len :]
            if tag == 0x02:
                await ws.send(b"\x81" + rid.to_bytes(4, "little") + b"\x00")
                push = msgspec.msgpack.encode({"hp": 99})
                topic = name.encode()
                await ws.send(
                    b"\x82" + bytes([len(node)]) + node.encode() + len(key).to_bytes(2, "little") + key.encode()
                    + bytes([len(topic)]) + topic + push
                )  # fmt: skip
            elif self.moved:
                self.moved = False
                await ws.send(b"\x81" + rid.to_bytes(4, "little") + b"\x03")  # Moved
            elif name == "fail":
                await ws.send(
                    b"\x81" + rid.to_bytes(4, "little") + b"\x01unknown target"
                )  # AppError
            else:
                args = msgspec.msgpack.decode(payload)
                reply = msgspec.msgpack.encode({"echo": args, "host": self.host})
                await ws.send(b"\x81" + rid.to_bytes(4, "little") + b"\x00" + reply)


@pytest.fixture
async def gateways() -> AsyncIterator[tuple[Gateway, Gateway]]:
    a, b = Gateway("host-a"), Gateway("host-b")
    async with (
        serve(a.handle, "127.0.0.1", 0) as server_a,
        serve(b.handle, "127.0.0.1", 0) as server_b,
    ):
        a.port = next(iter(server_a.sockets)).getsockname()[1]
        b.port = next(iter(server_b.sockets)).getsockname()[1]
        yield a, b


async def test_calls_pushes_pings_errors_and_moves(gateways: tuple[Gateway, Gateway]) -> None:
    a, b = gateways
    hosts = iter([a, b, b])

    async def dial() -> ExecEndpoint:
        target = next(hosts)
        return ExecEndpoint(
            gateway_url=f"ws://127.0.0.1:{target.port}/", token="tok/en=1", host=target.host
        )

    connection = AsyncExecConnection(dial, call_timeout=3)
    await connection.connect()
    assert connection.host == "host-a"
    assert a.tokens == ["tok%2Fen%3D1"]  # the token is URL-encoded

    assert await connection.call("combat", "m1", "hit", {"weapon": 1}) == {
        "echo": {"weapon": 1},
        "host": "host-a",
    }

    pushes: list[ExecPush] = []
    arrived = asyncio.Event()

    def on_push(push: ExecPush) -> None:
        pushes.append(push)
        arrived.set()

    unsubscribe = await connection.subscribe("arena", "m1", "state", on_push)
    await asyncio.wait_for(arrived.wait(), 3)
    assert (pushes[0].topic, pushes[0].value) == ("state", {"hp": 99})
    assert await connection.ping() >= 0

    with pytest.raises(CrowdyExecError) as caught:
        await connection.call("combat", "m1", "fail")
    assert (caught.value.status, str(caught.value)) == ("AppError", "AppError: unknown target")

    moves: list[str] = []
    connection.on_reconnect(moves.append)
    a.moved = True
    assert (await connection.call("combat", "m1", "hit", 2))["host"] == "host-b"
    assert moves == ["host-b"]
    assert connection.host == "host-b"
    await unsubscribe()
    await connection.close()


async def test_a_4401_close_is_denied(gateways: tuple[Gateway, Gateway]) -> None:
    a, _ = gateways
    a.deny = True
    connection = await AsyncExecConnection.open(f"ws://127.0.0.1:{a.port}", "t", call_timeout=3)
    with pytest.raises(CrowdyExecError) as caught:
        await connection.call("combat", "m1", "hit")
    assert caught.value.status == "Denied"
    await connection.close()


def test_the_blocking_connection_calls_and_subscribes() -> None:
    gateway = Gateway("host-sync")
    ready = threading.Event()
    stop: asyncio.Future[None] | None = None
    loop = asyncio.new_event_loop()

    async def run() -> None:
        nonlocal stop
        stop = loop.create_future()
        async with serve(gateway.handle, "127.0.0.1", 0) as server:
            gateway.port = next(iter(server.sockets)).getsockname()[1]
            ready.set()
            await stop

    thread = threading.Thread(target=loop.run_until_complete, args=(run(),), daemon=True)
    thread.start()
    assert ready.wait(5)
    try:
        connection = ExecConnection.open(f"ws://127.0.0.1:{gateway.port}", "t", call_timeout=3)
        assert connection.call("combat", "k", "hit", [1, 2]) == {
            "echo": [1, 2],
            "host": "host-sync",
        }
        got = threading.Event()
        values: list[Any] = []
        connection.subscribe(
            "arena", "k", "state", lambda push: (values.append(push.value), got.set())
        )
        assert got.wait(3)
        assert values == [{"hp": 99}]
        assert connection.ping() >= 0
        connection.close()
    finally:
        assert stop is not None
        loop.call_soon_threadsafe(stop.set_result, None)
        thread.join(5)


# ---- which gateways client.exec.connect dials (CrowdyJS's execGatewayRefusal) ----

GATEWAY_CASES = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "vendor/CrowdyCPP/tools/parity/fixtures/exec-gateway-cases.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", GATEWAY_CASES["cases"], ids=lambda c: c["note"])
def test_the_gateway_check_answers_as_crowdyjs_does(case: dict[str, Any]) -> None:
    from crowdypy.domains.exec import exec_gateway_refusal

    why = exec_gateway_refusal(case["gameApi"], case["gateway"])
    assert (why is None) == case["dials"], why


async def test_connect_refuses_a_gateway_off_the_estate_without_dialing(
    api: Any, graphql: Any
) -> None:
    from crowdypy.domains.exec import ExecAPI

    api.reply_with_root(
        {
            "gatewayUrl": "wss://gw.example.org",
            "token": "secret-token",
            "host": "h",
            "expiresAt": "x",
        }
    )
    with pytest.raises(CrowdyExecError) as raised:
        await ExecAPI(graphql).connect(42, node_type="mod:x", key="7")
    assert raised.value.status == "Unavailable"
    assert "refusing the gateway wss://gw.example.org" in raised.value.message
    assert "outside the estate of ck.example.test" in raised.value.message


@pytest.mark.parametrize(("status", "expected"), [(401, "Denied"), (429, "Unavailable")])
async def test_a_refused_upgrade_reports_the_gateway_s_answer(status: int, expected: str) -> None:
    from http import HTTPStatus

    def refuse(connection: ServerConnection, request: Any) -> Any:
        return connection.respond(HTTPStatus(status), "bad   signature\n")

    async def never(connection: ServerConnection) -> None:  # pragma: no cover - refused first
        await connection.wait_closed()

    async with serve(never, "127.0.0.1", 0, process_request=refuse) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(CrowdyExecError) as raised:
            await AsyncExecConnection.open(f"ws://127.0.0.1:{port}", "t", reconnect=False)
    assert raised.value.status == expected
    assert (
        raised.value.message
        == f"{expected}: the gateway refused the connection (HTTP {status}: bad signature)"
    )
