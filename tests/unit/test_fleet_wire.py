"""The fleet session's wire messages (49 to 53), through CrowdyCPP's codec.

The golden bytes are CrowdyCPP's (its ``wire_test`` and ``fleet_session_test``), computed
independently from the public wire format: the same inputs must give the same bytes here.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from crowdypy import wire
from crowdypy.errors import CrowdyProtocolError
from crowdypy.wire import (
    ClientCapability,
    FleetCarryStatus,
    FleetReleaseReason,
    MessageType,
    ReleaseReason,
    SessionReleaseReason,
    SpeakerListMode,
)

FLEET_TOKEN = "fleet-token-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP"
PLAYER_TOKEN = "player-token-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO"
FLEET_ID = 9001
PLAYER_ID = 4242
APP_ID = 7
EPOCH_MS = 1_795_000_000_000

# FLEET_CARRY (49) for the player above at EPOCH_MS, sequence 0.
GOLDEN_FLEET_CARRY = (
    "3107000000000000000000000000000000000000000000000000000000000000000000010000000000000000"
    "0000000000000000000000000000000000000000000000009210000000000000005e56eea10100005602abb9"
    "300581f6a20e247ea0b406f690646a3d27f9427e95c5f6363115b35a9d69b5b07c19a2450e61725d9995c19c"
    "940e41fa602fcce2535e780e5d2d81e3292300000000000000"
)
# FLEET_RELEASE (50) for the player, sequence 2.
GOLDEN_FLEET_RELEASE = (
    "3207000000000000000000000000000000000000000000000000000000000000000000010000000000000000"
    "0000000000000000000000000000000000000000000000009210000000000000903cf330c14c7344619b97d3"
    "736c97d042c2669c9c8c1ed8e4b1922c327f684d292300000000000002"
)
# ACTOR_UPDATE_REQUEST (128) for the carried player, sequence 1: chunk (1,-2,3), distance 8,
# uuid "0123456789abcdef0123456789abcdef", payload de ad be ef.
GOLDEN_CARRIED_ACTOR_UPDATE = (
    "8007000000000000000100000000000000feffffffffffffff03000000000000000800013031323334353637"
    "383961626364656630313233343536373839616263646566deadbeefafca3c1e53f866c6384fb186e2396664"
    "b62d7acd0fee6d9445b96e283d1c09f4921000000000000001"
)


def le64(value: int) -> bytes:
    return value.to_bytes(8, "little", signed=True)


def own(frame: bytes, key: str = FLEET_TOKEN) -> bytes:
    """A 51, 52 or 53 as it travels alone: followed by HMAC-SHA256(key, frame || key)."""
    octets = key.encode("latin-1")
    return frame + hmac.new(octets, frame + octets, hashlib.sha256).digest()


def test_the_carry_proof_is_keyed_with_the_players_token() -> None:
    message = b"ck-fleet-carry" + le64(FLEET_ID) + le64(PLAYER_ID) + le64(EPOCH_MS)
    expected = hmac.new(PLAYER_TOKEN.encode(), message, hashlib.sha256).digest()
    assert wire.fleet_carry_proof(PLAYER_TOKEN, FLEET_ID, PLAYER_ID, EPOCH_MS) == expected
    assert wire.FLEET_CARRY_PROOF_LABEL == "ck-fleet-carry"
    # Another time, another player or another session is another proof.
    assert wire.fleet_carry_proof(PLAYER_TOKEN, FLEET_ID, PLAYER_ID, EPOCH_MS + 1) != expected
    assert wire.fleet_carry_proof(PLAYER_TOKEN, FLEET_ID, PLAYER_ID + 1, EPOCH_MS) != expected
    assert wire.fleet_carry_proof(PLAYER_TOKEN, FLEET_ID + 1, PLAYER_ID, EPOCH_MS) != expected
    with pytest.raises(CrowdyProtocolError, match="64 octets"):
        wire.fleet_carry_proof("short", FLEET_ID, PLAYER_ID, EPOCH_MS)


def test_a_fleet_carry_is_the_golden_message() -> None:
    carry = wire.encode_fleet_carry(
        FLEET_TOKEN,
        APP_ID,
        fleet_game_token_id=FLEET_ID,
        player_token=PLAYER_TOKEN,
        player_game_token_id=PLAYER_ID,
        epoch_millis=EPOCH_MS,
    )
    assert carry.hex() == GOLDEN_FLEET_CARRY
    assert wire.verify_long_spatial(FLEET_TOKEN, carry)
    assert not wire.verify_long_spatial(PLAYER_TOKEN, carry)
    message = wire.parse_long_spatial(carry)
    # The capabilities' layout, under the session's own game token id.
    assert (message.type, message.app_id, message.chunk) == (MessageType.FLEET_CARRY, 7, (0, 0, 0))
    assert (message.distance, message.uuid, message.epoch_or_token_id) == (0, b"", FLEET_ID)
    assert len(message.payload) == wire.FLEET_CARRY_PAYLOAD_BYTES == 48
    assert message.payload[:8] == le64(PLAYER_ID)
    assert message.payload[8:16] == le64(EPOCH_MS)
    assert message.payload[16:] == wire.fleet_carry_proof(
        PLAYER_TOKEN, FLEET_ID, PLAYER_ID, EPOCH_MS
    )
    assert next(iter(wire.parse_datagram(carry))) == message


def test_a_fleet_release_is_the_golden_message() -> None:
    release = wire.encode_fleet_release(
        FLEET_TOKEN,
        APP_ID,
        fleet_game_token_id=FLEET_ID,
        player_game_token_id=PLAYER_ID,
        sequence=2,
    )
    assert release.hex() == GOLDEN_FLEET_RELEASE
    message = wire.parse_long_spatial(release)
    assert (message.type, message.payload) == (MessageType.FLEET_RELEASE, le64(PLAYER_ID))
    assert (message.epoch_or_token_id, message.sequence) == (FLEET_ID, 2)


def test_a_carried_players_message_names_the_player_and_is_signed_by_the_fleet_token() -> None:
    # What CrowdyJS's carriedPlayerContext does: the tail's id is the player's, the key the
    # fleet token's.
    update = wire.encode_long_spatial(
        FLEET_TOKEN,
        MessageType.ACTOR_UPDATE_REQUEST,
        APP_ID,
        (1, -2, 3),
        "0123456789abcdef0123456789abcdef",
        bytes.fromhex("deadbeef"),
        distance=8,
        game_token_id=PLAYER_ID,
        sequence=1,
    )
    assert update.hex() == GOLDEN_CARRIED_ACTOR_UPDATE


def test_a_carry_result_is_verified_and_read() -> None:
    frame = bytes([51]) + le64(PLAYER_ID) + le64(77) + bytes([FleetCarryStatus.CARRIED])
    assert len(frame) == wire.FLEET_CARRY_RESULT_BYTES == 18
    assert wire.verify_fleet_frame(FLEET_TOKEN, own(frame)) == frame
    assert wire.parse_fleet_carry_result(frame) == (PLAYER_ID, 77, 0)
    result = wire.parse_fleet_carry_result(frame)
    assert (result.player_game_token_id, result.user_id, result.status) == (PLAYER_ID, 77, 0)
    refused = bytes([51]) + le64(-5) + le64(0) + bytes([FleetCarryStatus.PROOF_FAILED])
    assert wire.parse_fleet_carry_result(refused) == (-5, 0, 3)
    for bad in (frame[:-1], frame + b"\0", bytes([52]) + frame[1:], b""):
        with pytest.raises(CrowdyProtocolError, match="Malformed"):
            wire.parse_fleet_carry_result(bad)


def test_a_released_frame_is_verified_and_read() -> None:
    frame = bytes([52]) + le64(PLAYER_ID) + bytes([FleetReleaseReason.IDLE])
    assert len(frame) == wire.FLEET_RELEASED_BYTES == 10
    assert wire.verify_fleet_frame(FLEET_TOKEN, own(frame)) == frame
    released = wire.parse_fleet_released(frame)
    assert (released.player_game_token_id, released.reason) == (PLAYER_ID, 5)
    with pytest.raises(CrowdyProtocolError, match="Malformed"):
        wire.parse_fleet_released(frame + b"\0")


def test_a_frame_the_fleet_token_did_not_sign_is_refused() -> None:
    frame = bytes([52]) + le64(PLAYER_ID) + bytes([1])
    with pytest.raises(CrowdyProtocolError, match="HmacMismatch"):
        wire.verify_fleet_frame(FLEET_TOKEN, own(frame, PLAYER_TOKEN))
    tampered = bytearray(own(frame))
    tampered[9] = 2  # the reason, after it was signed
    with pytest.raises(CrowdyProtocolError, match="HmacMismatch"):
        wire.verify_fleet_frame(FLEET_TOKEN, bytes(tampered))
    # Too short to be a frame and its HMAC, and a type that is not a fleet frame.
    with pytest.raises(CrowdyProtocolError, match="Malformed"):
        wire.verify_fleet_frame(FLEET_TOKEN, frame)
    with pytest.raises(CrowdyProtocolError, match="Malformed"):
        wire.verify_fleet_frame(FLEET_TOKEN, own(bytes([3, 1, 7]) + bytes(8)))


def test_a_delivery_lists_the_only_players_who_may_receive_it() -> None:
    notification = bytes([MessageType.CHANNEL_MESSAGE_NOTIFICATION]) + bytes(60)
    frame = bytes([53, 2]) + le64(PLAYER_ID) + le64(PLAYER_ID + 1) + notification
    assert wire.verify_fleet_frame(FLEET_TOKEN, own(frame)) == frame
    delivery = wire.parse_fleet_delivery(frame)
    assert delivery.players == (PLAYER_ID, PLAYER_ID + 1)
    assert delivery.notification == notification
    full = bytes([53, 32]) + b"".join(le64(i) for i in range(32)) + notification
    assert len(wire.parse_fleet_delivery(full).players) == wire.FLEET_MAX_DELIVERY_PLAYERS == 32
    for bad in (
        bytes([53, 0]) + notification,  # nobody listed
        bytes([53, 33]) + b"".join(le64(i) for i in range(33)) + notification,  # too many
        bytes([53, 2]) + le64(PLAYER_ID) + le64(PLAYER_ID + 1),  # no notification after the list
        bytes([53, 2]) + le64(PLAYER_ID),  # shorter than its count says
    ):
        with pytest.raises(CrowdyProtocolError, match="Malformed"):
            wire.parse_fleet_delivery(bad)


def test_the_constants_and_enums_are_the_wires() -> None:
    assert wire.FLEET_MAX_DATAGRAM_BYTES == 1242
    assert wire.FLEET_TOKEN_CHARS == wire.TOKEN_OCTETS == 64
    assert wire.FLEET_CARRY_CLOCK_TOLERANCE_MS == 30_000
    assert wire.CONTROL_NONCE_BYTES == 8
    assert wire.MAX_MEDIA_SPEAKERS == 32
    assert [(m.name, m.value) for m in MessageType if 47 <= m.value <= 53] == [
        ("CLIENT_MEDIA_SPEAKERS", 47),
        ("FLEET_CARRY", 49),
        ("FLEET_RELEASE", 50),
        ("FLEET_CARRY_RESULT", 51),
        ("FLEET_RELEASED", 52),
        ("FLEET_DELIVERY", 53),
    ]
    assert [(s.name, s.value) for s in FleetCarryStatus] == [
        ("CARRIED", 0),
        ("NO_LIVE_TOKEN", 1),
        ("OTHER_ORGANIZATION", 2),
        ("PROOF_FAILED", 3),
        ("SESSION_FULL", 4),
    ]
    assert [(r.name, r.value) for r in FleetReleaseReason] == [
        ("CARRIED_ELSEWHERE", 1),
        ("SIGN_IN_ENDED", 2),
        ("IDLE", 5),
    ]
    assert SessionReleaseReason is ReleaseReason
    assert (SessionReleaseReason.DEAUTHORIZED, SessionReleaseReason.REBALANCE) == (0, 4)
    assert [(c.name, c.value) for c in ClientCapability] == [
        ("BUNDLE_SIGNED", 1),
        ("BOUND_CONTROL", 2),
        ("OWN_MEDIA_ECHO", 4),
        ("FLEET_OWN_STATE", 8),
    ]
    assert [(m.name, m.value) for m in SpeakerListMode] == [
        ("KEEP", 0),
        ("ONLY", 1),
        ("EVERYONE", 2),
    ]
