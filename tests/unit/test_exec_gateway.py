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
from crowdypy.errors import ApiFailure, CrowdyGraphQLError, api_failure_of
from crowdypy.exec_gateway import (
    AsyncExecConnection,
    ExecCall,
    ExecCallAs,
    ExecConnection,
    ExecPing,
    ExecPong,
    ExecPush,
    ExecReply,
    ExecSubscribe,
    decode_exec_frame,
    encode_exec_frame,
    exec_player_id,
)

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "exec-client-frames.json").read_text(
        encoding="utf-8"
    )
)


def client_frame(spec: dict[str, Any]) -> ExecCall | ExecCallAs | ExecSubscribe | ExecPing:
    kind = spec["kind"]
    if kind == "call":
        payload = bytes.fromhex(spec["payloadHex"])
        return ExecCall(spec["rid"], spec["nodeType"], spec["key"], spec["method"], payload)
    if kind == "callAs":
        payload = bytes.fromhex(spec["payloadHex"])
        return ExecCallAs(
            spec["rid"], spec["player"], spec["nodeType"], spec["key"], spec["method"], payload
        )
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


@pytest.mark.parametrize("player", [-1, 2**64, "1.5", "-3", "", " 7", True, 4.0, None])
def test_a_call_as_player_is_a_whole_number_of_64_bits(player: Any) -> None:
    with pytest.raises(ValueError, match="from 0 to 2\\^64 - 1"):
        exec_player_id(player)
    with pytest.raises(ValueError):
        encode_exec_frame(ExecCallAs(1, player, "player", "k", "grant"))


def test_a_call_as_player_is_taken_as_an_int_or_its_decimal_string() -> None:
    assert exec_player_id(0) == 0
    assert exec_player_id("42") == 42
    assert exec_player_id(2**64 - 1) == exec_player_id("18446744073709551615") == 2**64 - 1


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
        self.end_access: str | None = None
        #: Every binary message received, as sent.
        self.frames: list[bytes] = []

    async def handle(self, ws: ServerConnection) -> None:
        path = ws.request.path if ws.request is not None else ""
        self.tokens.append(path.split("token=", 1)[-1])
        if self.deny:
            await ws.close(4401, "token expired")
            return
        async for message in ws:
            assert isinstance(message, bytes)
            self.frames.append(message)
            tag = message[0]
            rid = int.from_bytes(message[1:5], "little")
            if tag == 0x04:
                await ws.send(b"\x84" + message[1:5])
                continue
            player: int | None = None
            if tag == 0x05:  # call-as: the player's id follows the request id
                player = int.from_bytes(message[5:13], "little")
                message = message[:5] + message[13:]
            node_len = message[5]
            node = message[6 : 6 + node_len].decode()
            at = 6 + node_len
            key_len = int.from_bytes(message[at : at + 2], "little")
            key = message[at + 2 : at + 2 + key_len].decode()
            at += 2 + key_len
            name_len = message[at]
            name = message[at + 1 : at + 1 + name_len].decode()
            payload = message[at + 1 + name_len :]
            if self.end_access is not None:
                await ws.close(4403, self.end_access)
                return
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
                answer = {"echo": args, "host": self.host}
                if player is not None:
                    answer["player"] = str(player)
                reply = msgspec.msgpack.encode(answer)
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


async def test_a_server_connection_calls_for_a_player_with_frame_05(
    gateways: tuple[Gateway, Gateway],
) -> None:
    a, _ = gateways
    connection = await AsyncExecConnection.open_as_server(
        f"ws://127.0.0.1:{a.port}", "server-token", call_timeout=3
    )
    assert connection.is_server
    assert await connection.call_as(42, "player", "42", "grant", {"gold": 10}) == {
        "echo": {"gold": 10},
        "host": "host-a",
        "player": "42",
    }
    # The fixture's frame, with the request id this connection allocated.
    assert a.frames[-1] == encode_exec_frame(
        ExecCallAs(1, 42, "player", "42", "grant", bytes.fromhex("81a4676f6c640a"))
    )
    assert (
        a.frames[-1].hex()
        == "05010000002a0000000000000006706c6179657202003432056772616e7481a4676f6c640a"
    )
    # A user id past 2^53 travels whole, as an int or its decimal string.
    big = 18446744073709551614
    assert (await connection.call_as(str(big), "arena", "m1", "state"))["player"] == str(big)
    assert (await connection.call_as(big, "arena", "m1", "state"))["player"] == str(big)
    reply = await connection.call_as_raw(7, "lobby", "", "report", msgspec.msgpack.encode(None))
    assert msgspec.msgpack.decode(reply)["player"] == "7"
    # The server's own call is an ordinary call frame.
    assert "player" not in await connection.call("lobby", "", "report", {"players": 3})
    assert a.frames[-1][0] == 0x01
    with pytest.raises(ValueError):
        await connection.call_as(-1, "player", "k", "grant")
    await connection.close()


