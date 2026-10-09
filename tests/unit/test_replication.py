"""The native replication connection against a fake replication server on 127.0.0.1.

The fake server receives the client's signed datagrams, verifies them with the codec, and
answers with signed notifications, echoes and errors, as the replication-API docs describe.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from crowdypy import _native, media, wire
from crowdypy.errors import CrowdyRealtimeError, CrowdyReplicationError
from crowdypy.replication import (
    STATUS,
    Assignment,
    AsyncReplicationConnection,
    ConnState,
    ReplicationConnection,
    TokenMaterial,
)
from crowdypy.wire import DecayRate, MessageType

TOKEN = "t" * 64
TOKEN_ID = 42
ME = "a" * 32
OTHER = "b" * 32
QUIET = {"session_ready_wait_ms": 0, "advertise_capabilities": False, "bundle_window_ms": 0}


class FakeServer:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(3.0)
        self.port = self.sock.getsockname()[1]
        self.client: tuple[str, int] | None = None

    def recv(self) -> bytes:
        data, self.client = self.sock.recvfrom(2048)
        return data

    def messages(self) -> list[bytes]:
        """One datagram's messages (a bundle is unpacked)."""
        return wire.split_datagram(self.recv())

    def send(self, datagram: bytes) -> None:
        assert self.client is not None
        self.sock.sendto(datagram, self.client)

    def notify(self, message_type: int, uuid: str, payload: bytes, *, sequence: int = 0) -> None:
        self.send(
            wire.encode_long_spatial(
                TOKEN, message_type, 7, (1, 2, 3), uuid, payload,
                distance=8, game_token_id=1_700_000_000_000, sequence=sequence,
            )
        )  # fmt: skip

    def close(self) -> None:
        self.sock.close()


class Provider:
    """A session provider over the fake server, counting what the connection asked."""

    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.assigned = 0
        self.refreshed: list[Assignment | None] = []
        self.authorize = True
        self.release = threading.Event()
        self.release.set()

    def assign_server(self) -> Assignment:
        self.release.wait(10)
        self.assigned += 1
        return Assignment("127.0.0.1", "", self.server.port)

    def refresh_token(self, current: Assignment | None) -> TokenMaterial:
        self.refreshed.append(current)
        return TokenMaterial(TOKEN, TOKEN_ID, int(time.time() * 1000) + 3_600_000, self.authorize)


class AsyncProvider(Provider):
    async def assign_server(self) -> Assignment:  # type: ignore[override]
        await asyncio.sleep(0)
        return super().assign_server()

    async def refresh_token(self, current: Assignment | None) -> TokenMaterial:  # type: ignore[override]
        await asyncio.sleep(0)
        return super().refresh_token(current)


@pytest.fixture
def server() -> Iterator[FakeServer]:
    fake = FakeServer()
    yield fake
    fake.close()


def token(expires_at_ms: int = 0) -> TokenMaterial:
    return TokenMaterial(TOKEN, TOKEN_ID, expires_at_ms)


def wait_for_rows(connection: ReplicationConnection, want: int, timeout: float = 3.0) -> list[Any]:
    rows: list[Any] = []
    deadline = time.monotonic() + timeout
    while len(rows) < want and time.monotonic() < deadline:
        connection.wait(0.2)
        rows.extend(connection.poll())
    return rows


def test_a_send_is_signed_with_crowdyjs_defaults(server: FakeServer) -> None:
    provider = Provider(server)
    with ReplicationConnection(provider, token(), app_id=7, **QUIET) as connection:
        sequence = connection.send_actor_update((1, -2, 3), ME, b"pose")
        connection.flush_sends()
        (datagram,) = server.messages()
        assert wire.verify_long_spatial(TOKEN, datagram)
        message = wire.parse_long_spatial(datagram)
        assert message.type == MessageType.ACTOR_UPDATE_REQUEST
        assert (message.app_id, message.chunk, message.uuid) == (7, (1, -2, 3), ME.encode())
        assert (message.distance, message.decay) == (8, DecayRate.EXPONENTIAL)
        assert (message.payload, message.sequence) == (b"pose", sequence)
        assert provider.assigned == 1

        connection.send_audio_packet((0, 0, 0), ME, b"pcm")
        connection.flush_sends()
        audio = wire.parse_long_spatial(server.recv())
        assert (audio.type, audio.distance, audio.decay) == (MessageType.CLIENT_AUDIO_PACKET, 1, 0)


