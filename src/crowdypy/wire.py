"""The public Replication API wire codec, from CrowdyCPP's native implementation.

The replication client uses this codec internally without crossing into Python; this
module exposes it for tools, tests and custom transports. Integers are little-endian,
actor uuids are 32 ASCII octets, and the 64-character app-scoped token is used as-is as
the 64-octet HMAC key (``HMAC-SHA256(token, prefix || token)``). See the public wire
format and HMAC pages of the Replication API docs.

Buffers may be any object exporting the buffer protocol (``bytes``, ``bytearray``,
``memoryview``, numpy arrays); nothing is copied on the way in.
"""

from __future__ import annotations

import operator
from collections.abc import Iterator, Sequence
from enum import IntEnum
from typing import Any, NamedTuple

from crowdypy import _native
from crowdypy.errors import CrowdyProtocolError

_wire: Any = _native.wire

__all__ = [
    "CHANNEL_RANGED_MAX_DISTANCE",
    "HMAC_TAG_SIZE",
    "LONG_SPATIAL_HEADER_SIZE",
    "MAX_BUNDLE_MEMBERS",
    "MAX_CHANNEL_PAYLOAD",
    "MAX_DATAGRAM_SIZE",
    "MAX_DISTANCE",
    "MAX_LONG_SPATIAL_PAYLOAD",
    "TOKEN_OCTETS",
    "UUID_SIZE",
    "VOXEL_STATE_MAX_BYTES",
    "ChannelAudioNotification",
    "ChannelNotification",
    "DecayRate",
    "ErrorCode",
    "GenericError",
    "LongSpatialMessage",
    "MessageType",
    "VoxelPayload",
    "assert_voxel_edit",
    "bundle",
    "encode_channel_audio",
    "encode_channel_message",
    "encode_event_payload",
    "encode_long_spatial",
    "encode_ranged_channel_message",
    "encode_voxel_payload",
    "parse_channel_notification",
    "parse_datagram",
    "parse_generic_error",
    "parse_long_spatial",
    "parse_voxel_payload",
    "spatial_hmac",
    "split_datagram",
    "verify_command_reconnect",
    "verify_long_spatial",
    "verify_signed_bundle",
]

MAX_DATAGRAM_SIZE: int = _wire.MAX_DATAGRAM_SIZE
MAX_LONG_SPATIAL_PAYLOAD: int = _wire.MAX_LONG_SPATIAL_PAYLOAD
LONG_SPATIAL_HEADER_SIZE: int = _wire.LONG_SPATIAL_HEADER_SIZE
UUID_SIZE: int = _wire.UUID_SIZE
HMAC_TAG_SIZE: int = _wire.HMAC_TAG_SIZE
TOKEN_OCTETS: int = _wire.TOKEN_OCTETS
MAX_BUNDLE_MEMBERS: int = _wire.MAX_BUNDLE_MEMBERS
MAX_CHANNEL_PAYLOAD: int = _wire.MAX_CHANNEL_PAYLOAD
MAX_DISTANCE: int = _wire.MAX_DISTANCE
#: The largest ``max_distance`` a distance-limited channel message takes, in chunks.
CHANNEL_RANGED_MAX_DISTANCE: int = _wire.CHANNEL_RANGED_MAX_DISTANCE
#: Most bytes a voxel edit's state may carry; the server refuses longer with INVALID_REQUEST.
VOXEL_STATE_MAX_BYTES: int = _wire.VOXEL_STATE_MAX_BYTES


class MessageType(IntEnum):
    BAD_MESSAGE = 0
    MESSAGE_BUNDLE = 2
    GENERIC_ERROR = 3
    CHANNEL_MESSAGE_REQUEST = 17
    CHANNEL_MESSAGE_NOTIFICATION = 18
    COMMAND_RECONNECT = 22
    CLIENT_ACTOR_HEARTBEAT = 26
    CLIENT_CAPABILITIES = 29
    MESSAGE_BUNDLE_SIGNED = 30
    CHANNEL_MESSAGE_RANGED_REQUEST = 32
    CHANNEL_AUDIO_REQUEST = 35
    CHANNEL_AUDIO_NOTIFICATION = 36
    ACTOR_UPDATE_REQUEST = 128
    ACTOR_UPDATE_NOTIFICATION = 130
    VOXEL_UPDATE_REQUEST = 131
    VOXEL_UPDATE_NOTIFICATION = 133
    CLIENT_AUDIO_PACKET = 134
    CLIENT_AUDIO_NOTIFICATION = 135
    CLIENT_TEXT_PACKET = 136
    CLIENT_TEXT_NOTIFICATION = 137
    CLIENT_EVENT_NOTIFICATION = 138
    SERVER_EVENT_NOTIFICATION = 139
    GENERIC_SPATIAL_1 = 140
    SINGLE_ACTOR_MESSAGE = 142
    CLIENT_VIDEO_PACKET = 143
    CLIENT_VIDEO_NOTIFICATION = 144
    ACTOR_LEFT_NOTIFICATION = 145


