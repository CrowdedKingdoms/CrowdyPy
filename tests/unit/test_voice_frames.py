"""The voice payload convention, replayed from CrowdyJS's shared fixture.

``tests/fixtures/voice-frames.json`` is CrowdyJS's ``test/unit/fixtures/voice-frames.json``
at the pin, byte for byte (``test_fixture_provenance.py``); CrowdyCPP replays the same file, and
its implementation is the one running here, so the three SDKs agree on every case.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from crowdypy import media
from crowdypy.media import (
    VoiceCodec,
    VoiceFlag,
    VoiceHeader,
    VoiceJitterBuffer,
    VoicePacketizer,
    decode_voice_packet,
    encode_voice_header,
    encode_voice_packet,
)

FIXTURE: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "voice-frames.json").read_text("utf-8")
)


def header(fields: dict[str, Any]) -> VoiceHeader:
    return VoiceHeader(
        version=fields.get("version", 1),
        codec=fields["codec"],
        seq=fields["seq"],
        timestamp=fields["timestamp"],
        frame_ms=fields["frameMs"],
        flags=fields["flags"],
    )


def test_header_constants_are_the_convention() -> None:
    assert media.VOICE_HEADER_BYTES == 10
    assert media.VOICE_HEADER_VERSION == 1
    assert media.MAX_VOICE_FRAME_BYTES == 1113
    assert media.VOICE_TARGET_DELAY_MS == 60
    assert media.VOICE_JITTER_MAX_FRAMES == 64
    assert media.VOICE_RESET_AFTER_MS == 200
    assert {c.name: int(c) for c in VoiceCodec} == {"RAW": 0, "OPUS": 1, "MULAW": 2}
    assert {f.name: int(f) for f in VoiceFlag} == {"SPURT_START": 1, "SPURT_END": 2}
    assert media.voice_clock_rate(VoiceCodec.OPUS) == 48000
    assert media.voice_clock_rate(VoiceCodec.MULAW) == 8000
    assert media.voice_clock_rate(VoiceCodec.RAW) == 1000
    assert media.voice_clock_rate(9) == 1000
    assert media.voice_samples_per_frame(VoiceCodec.OPUS, 20) == 960
    assert media.voice_samples_per_frame(VoiceCodec.MULAW, 60) == 480
    assert media.voice_seq_diff(0, 65535) == 1
    assert media.voice_seq_diff(65535, 0) == -1
    assert media.voice_seq_diff(0x8000, 0) == -0x8000
    assert media.voice_seq_diff(0x7FFF, 0) == 0x7FFF


def test_fixture_header_encodings() -> None:
    for case in FIXTURE["headers"]:
        note = case["note"]
        packet = encode_voice_packet(header(case["header"]), bytes.fromhex(case["frame"]))
        assert packet.hex() == case["packet"], note
        assert encode_voice_header(header(case["header"])).hex() == case["packet"][:20], note
        decoded = decode_voice_packet(bytes.fromhex(case["packet"]))
        assert decoded is not None, note
        assert decoded.header == header(case["header"]), note
        assert decoded.frame.hex() == case["frame"], note
    for case in FIXTURE["decoded"]:
        decoded = decode_voice_packet(bytes.fromhex(case["packet"]))
        assert decoded is not None, case["note"]
        assert decoded.header == header(case["header"]), case["note"]
        assert decoded.frame.hex() == case["frame"], case["note"]
    for case in FIXTURE["rejected"]:
        assert decode_voice_packet(bytes.fromhex(case["packet"])) is None, case["note"]


def test_fixture_packetizer() -> None:
    for case in FIXTURE["packetizer"]:
        options = case["options"]
        packetizer = VoicePacketizer(
            options["codec"],
            options["frameMs"],
            seq=options.get("seq", 0),
            timestamp=options.get("timestamp", 0),
        )
        for step in case["steps"]:
            if "skip" in step:
                packetizer.skip(step["skip"])
                assert packetizer.next_seq == step["nextSeq"], case["note"]
                assert packetizer.next_timestamp == step["nextTimestamp"], case["note"]
            else:
                packet = packetizer.packetize(
                    bytes.fromhex(step["packetize"]), last=step.get("last") is True
                )
                assert packet.hex() == step["packet"], f"{case['note']}: {step['packetize']}"


def _buffer(options: dict[str, Any]) -> VoiceJitterBuffer:
    names = {
        "targetDelayMs": "target_delay_ms",
        "maxFrames": "max_frames",
        "resetAfterMs": "reset_after_ms",
    }
    return VoiceJitterBuffer(**{names[key]: value for key, value in options.items()})


def test_fixture_jitter_buffer() -> None:
    for case in FIXTURE["jitter"]:
        buffer = _buffer(case.get("options") or {})
        for event in case["events"]:
            where = f"{case['note']} @{event['at']}"
            if "push" in event:
                packet = (
                    bytes.fromhex(event["packet"])
                    if "packet" in event
                    else encode_voice_packet(header(event["header"]), bytes.fromhex(event["frame"]))
                )
                assert buffer.push(event["push"], packet, event["at"]) == event["result"], where
                assert buffer.buffered_count(event["push"]) == event["buffered"], where
            elif "pull" in event:
                played = []
                for slot in buffer.pull(event["pull"], event["at"]):
                    assert slot.key == event["pull"], where
                    assert slot.gap == (slot.frame is None), where
                    played.append(
                        {
                            "seq": slot.seq,
                            "timestamp": slot.timestamp,
                            "codec": slot.codec,
                            "frameMs": slot.frame_ms,
                            "flags": slot.flags,
                            "frame": None if slot.frame is None else slot.frame.hex(),
                        }
                    )
                assert played == event["expect"], where
                assert buffer.sender_count == event["senders"], where
            elif "forget" in event:
                buffer.forget(event["forget"])
                assert buffer.sender_count == event["senders"], where
        counters = {
            "late": buffer.late,
            "duplicates": buffer.duplicates,
            "malformed": buffer.malformed,
            "discarded": buffer.discarded,
            "gaps": buffer.gaps,
        }
        assert counters == case["counters"], case["note"]


def test_the_fixture_exercises_reordering_loss_wrap_and_every_push_result() -> None:
    results: set[str] = set()
    gaps = 0
    wrapped = False
    for case in FIXTURE["jitter"]:
        for event in case["events"]:
            if "result" in event:
                results.add(event["result"])
            for slot in event.get("expect") or []:
                gaps += slot["frame"] is None
                wrapped |= slot["seq"] == 0 and slot["timestamp"] > 0
    assert sorted(results) == ["buffered", "duplicate", "late", "malformed"]
    assert gaps > 0
    assert wrapped


@pytest.mark.parametrize(
    "bad",
    [
        {"codec": 256},
        {"seq": 65536},
        {"seq": -1},
        {"timestamp": 2**32},
        {"frame_ms": 256},
        {"flags": 1.5},
    ],
)
def test_writers_refuse_fields_outside_their_width(bad: dict[str, Any]) -> None:
    fields: dict[str, Any] = {"codec": 1, "seq": 0, "timestamp": 0, "frame_ms": 20, "flags": 0}
    with pytest.raises(ValueError, match=next(iter(bad))):
        encode_voice_header(VoiceHeader(**{**fields, **bad}))


def test_writers_refuse_frames_that_do_not_fit() -> None:
    ok = VoiceHeader(codec=1, frame_ms=20)
    assert len(encode_voice_packet(ok, bytes(1113))) == 1123
    with pytest.raises(ValueError, match="exceeds 1113"):
        encode_voice_packet(ok, bytes(1114))
    with pytest.raises(ValueError, match="frame_ms"):
        VoicePacketizer(1, 0)
    packetizer = VoicePacketizer(VoiceCodec.OPUS, 20)
    with pytest.raises(ValueError, match="exceeds 1113"):
        packetizer.packetize(bytes(1114))
    assert packetizer.next_seq == 0, "a refused frame is not numbered"
    with pytest.raises(ValueError, match="frames"):
        packetizer.skip(-1)
    with pytest.raises(ValueError, match="target_delay_ms"):
        VoiceJitterBuffer(target_delay_ms=-1)
    with pytest.raises(ValueError, match="max_frames"):
        VoiceJitterBuffer(max_frames=0)
    with pytest.raises(ValueError, match="reset_after_ms"):
        VoiceJitterBuffer(target_delay_ms=60, reset_after_ms=60)


def test_a_skip_wraps_the_timestamp_like_crowdyjs() -> None:
    packetizer = VoicePacketizer(VoiceCodec.OPUS, 20, timestamp=2**32 - 960)
    packetizer.skip(2**32 + 1)  # frames % 2**32 == 1: one frame of 960 samples
    assert packetizer.next_timestamp == 0
    packetizer.skip(0)
    first = decode_voice_packet(packetizer.packetize(b"\x01"))
    assert first is not None
    assert first.header.flags == VoiceFlag.SPURT_START


def test_readers_never_raise_on_a_packet_from_the_network() -> None:
    buffer = VoiceJitterBuffer()
    rng = random.Random(7)
    for i in range(2000):
        data = bytearray(rng.randrange(256) for _ in range(rng.randrange(24)))
        if i % 3 == 0 and data:
            data[0] = 1
        decode_voice_packet(bytes(data))
        buffer.push(f"k{i % 5}", bytes(data), i)
        buffer.poll(i)
    assert buffer.malformed > 0


def test_poll_plays_every_sender_and_one_sender_holds_at_most_max_frames() -> None:
    buffer = VoiceJitterBuffer(max_frames=4)

    def packet(seq: int, flags: int = 0) -> bytes:
        return encode_voice_packet(
            VoiceHeader(codec=2, seq=seq, timestamp=seq * 160, frame_ms=20, flags=flags),
            bytes([seq % 256]),
        )

    assert buffer.push("x", packet(0, 1), 0) == "buffered"
    assert buffer.push("y", packet(100, 1), 5) == "buffered"
    for seq in (1, 2, 3):
        assert buffer.push("x", packet(seq), 1) == "buffered"
    assert buffer.buffered_count("x") == 4
    assert buffer.push("x", packet(4), 2) == "buffered", "outside the window: it starts over"
    assert buffer.buffered_count("x") == 1
    assert buffer.discarded == 4
    played = buffer.poll(70)
    assert sorted((slot.key, slot.seq) for slot in played) == [("x", 4), ("y", 100)]
    buffer.forget("y")
    assert buffer.sender_count == 1


def test_a_sender_speaking_with_the_packetizer_plays_back_in_order() -> None:
    packetizer = VoicePacketizer(VoiceCodec.OPUS, 20, seq=65530)
    buffer = VoiceJitterBuffer()
    # One packet every 20 ms; every fourth one is overtaken by the packet after it.
    arrivals = [
        (i * 20 + (25 if i % 4 == 1 else 0), packetizer.packetize(bytes([i]), last=i == 9))
        for i in range(10)
    ]
    played = []
    for t in range(401):
        for at, packet in arrivals:
            if at == t:
                assert buffer.push("speaker", packet, t) == "buffered"
        played += buffer.pull("speaker", t)
    assert [slot.frame for slot in played] == [bytes([i]) for i in range(10)]
    assert played[0].flags == VoiceFlag.SPURT_START
    assert played[9].flags == VoiceFlag.SPURT_END
    assert [slot.seq for slot in played] == [65530, 65531, 65532, 65533, 65534, 65535, 0, 1, 2, 3]
    assert (buffer.gaps, buffer.late) == (0, 0)


def test_the_clock_defaults_to_a_monotonic_one() -> None:
    buffer = VoiceJitterBuffer(target_delay_ms=0)
    packet = encode_voice_packet(VoiceHeader(codec=0, frame_ms=20, flags=1), b"pcm")
    assert buffer.push("me", packet) == "buffered"
    [slot] = buffer.pull("me")
    assert (slot.frame, slot.gap) == (b"pcm", False)
