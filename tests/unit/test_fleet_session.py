"""The fleet session against a fake realtime server on 127.0.0.1, in test_replication's manner.

The fake server receives what the session sends, checks it with the codec, and answers with
the fleet session's server-to-client messages (FLEET_CARRY_RESULT, FLEET_RELEASED,
FLEET_DELIVERY, signed bundles, refusals and releases), as the wire-format docs describe.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import pytest

from crowdypy import fleet as fleet_module
from crowdypy import wire
from crowdypy.domains.fleet_sessions import FleetServer, FleetSessionAnswer
from crowdypy.domains.portal import AppTokenResponse
from crowdypy.errors import CrowdyRealtimeError, CrowdyReplicationError
from crowdypy.fleet import (
    CARRY_RESULT,
    TABLE_MARGIN,
    ActorNotPresent,
    AsyncFleetSession,
    FleetBatchSend,
    FleetCarryOutcome,
    FleetCarryResult,
    FleetNotCarriedError,
    FleetPlayer,
    FleetPlayerToken,
    FleetReleased,
    FleetSendRefused,
    FleetSession,
    NotPresentEnd,
    fleet_current_server,
)
from crowdypy.replication import NOT_PRESENT, NOT_PRESENT_ENDED, STATUS, Assignment, ConnState
from crowdypy.wire import DecayRate, ErrorCode, MessageType

FLEET_TOKEN = "fleet-token-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP"
PLAYER_TOKEN = "player-token-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO"
FLEET_ID = 9001
PLAYER_ID = 4242
USER_ID = 77
APP_ID = 7
ACTOR = "0123456789abcdef0123456789abcdef"
OTHER = "b" * 32

# ACTOR_UPDATE_REQUEST (128) for the carried player: chunk (1,-2,3), distance 8, no decay, the
# actor above, payload de ad be ef. CrowdyCPP's golden vector; its last byte is the sequence.
GOLDEN_CARRIED_ACTOR_UPDATE = (
    "8007000000000000000100000000000000feffffffffffffff03000000000000000800013031323334353637"
    "383961626364656630313233343536373839616263646566deadbeefafca3c1e53f866c6384fb186e2396664"
    "b62d7acd0fee6d9445b96e283d1c09f4921000000000000001"
)

# One message a datagram and no periodic capabilities, so a test reads exactly what it caused;
# the silence timeout is off, since these tests move no clock past a server that says nothing.
QUIET: dict[str, Any] = {
    "session_ready_wait_ms": 0,
    "bundle_sends": False,
    "capabilities_interval_ms": 3_600_000,
    "silence_timeout_ms": 0,
    # A carry is sent once unless a test asks for re-sends. At the default (250 ms) a slow
    # runner's answer came after a re-send, and the next read took that re-send for the next
    # player's carry (Windows, 2026-11-28).
    "carry_retry_ms": 3_600_000,
}


def le64(value: int) -> bytes:
    return value.to_bytes(8, "little", signed=True)


def token_for(player: int) -> str:
    """Another player's token: 64 characters ending in the id."""
    return f"{player:p>64}"


# ---- what the realtime server sends a fleet session ----


def carry_result(player: int, user: int, status: int) -> bytes:
    return bytes([MessageType.FLEET_CARRY_RESULT]) + le64(player) + le64(user) + bytes([status])


def released_frame(player: int, reason: int) -> bytes:
    return bytes([MessageType.FLEET_RELEASED]) + le64(player) + bytes([reason])


def delivery(players: list[int], notification: bytes) -> bytes:
    return (
        bytes([MessageType.FLEET_DELIVERY, len(players)])
        + b"".join(le64(p) for p in players)
        + notification
    )


def own(frame: bytes, key: str = FLEET_TOKEN) -> bytes:
    """A 51, 52 or 53 as it travels alone or in a plain bundle: followed by its own HMAC."""
    return frame + wire.spatial_hmac(key, frame)


def notification(
    kind: int, payload: bytes, *, sequence: int = 0, key: str = FLEET_TOKEN, app_id: int = APP_ID
) -> bytes:
    """A signed server-to-client long-spatial notification (epoch millis in the tail)."""
    return wire.encode_long_spatial(
        key, kind, app_id, (1, 2, 3), OTHER, payload,
        distance=8, game_token_id=1_700_000_000_000, sequence=sequence,
    )  # fmt: skip


def unsigned(note: bytes) -> bytes:
    """The same notification in its unsigned form: containsAuth 0 and no HMAC of its own, as a
    signed bundle's member is and as the notification inside a FLEET_DELIVERY always is."""
    body = bytearray(note)
    body[35] = 0  # containsAuth
    del body[-41:-9]  # the HMAC, before the 9-byte tail
    return bytes(body)


def channel_note(kind: int, channel_id: int, payload: bytes) -> bytes:
    """A channel notification (18) or channel audio (36): no HMAC in its layout."""
    return (
        bytes([kind])
        + le64(channel_id)
        + b"c" * 32
        + len(payload).to_bytes(2, "little")
        + payload
        + le64(1_700_000_000_001)
        + bytes([9])
    )


def signed_bundle(members: list[bytes]) -> bytes:
    """MESSAGE_BUNDLE_SIGNED (30) over ``members``, signed with the fleet token."""
    body = bytes([MessageType.MESSAGE_BUNDLE_SIGNED]) + b"".join(
        len(m).to_bytes(2, "little") + m for m in members
    )
    return body + wire.spatial_hmac(FLEET_TOKEN, body)


def plain_bundle(members: list[bytes]) -> bytes:
    return bytes([MessageType.MESSAGE_BUNDLE]) + b"".join(
        len(m).to_bytes(2, "little") + m for m in members
    )


def bound_release(reason: int, nonce: bytes, key: str = FLEET_TOKEN) -> bytes:
    prefix = bytes([MessageType.COMMAND_SESSION_RELEASED_BOUND, reason]) + nonce
    return prefix + hmac.new(key.encode("latin-1"), prefix, hashlib.sha256).digest()


class FakeServer:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(3.0)
        self.port = self.sock.getsockname()[1]
        self.client: tuple[str, int] | None = None

    def recv(self, timeout: float = 3.0) -> bytes:
        self.sock.settimeout(timeout)
        data, self.client = self.sock.recvfrom(2048)
        return data

    def expect_nothing(self, within: float = 0.08) -> None:
        self.sock.settimeout(within)
        with pytest.raises(TimeoutError):
            self.sock.recvfrom(2048)

    def send(self, datagram: bytes) -> None:
        assert self.client is not None
        self.sock.sendto(datagram, self.client)

    def close(self) -> None:
        self.sock.close()

    def answer(self, **over: Any) -> dict[str, Any]:
        """What ``openFleetSession`` answers for a session on this server."""
        return {
            "token": FLEET_TOKEN,
            "gameTokenId": str(FLEET_ID),
            "appId": str(APP_ID),
            "expiresAt": None,
            "maxPlayers": 256,
            "server": {"ip4": "127.0.0.1", "ip6": None, "clientPort": self.port},
            **over,
        }


@pytest.fixture
def server() -> Iterator[FakeServer]:
    fake = FakeServer()
    yield fake
    fake.close()


class Seen:
    """Every handler call, in order, by name."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def handlers(self) -> dict[str, Callable[..., Any]]:
        return {
            name: (lambda *args, name=name: self.calls.append((name, args)))
            for name in fleet_module.HANDLER_NAMES
            if name != "any"
        }

    def of(self, name: str) -> list[tuple[Any, ...]]:
        return [args for called, args in self.calls if called == name]


def poll_until(fleet: FleetSession, done: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        fleet.wait(0.02)
        fleet.poll()
    fleet.poll()
    assert done(), "the session did not get there in time"


def settle(fleet: FleetSession, seconds: float = 0.1) -> None:
    """A few passes with nothing expected, so anything wrongly queued would be dispatched."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        fleet.wait(0.02)
        fleet.poll()


def tail_id(datagram: bytes, key: str = FLEET_TOKEN) -> int:
    """The game token id in a request's tail, after checking that ``key`` signed it."""
    signed, tag = datagram[:-41], datagram[-41:-9]
    assert wire.spatial_hmac(key, signed) == tag
    return int.from_bytes(datagram[-9:-1], "little", signed=True)


def read_capabilities(datagram: bytes) -> tuple[int, bytes]:
    """The flags and the control nonce of the session's CLIENT_CAPABILITIES: under its own id,
    signed with the fleet token."""
    message = wire.parse_long_spatial(datagram)
    assert message.type == MessageType.CLIENT_CAPABILITIES
    assert message.epoch_or_token_id == FLEET_ID
    assert wire.verify_long_spatial(FLEET_TOKEN, datagram)
    assert len(message.payload) == 4 + wire.CONTROL_NONCE_BYTES
    return int.from_bytes(message.payload[:4], "little"), bytes(message.payload[4:])


