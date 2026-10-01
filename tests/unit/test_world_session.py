"""World Stores over a fake replication server: everything the server sends lands in the
native stores on tick(), and the callbacks registered in Python fire from the tick."""

from __future__ import annotations

import socket
import struct
import time
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from crowdypy import wire
from crowdypy.codecs import f32, reserved, struct_codec, u16
from crowdypy.replication import Assignment, ReplicationConnection, TokenMaterial
from crowdypy.stores import WorldSessionCore
from crowdypy.wire import MessageType

TOKEN = "t" * 64
ME = "a" * 32
OTHER = "b" * 32
QUIET = {"session_ready_wait_ms": 0, "advertise_capabilities": False, "bundle_window_ms": 0}
POSE = struct_codec({"x": f32(), "y": f32(), "z": f32(), "yaw": u16(), "_": reserved(2)})


class FakeServer:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(3.0)
        self.port = self.sock.getsockname()[1]
        self.client: Any = None
        self.pending: list[bytes] = []

    def recv(self) -> wire.LongSpatialMessage:
        while True:
            while not self.pending:
                data, self.client = self.sock.recvfrom(2048)
                self.pending.extend(wire.split_datagram(data))
            raw = self.pending.pop(0)
            if raw[0] == MessageType.CLIENT_ACTOR_HEARTBEAT:
                continue
            return wire.parse_long_spatial(raw)

    def notify(
        self, kind: int, uuid: str, payload: bytes, *, chunk: Any = (1, 2, 3), sequence: int = 0
    ) -> None:
        self.sock.sendto(
            wire.encode_long_spatial(
                TOKEN, kind, 7, chunk, uuid, payload, distance=8,
                game_token_id=1_700_000_000_000, sequence=sequence,
            ),
            self.client,
        )  # fmt: skip

    def channel(self, channel_id: int, sender: str, payload: bytes) -> None:
        frame = (
            bytes([MessageType.CHANNEL_MESSAGE_NOTIFICATION])
            + struct.pack("<q", channel_id)
            + sender.encode()
            + struct.pack("<H", len(payload))
            + payload
            + struct.pack("<qB", 1_700_000_000_000, 0)
        )
        self.sock.sendto(frame, self.client)

    def close(self) -> None:
        self.sock.close()


class Provider:
    def __init__(self, port: int) -> None:
        self.port = port

    def assign_server(self) -> Assignment:
        return Assignment("127.0.0.1", "", self.port)

    def refresh_token(self, current: Assignment | None) -> TokenMaterial:
        raise RuntimeError


@pytest.fixture
def world() -> Iterator[tuple[FakeServer, ReplicationConnection, WorldSessionCore]]:
    server = FakeServer()
    connection = ReplicationConnection(
        Provider(server.port), TokenMaterial(TOKEN, 42), app_id=7, **QUIET
    )
    connection.connect()
    session = WorldSessionCore(
        object(),  # type: ignore[arg-type]  # these tests make no GraphQL or udp facade calls
        7, connection,  # type: ignore[arg-type]
        actor_uuid=ME, send_hz=1000, heartbeat_interval_ms=0, stale_after_ms=60_000,
        chunk_options={"write_back_interval_ms": None},
    )  # fmt: skip
    yield server, connection, session
    session.dispose()
    connection.close()
    server.close()


