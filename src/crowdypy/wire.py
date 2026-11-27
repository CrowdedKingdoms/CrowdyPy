"""The public Replication API wire codec, from CrowdyCPP's native implementation.

The replication client uses this codec internally without crossing into Python; this
module exposes it for tools, tests and custom transports. Integers are little-endian,
actor uuids are 32 ASCII octets, and the 64-character app-scoped token is used as-is as
the 64-octet HMAC key (``HMAC-SHA256(token, prefix || token)``). See the public wire
format and HMAC pages of the Replication API docs.

Buffers may be any object exporting the buffer protocol (``bytes``, ``bytearray``,
``memoryview``, numpy arrays); nothing is copied on the way in.

The fleet session's messages (49 to 53, realtime servers v0.50.3) are here too:
:func:`fleet_carry_proof`, :func:`encode_fleet_carry`, :func:`encode_fleet_release`,
:func:`verify_fleet_frame` and the three ``parse_fleet_*`` functions. A fleet token signs for
every player its session carries: it stays on a server the organization runs, beside the
organization's org token, and never goes to a player's device. ``crowdypy.fleet`` is the
session that sends and receives these.
"""

from __future__ import annotations

import operator
from collections.abc import Iterator, Sequence
from enum import IntEnum, IntFlag
from typing import Any, NamedTuple

from crowdypy import _native
from crowdypy.errors import CrowdyProtocolError

_wire: Any = _native.wire