def read_carry(datagram: bytes, player_token: str = PLAYER_TOKEN) -> tuple[int, int]:
    """The player and the time a FLEET_CARRY names, after checking that the message is the
    session's own and that its proof was made with the player's token."""
    message = wire.parse_long_spatial(datagram)
    assert message.type == MessageType.FLEET_CARRY
    assert (message.app_id, message.chunk, message.distance) == (APP_ID, (0, 0, 0), 0)
    assert (message.uuid, message.epoch_or_token_id) == (b"", FLEET_ID)
    assert wire.verify_long_spatial(FLEET_TOKEN, datagram)
    assert len(message.payload) == wire.FLEET_CARRY_PAYLOAD_BYTES
    player = int.from_bytes(message.payload[:8], "little", signed=True)
    epoch_ms = int.from_bytes(message.payload[8:16], "little", signed=True)
    assert message.payload[16:] == wire.fleet_carry_proof(player_token, FLEET_ID, player, epoch_ms)
    return player, epoch_ms


def open_connected(server: FakeServer, seen: Seen | None = None, **options: Any) -> FleetSession:
    """A connected session; its first message, the capabilities that bind it, is read."""
    handlers = seen.handlers() if seen is not None else None
    fleet = FleetSession(server.answer(), handlers=handlers, **{**QUIET, **options})
    fleet.connect()
    flags, nonce = read_capabilities(server.recv())
    assert flags & 0b11 == 0b11  # BUNDLE_SIGNED and BOUND_CONTROL
    fleet.nonce = nonce  # type: ignore[attr-defined]
    poll_until(fleet, lambda: fleet.state == ConnState.CONNECTED)
    return fleet


def carry_as(
    fleet: FleetSession,
    server: FakeServer,
    player: int = PLAYER_ID,
    user: int = USER_ID,
    token: str = PLAYER_TOKEN,
) -> None:
    fleet.carry_nowait(FleetPlayerToken(token, player))
    assert read_carry(server.recv(), token)[0] == player
    server.send(own(carry_result(player, user, 0)))
    poll_until(fleet, lambda: fleet.carries(player))


# ---- carrying, sending and releasing ----


def test_carry_send_and_release(server: FakeServer) -> None:
    seen = Seen()
    fleet = open_connected(server, seen)
    try:
        # A send for a player the session does not carry leaves nothing on the wire.
        with pytest.raises(FleetNotCarriedError) as refused:
            fleet.send_actor_update(PLAYER_ID, (1, -2, 3), ACTOR, b"pose")
        assert (refused.value.code, refused.value.player_game_token_id) == ("NotCarried", PLAYER_ID)
        assert isinstance(refused.value, CrowdyReplicationError)
        server.expect_nothing()

        before = int(time.time() * 1000)
        assert (
            fleet.carry_nowait({"token": PLAYER_TOKEN, "gameTokenId": str(PLAYER_ID)}) == PLAYER_ID
        )
        player, epoch_ms = read_carry(server.recv())
        assert player == PLAYER_ID
        assert before - 5 <= epoch_ms <= int(time.time() * 1000) + 5
        assert fleet.players() == [FleetPlayer(PLAYER_ID, None, False, True)]
        assert not fleet.carries(PLAYER_ID)
        with pytest.raises(FleetNotCarriedError):  # a carry still waiting for its result
            fleet.send_actor_update(PLAYER_ID, (1, -2, 3), ACTOR, b"pose")

        server.send(own(carry_result(PLAYER_ID, USER_ID, 0)))
        poll_until(fleet, lambda: fleet.carries(PLAYER_ID))
        assert seen.of("carry_result") == [
            (FleetCarryResult(PLAYER_ID, USER_ID, FleetCarryOutcome.CARRIED, 0),)
        ]
        assert seen.of("carry_result")[0][0].carried
        assert fleet.players() == [FleetPlayer(PLAYER_ID, USER_ID, True, False)]
        assert fleet.carried_count == 1
        assert fleet.carries(str(PLAYER_ID))  # an id is taken as its decimal string too

        sequence = fleet.send_actor_update(
            PLAYER_ID, (1, -2, 3), ACTOR, bytes.fromhex("deadbeef"), decay=DecayRate.NONE
        )
        sent = server.recv()
        assert sent[:-1].hex() == GOLDEN_CARRIED_ACTOR_UPDATE[:-2]
        assert sent[-1] == sequence
        assert tail_id(sent) == PLAYER_ID  # the player's id, the fleet token's HMAC
        assert wire.spatial_hmac(PLAYER_TOKEN, sent[:-41]) != sent[-41:-9]

        assert fleet.release(PLAYER_ID) is True
        release = wire.parse_long_spatial(server.recv())
        assert (release.type, release.payload) == (MessageType.FLEET_RELEASE, le64(PLAYER_ID))
        assert release.epoch_or_token_id == FLEET_ID
        assert not fleet.carries(PLAYER_ID)
        assert fleet.players() == []
        with pytest.raises(FleetNotCarriedError):
            fleet.send_actor_update(PLAYER_ID, (1, -2, 3), ACTOR, b"pose")

        stats = fleet.stats()
        assert stats["messages_sent_by_type"] == {29: 1, 49: 1, 128: 1, 50: 1}
        assert stats["messages_received_by_type"] == {51: 1}
        assert (stats["hmac_failures"], stats["wrong_app"], stats["unsigned_notifications"]) == (
            0,
            0,
            0,
        )
        assert [state for (state,) in seen.of("status")] == [
            ConnState.CONNECTING,
            ConnState.CONNECTED,
        ]
    finally:
        fleet.close()
    assert fleet.state == ConnState.CLOSED