class ErrorCode(IntEnum):
    NO_ERROR = 0
    UNKNOWN_ERROR = 1
    INVALID_TOKEN = 5
    APP_NOT_FOUND = 6
    UNAUTHORIZED = 7
    GAME_TOKEN_WRONG_SIZE = 13
    INVALID_REQUEST = 15
    INVALID_APP_ID = 18
    USER_NOT_AUTHENTICATED = 20
    TOKEN_EXPIRED = 32
    #: The app's runtime gate is not active, so the server refused the send. Tell the
    #: player the world is paused rather than retrying.
    APP_PAUSED = 33


class DecayRate(IntEnum):
    """Replication density across Chebyshev distance rings 1-8."""

    NONE = 0
    EXPONENTIAL = 1
    LINEAR_50 = 2
    LINEAR_25 = 3
    LINEAR_10 = 4
    LINEAR_5 = 5


class LongSpatialMessage(NamedTuple):
    type: int
    app_id: int
    chunk: tuple[int, int, int]
    distance: int
    decay: int
    contains_auth: bool
    uuid: bytes
    payload: bytes
    #: Server to client: the server's epoch millis. Client to server: the game token id.
    epoch_or_token_id: int
    sequence: int


class ChannelNotification(NamedTuple):
    channel_id: int
    sender_uuid: bytes
    payload: bytes
    epoch_millis: int
    sequence: int


class ChannelAudioNotification(NamedTuple):
    """Channel audio from another member (CHANNEL_AUDIO_NOTIFICATION, 36): a channel
    notification's layout with its own type byte. ``payload`` is opaque to the server."""

    channel_id: int
    sender_uuid: bytes
    payload: bytes
    epoch_millis: int
    sequence: int


class GenericError(NamedTuple):
    sequence: int
    code: int


class VoxelPayload(NamedTuple):
    x: int
    y: int
    z: int
    voxel_type: int
    state: bytes