def test_a_ranged_channel_send_carries_the_connection_app(server: FakeServer) -> None:
    with ReplicationConnection(Provider(server), token(), app_id=7, **QUIET) as connection:
        sequence = connection.send_ranged_channel_message(321, ME, b"near", (4, -5, 6), 12)
        connection.flush_sends()
        (datagram,) = server.messages()
        assert datagram == wire.encode_ranged_channel_message(
            TOKEN, 321, ME, b"near", app_id=7, chunk=(4, -5, 6), max_distance=12,
            game_token_id=TOKEN_ID, sequence=sequence,
        )  # fmt: skip
        for bad in (-1, wire.CHANNEL_RANGED_MAX_DISTANCE + 1):
            with pytest.raises(CrowdyReplicationError) as refused:
                connection.send_ranged_channel_message(321, ME, b"near", (0, 0, 0), bad)
            assert refused.value.code == "InvalidArgument"


def test_notifications_arrive_as_columns_and_rows(server: FakeServer) -> None:
    with ReplicationConnection(Provider(server), token(), app_id=7, **QUIET) as connection:
        connection.send_heartbeat((0, 0, 0), ME)
        connection.flush_sends()
        server.recv()
        server.notify(MessageType.ACTOR_UPDATE_NOTIFICATION, OTHER, b"\x01\x02", sequence=9)
        server.notify(
            MessageType.VOXEL_UPDATE_NOTIFICATION,
            OTHER,
            wire.encode_voxel_payload(1, 2, 3, 77, b"st"),
        )
        events = [e for e in wait_for_rows(connection, 4) if e.type != STATUS]
        assert [e.type for e in events] == [
            MessageType.ACTOR_UPDATE_NOTIFICATION,
            MessageType.VOXEL_UPDATE_NOTIFICATION,
        ]
        actor, voxel = events
        assert (actor.uuid, actor.chunk, actor.payload, actor.sequence) == (
            OTHER,
            (1, 2, 3),
            b"\x01\x02",
            9,
        )
        assert (voxel.voxel, voxel.voxel_type, voxel.payload) == ((1, 2, 3), 77, b"st")


def test_a_batch_exposes_zero_copy_columns(server: FakeServer) -> None:
    with ReplicationConnection(Provider(server), token(), app_id=7, **QUIET) as connection:
        wait_for_rows(connection, 2)  # Connecting, Connected
        connection.send_heartbeat((0, 0, 0), ME)
        connection.flush_sends()
        server.recv()
        for i in range(5):
            server.notify(
                MessageType.ACTOR_UPDATE_NOTIFICATION, OTHER, bytes([i]) * (i + 1), sequence=i
            )
        assert connection.wait(3)
        time.sleep(0.2)
        batch = connection.poll()
        actors = batch.types == MessageType.ACTOR_UPDATE_NOTIFICATION
        assert np.count_nonzero(actors) == 5
        assert batch.chunks[actors].tolist() == [[1, 2, 3]] * 5
        assert batch.sequences[actors].tolist() == [0, 1, 2, 3, 4]
        offsets, data = batch.payload_offsets, batch.payload_data
        rows = np.flatnonzero(actors)
        assert [bytes(data[offsets[i] : offsets[i + 1]]) for i in rows] == [
            bytes([i]) * (i + 1) for i in range(5)
        ]
        assert bytes(batch.uuids[rows[0]]) == OTHER.encode()
        assert batch.chunks.base is not None or batch.chunks.flags.owndata is False