def test_every_send_names_the_carried_player_and_is_signed_by_the_fleet_token(
    server: FakeServer,
) -> None:
    fleet = open_connected(server)
    try:
        carry_as(fleet, server)
        chunk = (4, -5, 6)
        sends: list[tuple[int, Callable[[], int]]] = [
            (MessageType.ACTOR_UPDATE_REQUEST, lambda: fleet.send_actor_update(PLAYER_ID, chunk, ACTOR, b"s")),
            (MessageType.VOXEL_UPDATE_REQUEST, lambda: fleet.send_voxel_update(PLAYER_ID, chunk, ACTOR, (1, 2, 3), 9, b"st")),
            (MessageType.CLIENT_EVENT_NOTIFICATION, lambda: fleet.send_client_event(PLAYER_ID, chunk, ACTOR, 12, b"ev")),
            (MessageType.CLIENT_TEXT_PACKET, lambda: fleet.send_text_packet(PLAYER_ID, chunk, ACTOR, "hello")),
            (MessageType.CLIENT_AUDIO_PACKET, lambda: fleet.send_audio_packet(PLAYER_ID, chunk, ACTOR, b"pcm")),
            (MessageType.CLIENT_VIDEO_PACKET, lambda: fleet.send_video_packet(PLAYER_ID, chunk, ACTOR, b"frag")),
            (MessageType.GENERIC_SPATIAL_1, lambda: fleet.send_generic_spatial(PLAYER_ID, chunk, ACTOR, b"app")),
            (MessageType.SINGLE_ACTOR_MESSAGE, lambda: fleet.send_single_actor_message(PLAYER_ID, chunk, OTHER, b"dm")),
            (MessageType.CHANNEL_MESSAGE_REQUEST, lambda: fleet.send_channel_message(PLAYER_ID, 321, ACTOR, b"chat")),
            (MessageType.CHANNEL_AUDIO_REQUEST, lambda: fleet.send_channel_audio(PLAYER_ID, "321", ACTOR, b"voice")),
            (MessageType.CHANNEL_MESSAGE_RANGED_REQUEST, lambda: fleet.send_ranged_channel_message(PLAYER_ID, 321, ACTOR, b"near", chunk, 12)),
            (MessageType.CLIENT_ACTOR_HEARTBEAT, lambda: fleet.send_heartbeat(PLAYER_ID, chunk, ACTOR)),
        ]  # fmt: skip
        for kind, send in sends:
            sequence = send()
            datagram = server.recv()
            assert datagram[0] == kind, MessageType(kind).name
            assert tail_id(datagram) == PLAYER_ID, MessageType(kind).name
            assert datagram[-1] == sequence

        text = wire.parse_long_spatial(
            (fleet.send_text_packet(PLAYER_ID, chunk, ACTOR, "héllo"), server.recv())[1]
        )
        assert (text.payload, text.chunk, text.app_id) == ("héllo".encode(), chunk, APP_ID)

        assert fleet.send_video_frame(PLAYER_ID, chunk, ACTOR, bytes(2500), 3) == 3
        for _ in range(3):
            fragment = server.recv()
            assert fragment[0] == MessageType.CLIENT_VIDEO_PACKET
            assert tail_id(fragment) == PLAYER_ID

        # Each carried player has their own speaker lists.
        fleet.set_speakers(PLAYER_ID, voice=[OTHER])
        speakers = wire.parse_long_spatial(server.recv())
        assert speakers.type == MessageType.CLIENT_MEDIA_SPEAKERS
        assert speakers.epoch_or_token_id == PLAYER_ID
        assert speakers.payload == bytes([1, 1, 0, 0]) + OTHER.encode()
        fleet.set_speakers(PLAYER_ID, video=None)  # everyone in range again
        assert wire.parse_long_spatial(server.recv()).payload == bytes([0, 0, 2, 0])

        # The session's own keepalive registers no actor and goes under its own id.
        fleet.send_session_heartbeat()
        own_beat = wire.parse_long_spatial(server.recv())
        assert own_beat.type == MessageType.CLIENT_ACTOR_HEARTBEAT
        assert (own_beat.epoch_or_token_id, own_beat.uuid, own_beat.chunk) == (
            FLEET_ID,
            b"",
            (0, 0, 0),
        )

        # Refused before anything is sent.
        for bad in (
            lambda: fleet.send_voxel_update(PLAYER_ID, chunk, ACTOR, (0, 0, 0), 1, bytes(1025)),
            lambda: fleet.send_voxel_update(PLAYER_ID, chunk, ACTOR, (40000, 0, 0), 1),
            lambda: fleet.send_ranged_channel_message(PLAYER_ID, 321, ACTOR, b"x", chunk, -1),
            lambda: fleet.send_actor_update(PLAYER_ID, chunk, ACTOR, b"s", distance=9),
            lambda: fleet.set_speakers(PLAYER_ID, voice=["short"]),
            lambda: fleet.send_actor_update("not-a-number", chunk, ACTOR, b"s"),
        ):
            with pytest.raises(CrowdyReplicationError) as refused:
                bad()
            assert refused.value.code == "InvalidArgument"
        for other in (
            lambda: fleet.send_text_packet(999, chunk, ACTOR, "x"),
            lambda: fleet.send_channel_message(999, 321, ACTOR, b"x"),
            lambda: fleet.send_heartbeat(999, chunk, ACTOR),
            lambda: fleet.set_speakers(999, voice=None),
            lambda: fleet.send_video_frame(999, chunk, ACTOR, b"f", 1),
        ):
            with pytest.raises(FleetNotCarriedError):
                other()
        server.expect_nothing()
    finally:
        fleet.close()


def test_a_tick_of_actor_updates_is_one_call(server: FakeServer) -> None:
    fleet = open_connected(server)
    try:
        players = [1001, 1002, 1003]
        for player in players:
            carry_as(fleet, server, player, player + 1, token_for(player))
        chunks = [(1, 0, 0), (2, 0, 0), (3, 0, 0), (4, 0, 0)]
        uuids = [f"{i:0>32}" for i in range(4)]
        # The second row's player is not carried: it is skipped and named, the rest leave.
        sent = fleet.send_actor_updates(
            [1001, 9999, 1002, "1003"], chunks, uuids, [b"a", b"bb", b"ccc", b""]
        )
        assert sent == FleetBatchSend(accepted=3, not_carried=[1])
        got = [wire.parse_long_spatial(server.recv()) for _ in range(3)]
        assert [(m.epoch_or_token_id, m.chunk, m.payload) for m in got] == [
            (1001, (1, 0, 0), b"a"),
            (1002, (3, 0, 0), b"ccc"),
            (1003, (4, 0, 0), b""),
        ]
        assert all(m.type == MessageType.ACTOR_UPDATE_REQUEST and m.decay == 1 for m in got)

        # Columns: numpy arrays in, a fixed stride for the states.
        ids = np.array(players, dtype=np.int64)
        cells = np.array([[5, 0, 0], [6, 0, 0], [7, 0, 0]], dtype=np.int64)
        names = np.frombuffer("".join(uuids[:3]).encode(), dtype=np.uint8)
        sent = fleet.send_actor_updates(ids, cells, names, b"xxyyzz", stride=2, distance=4)
        assert sent == FleetBatchSend(accepted=3, not_carried=[])
        got = [wire.parse_long_spatial(server.recv()) for _ in range(3)]
        assert [(m.epoch_or_token_id, m.payload, m.distance) for m in got] == [
            (1001, b"xx", 4),
            (1002, b"yy", 4),
            (1003, b"zz", 4),
        ]

        with pytest.raises(CrowdyReplicationError) as refused:
            fleet.send_actor_updates([1001, 1002], chunks, uuids, [b"a", b"b"])
        assert refused.value.code == "InvalidArgument"
        with pytest.raises(CrowdyReplicationError):
            fleet.send_actor_updates(["x"], [(0, 0, 0)], [uuids[0]], [b"a"])
    finally:
        fleet.close()


# ---- how a carry ends ----


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (2, FleetCarryOutcome.OTHER_ORGANIZATION),
        (3, FleetCarryOutcome.PROOF_FAILED),
        (4, FleetCarryOutcome.SESSION_FULL),
        (9, FleetCarryOutcome.REFUSED),
    ],
)
def test_a_refused_carry_ends_as_its_status(
    server: FakeServer, status: int, outcome: FleetCarryOutcome
) -> None:
    seen = Seen()
    fleet = open_connected(server, seen)
    try:
        fleet.carry_nowait(FleetPlayerToken(PLAYER_TOKEN, PLAYER_ID))
        read_carry(server.recv())
        server.send(own(carry_result(PLAYER_ID, 0, status)))
        poll_until(fleet, lambda: bool(seen.of("carry_result")))
        (result,) = seen.of("carry_result")[0]
        assert result == FleetCarryResult(PLAYER_ID, None, outcome, status)
        assert not result.carried
        assert fleet.players() == []  # a refused carry leaves the table
        assert not fleet.carries(PLAYER_ID)
    finally:
        fleet.close()


def test_no_live_token_is_asked_once_more(server: FakeServer) -> None:
    seen = Seen()
    fleet = open_connected(server, seen, carry_retry_ms=60)
    try:
        fleet.carry_nowait(FleetPlayerToken(PLAYER_TOKEN, PLAYER_ID))
        _, first = read_carry(server.recv())
        server.send(own(carry_result(PLAYER_ID, 0, 1)))
        # A token minted a moment ago may not have reached the server yet: the answer settles
        # nothing, and the carry goes again with a new time and a new proof.
        poll_until(fleet, lambda: fleet.stats()["no_live_token_retries"] == 1)
        assert seen.of("carry_result") == []
        _, second = read_carry(server.recv())
        assert second > first
        # Only an answer that comes after that carry settles it.
        server.send(own(carry_result(PLAYER_ID, 0, 1)))
        poll_until(fleet, lambda: bool(seen.of("carry_result")))
        assert seen.of("carry_result") == [
            (FleetCarryResult(PLAYER_ID, None, FleetCarryOutcome.NO_LIVE_TOKEN, 1),)
        ]
        assert fleet.stats()["no_live_token_retries"] == 1
    finally:
        fleet.close()


def test_an_unanswered_carry_is_sent_again_and_times_out(server: FakeServer) -> None:
    seen = Seen()
    fleet = open_connected(server, seen, carry_retry_ms=60, carry_timeout_ms=250)
    try:
        started = time.monotonic()
        fleet.carry_nowait(FleetPlayerToken(PLAYER_TOKEN, PLAYER_ID))
        times = [read_carry(server.recv())[1] for _ in range(3)]
        assert times == sorted(set(times))  # each send has a new time, so a new proof
        poll_until(fleet, lambda: bool(seen.of("carry_result")))
        assert seen.of("carry_result") == [
            (FleetCarryResult(PLAYER_ID, None, FleetCarryOutcome.TIMED_OUT, None),)
        ]
        assert 0.2 <= time.monotonic() - started < 2.0
        assert fleet.players() == []
    finally:
        fleet.close()


