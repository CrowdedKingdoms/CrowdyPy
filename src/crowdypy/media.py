"""Webcam video and voice over replication.

**Video.** A frame (JPEG or WebP bytes) travels as up to 16 ``CLIENT_VIDEO_PACKET`` fragments,
each a 6-byte header plus a slice. ``connection.send_video_frame`` fragments and sends in one
call; :class:`VideoFrameAssembler` puts received fragments back together, natively, including a
whole batch's worth at once (:meth:`VideoFrameAssembler.ingest_batch`).

**Voice** is an optional convention for what an audio payload carries (``send_audio_packet``,
``send_channel_audio``); the server never looks inside one, and an app with a format of its own
keeps it. Every SDK writes it byte for byte the same (CrowdyJS's ``media/voice-frames.ts`` is
the reference, CrowdyCPP's ``voice_frames.hpp`` runs here): a 10-byte header, integers
little-endian, in front of each codec frame::

    offset  size  field      meaning
    0       1     version    1. A reader refuses any other version.
    1       1     codec      0 = unspecified / raw, 1 = Opus 48 kHz mono, 2 = G.711 µ-law 8 kHz.
    2       2     seq        uint16, +1 per packet sent, wraps.
    4       4     timestamp  uint32 in codec samples (48 kHz for Opus, 8 kHz for µ-law;
                             milliseconds for codec 0 and any codec not listed), wraps.
    8       1     frame_ms   the frame's duration in milliseconds.
    9       1     flags      bit 0: the first packet after silence (a talk spurt starts);
                             bit 1: the last packet before silence. Other bits are reserved.
    10      ...   frame      one codec frame.

:func:`decode_voice_packet` returns ``None`` for a packet shorter than 10 bytes or of another
version, and nothing here raises on a packet from the network. :class:`VoicePacketizer` numbers
one sender's frames; :class:`VoiceJitterBuffer` puts each sender's packets back in order, plays
them out after a fixed delay and reports the frames that never came as gaps.

No codec ships with CrowdyPy and the wheel links none: which codec an app uses, and the
library that encodes and decodes it (a libopus binding for Opus, say), is the app's choice.
These helpers carry its frames without reading them. Conceal a gap by repeating the last
decoded frame more quietly, or play silence. Where a voice sits in the world (panning,
distance) is the game's to decide.
"""

from __future__ import annotations

import math
import operator
import time
from enum import IntEnum, IntFlag
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple, cast

from crowdypy import _native

if TYPE_CHECKING:
    from crowdypy.replication import NotificationBatch

__all__ = [
    "MAX_VIDEO_FRAGMENTS",
    "MAX_VIDEO_FRAGMENT_BODY_BYTES",
    "MAX_VOICE_FRAME_BYTES",
    "VIDEO_FRAGMENT_HEADER_BYTES",
    "VIDEO_FRAGMENT_VERSION",
    "VIDEO_FRAME_TIMEOUT_MS",
    "VOICE_HEADER_BYTES",
    "VOICE_HEADER_VERSION",
    "VOICE_JITTER_MAX_FRAMES",
    "VOICE_RESET_AFTER_MS",
    "VOICE_TARGET_DELAY_MS",
    "VideoCodec",
    "VideoFragmentHeader",
    "VideoFrame",
    "VideoFrameAssembler",
    "VoiceCodec",
    "VoiceFlag",
    "VoiceHeader",
    "VoiceJitterBuffer",
    "VoicePacket",
    "VoicePacketizer",
    "VoicePlayout",
    "VoicePushResult",
    "decode_voice_packet",
    "encode_voice_header",
    "encode_voice_packet",
    "fragment_frame",
    "is_newer_frame_id",
    "parse_video_fragment_header",
    "voice_clock_rate",
    "voice_samples_per_frame",
    "voice_seq_diff",
]

_r: Any = _native.replication

VIDEO_FRAGMENT_HEADER_BYTES: Final[int] = _r.VIDEO_FRAGMENT_HEADER_BYTES
MAX_VIDEO_FRAGMENT_BODY_BYTES: Final[int] = _r.MAX_VIDEO_FRAGMENT_BODY_BYTES
MAX_VIDEO_FRAGMENTS: Final[int] = _r.MAX_VIDEO_FRAGMENTS
VIDEO_FRAME_TIMEOUT_MS: Final[int] = _r.VIDEO_FRAME_TIMEOUT_MS
VIDEO_FRAGMENT_VERSION: Final[int] = _r.VIDEO_FRAGMENT_VERSION