def test_status_rows_report_the_lifecycle(server: FakeServer) -> None:
    connection = ReplicationConnection(Provider(server), token(), app_id=7, **QUIET)
    connection.connect()
    states = [e.state for e in wait_for_rows(connection, 2) if e.type == STATUS]
    assert states[:2] == [ConnState.CONNECTING, ConnState.CONNECTED]
    connection.close()
    assert connection.state == ConnState.CLOSED


def test_many_actor_updates_in_one_call(server: FakeServer) -> None:
    with ReplicationConnection(Provider(server), token(), app_id=7, **QUIET) as connection:
        n = 40
        chunks = np.arange(n * 3, dtype=np.int64).reshape(n, 3)
        uuids = np.frombuffer(b"".join(f"{i:032d}".encode() for i in range(n)), dtype=np.uint8)
        states = [bytes([i]) * 8 for i in range(n)]
        assert connection.send_actor_updates(chunks, uuids, states) == n
        received: list[bytes] = []
        while len(received) < n:
            received.extend(server.messages())
        assert all(wire.verify_long_spatial(TOKEN, m) for m in received)
        parsed = [wire.parse_long_spatial(m) for m in received]
        assert [p.chunk for p in parsed] == [tuple(row) for row in chunks.tolist()]
        assert [p.payload for p in parsed] == states
        assert [p.uuid for p in parsed] == [f"{i:032d}".encode() for i in range(n)]

        assert connection.send_actor_updates([(9, 9, 9)], [ME], b"abcd", stride=4) == 1
        (last,) = server.messages()
        assert wire.parse_long_spatial(last).payload == b"abcd"


def test_a_bad_batch_is_refused_before_anything_is_sent(server: FakeServer) -> None:
    with ReplicationConnection(Provider(server), token(), app_id=7, **QUIET) as connection:
        with pytest.raises(CrowdyReplicationError) as caught:
            connection.send_actor_updates(np.zeros((2, 3), np.int64), b"x" * 32, [b"", b""])
        assert caught.value.code == "InvalidArgument"


def test_a_refresh_names_the_current_server_and_keeps_it(server: FakeServer) -> None:
    provider = Provider(server)
    soon = int(time.time() * 1000) + 1000
    options = {**QUIET, "refresh_lead_ms": 60_000}
    with ReplicationConnection(
        provider, token(expires_at_ms=soon), app_id=7, **options
    ) as connection:
        deadline = time.monotonic() + 3
        while not provider.refreshed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert provider.refreshed == [Assignment("127.0.0.1", "", server.port)]
        time.sleep(0.1)
        assert provider.assigned == 1  # authorized on the current server: no re-placement
        assert connection.state == ConnState.CONNECTED


def test_disconnect_releases_a_connect_waiting_on_the_provider(server: FakeServer) -> None:
    provider = Provider(server)
    provider.release.clear()
    connection = ReplicationConnection(provider, token(), app_id=7, **QUIET)
    errors: list[BaseException] = []

    def connect() -> None:
        try:
            connection.connect()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=connect)
    thread.start()
    time.sleep(0.2)
    started = time.monotonic()
    connection.native.bridge.close()
    thread.join(5)
    assert not thread.is_alive()
    assert time.monotonic() - started < 2
    assert isinstance(errors[0], CrowdyReplicationError)
    assert errors[0].code == "Closed"
    provider.release.set()
    connection.close()


async def test_the_loop_wakes_for_a_batch(server: FakeServer) -> None:
    async with AsyncReplicationConnection(
        AsyncProvider(server), token(), app_id=7, **QUIET
    ) as connection:
        connection.send_heartbeat((0, 0, 0), ME)
        connection.flush_sends()
        await asyncio.to_thread(server.recv)
        seen: list[Any] = []
        unsubscribe = connection.subscribe({"text": seen.append})

        async def first_text() -> Any:
            async for batch in connection.batches():
                for event in batch:
                    if event.type == MessageType.CLIENT_TEXT_NOTIFICATION:
                        return event
            return None

        waiting = asyncio.create_task(first_text())
        await asyncio.sleep(0)
        server.notify(MessageType.CLIENT_TEXT_NOTIFICATION, OTHER, b"hello")
        event = await asyncio.wait_for(waiting, 3)
        assert event.payload == b"hello"
        assert [e.payload for e in seen] == [b"hello"]
        unsubscribe()