def test_the_blocking_carry_returns_how_it_ended(server: FakeServer) -> None:
    fleet = open_connected(server)
    try:

        def answer() -> None:
            read_carry(server.recv())
            server.send(own(carry_result(PLAYER_ID, USER_ID, 0)))

        thread = threading.Thread(target=answer)
        thread.start()
        minted = AppTokenResponse(
            token=PLAYER_TOKEN,
            game_token_id=str(PLAYER_ID),
            app_id="7",
            expires_at="2099-01-01T00:00:00Z",
        )
        result = fleet.carry(minted)  # the response of the mint is taken as it is
        thread.join(3)
        assert result == FleetCarryResult(PLAYER_ID, USER_ID, FleetCarryOutcome.CARRIED, 0)
        # The batch read while waiting is kept for the next poll.
        kept = fleet.poll()
        assert CARRY_RESULT in [event.type for event, _ in kept]

        # release() while a carry waits ends it `released`.
        def release() -> None:
            read_carry(server.recv(), token_for(1001))
            fleet.release(1001)

        thread = threading.Thread(target=release)
        thread.start()
        result = fleet.carry(FleetPlayerToken(token_for(1001), 1001))
        thread.join(3)
        assert result == FleetCarryResult(1001, None, FleetCarryOutcome.RELEASED, None)
    finally:
        fleet.close()


def test_a_carry_needs_a_connected_session_and_a_players_token(server: FakeServer) -> None:
    fleet = FleetSession(server.answer(), **QUIET)
    with pytest.raises(CrowdyReplicationError) as refused:
        fleet.carry_nowait(FleetPlayerToken(PLAYER_TOKEN, PLAYER_ID))
    assert refused.value.code == "NotConnected"
    fleet.connect()
    try:
        for bad in (
            FleetPlayerToken("short", PLAYER_ID),
            {"token": PLAYER_TOKEN},
            {"gameTokenId": PLAYER_ID},
            FleetPlayerToken(PLAYER_TOKEN, "4x"),
            FleetPlayerToken(PLAYER_TOKEN, 2**63),
            FleetPlayerToken(PLAYER_TOKEN, FLEET_ID),  # the session's own id is not a player
        ):
            with pytest.raises(CrowdyReplicationError) as refused:
                fleet.carry_nowait(bad)
            assert refused.value.code == "InvalidArgument"
            assert PLAYER_TOKEN not in str(refused.value)
        assert fleet.players() == []
    finally:
        fleet.close()
    with pytest.raises(CrowdyRealtimeError) as closed:
        fleet.carry_nowait(FleetPlayerToken(PLAYER_TOKEN, PLAYER_ID))
    assert closed.value.code == "FLEET_CLOSED"


def test_a_refreshed_token_replaces_the_old_one_with_no_gap(server: FakeServer) -> None:
    seen = Seen()
    fleet = open_connected(server, seen)
    try:
        carry_as(fleet, server)
        new_token, new_id = token_for(5000), 5000
        fleet.carry_nowait(FleetPlayerToken(new_token, new_id, replaces=PLAYER_ID))
        assert read_carry(server.recv(), new_token)[0] == new_id
        # The player stays carried throughout, under both ids, and every send names the new
        # id from the moment the new FLEET_CARRY left.
        assert fleet.carries(PLAYER_ID)
        assert fleet.carries(new_id)
        fleet.send_actor_update(PLAYER_ID, (0, 0, 0), ACTOR, b"a")
        assert tail_id(server.recv()) == new_id
        fleet.send_actor_update(new_id, (0, 0, 0), ACTOR, b"b")
        assert tail_id(server.recv()) == new_id

        server.send(own(carry_result(new_id, USER_ID, 0)))
        poll_until(fleet, lambda: len(seen.of("carry_result")) == 2)
        # Once the carry is confirmed the old id names nobody.
        assert fleet.players() == [FleetPlayer(new_id, USER_ID, True, False)]
        assert not fleet.carries(PLAYER_ID)
        with pytest.raises(FleetNotCarriedError):
            fleet.send_actor_update(PLAYER_ID, (0, 0, 0), ACTOR, b"c")

        # A late word about the old id changes nothing, and is counted.
        server.send(own(released_frame(PLAYER_ID, 2)))
        poll_until(fleet, lambda: fleet.stats()["replaced_id_frames"] == 1)
        assert seen.of("released") == []
        assert fleet.carries(new_id)
    finally:
        fleet.close()


# ---- what the session receives ----


def test_state_arrives_for_the_session_and_communications_list_their_players(
    server: FakeServer,
) -> None:
    seen = Seen()
    fleet = open_connected(server, seen)
    every: list[tuple[int, Any]] = []
    fleet.set_handlers(
        {**seen.handlers(), "any": lambda n, players: every.append((n.type, players))}
    )
    try:
        carry_as(fleet, server)
        carry_as(fleet, server, 4243, 78, token_for(4243))

        # State arrives plain, once, for the session as a whole.
        server.send(notification(MessageType.ACTOR_UPDATE_NOTIFICATION, b"\x01\x02", sequence=9))
        # A communication arrives inside a FLEET_DELIVERY, with the only players it is for.
        text = unsigned(notification(MessageType.CLIENT_TEXT_NOTIFICATION, b"hi", sequence=4))
        server.send(own(delivery([PLAYER_ID, 4243], text)))
        poll_until(fleet, lambda: bool(seen.of("text")))

        (actor, actor_players) = seen.of("actor_update")[0]
        assert actor_players is None
        assert (actor.type, actor.app_id, actor.chunk) == (
            MessageType.ACTOR_UPDATE_NOTIFICATION,
            7,
            (1, 2, 3),
        )
        assert (actor.uuid, actor.payload, actor.sequence) == (OTHER, b"\x01\x02", 9)
        (said, said_players) = seen.of("text")[0]
        assert said_players == (PLAYER_ID, 4243)
        assert (said.payload, said.sequence, said.uuid) == (b"hi", 4, OTHER)

        # Both signed forms: bare members of a signed bundle, and members of a plain bundle
        # that carry their own HMAC.
        voxel = unsigned(
            notification(
                MessageType.VOXEL_UPDATE_NOTIFICATION, wire.encode_voxel_payload(1, 2, 3, 77, b"st")
            )
        )
        event = unsigned(
            notification(
                MessageType.CLIENT_EVENT_NOTIFICATION, wire.encode_event_payload(12, b"ev")
            )
        )
        server.send(
            signed_bundle(
                [
                    voxel,
                    delivery([4243], event),
                    delivery(
                        [PLAYER_ID],
                        channel_note(MessageType.CHANNEL_MESSAGE_NOTIFICATION, 321, b"chat"),
                    ),
                ]
            )
        )
        server.send(
            plain_bundle(
                [
                    own(
                        delivery(
                            [PLAYER_ID, 4243],
                            channel_note(MessageType.CHANNEL_AUDIO_NOTIFICATION, 321, b"voice"),
                        )
                    )
                ]
            )
        )
        poll_until(fleet, lambda: bool(seen.of("channel_audio")))

        (edit, edit_players) = seen.of("voxel_update")[0]
        assert edit_players is None
        assert (edit.voxel, edit.voxel_type, edit.payload) == ((1, 2, 3), 77, b"st")
        (happened, event_players) = seen.of("client_event")[0]
        assert (happened.event_type, happened.payload, event_players) == (12, b"ev", (4243,))
        (chat, chat_players) = seen.of("channel_message")[0]
        assert (chat.channel_id, chat.payload, chat.uuid, chat_players) == (
            321,
            b"chat",
            "c" * 32,
            (PLAYER_ID,),
        )
        (voice, voice_players) = seen.of("channel_audio")[0]
        assert (voice.channel_id, voice.payload, voice_players) == (
            321,
            b"voice",
            (PLAYER_ID, 4243),
        )

        # `any` hears each notification before its own handler, and none of the session's own
        # events.
        assert every == [
            (MessageType.ACTOR_UPDATE_NOTIFICATION, None),
            (MessageType.CLIENT_TEXT_NOTIFICATION, (PLAYER_ID, 4243)),
            (MessageType.VOXEL_UPDATE_NOTIFICATION, None),
            (MessageType.CLIENT_EVENT_NOTIFICATION, (4243,)),
            (MessageType.CHANNEL_MESSAGE_NOTIFICATION, (PLAYER_ID,)),
            (MessageType.CHANNEL_AUDIO_NOTIFICATION, (PLAYER_ID, 4243)),
        ]
        assert fleet.stats()["signed_bundles_received"] == 1
    finally:
        fleet.close()