class VideoCodec(IntEnum):
    JPEG = 0
    WEBP = 1


class VideoFragmentHeader(NamedTuple):
    version: int
    codec: int
    frame_id: int
    frag_index: int
    frag_count: int


class VideoFrame(NamedTuple):
    """A frame the assembler completed. ``completed_at_ms`` is when its last fragment came."""

    uuid: str
    frame_id: int
    codec: VideoCodec
    data: bytes
    completed_at_ms: int


def _now_ms() -> int:
    return int(time.time() * 1000)


def fragment_frame(
    frame: Any,
    frame_id: int,
    codec: int = VideoCodec.JPEG,
    max_body: int = MAX_VIDEO_FRAGMENT_BODY_BYTES,
) -> list[bytes]:
    """The fragments of one encoded frame; empty when it needs more than 16 (send a smaller
    one: a partial frame is never sent)."""
    result: list[bytes] = _r.fragment_frame(frame, frame_id, int(codec), max_body)
    return result


def parse_video_fragment_header(packet: Any) -> VideoFragmentHeader | None:
    """The header of a fragment, or ``None`` for one a receiver must drop."""
    header = _r.parse_video_fragment_header(packet)
    return VideoFragmentHeader(*header) if header is not None else None


def is_newer_frame_id(a: int, b: int) -> bool:
    """Whether frame id ``a`` comes after ``b``, across the uint16 wrap."""
    result: bool = _r.is_newer_frame_id(a, b)
    return result


def _frame(raw: tuple[str, int, int, bytes, int]) -> VideoFrame:
    uuid, frame_id, codec, data, completed = raw
    return VideoFrame(uuid, frame_id, VideoCodec(codec), data, completed)


class VideoFrameAssembler:
    """Reassembles fragments per sender. A newer frame from a sender abandons its older one;
    :meth:`prune` abandons frames that stopped arriving, :meth:`forget` drops a sender that
    left (an ``actor_left`` is the natural trigger)."""

    def __init__(self, timeout_ms: int = VIDEO_FRAME_TIMEOUT_MS) -> None:
        self._native: Any = _r.VideoFrameAssembler(timeout_ms)

    def ingest(
        self, uuid: str | bytes, packet: Any, now_ms: int | None = None
    ) -> VideoFrame | None:
        """Add one fragment; returns the frame when this fragment completed it."""
        raw = self._native.ingest(uuid, packet, _now_ms() if now_ms is None else now_ms)
        return _frame(raw) if raw is not None else None

    def ingest_batch(self, batch: NotificationBatch, now_ms: int | None = None) -> list[VideoFrame]:
        """Add every video fragment in a batch; returns the frames completed, in order."""
        raws = self._native.ingest_batch(batch._native, _now_ms() if now_ms is None else now_ms)
        return [_frame(raw) for raw in raws]

    def prune(self, now_ms: int | None = None) -> int:
        result: int = self._native.prune(_now_ms() if now_ms is None else now_ms)
        return result

    def forget(self, uuid: str | bytes) -> None:
        self._native.forget(uuid)

    @property
    def pending_count(self) -> int:
        result: int = self._native.pending_count
        return result

    @property
    def dropped(self) -> int:
        result: int = self._native.dropped
        return result

    @property
    def abandoned(self) -> int:
        result: int = self._native.abandoned
        return result


# ------------------------------------------------------------------ voice

#: Bytes of voice header in front of every codec frame.
VOICE_HEADER_BYTES: Final[int] = _r.VOICE_HEADER_BYTES
#: The one header version this SDK writes and accepts.
VOICE_HEADER_VERSION: Final[int] = _r.VOICE_HEADER_VERSION
#: Largest codec frame one audio payload carries with the HMAC tail present (1123 - 10).
MAX_VOICE_FRAME_BYTES: Final[int] = _r.MAX_VOICE_FRAME_BYTES
#: Default :class:`VoiceJitterBuffer` ``target_delay_ms``.
VOICE_TARGET_DELAY_MS: Final[int] = _r.VOICE_TARGET_DELAY_MS
#: Default :class:`VoiceJitterBuffer` ``max_frames``.
VOICE_JITTER_MAX_FRAMES: Final[int] = _r.VOICE_JITTER_MAX_FRAMES
#: Default :class:`VoiceJitterBuffer` ``reset_after_ms``.
VOICE_RESET_AFTER_MS: Final[int] = _r.VOICE_RESET_AFTER_MS