async def test_call_as_is_denied_locally_on_a_connection_that_is_not_a_servers(
    gateways: tuple[Gateway, Gateway],
) -> None:
    a, _ = gateways
    connection = await AsyncExecConnection.open(f"ws://127.0.0.1:{a.port}", "t", call_timeout=3)
    assert not connection.is_server
    for call in (
        connection.call_as(42, "player", "42", "grant", {"gold": 10}),
        connection.call_as_raw(42, "player", "42", "grant"),
    ):
        with pytest.raises(CrowdyExecError) as caught:
            await call
        assert caught.value.status == "Denied"
        assert "connect_as_server" in caught.value.message
    assert a.frames == []  # nothing was sent: a gateway closes a connection sent this frame
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
        assert not connection.is_server
        before = len(gateway.frames)
        with pytest.raises(CrowdyExecError) as denied:
            connection.call_as(42, "player", "42", "grant")
        assert denied.value.status == "Denied"
        assert len(gateway.frames) == before
        connection.close()

        server = ExecConnection.open_as_server(
            f"ws://127.0.0.1:{gateway.port}", "server-token", call_timeout=3
        )
        assert server.is_server
        assert server.call_as("42", "player", "42", "grant", {"gold": 10}) == {
            "echo": {"gold": 10},
            "host": "host-sync",
            "player": "42",
        }
        assert gateway.frames[-1][0] == 0x05
        assert msgspec.msgpack.decode(server.call_as_raw(9, "player", "9", "grant", b"\xc0")) == {
            "echo": None,
            "host": "host-sync",
            "player": "9",
        }
        server.close()
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


async def test_a_4403_close_ends_the_connection_for_good(gateways: tuple[Gateway, Gateway]) -> None:
    a, _ = gateways
    dials = 0

    async def dial() -> ExecEndpoint:
        nonlocal dials
        dials += 1
        return ExecEndpoint(gateway_url=f"ws://127.0.0.1:{a.port}", token="t", host=a.host)

    connection = AsyncExecConnection(dial, call_timeout=3)
    await connection.connect()
    reasons: list[str] = []
    ended = asyncio.Event()

    def on_ended(reason: str) -> None:
        reasons.append(reason)
        ended.set()

    connection.on_access_ended(on_ended)
    a.end_access = "access ended: app access suspended"
    with pytest.raises(CrowdyExecError) as caught:
        await connection.call("combat", "m1", "hit")
    assert caught.value.status == "Denied"
    await asyncio.wait_for(ended.wait(), 3)
    await asyncio.sleep(0.5)
    assert reasons == ["access ended: app access suspended"]
    assert dials == 1  # final: no redial
    with pytest.raises(CrowdyExecError):
        await connection.call("combat", "m1", "hit")
    await connection.close()


def test_the_blocking_connection_ends_on_4403_too() -> None:
    gateway = Gateway("host-sync")
    gateway.end_access = "access ended: account_block"
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
        dials = 0

        def dial() -> ExecEndpoint:
            nonlocal dials
            dials += 1
            return ExecEndpoint(
                gateway_url=f"ws://127.0.0.1:{gateway.port}", token="t", host=gateway.host
            )

        connection = ExecConnection(dial, call_timeout=3)
        connection.connect()
        ended = threading.Event()
        reasons: list[str] = []
        connection.on_access_ended(lambda reason: (reasons.append(reason), ended.set()))
        with pytest.raises(CrowdyExecError) as caught:
            connection.call("combat", "m1", "hit")
        assert caught.value.status == "Denied"
        assert ended.wait(3)
        assert reasons == ["access ended: account_block"]
        assert dials == 1
        connection.close()
    finally:
        assert stop is not None
        loop.call_soon_threadsafe(stop.set_result, None)
        thread.join(5)


# ---- CrowdyCPP 0.95.0, CrowdyJS 18.41.0: why a hub connection could not be made ----

BUSY = CrowdyGraphQLError(
    [
        {
            "message": "the platform is busy",
            "extensions": {"code": "PLATFORM_BUSY", "retryAfterMs": 750},
        }
    ]
)
BUSY_FAILURE = ApiFailure("Rejected", "PLATFORM_BUSY", 750, "the platform is busy")


async def test_a_refused_connect_call_is_kept_and_cleared_by_the_next(
    gateways: tuple[Gateway, Gateway],
) -> None:
    a, _ = gateways
    refusals: list[Exception] = [BUSY]

    async def dial() -> ExecEndpoint:
        if refusals:
            raise refusals.pop()
        return ExecEndpoint(gateway_url=f"ws://127.0.0.1:{a.port}", token="t1", host="host-a")

    connection = AsyncExecConnection(dial, reconnect=False)
    assert connection.last_connect_failure() == ApiFailure()
    # The first connect raises the API's own error, and the connection keeps why.
    with pytest.raises(CrowdyGraphQLError) as refused:
        await connection.connect()
    assert api_failure_of(refused.value) == BUSY_FAILURE
    failure = connection.last_connect_failure()
    assert failure == BUSY_FAILURE
    assert failure.failed()
    assert failure.platform_busy()

    await connection.connect()
    assert connection.host == "host-a"
    assert not connection.last_connect_failure().failed()
    await connection.close()