def test_only_what_the_fleet_token_signed_for_its_own_app_reaches_a_handler(
    server: FakeServer,
) -> None:
    seen = Seen()
    fleet = open_connected(server, seen)
    try:
        carry_as(fleet, server)
        text = unsigned(notification(MessageType.CLIENT_TEXT_NOTIFICATION, b"hi"))
        # A delivery another key signed, and one changed after it was signed.
        server.send(own(delivery([PLAYER_ID], text), PLAYER_TOKEN))
        tampered = bytearray(own(delivery([PLAYER_ID], text)))
        tampered[2] ^= 1
        server.send(bytes(tampered))
        poll_until(fleet, lambda: fleet.stats()["hmac_failures"] == 2)
        # A notification nothing signed.
        server.send(unsigned(notification(MessageType.ACTOR_UPDATE_NOTIFICATION, b"x")))
        poll_until(fleet, lambda: fleet.stats()["unsigned_notifications"] == 1)
        # Another app's state and another app's communication, however they were signed.
        server.send(notification(MessageType.ACTOR_UPDATE_NOTIFICATION, b"x", app_id=8))
        other_app = unsigned(notification(MessageType.CLIENT_TEXT_NOTIFICATION, b"hi", app_id=8))
        server.send(own(delivery([PLAYER_ID], other_app)))
        poll_until(fleet, lambda: fleet.stats()["wrong_app"] == 2)
        settle(fleet)
        for name in ("actor_update", "text", "generic_error", "released"):
            assert seen.of(name) == []
        assert fleet.carries(PLAYER_ID)
    finally:
        fleet.close()


def test_a_batch_has_the_players_of_each_row_in_columns(server: FakeServer) -> None:
    fleet = open_connected(server)
    try:
        carry_as(fleet, server)
        fleet.poll()
        text = unsigned(notification(MessageType.CLIENT_TEXT_NOTIFICATION, b"hi"))
        server.send(
            signed_bundle(
                [
                    unsigned(notification(MessageType.ACTOR_UPDATE_NOTIFICATION, b"abc")),
                    delivery([PLAYER_ID, 11, 12], text),
                    delivery([13], text),
                ]
            )
        )
        assert fleet.wait(3)
        time.sleep(0.05)
        batch = fleet.poll()
        assert len(batch) == 3
        assert bool(batch)
        assert batch.listed.tolist() == [0, 1, 1]
        assert batch.recipient_offsets.tolist() == [0, 0, 3, 4]
        assert batch.recipient_ids.tolist() == [PLAYER_ID, 11, 12, 13]
        assert [batch.recipients(i) for i in range(3)] == [None, (PLAYER_ID, 11, 12), (13,)]
        assert batch.notifications.types.tolist() == [
            MessageType.ACTOR_UPDATE_NOTIFICATION,
            MessageType.CLIENT_TEXT_NOTIFICATION,
            MessageType.CLIENT_TEXT_NOTIFICATION,
        ]
        assert batch.notifications.payload(0) == b"abc"
        assert [(event.payload, players) for event, players in batch] == [
            (b"abc", None),
            (b"hi", (PLAYER_ID, 11, 12)),
            (b"hi", (13,)),
        ]
        assert batch[-1][1] == (13,)
        # The columns are views that keep the batch alive.
        ids = batch.recipient_ids
        del batch
        assert ids.tolist() == [PLAYER_ID, 11, 12, 13]
        assert len(fleet.poll()) == 0
    finally:
        fleet.close()


@pytest.mark.parametrize("reason", [1, 2, 5, 9])
def test_the_server_ends_a_carry_with_its_reason(server: FakeServer, reason: int) -> None:
    seen = Seen()
    fleet = open_connected(server, seen)
    try:
        carry_as(fleet, server)
        # A release for a player the table does not hold reports nothing.
        server.send(own(released_frame(555, reason)))
        server.send(own(released_frame(PLAYER_ID, reason)))
        poll_until(fleet, lambda: bool(seen.of("released")))
        assert seen.of("released") == [(FleetReleased(PLAYER_ID, USER_ID, reason),)]
        assert not fleet.carries(PLAYER_ID)
        assert fleet.players() == []
        with pytest.raises(FleetNotCarriedError):
            fleet.send_actor_update(PLAYER_ID, (0, 0, 0), ACTOR, b"x")
    finally:
        fleet.close()


def test_a_refusal_says_whether_it_was_verified_and_changes_nothing(server: FakeServer) -> None:
    seen = Seen()
    fleet = open_connected(server, seen)
    try:
        carry_as(fleet, server)
        sequence = fleet.send_text_packet(PLAYER_ID, (0, 0, 0), ACTOR, "hi")
        server.recv()

        # Alone, a refusal is 3 bytes with no room for a signature: a hint.
        server.send(bytes([MessageType.GENERIC_ERROR, sequence, ErrorCode.UNAUTHORIZED]))
        poll_until(fleet, lambda: len(seen.of("generic_error")) == 1)
        (error, verified) = seen.of("generic_error")[0]
        assert (error.error_code, error.sequence, verified) == (
            ErrorCode.UNAUTHORIZED,
            sequence,
            False,
        )
        assert seen.of("send_refused") == []

        # Inside a bundle the fleet token signed it is verified, and names the player.
        server.send(
            signed_bundle([bytes([MessageType.GENERIC_ERROR, sequence, ErrorCode.TOKEN_EXPIRED])])
        )
        poll_until(fleet, lambda: len(seen.of("generic_error")) == 2)
        (error, verified) = seen.of("generic_error")[1]
        assert (error.error_code, error.sequence, verified) == (
            ErrorCode.TOKEN_EXPIRED,
            sequence,
            True,
        )
        (refused,) = seen.of("send_refused")[0]
        assert refused == FleetSendRefused(PLAYER_ID, USER_ID, sequence, ErrorCode.TOKEN_EXPIRED)
        assert refused.player_token_expired
        assert fleet.stats()["send_refusals_named"] == 1

        # The session changes nothing on a refusal: the carry stands and no refresh is due.
        assert fleet.carries(PLAYER_ID)
        assert not fleet.move_ordered
        assert not fleet.refresh_due()
        assert seen.of("move_ordered") == seen.of("released") == []
    finally:
        fleet.close()


# ---- keeping the session ----


def test_the_server_releases_the_session_and_a_refresh_carries_every_player_again(
    server: FakeServer,
) -> None:
    seen = Seen()
    fleet = open_connected(server, seen)
    try:
        carry_as(fleet, server)
        assert fleet.refresh_input() == {
            "appId": "7",
            "gameTokenId": "9001",
            "currentServer": {"ip4": "127.0.0.1", "clientPort": server.port},
        }
        assert not fleet.refresh_due()  # no expiry and no move order

        # A release another key signed, or bound to another connection, changes nothing.
        server.send(bound_release(0, fleet.nonce, PLAYER_TOKEN))  # type: ignore[attr-defined]
        server.send(bound_release(0, bytes(8)))
        settle(fleet)
        assert seen.of("session_released") == []
        assert fleet.carries(PLAYER_ID)

        # The signed word that the session ended: the Application API closed it (reason 0).
        server.send(bound_release(0, fleet.nonce))  # type: ignore[attr-defined]
        poll_until(fleet, lambda: bool(seen.of("session_released")))
        assert seen.of("session_released") == [(0,)]
        assert seen.of("move_ordered") == [()]
        assert fleet.move_ordered
        assert fleet.refresh_due()
        # After a move order the refresh names no server: the answer's server is the next one.
        assert fleet.refresh_input() == {"appId": "7", "gameTokenId": "9001"}
        assert fleet.stats()["session_releases"] == 1
        # The session carries nobody until it is placed again, and sends no carry meanwhile.
        assert not fleet.carries(PLAYER_ID)
        assert fleet.players() == [FleetPlayer(PLAYER_ID, USER_ID, False, True)]
        with pytest.raises(FleetNotCarriedError):
            fleet.send_actor_update(PLAYER_ID, (0, 0, 0), ACTOR, b"x")
        settle(fleet)
        assert seen.of("move_ordered") == [()]  # once for one order
        server.expect_nothing()

        # The refresh answers the same server: capabilities first, then every player again,
        # each with a new time and proof.
        fleet.apply_refresh(server.answer(expiresAt="2099-01-01T00:00:00.000Z"))
        assert not fleet.move_ordered
        first, second = server.recv(), server.recv()
        assert first[0] == MessageType.CLIENT_CAPABILITIES
        assert read_carry(second)[0] == PLAYER_ID
        server.send(own(carry_result(PLAYER_ID, USER_ID, 0)))
        poll_until(fleet, lambda: fleet.carries(PLAYER_ID))
        # Each carry made again reaches the carry_result handler.
        assert len(seen.of("carry_result")) == 2
        assert fleet.expires_at == 4_070_908_800_000
        assert fleet.refresh_input()["currentServer"] == {
            "ip4": "127.0.0.1",
            "clientPort": server.port,
        }
        fleet.send_actor_update(PLAYER_ID, (0, 0, 0), ACTOR, b"x")
        assert tail_id(server.recv()) == PLAYER_ID
    finally:
        fleet.close()