_U8 = 0xFF
_U16 = 0xFFFF
_U32 = 0xFFFFFFFF


class VoiceCodec(IntEnum):
    """Codecs the voice header names. Any other value is carried through as it is."""

    #: Unspecified, or raw samples in a format the app defines. The timestamp counts ms.
    RAW = 0
    #: Opus, 48 kHz, one channel.
    OPUS = 1
    #: G.711 µ-law, 8 kHz, one byte per sample.
    MULAW = 2


class VoiceFlag(IntFlag):
    """Voice header flag bits."""

    #: The first packet after silence: a talk spurt starts.
    SPURT_START = 1
    #: The last packet before silence.
    SPURT_END = 2


class VoiceHeader(NamedTuple):
    """A parsed or to-be-written voice header. Writers always write version 1."""

    version: int = VOICE_HEADER_VERSION
    codec: int = VoiceCodec.RAW
    seq: int = 0
    timestamp: int = 0
    frame_ms: int = 0
    flags: int = 0


class VoicePacket(NamedTuple):
    """A parsed voice packet: its header and the codec frame after it."""

    header: VoiceHeader
    frame: bytes


#: What :meth:`VoiceJitterBuffer.push` did with a packet.
VoicePushResult = Literal["buffered", "late", "duplicate", "malformed"]


class VoicePlayout(NamedTuple):
    """One played slot: a frame, or a gap where one never came."""

    #: The sender's key, as pushed.
    key: str
    seq: int
    #: The frame's timestamp; for a gap, the one its frame would have had.
    timestamp: int
    codec: int
    frame_ms: int
    #: The frame's flags; 0 for a gap.
    flags: int
    #: True when the frame is missing: conceal ``frame_ms`` of audio (or play silence).
    gap: bool
    #: The codec frame, or ``None`` for a gap.
    frame: bytes | None


def _field(name: str, value: Any, maximum: int, minimum: int = 0) -> int:
    try:
        number = operator.index(value)
    except TypeError:
        number = None
    if number is None or not minimum <= number <= maximum:
        raise ValueError(f"{name} must be an integer {minimum}-{maximum}, got {value!r}")
    return number


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _clock(now_ms: float | None) -> int:
    return _monotonic_ms() if now_ms is None else math.floor(now_ms)


def voice_clock_rate(codec: int) -> int:
    """The sample clock a codec's timestamps count: 48000 (Opus), 8000 (µ-law), else 1000."""
    if codec == VoiceCodec.OPUS:
        return 48000
    if codec == VoiceCodec.MULAW:
        return 8000
    return 1000


def voice_samples_per_frame(codec: int, frame_ms: int) -> int:
    """How far the timestamp moves for one frame of ``frame_ms`` milliseconds."""
    return voice_clock_rate(codec) // 1000 * frame_ms


def voice_seq_diff(a: int, b: int) -> int:
    """The distance from seq ``b`` to seq ``a`` on the wrapping uint16 counter, from -32768 to
    32767: positive when ``a`` is newer."""
    d = (a - b) & _U16
    return d - 0x10000 if d >= 0x8000 else d


def _header_fields(header: VoiceHeader) -> tuple[int, int, int, int, int]:
    return (
        _field("codec", header.codec, _U8),
        _field("seq", header.seq, _U16),
        _field("timestamp", header.timestamp, _U32),
        _field("frame_ms", header.frame_ms, _U8),
        _field("flags", header.flags, _U8),
    )


def encode_voice_header(header: VoiceHeader) -> bytes:
    """Write a voice header (always version 1). Raises ``ValueError`` when a field is outside
    its width: codec, frame_ms and flags 0-255, seq 0-65535, timestamp 0-4294967295."""
    result: bytes = _r.encode_voice_header(*_header_fields(header))
    return result