def test_the_blocking_connection_keeps_why_too() -> None:
    def dial() -> ExecEndpoint:
        raise CrowdyGraphQLError(
            [{"message": "not\nyours", "extensions": {"code": "FORBIDDEN", "retryAfterMs": -4}}]
        )

    connection = ExecConnection(dial, reconnect=False)
    with pytest.raises(CrowdyGraphQLError):
        connection.connect()
    failure = connection.last_connect_failure()
    assert failure == ApiFailure("Rejected", "FORBIDDEN", 0, "not yours")
    assert not failure.platform_busy()
    connection.close()


def test_what_the_api_said_is_bounded() -> None:
    hour = CrowdyGraphQLError(
        [
            {"message": "first", "extensions": {"code": "C" * 200}},
            {"message": "second", "extensions": {"code": "PLATFORM_BUSY", "retryAfterMs": 3.6e6}},
        ]
    )
    failure = api_failure_of(hour)
    # The first error's code and message, the first wait any error names, each bounded.
    assert (failure.code, failure.retry_after_ms, failure.message) == ("C" * 64, 30_000, "first")
    long = api_failure_of(CrowdyGraphQLError([{"message": "line\r\n" + "é" * 600}]))
    assert long.code == ""
    assert long.message.startswith("line éé")
    assert len(long.message.encode()) <= 500
    assert "\n" not in long.message
    assert api_failure_of(TimeoutError()).error == "Timeout"
    assert api_failure_of(RuntimeError("boom")) == ApiFailure("Rejected", "", 0, "boom")


async def test_a_failed_connect_names_the_gateway_and_never_the_token() -> None:
    token = "connect-token-" + "s" * 50
    errors: list[CrowdyExecError] = []
    # Nothing listens here: the socket's own error is what the message carries.
    with pytest.raises(CrowdyExecError) as failed:
        await AsyncExecConnection.open(f"ws://user:pw@127.0.0.1:1/gw?token={token}", token)
    errors.append(failed.value)
    assert failed.value.status == "Unavailable"
    assert failed.value.message.startswith("Unavailable: connecting to ws://127.0.0.1:1 failed")
    # A URL that is no URL at all may be the token passed in the URL's place.
    with pytest.raises(CrowdyExecError) as failed:
        await AsyncExecConnection.open(token, token)
    errors.append(failed.value)
    assert "no socket could be opened to a gateway URL that does not parse" in str(failed.value)
    assert failed.value.__cause__ is None
    with pytest.raises(CrowdyExecError) as failed:
        ExecConnection.open(token, token)
    errors.append(failed.value)
    assert failed.value.__cause__ is None
    for error in errors:
        assert token not in str(error)
        assert "pw@" not in str(error)


def test_a_close_reason_is_shown_without_the_token_on_one_line_and_bounded() -> None:
    from crowdypy.exec_gateway import _closed_why

    token = "connect-token-" + "s" * 50
    endpoint = ExecEndpoint(
        gateway_url=f"wss://gw.example.org/?token={token}", token=token, host="h"
    )
    # The close reason is the gateway's text: it is treated as a failed connect's is.
    why = _closed_why(
        endpoint, RuntimeError(f"closed 1008 for wss://gw/?token={token}\r\n" + "x" * 900)
    )
    assert why.startswith(
        "the gateway closed the connection (closed 1008 for wss://gw/?token=<token>"
    )
    assert token not in why
    assert "\n" not in why
    assert "\r" not in why
    assert len(why) <= len("the gateway closed the connection ()") + 200
    # A connection that never had an endpoint still answers, and still hides a token in a query.
    assert "tail" not in _closed_why(None, RuntimeError("closed ?token=secret-tail"))


def test_a_dropped_read_waits_the_apis_hint_up_to_30_seconds() -> None:
    from crowdypy._dropped_read import dropped_read_wait_ms

    def busy(wait: Any) -> CrowdyGraphQLError:
        return CrowdyGraphQLError(
            [{"message": "busy", "extensions": {"code": "PLATFORM_BUSY", "retryAfterMs": wait}}]
        )

    assert dropped_read_wait_ms(busy(4_000), 1_500) == 4_000
    assert dropped_read_wait_ms(busy(200), 1_500) == 1_500  # never below the caller's interval
    assert dropped_read_wait_ms(busy(3_600_000), 1_500) == 30_000
    assert dropped_read_wait_ms(busy(3_600_000), 60_000) == 60_000  # the caller's own, if longer
    assert dropped_read_wait_ms(RuntimeError("boom"), 1_500) == 1_500