def tick_until(session: WorldSessionCore, condition: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        session.tick()
        time.sleep(0.005)


def test_your_actor_joins_and_its_echo_is_recorded(world: Any) -> None:
    server, _, session = world
    assert session.actor_uuid == ME
    session.self.join((1, 2, 3), b"hello")
    sent = server.recv()
    assert (sent.type, sent.uuid, sent.chunk, sent.payload) == (
        MessageType.ACTOR_UPDATE_REQUEST, ME.encode(), (1, 2, 3), b"hello",
    )  # fmt: skip
    assert session.self.status == "pending"
    server.notify(MessageType.ACTOR_UPDATE_NOTIFICATION, ME, b"hello", sequence=sent.sequence)
    tick_until(session, lambda: session.self.status == "acked")
    ack = session.self.last_ack()
    assert ack is not None
    assert (ack["sequence"], ack["state"]) == (sent.sequence, b"hello")
    assert session.self.chunk == (1, 2, 3)
    assert session.actors.count == 0  # your own echo is not someone else


def test_other_actors_land_in_the_registry_and_its_lanes(world: Any) -> None:
    server, _, session = world
    session.self.join((0, 0, 0), b"x")
    server.recv()
    joined: list[Any] = []
    left: list[Any] = []
    session.actors.on_join(joined.append)
    session.actors.on_leave(left.append)
    mobs = session.actors.lane("mobs", tag_offset=0, tag_value=0xEE)
    pose = POSE.encode({"x": 1.5, "y": 2.0, "z": -3.0, "yaw": 90})
    server.notify(MessageType.ACTOR_UPDATE_NOTIFICATION, OTHER, pose)
    server.notify(MessageType.ACTOR_UPDATE_NOTIFICATION, "c" * 32, b"\xee" + bytes(15))
    tick_until(session, lambda: session.actors.count == 2)

    assert [a.uuid for a in joined] == [OTHER, "c" * 32]
    assert mobs.count == 1
    assert mobs.get("c" * 32) is not None
    actor = session.actors.get(OTHER)
    assert actor is not None
    assert (actor.chunk, actor.state) == ((1, 2, 3), pose)

    snapshot = session.actors.snapshot()
    assert len(snapshot) == 2
    rows = {bytes(snapshot.uuids[i]).decode(): i for i in range(len(snapshot))}
    lengths = np.diff(snapshot.state_offsets)
    assert lengths[rows[OTHER]] == POSE.size
    decoded = POSE.decode(
        bytes(snapshot.state_data[snapshot.state_offsets[rows[OTHER]] :][: POSE.size])
    )
    assert decoded["yaw"] == 90
    assert pytest.approx(decoded["x"]) == 1.5

    server.notify(MessageType.ACTOR_LEFT_NOTIFICATION, OTHER, b"")
    tick_until(session, lambda: session.actors.count == 1)
    assert [a.uuid for a in left] == [OTHER]


def test_a_struct_codec_decodes_a_whole_lane_at_once(world: Any) -> None:
    server, _, session = world
    session.self.join((0, 0, 0), b"x")
    server.recv()
    for i in range(10):
        pose = POSE.encode({"x": float(i), "y": 0.0, "z": 0.0, "yaw": i})
        server.notify(MessageType.ACTOR_UPDATE_NOTIFICATION, f"{i:032d}", pose)
    tick_until(session, lambda: session.actors.count == 10)
    snapshot = session.actors.snapshot()
    poses = POSE.decode_many(snapshot.state_offsets, snapshot.state_data)
    assert sorted(poses["yaw"].tolist()) == list(range(10))
    assert sorted(poses["x"].tolist()) == [float(i) for i in range(10)]


def test_voxels_inboxes_events_and_errors(world: Any) -> None:
    server, connection, session = world
    session.self.join((0, 0, 0), b"x")
    joined = server.recv()
    changed: list[Any] = []
    messages: list[Any] = []
    events: list[Any] = []
    errors: list[Any] = []
    session.chunks.on_chunk_changed(changed.append)
    session.channel_inbox.on_message(messages.append)
    session.direct_inbox.on_message(messages.append)
    session.events.on(7, events.append)
    session.errors.on_error(errors.append)

    server.notify(
        MessageType.VOXEL_UPDATE_NOTIFICATION, OTHER, wire.encode_voxel_payload(1, 2, 3, 9, b"st")
    )
    server.channel(55, OTHER, b"hello channel")
    server.notify(MessageType.SINGLE_ACTOR_MESSAGE, ME, b"hello you")
    server.notify(
        MessageType.CLIENT_EVENT_NOTIFICATION, OTHER, wire.encode_event_payload(7, b"boom")
    )
    server.sock.sendto(
        bytes([MessageType.GENERIC_ERROR, joined.sequence, wire.ErrorCode.UNAUTHORIZED]),
        server.client,
    )
    tick_until(session, lambda: len(messages) == 2 and events and errors and changed)

    assert session.chunks.voxel_type_at((1, 2, 3), 1, 2, 3) == 9
    assert session.chunks.voxel_state_at((1, 2, 3), 1, 2, 3) == b"st"
    assert changed[0].coord == (1, 2, 3)
    assert {(m.channel_id, m.payload) for m in messages} == {
        (55, b"hello channel"),
        (0, b"hello you"),
    }
    assert session.channel_inbox.channels() == [55]
    assert [m.payload for m in session.channel_inbox.messages(55)] == [b"hello channel"]
    assert (events[0].event_type, events[0].sender, events[0].state) == (7, OTHER, b"boom")
    last = session.events.last_event(7)
    assert last is not None
    assert last.state == b"boom"
    error = session.errors.last()
    assert error is not None
    assert (error.code, error.kind, error.uuid) == (wire.ErrorCode.UNAUTHORIZED, "actorUpdate", ME)
    assert session.self.status == "error"
    assert errors[0].sequence == joined.sequence

    server.notify(MessageType.CLIENT_TEXT_NOTIFICATION, OTHER, b"forwarded")
    texts: list[bytes] = []
    deadline = time.monotonic() + 3
    while not texts and time.monotonic() < deadline:
        session.tick()
        texts += [
            e.payload for e in connection.poll() if e.type == MessageType.CLIENT_TEXT_NOTIFICATION
        ]
        time.sleep(0.005)
    assert texts == [b"forwarded"]


def test_local_edits_replicate_and_queue_for_write_back(world: Any) -> None:
    server, _, session = world
    session.self.join((0, 0, 0), b"x")
    server.recv()
    session.chunks.seed((4, 4, 4), bytes(4096), write_back=False)
    assert session.chunks.pending_write_backs == 0
    sequence = session.chunks.set_voxel((4, 4, 4), 1, 1, 1, 5, b"meta")
    sent = server.recv()
    assert (sent.type, sent.chunk, sent.sequence) == (
        MessageType.VOXEL_UPDATE_REQUEST,
        (4, 4, 4),
        sequence,
    )
    assert wire.parse_voxel_payload(sent.payload) == (1, 1, 1, 5, b"meta")
    assert session.chunks.voxel_type_at((4, 4, 4), 1, 1, 1) == 5
    assert session.chunks.pending_write_backs == 1
    chunk = session.chunks.get((4, 4, 4))
    assert chunk is not None
    assert chunk.dirty


def test_engine_lanes_split_mobs_from_players_natively(world: Any) -> None:
    from crowdypy.kit import FLAG_GROUNDED, FLAG_MOB, EnginePose, encode_engine_pose, engine_lanes

    server, _, session = world
    session.self.join((0, 0, 0), b"x")
    server.recv()
    lanes = {name: session.actors.lane(name, **spec) for name, spec in engine_lanes().items()}
    server.notify(
        MessageType.ACTOR_UPDATE_NOTIFICATION,
        "m" * 32,
        encode_engine_pose(EnginePose(flags=FLAG_MOB)),
    )
    server.notify(
        MessageType.ACTOR_UPDATE_NOTIFICATION,
        "p" * 32,
        encode_engine_pose(EnginePose(flags=FLAG_GROUNDED)),
    )
    tick_until(session, lambda: session.actors.count == 2)
    assert [a.uuid for a in lanes["mobs"].list()] == ["m" * 32]
    assert [a.uuid for a in lanes["players"].list()] == ["p" * 32]
    assert lanes["npcs"].count == 0