def encode_voice_packet(header: VoiceHeader, frame: Any) -> bytes:
    """``header || frame``, ready for ``send_audio_packet`` or ``send_channel_audio``. Raises
    ``ValueError`` as :func:`encode_voice_header` does, and for a frame longer than
    :data:`MAX_VOICE_FRAME_BYTES`."""
    fields = _header_fields(header)
    size = memoryview(frame).nbytes
    if size > MAX_VOICE_FRAME_BYTES:
        raise ValueError(f"voice frame of {size} bytes exceeds {MAX_VOICE_FRAME_BYTES} bytes")
    result: bytes = _r.encode_voice_packet(*fields, frame)
    return result


def decode_voice_packet(packet: Any) -> VoicePacket | None:
    """Parse a voice packet. ``None`` for a packet shorter than :data:`VOICE_HEADER_BYTES` or of
    a version other than :data:`VOICE_HEADER_VERSION`; codecs and flag bits this SDK does not
    know are returned as they are."""
    raw = _r.decode_voice_packet(packet)
    if raw is None:
        return None
    *header, frame = raw
    return VoicePacket(VoiceHeader(*header), frame)


class VoicePacketizer:
    """Numbers one sender's frames: each packet gets the next seq (uint16, wraps) and a
    timestamp one frame later than the last (uint32, wraps). The first packet, and the first
    after a ``last`` one or a :meth:`skip`, carries :attr:`VoiceFlag.SPURT_START`.

    Keep one packetizer for as long as the sender speaks with the same codec and frame
    duration, muted stretches included: a fresh one starts its seq over, and a receiver that
    still holds the old stream drops packets that look old to it until the seq catches up or
    the stream is reset.
    """

    def __init__(self, codec: int, frame_ms: int, *, seq: int = 0, timestamp: int = 0) -> None:
        """``codec`` is a :class:`VoiceCodec` or an app's own value (0-255); ``frame_ms`` the
        duration of every frame, 1-255. Raises ``ValueError`` for a value outside its range."""
        self._native: Any = _r.VoicePacketizer(
            _field("codec", codec, _U8),
            _field("frame_ms", frame_ms, _U8, 1),
            _field("seq", seq, _U16),
            _field("timestamp", timestamp, _U32),
        )

    @property
    def codec(self) -> int:
        result: int = self._native.codec
        return result

    @property
    def frame_ms(self) -> int:
        result: int = self._native.frame_ms
        return result

    @property
    def next_seq(self) -> int:
        """The seq the next packet will carry."""
        result: int = self._native.next_seq
        return result

    @property
    def next_timestamp(self) -> int:
        """The timestamp the next packet will carry."""
        result: int = self._native.next_timestamp
        return result

    def packetize(self, frame: Any, *, last: bool = False) -> bytes:
        """The next packet: the header, then ``frame``. ``last``: this is the last packet
        before silence (:attr:`VoiceFlag.SPURT_END`); the next one starts a talk spurt. Raises
        ``ValueError`` for a frame longer than :data:`MAX_VOICE_FRAME_BYTES`, and then nothing
        is numbered."""
        size = memoryview(frame).nbytes
        if size > MAX_VOICE_FRAME_BYTES:
            raise ValueError(f"voice frame of {size} bytes exceeds {MAX_VOICE_FRAME_BYTES} bytes")
        result: bytes = self._native.packetize(frame, bool(last))
        return result

    def skip(self, frames: int = 1) -> None:
        """Silence: move the timestamp past ``frames`` frames that are not sent (the seq does
        not move), and start a talk spurt with the next packet. ``skip(0)`` only does the
        latter. Raises ``ValueError`` unless ``frames`` is a non-negative integer."""
        try:
            count = operator.index(frames)
        except TypeError:
            count = -1
        if count < 0:
            raise ValueError(f"frames must be a non-negative integer, got {frames!r}")
        self._native.skip(count % (_U32 + 1))