__all__ = [
    "CHANNEL_RANGED_MAX_DISTANCE",
    "CONTROL_NONCE_BYTES",
    "FLEET_CARRY_CLOCK_TOLERANCE_MS",
    "FLEET_CARRY_PAYLOAD_BYTES",
    "FLEET_CARRY_PROOF_LABEL",
    "FLEET_CARRY_RESULT_BYTES",
    "FLEET_MAX_DATAGRAM_BYTES",
    "FLEET_MAX_DELIVERY_PLAYERS",
    "FLEET_RELEASED_BYTES",
    "FLEET_TOKEN_CHARS",
    "HMAC_TAG_SIZE",
    "LONG_SPATIAL_HEADER_SIZE",
    "MAX_BUNDLE_MEMBERS",
    "MAX_CHANNEL_PAYLOAD",
    "MAX_DATAGRAM_SIZE",
    "MAX_DISTANCE",
    "MAX_LONG_SPATIAL_PAYLOAD",
    "MAX_MEDIA_SPEAKERS",
    "TOKEN_OCTETS",
    "UUID_SIZE",
    "VOXEL_STATE_MAX_BYTES",
    "ChannelAudioNotification",
    "ChannelNotification",
    "ClientCapability",
    "DecayRate",
    "ErrorCode",
    "FleetCarryResultFrame",
    "FleetCarryStatus",
    "FleetDeliveryFrame",
    "FleetReleaseReason",
    "FleetReleasedFrame",
    "GenericError",
    "LongSpatialMessage",
    "MessageType",
    "ReleaseReason",
    "SessionReleaseReason",
    "SpeakerListMode",
    "VoxelPayload",
    "assert_voxel_edit",
    "bundle",
    "encode_channel_audio",
    "encode_channel_message",
    "encode_event_payload",
    "encode_fleet_carry",
    "encode_fleet_release",
    "encode_long_spatial",
    "encode_ranged_channel_message",
    "encode_voxel_payload",
    "fleet_carry_proof",
    "parse_channel_notification",
    "parse_datagram",
    "parse_fleet_carry_result",
    "parse_fleet_delivery",
    "parse_fleet_released",
    "parse_generic_error",
    "parse_long_spatial",
    "parse_voxel_payload",
    "spatial_hmac",
    "split_datagram",
    "verify_command_reconnect",
    "verify_fleet_frame",
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
#: The nonce a capabilities message carries after its flags with ``BOUND_CONTROL``, in bytes.
CONTROL_NONCE_BYTES: int = _wire.CONTROL_NONCE_SIZE
#: The most speakers one list of a CLIENT_MEDIA_SPEAKERS may name.
MAX_MEDIA_SPEAKERS: int = _wire.MAX_SPEAKERS
#: The largest datagram a fleet session is sent: a FLEET_DELIVERY for one player, alone, with
#: its own HMAC. Nothing else the realtime servers send a client is above
#: :data:`MAX_DATAGRAM_SIZE`.
FLEET_MAX_DATAGRAM_BYTES: int = _wire.FLEET_MAX_RECEIVE_DATAGRAM_SIZE
#: The most players one FLEET_DELIVERY lists; the server splits a longer list over frames.
FLEET_MAX_DELIVERY_PLAYERS: int = _wire.FLEET_MAX_DELIVERY_RECIPIENTS
#: A token as its 64 octets: a fleet token and a player's app token alike.
FLEET_TOKEN_CHARS: int = TOKEN_OCTETS
#: The ASCII label a FLEET_CARRY proof starts with (14 bytes).
FLEET_CARRY_PROOF_LABEL: str = _wire.FLEET_CARRY_PROOF_LABEL
#: A FLEET_CARRY whose time is further than this from the server's is refused (status 3).
FLEET_CARRY_CLOCK_TOLERANCE_MS: int = _wire.FLEET_CARRY_CLOCK_TOLERANCE_MS
#: FLEET_CARRY's app payload: ``[8B player gameTokenId][8B epoch ms][32B proof]``.
FLEET_CARRY_PAYLOAD_BYTES: int = _wire.FLEET_CARRY_PAYLOAD_SIZE
#: FLEET_CARRY_RESULT without its HMAC: ``[1B 51][8B player gameTokenId][8B user id][1B status]``.
FLEET_CARRY_RESULT_BYTES: int = _wire.FLEET_CARRY_RESULT_SIZE
#: FLEET_RELEASED without its HMAC: ``[1B 52][8B player gameTokenId][1B reason]``.
FLEET_RELEASED_BYTES: int = _wire.FLEET_RELEASED_SIZE


class MessageType(IntEnum):
    BAD_MESSAGE = 0
    MESSAGE_BUNDLE = 2
    GENERIC_ERROR = 3
    CHANNEL_MESSAGE_REQUEST = 17
    CHANNEL_MESSAGE_NOTIFICATION = 18
    COMMAND_RECONNECT = 22
    CLIENT_ACTOR_HEARTBEAT = 26
    #: ``[28][reason][32-byte HMAC over the two leading bytes, keyed on the game token]``.
    #: The connection handles it; see :class:`ReleaseReason`.
    COMMAND_SESSION_RELEASED = 28
    CLIENT_CAPABILITIES = 29
    #: ``[44][8-byte nonce][32-byte HMAC over the first 9 bytes]``: COMMAND_RECONNECT bound to
    #: the assignment's nonce, which the capabilities message carries (replication server
    #: v0.45.0). The connection handles it; one with another assignment's nonce is counted in
    #: ``stats()["stale_bound_control"]`` and dropped.
    COMMAND_RECONNECT_BOUND = 44
    #: ``[45][reason][8-byte nonce][32-byte HMAC over the first 10 bytes]``: the bound
    #: COMMAND_SESSION_RELEASED. A verified idle release is final, without the idle guard.
    COMMAND_SESSION_RELEASED_BOUND = 45
    #: ``[46][8-byte nonce][32-byte HMAC over the first 9 bytes]``: the server acknowledges the
    #: bound forms for this assignment (replication server v0.46.0). The connection handles it,
    #: counted in ``stats()["bound_control_acks"]``; after it, a legacy 22 or 28 on the
    #: assignment is dropped and counted in ``stats()["legacy_control_dropped"]``.
    CONTROL_BOUND_ACK = 46
    #: The speakers whose spatial voice and video a session receives; the capabilities' layout.
    CLIENT_MEDIA_SPEAKERS = 47
    #: Client to server, a fleet session only: start carrying a player. The capabilities'
    #: layout under the session's own game token id; see :func:`encode_fleet_carry`.
    FLEET_CARRY = 49
    #: Client to server, a fleet session only: the player left the game.
    FLEET_RELEASE = 50
    #: Server to client, a fleet session only: the answer to a FLEET_CARRY.
    FLEET_CARRY_RESULT = 51
    #: Server to client, a fleet session only: a carry ended.
    FLEET_RELEASED = 52
    #: Server to client, a fleet session only: one notification and the only carried players
    #: who may receive it.
    FLEET_DELIVERY = 53
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
    #: A chunk near the player was replaced as a whole (a hub's or mod's ``world.set_chunk``);
    #: the payload is its new ``updatedAt`` in epoch milliseconds (replication server v0.45.0).
    CHUNK_CHANGED_NOTIFICATION = 146


class ReleaseReason(IntEnum):
    """Why the server ended a session (``COMMAND_SESSION_RELEASED``'s second byte)."""

    DEAUTHORIZED = 0
    STALE = 1
    TOKEN_EXPIRED = 2
    OVERLOAD = 3
    REBALANCE = 4
    #: No gameplay input for the app's idle timeout (replication server v0.44.0). The
    #: connection moves to ``ConnState.INACTIVE`` and does not reconnect.
    IDLE = 5


#: CrowdyJS's name for :class:`ReleaseReason`.
SessionReleaseReason = ReleaseReason


class ClientCapability(IntFlag):
    """The flags of a CLIENT_CAPABILITIES (29): a little-endian u32 as the app payload."""

    #: This client reads MESSAGE_BUNDLE_SIGNED (one HMAC for a downlink bundle).
    BUNDLE_SIGNED = _wire.CLIENT_CAPABILITY_BUNDLE_SIGNED
    #: :data:`CONTROL_NONCE_BYTES` nonce bytes follow the flags, and the server sends this
    #: session the bound control frames (44 and 45) in place of 22 and 28.
    BOUND_CONTROL = _wire.CLIENT_CAPABILITY_BOUND_CONTROL
    #: Send this session its own spatial voice and video.
    OWN_MEDIA_ECHO = _wire.CLIENT_CAPABILITY_OWN_MEDIA_ECHO
    #: A fleet session only: send it the state its own carried players sent, when another of
    #: its carried players is in range. Off by default: a game server simulates its own players.
    FLEET_OWN_STATE = _wire.CLIENT_CAPABILITY_FLEET_OWN_STATE


class SpeakerListMode(IntEnum):
    """How a CLIENT_MEDIA_SPEAKERS (47) changes one kind's list."""

    KEEP = 0
    ONLY = 1
    EVERYONE = 2


class FleetCarryStatus(IntEnum):
    """FLEET_CARRY_RESULT's status byte. Treat a value not listed as a refusal."""

    #: The session carries the player; the result names their user id.
    CARRIED = 0
    #: No live token by that game token id (also: a token of another app).
    NO_LIVE_TOKEN = 1
    #: The token is another organization's player's.
    OTHER_ORGANIZATION = 2
    #: The proof or its time failed (also: a carry sent again after it ended, a replay).
    PROOF_FAILED = 3
    #: The session carries as many players as it may (also: the server has no place for it).
    SESSION_FULL = 4


class FleetReleaseReason(IntEnum):
    """FLEET_RELEASED's reason byte. Treat a value not listed as the carry having ended."""

    #: Another session carries the player now: they moved to another of your servers.
    CARRIED_ELSEWHERE = 1
    #: The player's sign-in ended (revoked, or expired and not carried again with a live token).
    SIGN_IN_ENDED = 2
    #: No gameplay input from the player for the app's idle timeout.
    IDLE = 5


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


class FleetCarryResultFrame(NamedTuple):
    """A FLEET_CARRY_RESULT (51)."""

    player_game_token_id: int
    #: The player's user id when ``status`` is 0 (carried); 0 otherwise.
    user_id: int
    #: :class:`FleetCarryStatus`; a value it does not list is a refusal.
    status: int


class FleetReleasedFrame(NamedTuple):
    """A FLEET_RELEASED (52)."""

    player_game_token_id: int
    #: :class:`FleetReleaseReason`; a value it does not list still means the carry ended.
    reason: int


class FleetDeliveryFrame(NamedTuple):
    """A FLEET_DELIVERY (53): one notification and the only carried players who may receive it."""

    #: 1 to :data:`FLEET_MAX_DELIVERY_PLAYERS` game token ids.
    players: tuple[int, ...]
    #: What a player's own session would have been sent, in its unsigned form: a long-spatial
    #: notification (:func:`parse_long_spatial`) or a channel notification, 18 or 36
    #: (:func:`parse_channel_notification`).
    notification: bytes


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


def fleet_carry_proof(
    player_token: Any, fleet_game_token_id: int, player_game_token_id: int, epoch_millis: int
) -> bytes:
    """The proof inside a FLEET_CARRY: it shows the game server holds the player's token.

    ``HMAC-SHA256(key = the player's 64-octet token, "ck-fleet-carry" || 8B fleet gameTokenId
    || 8B player gameTokenId || 8B epoch millis)``, 32 bytes. The player's token keys the
    proof and is not sent."""
    result: bytes = _call(
        _wire.fleet_carry_proof,
        player_token,
        int(fleet_game_token_id),
        int(player_game_token_id),
        int(epoch_millis),
    )
    return result


def encode_fleet_carry(
    fleet_token: Any,
    app_id: int,
    *,
    fleet_game_token_id: int,
    player_token: Any,
    player_game_token_id: int,
    epoch_millis: int,
    sequence: int = 0,
) -> bytes:
    """A FLEET_CARRY (49): start carrying a player. The capabilities' long-spatial layout
    (chunk (0,0,0), distance 0, no actor uuid) under the session's own game token id, signed
    with the fleet token; the app payload is ``[8B player gameTokenId][8B epoch
    millis][32B proof]`` (:func:`fleet_carry_proof`).

    The server refuses a time more than 30 seconds from its own, and one older than the last
    carry of that player it accepted from this session: a carry sent again needs a new time
    and a new proof."""
    result: bytes = _call(
        _wire.encode_fleet_carry,
        fleet_token,
        int(app_id),
        int(fleet_game_token_id),
        player_token,
        int(player_game_token_id),
        int(epoch_millis),
        int(sequence) & 0xFF,
    )
    return result


def encode_fleet_release(
    fleet_token: Any,
    app_id: int,
    *,
    fleet_game_token_id: int,
    player_game_token_id: int,
    sequence: int = 0,
) -> bytes:
    """A FLEET_RELEASE (50): the player left the game. FLEET_CARRY's layout; the app payload
    is ``[8B player gameTokenId]``. It has no answer."""
    result: bytes = _call(
        _wire.encode_fleet_release,
        fleet_token,
        int(app_id),
        int(fleet_game_token_id),
        int(player_game_token_id),
        int(sequence) & 0xFF,
    )
    return result


def verify_fleet_frame(fleet_token: Any, message: Any) -> bytes:
    """Verify a fleet session's server frame (51, 52 or 53) in the form that carries its own
    HMAC: alone in its datagram, or a member of a MESSAGE_BUNDLE (2). Its last 32 bytes are
    ``HMAC-SHA256(key = the fleet token, every byte before them || the fleet token)``.
    Returns the frame without its HMAC, for the ``parse_fleet_*`` functions.

    Raises :class:`crowdypy.errors.CrowdyProtocolError` (``HmacMismatch``) for a frame the
    fleet token did not sign, which must be dropped. A member of a MESSAGE_BUNDLE_SIGNED (30)
    carries no HMAC of its own: verify the bundle (:func:`verify_signed_bundle`) and give its
    members to the parsers as they are."""
    result: bytes = _call(_wire.verify_fleet_frame, fleet_token, message)
    return result


def parse_fleet_carry_result(frame: Any) -> FleetCarryResultFrame:
    """A FLEET_CARRY_RESULT (51) without its HMAC: 18 bytes exactly. Does not verify."""
    return FleetCarryResultFrame(*_call(_wire.parse_fleet_carry_result, frame))


def parse_fleet_released(frame: Any) -> FleetReleasedFrame:
    """A FLEET_RELEASED (52) without its HMAC: 10 bytes exactly. Does not verify."""
    return FleetReleasedFrame(*_call(_wire.parse_fleet_released, frame))


def parse_fleet_delivery(frame: Any) -> FleetDeliveryFrame:
    """A FLEET_DELIVERY (53) without its HMAC: ``[1B 53][1B count][count x 8B player
    gameTokenId][notification]``. Does not verify."""
    players, notification = _call(_wire.parse_fleet_delivery, frame)
    return FleetDeliveryFrame(tuple(players), notification)


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
            MessageType.CLIENT_MEDIA_SPEAKERS,
            MessageType.FLEET_CARRY,
            MessageType.FLEET_RELEASE,
        ):
            yield parse_long_spatial(member)
        else:
            yield member