def _call(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except _native.NativeError as exc:
        raise CrowdyProtocolError(str(exc)) from exc


def spatial_hmac(token: Any, prefix: Any) -> bytes:
    """``HMAC-SHA256(token, prefix || token)``: the tag every signed message carries."""
    result: bytes = _call(_wire.spatial_hmac, token, prefix)
    return result


def encode_long_spatial(
    token: Any,
    message_type: int,
    app_id: int,
    chunk: Sequence[int],
    uuid: Any,
    payload: Any = b"",
    *,
    distance: int = 0,
    decay: int = DecayRate.NONE,
    game_token_id: int,
    sequence: int = 0,
) -> bytes:
    """Encode and sign one long-spatial message (header, payload, HMAC, token id, seq)."""
    result: bytes = _call(
        _wire.encode_long_spatial,
        token,
        int(message_type),
        int(app_id),
        int(chunk[0]),
        int(chunk[1]),
        int(chunk[2]),
        int(distance),
        int(decay),
        uuid,
        payload,
        int(game_token_id),
        int(sequence) & 0xFF,
    )
    return result


def parse_long_spatial(datagram: Any) -> LongSpatialMessage:
    """Parse one long-spatial message. Does NOT verify its HMAC (see verify_long_spatial)."""
    t, app, cx, cy, cz, distance, decay, auth, uuid, payload, epoch, seq = _call(
        _wire.parse_long_spatial, datagram
    )
    return LongSpatialMessage(
        t, app, (cx, cy, cz), distance, decay, auth, uuid, payload, epoch, seq
    )


def verify_long_spatial(token: Any, datagram: Any) -> bool:
    """True when a signed message's HMAC matches (an unsigned one verifies trivially)."""
    return bool(_call(_wire.verify_long_spatial, token, datagram) == "Ok")


def verify_signed_bundle(token: Any, datagram: Any) -> bool:
    """True when a MESSAGE_BUNDLE_SIGNED's trailing HMAC matches."""
    return bool(_call(_wire.verify_signed_bundle, token, datagram) == "Ok")


def verify_command_reconnect(token: Any, datagram: Any) -> bool:
    """True when a COMMAND_RECONNECT is genuine. Ignore one that is not."""
    return bool(_call(_wire.verify_command_reconnect, token, datagram) == "Ok")


def encode_channel_message(
    token: Any, channel_id: int, uuid: Any, payload: Any, *, game_token_id: int, sequence: int = 0
) -> bytes:
    result: bytes = _call(
        _wire.encode_channel_message,
        token,
        int(channel_id),
        uuid,
        payload,
        int(game_token_id),
        int(sequence) & 0xFF,
    )
    return result


def encode_channel_audio(
    token: Any, channel_id: int, uuid: Any, payload: Any, *, game_token_id: int, sequence: int = 0
) -> bytes:
    """A CHANNEL_AUDIO_REQUEST (35): :func:`encode_channel_message`'s layout and signing with
    its own type byte. ``payload`` is opaque, at most :data:`MAX_CHANNEL_PAYLOAD` bytes
    (typically one :class:`crowdypy.media.VoicePacketizer` packet)."""
    result: bytes = _call(
        _wire.encode_channel_audio,
        token,
        int(channel_id),
        uuid,
        payload,
        int(game_token_id),
        int(sequence) & 0xFF,
    )
    return result


def encode_ranged_channel_message(
    token: Any,
    channel_id: int,
    uuid: Any,
    payload: Any,
    *,
    app_id: int,
    chunk: Sequence[int],
    max_distance: int,
    game_token_id: int,
    sequence: int = 0,
) -> bytes:
    """A CHANNEL_MESSAGE_RANGED_REQUEST: a channel publish that reaches only members whose
    own actor is within ``max_distance`` chunks of ``chunk`` (straight-line distance,
    inclusive). Members receive the ordinary CHANNEL_MESSAGE_NOTIFICATION."""
    if not 0 <= int(max_distance) <= CHANNEL_RANGED_MAX_DISTANCE:
        raise CrowdyProtocolError(
            f"InvalidArgument: max_distance must be 0..{CHANNEL_RANGED_MAX_DISTANCE}"
        )
    result: bytes = _call(
        _wire.encode_ranged_channel_message,
        token,
        int(channel_id),
        uuid,
        payload,
        int(app_id),
        int(chunk[0]),
        int(chunk[1]),
        int(chunk[2]),
        int(max_distance),
        int(game_token_id),
        int(sequence) & 0xFF,
    )
    return result


def parse_channel_notification(datagram: Any) -> ChannelNotification:
    """A CHANNEL_MESSAGE_NOTIFICATION (18), or a CHANNEL_AUDIO_NOTIFICATION (36), which has the
    same layout: read ``datagram[0]`` for which. :func:`parse_datagram` tells them apart."""
    return ChannelNotification(*_call(_wire.parse_channel_notification, datagram))


def parse_generic_error(datagram: Any) -> GenericError:
    return GenericError(*_call(_wire.parse_generic_error, datagram))


def assert_voxel_edit(voxel: Sequence[int], voxel_type: int, voxel_state: Any = None) -> None:
    """Check a voxel edit before it is sent: the position's three coordinates and the type
    are the app's signed 16-bit integers (the platform checks no narrower range), and the
    state is at most :data:`VOXEL_STATE_MAX_BYTES`. Raises ``ValueError`` naming the field
    that does not fit."""
    for name, value in (
        ("voxel x", voxel[0]),
        ("voxel y", voxel[1]),
        ("voxel z", voxel[2]),
        ("voxel type", voxel_type),
    ):
        try:
            number = operator.index(value)
        except TypeError:
            raise ValueError(f"{name} must be a signed 16-bit integer: {value!r}") from None
        if not -32768 <= number <= 32767:
            raise ValueError(f"{name} must be a signed 16-bit integer: {value!r}")
    size = memoryview(voxel_state).nbytes if voxel_state is not None else 0
    if size > VOXEL_STATE_MAX_BYTES:
        raise ValueError(f"the voxel state is {size} bytes; at most {VOXEL_STATE_MAX_BYTES}")


def encode_voxel_payload(x: int, y: int, z: int, voxel_type: int, state: Any = b"") -> bytes:
    result: bytes = _call(_wire.encode_voxel_payload, x, y, z, voxel_type, state)
    return result


def parse_voxel_payload(payload: Any) -> VoxelPayload:
    return VoxelPayload(*_call(_wire.parse_voxel_payload, payload))


def encode_event_payload(event_type: int, state: Any = b"") -> bytes:
    result: bytes = _call(_wire.encode_event_payload, event_type, state)
    return result


def bundle(messages: Sequence[Any]) -> bytes:
    """Pack complete messages into one MESSAGE_BUNDLE datagram (a lone one goes unwrapped)."""
    result: bytes = _call(_wire.bundle, list(messages))
    return result


def split_datagram(datagram: Any) -> list[bytes]:
    """The messages in a datagram: each member of a bundle, or the datagram itself.

    A MESSAGE_BUNDLE_SIGNED's trailing HMAC is stripped, not checked; verify it first.
    """
    result: list[bytes] = _call(_wire.split_datagram, datagram)
    return result


def parse_datagram(
    datagram: Any,
) -> Iterator[
    LongSpatialMessage | ChannelNotification | ChannelAudioNotification | GenericError | bytes
]:
    """Every message in a datagram, parsed. Control frames are yielded as raw bytes."""
    for member in split_datagram(datagram):
        kind = member[0] if member else 0
        if kind == MessageType.GENERIC_ERROR:
            yield parse_generic_error(member)
        elif kind == MessageType.CHANNEL_MESSAGE_NOTIFICATION:
            yield parse_channel_notification(member)
        elif kind == MessageType.CHANNEL_AUDIO_NOTIFICATION:
            yield ChannelAudioNotification(*parse_channel_notification(member))
        elif kind & 0x80 or kind in (
            MessageType.CLIENT_ACTOR_HEARTBEAT,
            MessageType.CLIENT_CAPABILITIES,
        ):
            yield parse_long_spatial(member)
        else:
            yield member