class VoiceJitterBuffer:
    """Puts each sender's voice packets back in order and plays them out after a fixed delay.

    Feed every audio payload to :meth:`push` with the sender's key (its actor uuid, or the
    channel and the uuid for channel audio), call :meth:`pull` (or :meth:`poll`) at least once a
    frame, and :meth:`forget` a sender that left. Pass both the same clock in milliseconds;
    both default to :func:`time.monotonic`.

    - A sender's stream starts with the first packet it accepts, which plays
      ``target_delay_ms`` after it arrived; each later seq plays ``frame_ms`` after the one
      before it.
    - :meth:`pull` returns the slots that are due, in seq order: the frame, or a gap
      (``frame`` ``None``) when it is missing. After the last packet before silence
      (:attr:`VoiceFlag.SPURT_END`) the stream reports nothing more. It reports slots only
      until ``reset_after_ms`` after the last packet it accepted, and is then reset.
    - A packet is late, and dropped, when its slot has passed or has been played. One older
      than the first slot of a spurt that has not started playing is fitted in front of it while
      its slot is still ahead.
    - The stream starts over, dropping what it holds, on a packet that starts a talk spurt and
      is newer than every packet it accepted; on a packet newer than the spurt's last packet
      before silence; on a change of codec or frame duration; on a packet ``max_frames`` or
      more seqs ahead of the next one to play; and on any packet after ``reset_after_ms``
      without one accepted. A sender that restarts its seq lower is dropped as late until then.
    - A sender holds at most ``max_frames`` frames.

    The frame duration is fixed within a talk spurt, and so is the delay once the spurt starts:
    a sender whose clock runs fast fills the window, one that runs slow underruns, and both
    recover at the next spurt.
    """

    def __init__(
        self,
        *,
        target_delay_ms: int = VOICE_TARGET_DELAY_MS,
        max_frames: int = VOICE_JITTER_MAX_FRAMES,
        reset_after_ms: int = VOICE_RESET_AFTER_MS,
    ) -> None:
        """Raises ``ValueError`` for a negative ``target_delay_ms``, a ``max_frames`` outside
        1-4096, or a ``reset_after_ms`` not above ``target_delay_ms``."""
        delay = _field("target_delay_ms", target_delay_ms, 2**62)
        frames = _field("max_frames", max_frames, 4096, 1)
        try:
            reset = operator.index(reset_after_ms)
        except TypeError:
            reset = None
        if reset is None or not delay < reset <= 2**62:
            raise ValueError(
                f"reset_after_ms must be an integer above target_delay_ms ({delay}), "
                f"got {reset_after_ms!r}"
            )
        self._native: Any = _r.VoiceJitterBuffer(delay, frames, reset)

    def push(self, key: str, packet: Any, now_ms: float | None = None) -> VoicePushResult:
        """Add one packet from the sender ``key``. Never raises on the packet's contents."""
        return cast(VoicePushResult, self._native.push(key, packet, _clock(now_ms)))

    def pull(self, key: str, now_ms: float | None = None) -> list[VoicePlayout]:
        """The sender's slots due by ``now_ms``, in seq order."""
        return [VoicePlayout(*slot) for slot in self._native.pull(key, _clock(now_ms))]

    def poll(self, now_ms: float | None = None) -> list[VoicePlayout]:
        """Every sender's due slots (:meth:`pull` for each), senders in no particular order."""
        return [VoicePlayout(*slot) for slot in self._native.poll(_clock(now_ms))]

    def forget(self, key: str) -> None:
        """Drop a sender's stream (it left)."""
        self._native.forget(key)

    def buffered_count(self, key: str) -> int:
        """Frames the sender has buffered."""
        result: int = self._native.buffered_count(key)
        return result

    @property
    def sender_count(self) -> int:
        """Senders with a stream."""
        result: int = self._native.sender_count
        return result

    @property
    def late(self) -> int:
        """Packets dropped because their slot had passed."""
        result: int = self._native.late
        return result

    @property
    def duplicates(self) -> int:
        """Packets dropped because their seq was already buffered."""
        result: int = self._native.duplicates
        return result

    @property
    def malformed(self) -> int:
        """Packets refused as not voice v1 (or with a frame duration of 0)."""
        result: int = self._native.malformed
        return result

    @property
    def discarded(self) -> int:
        """Buffered frames thrown away unplayed (a stream started over, went silent, or was
        forgotten)."""
        result: int = self._native.discarded
        return result

    @property
    def gaps(self) -> int:
        """Slots reported as gaps."""
        result: int = self._native.gaps
        return result
