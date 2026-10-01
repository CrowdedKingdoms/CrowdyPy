"""The wire codec against both SDKs' golden vectors, plus malformed-input fuzzing."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from crowdypy import wire
from crowdypy.errors import CrowdyProtocolError

FIXTURES = json.loads(
    (Path(__file__).resolve().parent.parent / "fixtures" / "binary-wire-fixtures.json").read_text()
)
JS_TOKEN = FIXTURES["gameToken"].encode()
JS_TOKEN_ID = int(FIXTURES["gameTokenId"])

# CrowdyCPP tests/wire_test.cpp: computed independently from the public HMAC and
# wire-format docs. appId=7, chunk=(1,-2,3), distance=8, decay=EXPONENTIAL,
# payload=de:ad:be:ef, gameTokenId=123456789, seq=42.
CPP_TOKEN = b"abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ+/"
CPP_UUID = b"0123456789abcdef0123456789abcdef"
CPP_GOLDEN_SPATIAL = bytes.fromhex(
    "8c07000000000000000100000000000000feffffffffffffff0300000000000000080101"
    + CPP_UUID.hex()
    + "deadbeef43ecd468d2593f17f3fb06368a2b6f4732fc2da165acf4ca2bfe16396c43362a15cd5b07000000002a"
)
CPP_GOLDEN_RECONNECT = bytes.fromhex(
    "16748a0e416baa63befba6ff5f52611cc093855e04e60ac057e14e8fbe198281e2"
)


def test_layout_constants() -> None:
    assert wire.LONG_SPATIAL_HEADER_SIZE == 68
    assert wire.MAX_DATAGRAM_SIZE == 1232
    assert wire.MAX_LONG_SPATIAL_PAYLOAD == 1123
    assert wire.UUID_SIZE == 32
    assert wire.TOKEN_OCTETS == 64
    assert wire.MAX_BUNDLE_MEMBERS == 32


def test_crowdycpp_golden_encode() -> None:
    encoded = wire.encode_long_spatial(
        CPP_TOKEN,
        wire.MessageType.GENERIC_SPATIAL_1,
        7,
        (1, -2, 3),
        CPP_UUID,
        b"\xde\xad\xbe\xef",
        distance=8,
        decay=wire.DecayRate.EXPONENTIAL,
        game_token_id=123456789,
        sequence=42,
    )
    assert encoded == CPP_GOLDEN_SPATIAL


def test_crowdycpp_golden_parse_and_verify() -> None:
    msg = wire.parse_long_spatial(CPP_GOLDEN_SPATIAL)
    assert msg.type == 140
    assert msg.app_id == 7
    assert msg.chunk == (1, -2, 3)
    assert msg.distance == 8
    assert msg.decay == wire.DecayRate.EXPONENTIAL
    assert msg.contains_auth is True
    assert msg.uuid == CPP_UUID
    assert msg.payload == b"\xde\xad\xbe\xef"
    assert msg.epoch_or_token_id == 123456789
    assert msg.sequence == 42
    assert wire.verify_long_spatial(CPP_TOKEN, CPP_GOLDEN_SPATIAL)
    tampered = bytearray(CPP_GOLDEN_SPATIAL)
    tampered[70] ^= 0xFF
    assert not wire.verify_long_spatial(CPP_TOKEN, bytes(tampered))


def test_hmac_is_the_documented_scheme() -> None:
    prefix = CPP_GOLDEN_SPATIAL[:-41]
    expected = hmac.new(CPP_TOKEN, prefix + CPP_TOKEN, hashlib.sha256).digest()
    assert wire.spatial_hmac(CPP_TOKEN, prefix) == expected
    assert CPP_GOLDEN_SPATIAL[-41:-9] == expected


def test_crowdycpp_golden_reconnect() -> None:
    assert wire.verify_command_reconnect(CPP_TOKEN, CPP_GOLDEN_RECONNECT)
    assert not wire.verify_command_reconnect(JS_TOKEN, CPP_GOLDEN_RECONNECT)


def test_buffers_are_accepted_without_copying() -> None:
    for view in (bytearray(CPP_GOLDEN_SPATIAL), memoryview(CPP_GOLDEN_SPATIAL)):
        assert wire.parse_long_spatial(view).sequence == 42


def _chunk(value: dict[str, str]) -> tuple[int, int, int]:
    return int(value["x"]), int(value["y"]), int(value["z"])


def _b64(value: str) -> bytes:
    return base64.b64decode(value) if value else b""


# CrowdyJS's serializer defaults (src/binary-wire.ts): what each send carries when the
# caller leaves distance/decay/sequence out. CrowdyPy's send API keeps the same ones.
def _encode_uplink(kind: str, i: dict[str, Any]) -> bytes:
    seq = int(i.get("sequenceNumber", 0))
    if kind == "channelMessage":
        return wire.encode_channel_message(
            JS_TOKEN,
            int(i["channelId"]),
            i["uuid"].encode(),
            _b64(i["payload"]),
            game_token_id=JS_TOKEN_ID,
            sequence=seq,
        )
    t = wire.MessageType
    table: dict[str, tuple[int, bytes, int, int]] = {
        "actorUpdate": (t.ACTOR_UPDATE_REQUEST, _b64(i.get("state", "")), 8, 1),
        "actorUpdateDefaults": (t.ACTOR_UPDATE_REQUEST, _b64(i.get("state", "")), 8, 1),
        "audioPacket": (t.CLIENT_AUDIO_PACKET, _b64(i.get("audioData", "")), 1, 0),
        "videoPacket": (t.CLIENT_VIDEO_PACKET, _b64(i.get("videoData", "")), 1, 0),
        "textPacket": (t.CLIENT_TEXT_PACKET, i.get("text", "").encode(), 8, 0),
        "singleActorMessage": (t.SINGLE_ACTOR_MESSAGE, _b64(i.get("payload", "")), 0, 0),
    }
    if kind == "voxelUpdate":
        v = i["voxel"]
        entry = (
            t.VOXEL_UPDATE_REQUEST,
            wire.encode_voxel_payload(
                v["x"], v["y"], v["z"], i["voxelType"], _b64(i.get("voxelState", ""))
            ),
            8,
            0,
        )
    elif kind == "clientEvent":
        entry = (
            t.CLIENT_EVENT_NOTIFICATION,
            wire.encode_event_payload(i["eventType"], _b64(i.get("state", ""))),
            8,
            0,
        )
    else:
        entry = table[kind]
    message_type, payload, distance, decay = entry
    uuid = (i.get("targetUuid") or i["uuid"]).encode()
    return wire.encode_long_spatial(
        JS_TOKEN,
        message_type,
        int(i["appId"]),
        _chunk(i["chunk"]),
        uuid,
        payload,
        distance=int(i.get("distance", distance)),
        decay=int(i.get("decayRate", decay)),
        game_token_id=JS_TOKEN_ID,
        sequence=seq,
    )


@pytest.mark.parametrize("entry", FIXTURES["uplink"], ids=lambda e: e["kind"])
def test_crowdyjs_uplink_fixtures(entry: dict[str, Any]) -> None:
    assert _encode_uplink(entry["kind"], entry["input"]).hex() == entry["bytesHex"]


_PAYLOAD_FIELDS = ("state", "audioData", "videoData", "payload")


def _check_spatial(msg: wire.LongSpatialMessage, want: dict[str, Any]) -> None:
    assert msg.app_id == int(want["appId"])
    assert msg.chunk == (int(want["chunkX"]), int(want["chunkY"]), int(want["chunkZ"]))
    assert msg.uuid.decode() == want["uuid"]
    assert msg.sequence == want["sequenceNumber"]
    assert msg.epoch_or_token_id == int(want["epochMillis"])
    if "distance" in want:
        assert msg.distance == want["distance"]
    typename = want["__typename"]
    if typename == "VoxelUpdateNotification":
        voxel = wire.parse_voxel_payload(msg.payload)
        assert (voxel.x, voxel.y, voxel.z) == (want["voxelX"], want["voxelY"], want["voxelZ"])
        assert voxel.voxel_type == want["voxelType"]
        assert voxel.state == _b64(want.get("voxelState", ""))
    elif typename == "ClientTextNotification":
        assert msg.payload.decode() == want["text"]
    elif typename in ("ClientEventNotification", "ServerEventNotification"):
        assert int.from_bytes(msg.payload[:2], "little") == want["eventType"]
        assert msg.payload[2:] == _b64(want.get("state", ""))
    elif typename == "ActorLeftNotification":
        reason = 1 if msg.payload[:1] == b"\x01" else 0
        assert reason == want.get("leftReason", 0)
    else:
        field = next(f for f in _PAYLOAD_FIELDS if f in want)
        assert msg.payload == _b64(want[field])


@pytest.mark.parametrize("entry", FIXTURES["downlink"], ids=lambda e: e["kind"])
def test_crowdyjs_downlink_fixtures(entry: dict[str, Any]) -> None:
    parsed = list(wire.parse_datagram(bytes.fromhex(entry["bytesHex"])))
    assert len(parsed) == len(entry["expected"])
    for msg, want in zip(parsed, entry["expected"], strict=True):
        if want["__typename"] == "GenericErrorResponse":
            assert isinstance(msg, wire.GenericError)
            assert msg.sequence == want["sequenceNumber"]
            assert wire.ErrorCode(msg.code).name == want["errorCode"]
        elif want["__typename"] == "ChannelMessageNotification":
            assert isinstance(msg, wire.ChannelNotification)
            assert msg.channel_id == int(want["channelId"])
            assert msg.sender_uuid.decode() == want["uuid"]
            assert msg.payload == _b64(want["payload"])
            assert msg.sequence == want["sequenceNumber"]
            assert msg.epoch_millis == int(want["epochMillis"])
        else:
            assert isinstance(msg, wire.LongSpatialMessage)
            _check_spatial(msg, want)


def test_bundle_round_trip_and_lone_member() -> None:
    a = CPP_GOLDEN_SPATIAL
    b = bytes([3, 7, 7])
    packed = wire.bundle([a, b])
    assert packed[0] == wire.MessageType.MESSAGE_BUNDLE
    assert wire.split_datagram(packed) == [a, b]
    assert wire.bundle([a]) == a


def test_bundle_refuses_what_does_not_fit() -> None:
    with pytest.raises(CrowdyProtocolError, match="BufferTooSmall"):
        wire.bundle([CPP_GOLDEN_SPATIAL] * 33)


def test_signed_bundle_verifies_one_tag_per_datagram() -> None:
    member = bytes([3, 9, 7])
    body = (
        bytes([wire.MessageType.MESSAGE_BUNDLE_SIGNED]) + len(member).to_bytes(2, "little") + member
    )
    tag = hmac.new(JS_TOKEN, body + JS_TOKEN, hashlib.sha256).digest()
    datagram = body + tag
    assert wire.verify_signed_bundle(JS_TOKEN, datagram)
    assert not wire.verify_signed_bundle(CPP_TOKEN, datagram)
    assert wire.split_datagram(datagram) == [member]


def test_capabilities_message_encodes() -> None:
    encoded = wire.encode_long_spatial(
        CPP_TOKEN,
        wire.MessageType.CLIENT_CAPABILITIES,
        7,
        (0, 0, 0),
        b"",
        (1).to_bytes(4, "little"),
        game_token_id=1,
        sequence=1,
    )
    assert encoded[0] == 29
    assert encoded[68:72] == b"\x01\x00\x00\x00"


def test_argument_errors_are_protocol_errors() -> None:
    with pytest.raises(CrowdyProtocolError, match="InvalidArgument"):
        wire.encode_long_spatial(b"short", 128, 1, (0, 0, 0), b"", game_token_id=1)
    with pytest.raises(CrowdyProtocolError, match="InvalidArgument"):
        wire.encode_long_spatial(CPP_TOKEN, 128, 1, (0, 0, 0), b"x" * 33, game_token_id=1)
    with pytest.raises(CrowdyProtocolError):
        wire.encode_long_spatial(CPP_TOKEN, 128, 1, (0, 0, 0), b"", b"x" * 1124, game_token_id=1)


@settings(max_examples=2000, deadline=None)
@given(st.binary(max_size=1400))
def test_parsers_never_crash_on_garbage(data: bytes) -> None:
    for parse in (
        wire.parse_long_spatial,
        wire.parse_channel_notification,
        wire.parse_generic_error,
        wire.split_datagram,
        wire.parse_voxel_payload,
    ):
        with contextlib.suppress(CrowdyProtocolError):
            parse(data)
    with contextlib.suppress(CrowdyProtocolError):
        list(wire.parse_datagram(data))
    for verify in (
        wire.verify_long_spatial,
        wire.verify_signed_bundle,
        wire.verify_command_reconnect,
    ):
        with contextlib.suppress(CrowdyProtocolError):
            verify(CPP_TOKEN, data)
