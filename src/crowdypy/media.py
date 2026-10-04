"""Webcam video over replication: fragment an encoded frame, reassemble received ones.

A frame (JPEG or WebP bytes) travels as up to 16 ``CLIENT_VIDEO_PACKET`` fragments, each a
6-byte header plus a slice. ``connection.send_video_frame`` fragments and sends in one call;
:class:`VideoFrameAssembler` puts received fragments back together, natively, including a whole
batch's worth at once (:meth:`VideoFrameAssembler.ingest_batch`).
"""

from __future__ import annotations

import time
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from crowdypy import _native

if TYPE_CHECKING:
    from crowdypy.replication import NotificationBatch

__all__ = [
    "MAX_VIDEO_FRAGMENTS",
    "MAX_VIDEO_FRAGMENT_BODY_BYTES",
    "VIDEO_FRAGMENT_HEADER_BYTES",
    "VIDEO_FRAGMENT_VERSION",
    "VIDEO_FRAME_TIMEOUT_MS",
    "VideoCodec",
    "VideoFragmentHeader",
    "VideoFrame",
    "VideoFrameAssembler",
    "fragment_frame",
    "is_newer_frame_id",
    "parse_video_fragment_header",
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