async def test_and_wait_resolves_on_the_echo_and_raises_on_an_error(server: FakeServer) -> None:
    async with AsyncReplicationConnection(
        AsyncProvider(server), token(), app_id=7, **QUIET
    ) as connection:
        sequence = connection.send_actor_update((1, 2, 3), ME, b"p")
        connection.flush_sends()
        await asyncio.to_thread(server.recv)
        waiting = asyncio.create_task(connection.wait_for_sequence(sequence, ME, 3))
        await asyncio.sleep(0)
        server.notify(MessageType.ACTOR_UPDATE_NOTIFICATION, OTHER, b"x", sequence=sequence)
        server.notify(MessageType.ACTOR_UPDATE_NOTIFICATION, ME, b"p", sequence=sequence)
        echo = await waiting
        assert (echo.uuid, echo.payload) == (ME, b"p")

        failing = connection.send_text_packet((1, 2, 3), ME, "hi")
        connection.flush_sends()
        await asyncio.to_thread(server.recv)
        waiting = asyncio.create_task(connection.wait_for_sequence(failing, ME, 3))
        await asyncio.sleep(0)
        server.send(bytes([MessageType.GENERIC_ERROR, failing, wire.ErrorCode.UNAUTHORIZED]))
        with pytest.raises(CrowdyRealtimeError) as caught:
            await waiting
        assert caught.value.code == "UNAUTHORIZED"

        with pytest.raises(CrowdyRealtimeError) as timed_out:
            await connection.wait_for_sequence(200, ME, 0.2)
        assert timed_out.value.code == "UDP_SEQUENCE_TIMEOUT"


def test_video_frames_fragment_and_reassemble() -> None:
    frame = bytes(range(256)) * 20
    parts = media.fragment_frame(frame, frame_id=7)
    assert 1 < len(parts) <= media.MAX_VIDEO_FRAGMENTS
    assembler = media.VideoFrameAssembler()
    done = [assembler.ingest(OTHER, part, now_ms=1000) for part in reversed(parts)]
    assert done[:-1] == [None] * (len(parts) - 1)
    completed = done[-1]
    assert completed is not None
    assert (completed.uuid, completed.frame_id, completed.data) == (OTHER, 7, frame)
    assert media.parse_video_fragment_header(parts[0])[2] == 7  # type: ignore[index]
    assert media.is_newer_frame_id(1, 0xFFFF)


def test_the_native_layer_reports_its_build() -> None:
    pin = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pin["tool"]["crowdypy"]["crowdycpp"]["version"] == _native.CROWDYCPP_VERSION
    assert _native.replication.STATUS_ROW == STATUS == 255


def test_threads_send_and_poll_one_connection_at_once(server: FakeServer) -> None:
    """Sends are thread-safe and poll() is serialized natively; on a free-threaded build there
    is no GIL to hide a race."""
    with ReplicationConnection(Provider(server), token(), app_id=7, **QUIET) as connection:
        stop = threading.Event()

        def sender(k: int) -> None:
            for i in range(2000):
                connection.send_actor_update((k, i, 0), ME, b"x")

        def poller() -> None:
            while not stop.is_set():
                connection.poll()

        senders = [threading.Thread(target=sender, args=(k,)) for k in range(4)]
        polling = threading.Thread(target=poller)
        polling.start()
        for thread in senders:
            thread.start()
        for thread in senders:
            thread.join()
        stop.set()
        polling.join()
        connection.flush_sends()
        stats = connection.stats()
        assert stats["messages_sent"] == 8000
        assert stats["sends_failed"] == 0