def test_a_refresh_on_the_same_server_keeps_the_carries(server: FakeServer) -> None:
    fleet = open_connected(server)
    try:
        carry_as(fleet, server)
        fleet.apply_refresh(server.answer(expiresAt="2099-01-01T00:00:00.000Z", maxPlayers=300))
        # The capabilities again, and no FLEET_CARRY: the carries stand.
        assert server.recv()[0] == MessageType.CLIENT_CAPABILITIES
        server.expect_nothing()
        assert fleet.carries(PLAYER_ID)
        assert (fleet.max_players, fleet.expires_at) == (300, 4_070_908_800_000)
    finally:
        fleet.close()


def test_a_refresh_that_answers_another_server_moves_the_session_there(server: FakeServer) -> None:
    seen = Seen()
    fleet = open_connected(server, seen)
    other = FakeServer()
    try:
        carry_as(fleet, server)
        fleet.apply_refresh(other.answer())
        assert fleet.current_server == Assignment("127.0.0.1", "", other.port)
        # The new server holds no carry: capabilities, then a FLEET_CARRY for every player.
        flags, _ = read_capabilities(other.recv())
        assert flags & 0b11 == 0b11
        assert read_carry(other.recv())[0] == PLAYER_ID
        # Until the carry is answered, sends for the player are refused locally.
        assert not fleet.carries(PLAYER_ID)
        with pytest.raises(FleetNotCarriedError):
            fleet.send_actor_update(PLAYER_ID, (0, 0, 0), ACTOR, b"x")
        other.send(own(carry_result(PLAYER_ID, USER_ID, 0)))
        poll_until(fleet, lambda: fleet.carries(PLAYER_ID))
        fleet.send_actor_update(PLAYER_ID, (0, 0, 0), ACTOR, b"x")
        assert tail_id(other.recv()) == PLAYER_ID
        assert fleet.refresh_input()["currentServer"] == {
            "ip4": "127.0.0.1",
            "clientPort": other.port,
        }
        assert len(seen.of("carry_result")) == 2
        server.expect_nothing()  # nothing more goes to the server it left
    finally:
        fleet.close()
        other.close()


def test_a_refresh_is_refused_unless_it_is_this_sessions(server: FakeServer) -> None:
    fleet = open_connected(server)
    try:
        carry_as(fleet, server)
        for answer in (
            server.answer(gameTokenId="9002"),  # another session
            server.answer(appId="8"),  # another app
            server.answer(token="short"),
            server.answer(server={"ip4": None, "ip6": None, "clientPort": server.port}),
            server.answer(server={"ip4": "127.0.0.1", "clientPort": 0}),
            server.answer(expiresAt="tomorrow"),
            {"token": FLEET_TOKEN},
            "not an answer",
        ):
            with pytest.raises(CrowdyReplicationError) as refused:
                fleet.apply_refresh(answer)  # type: ignore[arg-type]
            assert refused.value.code == "InvalidArgument"
            assert FLEET_TOKEN not in str(refused.value)
        # Nothing changed, and nothing was sent.
        server.expect_nothing()
        assert fleet.carries(PLAYER_ID)
        assert fleet.current_server == Assignment("127.0.0.1", "", server.port)
    finally:
        fleet.close()
    with pytest.raises(CrowdyRealtimeError) as closed:
        fleet.apply_refresh(server.answer())
    assert closed.value.code == "FLEET_CLOSED"
    assert not fleet.refresh_due()


def test_a_token_given_as_an_id_is_refused_and_not_shown(server: FakeServer) -> None:
    """A player's token passed where its id belongs is refused with a message that describes
    what was given and never contains it: messages are logged."""
    seen = Seen()
    fleet = open_connected(server, seen)
    try:
        carry_as(fleet, server)
        chunk = (1, 2, 3)
        for given, described in (
            (PLAYER_TOKEN, "a string of 64 characters"),
            (FLEET_TOKEN, "a string of 64 characters"),
            (2**63, "an integer outside the 64-bit range"),
            (True, "a bool"),
            (1.5, "a float"),
        ):
            for call in (
                lambda value: fleet.send_actor_update(value, chunk, ACTOR, b"s"),
                lambda value: fleet.release(value),
                lambda value: fleet.carries(value),
            ):
                with pytest.raises(CrowdyReplicationError) as refused:
                    call(given)
                assert refused.value.code == "InvalidArgument"
                assert described in str(refused.value)
                assert str(given) not in str(refused.value) or isinstance(given, bool)
                assert "token-0123456789" not in str(refused.value)
                assert "token-0123456789" not in repr(refused.value)
    finally:
        fleet.close()


def test_the_session_is_closed_before_its_native_close_runs(server: FakeServer) -> None:
    """``close`` marks the session closed first, so nothing asked while the native session
    shuts down is started."""
    fleet = open_connected(server)
    native = fleet._native
    seen_closed: list[bool] = []

    class Watching:
        def close(self) -> None:
            seen_closed.append(fleet._closed)
            native.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(native, name)

    fleet._native = Watching()
    fleet.close()
    assert seen_closed == [True]
    assert not fleet.refresh_due()


def test_a_refresh_is_due_at_half_of_the_sessions_lifetime(server: FakeServer) -> None:
    soon = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 2)) + ".000Z"
    fleet = FleetSession(server.answer(expiresAt=soon), **QUIET)
    try:
        expires_at = fleet.expires_at
        assert expires_at is not None
        assert 0 < expires_at - time.time() * 1000 <= 2_000
        assert not fleet.refresh_due()
        # Half of what was left when the session was given the token: at most a second.
        deadline = time.monotonic() + 3
        while not fleet.refresh_due() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert fleet.refresh_due()
        assert not fleet.move_ordered
    finally:
        fleet.close()
    assert FleetSession(server.answer(), **QUIET).expires_at is None


# ---- what a session is made from, and what it shows ----


def test_a_session_is_made_from_the_answer_and_never_shows_a_token(server: FakeServer) -> None:
    answer = FleetSessionAnswer(
        token=FLEET_TOKEN,
        game_token_id=str(FLEET_ID),
        app_id=str(APP_ID),
        expires_at="2099-01-01T00:00:00.000Z",
        max_players=64,
        server=FleetServer(ip4="127.0.0.1", client_port=server.port),
    )
    fleet = FleetSession(answer, **QUIET)
    assert (fleet.app_id, fleet.game_token_id, fleet.max_players) == (APP_ID, FLEET_ID, 64)
    assert fleet.expires_at == 4_070_908_800_000
    assert fleet.current_server == Assignment("127.0.0.1", "", server.port)
    assert fleet.state == ConnState.IDLE
    assert not fleet.move_ordered
    shown = repr(fleet)
    assert FLEET_TOKEN not in shown
    assert "token" not in shown.replace("game_token_id", "")
    assert shown == (
        f"FleetSession(app_id=7, game_token_id=9001, state=IDLE, "
        f"server=Assignment(ip4='127.0.0.1', ip6='', client_port={server.port}), carried=0)"
    )
    assert not hasattr(fleet, "token")
    assert FLEET_TOKEN not in str(vars(fleet))
    assert PLAYER_TOKEN not in repr(FleetPlayerToken(PLAYER_TOKEN, PLAYER_ID, replaces=1))

    # The same fields from your own HTTP client, with the ids read as numbers.
    numbered = FleetSession({**server.answer(), "gameTokenId": FLEET_ID, "appId": APP_ID}, **QUIET)
    assert (numbered.app_id, numbered.game_token_id) == (APP_ID, FLEET_ID)

    for bad in (
        server.answer(token="x" * 63),
        server.answer(token="é" * 64),
        server.answer(server={"ip4": "", "ip6": "", "clientPort": server.port}),
        server.answer(server={"ip4": "127.0.0.1", "clientPort": 70000}),
        server.answer(gameTokenId="nine"),
        {"token": FLEET_TOKEN, "game_token_id": "9001", "app_id": "7"},
        None,
    ):
        with pytest.raises(CrowdyReplicationError) as refused:
            FleetSession(bad, **QUIET)  # type: ignore[arg-type]
        assert refused.value.code == "InvalidArgument"
        assert FLEET_TOKEN not in str(refused.value)

    with pytest.raises(ValueError, match="unknown handler name"):
        FleetSession(server.answer(), handlers={"actor_updates": print}, **QUIET)
    with pytest.raises(ValueError, match="unknown handler name"):
        fleet.set_handlers({"carried": print})


