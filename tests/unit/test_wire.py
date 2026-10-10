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
    (Path(__file__).resolve().parent.parent / "fixtures" / "binary-wire-fixtures.json").read_text(
        encoding="utf-8"
    )
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


# CrowdyCPP tests/wire_test.cpp testRangedChannelGolden: token "A" x 64, gameTokenId 555,
# channel 100, uuid "u" x 32, app 2, chunk (-3,4,5), maxDistance 12, payload "hi", seq 9.
RANGED_TOKEN = b"A" * 64
RANGED_GOLDEN = bytes.fromhex(
    "206400000000000000" + "75" * 32 + "0200000000000000fdffffffffffffff0400000000000000"
    "05000000000000000c00000002006869017f1c386fd1ec421f0a72745fae881f8adbcb17bcd8a0bb3e"
    "446565651d18533d2b0200000000000009"
)


def _ranged(max_distance: int, payload: bytes = b"hi") -> bytes:
    return wire.encode_ranged_channel_message(
        RANGED_TOKEN,
        100,
        b"u" * 32,
        payload,
        app_id=2,
        chunk=(-3, 4, 5),
        max_distance=max_distance,
        game_token_id=555,
        sequence=9,
    )


def test_ranged_channel_golden_and_hmac() -> None:
    assert wire.MessageType.CHANNEL_MESSAGE_RANGED_REQUEST == 32
    assert wire.CHANNEL_RANGED_MAX_DISTANCE == 2**31 - 1
    encoded = _ranged(12)
    assert encoded == RANGED_GOLDEN
    expected = hmac.new(RANGED_TOKEN, encoded[:-41] + RANGED_TOKEN, hashlib.sha256).digest()
    assert encoded[-41:-9] == expected


def test_ranged_channel_refusals() -> None:
    assert len(_ranged(wire.CHANNEL_RANGED_MAX_DISTANCE, b"")) == 121
    assert len(_ranged(0, b"x" * wire.MAX_CHANNEL_PAYLOAD)) == 121 + wire.MAX_CHANNEL_PAYLOAD
    for bad in (-1, wire.CHANNEL_RANGED_MAX_DISTANCE + 1):
        with pytest.raises(CrowdyProtocolError, match="InvalidArgument"):
            _ranged(bad)
    with pytest.raises(CrowdyProtocolError, match="InvalidArgument"):
        _ranged(5, b"x" * (wire.MAX_CHANNEL_PAYLOAD + 1))


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
    if kind == "rangedChannelMessage":
        return wire.encode_ranged_channel_message(
            JS_TOKEN,
            int(i["channelId"]),
            i["uuid"].encode(),
            _b64(i["payload"]),
            app_id=int(i["appId"]),
            chunk=_chunk(i["chunk"]),
            max_distance=int(i["maxDistance"]),
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


AUDIO_TOKEN = b"T" * 64
AUDIO_UUID = b"v" * 32
# The vector CrowdyJS's channel-audio test and CrowdyCPP's wire_test pin for channel audio:
# channel 4242, payload 01..05, gameTokenId 777, seq 200.
AUDIO_GOLDEN = bytes.fromhex(
    "23921000000000000076767676767676767676767676767676767676767676767676767676767676"
    "760500010203040501f7c505bbdfc07169f661cb3e3c8d061bbf0065ef4bdc239df40c77112ff8"
    "85150903000000000000c8"
)


def test_channel_audio_is_a_channel_message_with_type_byte_35() -> None:
    assert wire.MessageType.CHANNEL_AUDIO_REQUEST == 35
    assert wire.MessageType.CHANNEL_AUDIO_NOTIFICATION == 36
    payload = bytes([1, 2, 3, 4, 5])
    audio = wire.encode_channel_audio(
        AUDIO_TOKEN, 4242, AUDIO_UUID, payload, game_token_id=777, sequence=200
    )
    assert audio == AUDIO_GOLDEN
    message = wire.encode_channel_message(
        AUDIO_TOKEN, 4242, AUDIO_UUID, payload, game_token_id=777, sequence=200
    )
    prefix = 1 + 8 + 32 + 2 + len(payload) + 1
    assert (audio[0], message[0]) == (35, 17)
    assert audio[1:prefix] == message[1:prefix]
    tag = hmac.new(AUDIO_TOKEN, audio[:prefix] + AUDIO_TOKEN, hashlib.sha256).digest()
    assert audio[prefix : prefix + 32] == tag
    with pytest.raises(CrowdyProtocolError, match="InvalidArgument"):
        wire.encode_channel_audio(AUDIO_TOKEN, 1, AUDIO_UUID, bytes(1025), game_token_id=1)


def _channel_audio_notification(payload: bytes) -> bytes:
    return (
        bytes([wire.MessageType.CHANNEL_AUDIO_NOTIFICATION])
        + (9).to_bytes(8, "little")
        + AUDIO_UUID
        + len(payload).to_bytes(2, "little")
        + payload
        + (1_700_000_000_555).to_bytes(8, "little")
        + bytes([4])
    )


def test_channel_audio_notifications_parse_standalone_and_bundled() -> None:
    frame = _channel_audio_notification(b"\x01\x01\x07\x00")
    expected = wire.ChannelAudioNotification(
        9, AUDIO_UUID, b"\x01\x01\x07\x00", 1_700_000_000_555, 4
    )
    assert list(wire.parse_datagram(frame)) == [expected]
    assert list(wire.parse_datagram(wire.bundle([frame, frame]))) == [expected, expected]
    assert tuple(wire.parse_channel_notification(frame)) == tuple(expected)
    assert not isinstance(next(wire.parse_datagram(frame)), wire.ChannelNotification)
    with pytest.raises(CrowdyProtocolError):
        wire.parse_channel_notification(frame[:30])


def test_app_paused_is_udp_error_33() -> None:
    assert wire.ErrorCode.APP_PAUSED == 33
    assert wire.parse_generic_error(bytes([3, 9, 33])) == (9, wire.ErrorCode.APP_PAUSED)


def test_assert_voxel_edit_takes_the_apps_int16s_and_a_bounded_state() -> None:
    assert wire.VOXEL_STATE_MAX_BYTES == 1024
    wire.assert_voxel_edit((-32768, 32767, 16), -1, bytes(1024))
    wire.assert_voxel_edit((0, 0, 0), 300)
    for voxel, voxel_type, name in (
        ((32768, 0, 0), 1, "voxel x"),
        ((0, -32769, 0), 1, "voxel y"),
        ((0, 0, 1.5), 1, "voxel z"),
        ((0, 0, 0), 40000, "voxel type"),
    ):
        with pytest.raises(ValueError, match=name):
            wire.assert_voxel_edit(voxel, voxel_type)
    with pytest.raises(ValueError, match="1025 bytes; at most 1024"):
        wire.assert_voxel_edit((0, 0, 0), 1, bytes(1025))


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