def test_a_handler_that_raises_is_reported_and_stops_nothing(server: FakeServer) -> None:
    errors: list[BaseException] = []
    results: list[FleetCarryResult] = []

    def broken(result: FleetCarryResult) -> None:
        results.append(result)
        raise RuntimeError("the roster is on fire")

    fleet = FleetSession(
        server.answer(), handlers={"carry_result": broken, "error": errors.append}, **QUIET
    )
    fleet.connect()
    try:
        server.recv()
        carry_as(fleet, server)
        carry_as(fleet, server, 4243, 78, token_for(4243))
        assert [r.player_game_token_id for r in results] == [PLAYER_ID, 4243]
        assert [str(e) for e in errors] == ["the roster is on fire"] * 2
        assert fleet.carried_count == 2
    finally:
        fleet.close()


def test_the_session_follows_capability_changes(server: FakeServer) -> None:
    fleet = FleetSession(server.answer(), fleet_own_state=True, **QUIET)
    fleet.connect()
    try:
        flags, _ = read_capabilities(server.recv())
        assert (
            flags
            == wire.ClientCapability.BUNDLE_SIGNED
            | wire.ClientCapability.BOUND_CONTROL
            | wire.ClientCapability.FLEET_OWN_STATE
        )
        poll_until(fleet, lambda: fleet.state == ConnState.CONNECTED)
        fleet.set_fleet_own_state(False)
        assert read_capabilities(server.recv())[0] == 0b0011
        fleet.set_own_media_echo(True)
        assert read_capabilities(server.recv())[0] == 0b0111
        fleet.set_own_media_echo(True)  # no change: nothing is sent
        server.expect_nothing()
    finally:
        fleet.close()


def test_the_silence_timeout_keeps_crowdycpps_own_minimum(server: FakeServer) -> None:
    # CrowdyCPP 0.95.0 passes at every value from its minimum of 3,000 ms, so the session
    # adds no floor of its own (0.40.0 raised a shorter one to 4,500). A timeout of exactly
    # 3,000 ms, which never passed with CrowdyCPP 0.94, orders the move.
    assert fleet_module.MIN_SILENCE_TIMEOUT_MS == 3_000
    assert not hasattr(fleet_module, "_silence")
    fleet = FleetSession(server.answer(), **{**QUIET, "silence_timeout_ms": 3_000})
    fleet.connect()
    try:
        started = time.monotonic()
        server.recv()
        poll_until(fleet, lambda: fleet.move_ordered, timeout=9.0)
        # Between the timeout and one capabilities interval (1,000 ms) later.
        assert 2.9 <= time.monotonic() - started < 8.0
        assert fleet.refresh_due()
    finally:
        fleet.close()
    # 0 switches it off, as a negative number does.
    for off in (0, -1):
        quiet = FleetSession(server.answer(), **{**QUIET, "silence_timeout_ms": off})
        quiet.close()


# ---- the asyncio session ----


async def test_the_asyncio_session_carries_and_delivers(server: FakeServer) -> None:
    seen = Seen()
    async with AsyncFleetSession(server.answer(), handlers=seen.handlers(), **QUIET) as fleet:
        read_capabilities(await asyncio.to_thread(server.recv))

        async def answer_carry() -> None:
            read_carry(await asyncio.to_thread(server.recv))
            server.send(own(carry_result(PLAYER_ID, USER_ID, 0)))

        answering = asyncio.create_task(answer_carry())
        result = await asyncio.wait_for(fleet.carry(FleetPlayerToken(PLAYER_TOKEN, PLAYER_ID)), 3)
        await answering
        assert result == FleetCarryResult(PLAYER_ID, USER_ID, FleetCarryOutcome.CARRIED, 0)
        assert seen.of("carry_result") == [(result,)]
        assert fleet.carries(PLAYER_ID)

        # The loop is woken by the socket pair: the handler runs with no poll from the test.
        text = unsigned(notification(MessageType.CLIENT_TEXT_NOTIFICATION, b"hi"))
        consumer = fleet.batches()
        next_batch = asyncio.ensure_future(anext(consumer))
        await asyncio.sleep(0)
        server.send(own(delivery([PLAYER_ID], text)))
        batch = await asyncio.wait_for(next_batch, 3)
        assert [(event.payload, players) for event, players in batch] == [(b"hi", (PLAYER_ID,))]
        assert [(n.payload, players) for n, players in seen.of("text")] == [(b"hi", (PLAYER_ID,))]
        await consumer.aclose()

        fleet.send_actor_update(PLAYER_ID, (0, 0, 0), ACTOR, b"x")
        assert tail_id(await asyncio.to_thread(server.recv)) == PLAYER_ID
        assert "AsyncFleetSession(app_id=7, game_token_id=9001, state=CONNECTED" in repr(fleet)
    assert fleet.state == ConnState.CLOSED
    assert not fleet.refresh_due()


async def test_the_asyncio_session_reports_carries_nothing_answered(server: FakeServer) -> None:
    seen = Seen()
    options = {**QUIET, "carry_retry_ms": 60, "carry_timeout_ms": 200}
    fleet = AsyncFleetSession(server.answer(), handlers=seen.handlers(), **options)
    await fleet.connect()
    try:
        await asyncio.to_thread(server.recv)
        # Awaited: the carry looks for its own deadline, which no datagram announces.
        result = await asyncio.wait_for(fleet.carry(FleetPlayerToken(PLAYER_TOKEN, PLAYER_ID)), 3)
        assert result == FleetCarryResult(PLAYER_ID, None, FleetCarryOutcome.TIMED_OUT, None)

        # Not awaited: the session's own housekeeping reports it to the handler.
        reported = asyncio.Event()
        fleet.set_handlers(
            {
                **seen.handlers(),
                "carry_result": lambda result: (
                    seen.calls.append(("carry_result", (result,))),
                    reported.set(),
                ),
            }
        )
        fleet.carry_nowait(FleetPlayerToken(token_for(1001), 1001))
        await asyncio.wait_for(reported.wait(), 4)
        assert seen.of("carry_result")[1] == (
            FleetCarryResult(1001, None, FleetCarryOutcome.TIMED_OUT, None),
        )

        # A carry still waiting when the session closes ends `closed`.
        waiting = asyncio.create_task(fleet.carry(FleetPlayerToken(token_for(1002), 1002)))
        await asyncio.sleep(0.05)
    finally:
        await fleet.close()
    assert await asyncio.wait_for(waiting, 3) == FleetCarryResult(
        1002, None, FleetCarryOutcome.CLOSED, None
    )


async def test_silence_orders_a_move_with_no_event_to_wake_the_loop(server: FakeServer) -> None:
    # The realtime server says nothing that verifies: after the silence timeout the session
    # needs a refresh, and tells the handler from its own housekeeping.
    ordered = asyncio.Event()
    options = {**QUIET, "silence_timeout_ms": 1, "capabilities_interval_ms": 15_000}
    fleet = AsyncFleetSession(server.answer(), handlers={"move_ordered": ordered.set}, **options)
    await fleet.connect()
    try:
        started = time.monotonic()
        await asyncio.to_thread(server.recv)
        assert not fleet.move_ordered
        assert not fleet.refresh_due()
        await asyncio.wait_for(ordered.wait(), 10)
        # A timeout below CrowdyCPP's minimum is raised to it: 3,000 ms.
        assert 2.9 <= time.monotonic() - started < 9.0
        assert fleet.move_ordered
        assert fleet.refresh_due()
        assert fleet.refresh_input() == {"appId": "7", "gameTokenId": "9001"}
        assert fleet.state == ConnState.CONNECTED  # it keeps its socket meanwhile
        # The answer names the same server: the session is heard from again and the order is
        # answered.
        fleet.apply_refresh(server.answer())
        assert not fleet.move_ordered
        assert "currentServer" in fleet.refresh_input()
    finally:
        await fleet.close()


def test_status_rows_are_the_connections(server: FakeServer) -> None:
    fleet = FleetSession(server.answer(), **QUIET)
    fleet.connect()
    try:
        server.recv()
        rows: list[Any] = []
        deadline = time.monotonic() + 3
        while len(rows) < 2 and time.monotonic() < deadline:
            fleet.wait(0.05)
            rows.extend(event for event, _ in fleet.poll())
        assert [row.type for row in rows] == [STATUS, STATUS]
        assert [row.state for row in rows] == [ConnState.CONNECTING, ConnState.CONNECTED]
    finally:
        fleet.close()


# ---- CrowdyCPP 0.95.0: the table's limit, a refused keepalive, and currentServer ----


def test_a_carry_past_the_tables_limit_ends_table_full_with_nothing_sent(
    server: FakeServer,
) -> None:
    seen = Seen()
    fleet = FleetSession(
        server.answer(maxPlayers=1),
        handlers=seen.handlers(),
        **{**QUIET, "carry_timeout_ms": 60_000},
    )
    fleet.connect()
    try:
        read_capabilities(server.recv())
        poll_until(fleet, lambda: fleet.state == ConnState.CONNECTED)
        assert TABLE_MARGIN == 32
        assert fleet.table_limit == 1 + TABLE_MARGIN
        # The table fills with carries that wait for their answer.
        for player in range(1, fleet.table_limit + 1):
            fleet.carry_nowait(FleetPlayerToken(token_for(player), player))
        assert len(fleet.players()) == fleet.table_limit
        sent_before = fleet.stats()["messages_sent_by_type"].get(MessageType.FLEET_CARRY, 0)

        result = fleet.carry(FleetPlayerToken(token_for(999), 999))
        assert result == FleetCarryResult(999, None, FleetCarryOutcome.TABLE_FULL, None)
        assert not result.carried
        assert seen.of("carry_result") == [(result,)]
        assert len(fleet.players()) == fleet.table_limit
        stats = fleet.stats()
        assert stats["carries_refused_table_full"] == 1
        # Nothing was sent for it: only the carries that were already waiting leave.
        assert all(row.game_token_id != 999 for row in fleet.players())
        assert stats["messages_sent_by_type"].get(MessageType.FLEET_CARRY, 0) >= sent_before

        # A carry for a player the table already holds is never refused this way.
        fleet.carry_nowait(FleetPlayerToken(token_for(1), 1))
        settle(fleet)
        assert fleet.stats()["carries_refused_table_full"] == 1

        # A player who left makes room.
        fleet.release(2)
        fleet.carry_nowait(FleetPlayerToken(token_for(999), 999))
        settle(fleet)
        assert fleet.stats()["carries_refused_table_full"] == 1
        assert any(row.game_token_id == 999 for row in fleet.players())

        # A refresh's answer may name another max_players: the limit follows.
        fleet.apply_refresh(server.answer(maxPlayers=4))
        assert (fleet.max_players, fleet.table_limit) == (4, 4 + TABLE_MARGIN)
    finally:
        fleet.close()


def test_the_tables_limit_counts_from_max_players(server: FakeServer) -> None:
    for max_players, limit in ((256, 288), (None, 1_056), (0, 1_056), (5_000, 1_056)):
        fleet = FleetSession(server.answer(maxPlayers=max_players), **QUIET)
        try:
            assert fleet.table_limit == limit
        finally:
            fleet.close()


def test_a_carried_players_refused_keepalive_is_reported_once(server: FakeServer) -> None:
    # For realtime servers v0.51.0: a keepalive is accepted only where the player's actor
    # update would be, so a player with no access there has every keepalive refused.
    seen = Seen()
    fleet = open_connected(server, seen)
    refusal = ErrorCode.UNAUTHORIZED
    try:
        carry_as(fleet, server)
        assert fleet.not_present(PLAYER_ID, ACTOR) is None

        # A refusal that arrived alone verifies nothing: a fleet session reports none.
        first = fleet.send_heartbeat(PLAYER_ID, (1, 2, 3), ACTOR)
        server.recv()
        server.send(bytes([MessageType.GENERIC_ERROR, first, refusal]))
        poll_until(fleet, lambda: len(seen.of("generic_error")) == 1)
        (error, verified) = seen.of("generic_error")[0]
        assert (error.keepalive, verified) == (True, False)
        assert seen.of("not_present") == []
        assert fleet.not_present(PLAYER_ID, ACTOR) is None

        # Inside a bundle the fleet token signed, it is the report: once.
        second = fleet.send_heartbeat(PLAYER_ID, (1, 2, 3), ACTOR)
        server.recv()
        server.send(signed_bundle([bytes([MessageType.GENERIC_ERROR, second, refusal])]))
        poll_until(fleet, lambda: len(seen.of("not_present")) == 1)
        report = ActorNotPresent(ACTOR, (1, 2, 3), second, True, PLAYER_ID)
        assert seen.of("not_present") == [(report,)]
        assert fleet.not_present(PLAYER_ID, ACTOR) == report
        assert fleet.not_present(PLAYER_ID, OTHER) is None
        (error, verified) = seen.of("generic_error")[1]
        assert (error.keepalive, error.error_code, verified) == (True, refusal, True)
        # A keepalive's UNAUTHORIZED never reaches send_refused.
        assert seen.of("send_refused") == []

        third = fleet.send_heartbeat(PLAYER_ID, (1, 2, 3), ACTOR)
        server.recv()
        server.send(signed_bundle([bytes([MessageType.GENERIC_ERROR, third, refusal])]))
        poll_until(fleet, lambda: len(seen.of("generic_error")) == 3)
        assert len(seen.of("not_present")) == 1
        stats = fleet.stats()
        assert (stats["keepalives_refused"], stats["not_present_reports"]) == (3, 1)

        # The session carries the player as before, and orders nothing.
        assert fleet.carries(PLAYER_ID)
        assert not fleet.move_ordered
        assert seen.of("released") == seen.of("move_ordered") == []

        # The player's next keepalive names another chunk: the report ends.
        fleet.send_heartbeat(PLAYER_ID, (9, 9, 9), ACTOR)
        poll_until(fleet, lambda: len(seen.of("not_present_ended")) == 1)
        assert seen.of("not_present_ended") == [(report, NotPresentEnd.MOVED)]
        assert fleet.not_present(PLAYER_ID, ACTOR) is None
    finally:
        fleet.close()


def test_not_present_rows_are_the_sessions_own(server: FakeServer) -> None:
    fleet = open_connected(server)
    try:
        carry_as(fleet, server)
        sequence = fleet.send_heartbeat(PLAYER_ID, (1, 2, 3), ACTOR)
        server.recv()
        server.send(
            signed_bundle([bytes([MessageType.GENERIC_ERROR, sequence, ErrorCode.UNAUTHORIZED])])
        )
        rows: list[Any] = []
        deadline = time.monotonic() + 3
        while len(rows) < 2 and time.monotonic() < deadline:
            fleet.wait(0.05)
            rows.extend(fleet.poll())
        assert [(event.type, players) for event, players in rows] == [
            (MessageType.GENERIC_ERROR, None),
            (NOT_PRESENT, None),
        ]
        report = rows[1][0].not_present
        assert report == ActorNotPresent(ACTOR, (1, 2, 3), sequence, True, PLAYER_ID)
        fleet.send_heartbeat(PLAYER_ID, (2, 2, 2), ACTOR)
        rows = []
        while not rows and time.monotonic() < deadline:
            fleet.wait(0.05)
            rows.extend(fleet.poll())
        assert [(event.type, event.not_present_end) for event, _ in rows] == [
            (NOT_PRESENT_ENDED, NotPresentEnd.MOVED)
        ]
    finally:
        fleet.close()


def test_a_refresh_names_the_one_address_the_session_connected_to(server: FakeServer) -> None:
    ip4, ip6, port = "203.0.113.10", "2001:db8::10", 7777

    def answer(**server_fields: Any) -> dict[str, Any]:
        return server.answer(server={"clientPort": port, **server_fields})

    cases: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = [
        ({"ip4": ip4, "ip6": None}, {}, {"ip4": ip4, "clientPort": port}),
        ({"ip4": ip4, "ip6": ip6}, {}, {"ip4": ip4, "clientPort": port}),
        ({"ip4": ip4, "ip6": ip6}, {"prefer_ipv6": True}, {"ip6": ip6, "clientPort": port}),
        # A server with an ip6 alone: an Application API from v2.90.0 takes it.
        ({"ip4": None, "ip6": ip6}, {}, {"ip6": ip6, "clientPort": port}),
    ]
    for fields, options, current in cases:
        fleet = FleetSession(answer(**fields), **{**QUIET, **options})
        try:
            assert fleet.refresh_input() == {
                "appId": str(APP_ID),
                "gameTokenId": str(FLEET_ID),
                "currentServer": current,
            }
            # The function names the same address the session does.
            prefer = bool(options.get("prefer_ipv6"))
            assert fleet_current_server(fleet.current_server, prefer) == current
            native = fleet_module._r.fleet_current_server(
                fields["ip4"] or "", fields["ip6"] or "", port, prefer
            )
            assert native == (current.get("ip4", ""), current.get("ip6", ""), port)
        finally:
            fleet.close()
    assert fleet_module._r.fleet_current_server("", "", port, False) is None
