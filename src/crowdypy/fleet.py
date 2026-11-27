"""Fleet sessions: one realtime session an organization's game server opens for itself, in
which it carries the players who are on it (realtime servers v0.50.3).

**A fleet session runs on a server your organization runs.** The organization's org token
opens it (``client.fleet_sessions``), and its fleet token signs for every player the session
carries. An org token and a fleet token never go to a player's device or into a browser page.
A game on a player's device uses ``client.udp`` with that player's own app token.

    api = crowdypy.AsyncCrowdyClient(http_url="https://ck.dev.crowdedkingdoms.com")
    api.set_token(os.environ["CK_ORG_TOKEN"])  # cko_..., scope fleet:session

    fleet = AsyncFleetSession(
        await api.fleet_sessions.open(app_id),  # one session for each game server
        handlers={
            "actor_update": lambda n, players: world.apply(n),  # state: once for the session
            "text": lambda n, players: chat.deliver(players, n),  # only these players
            "released": lambda r: roster.remove(r.player_game_token_id),
        },
    )
    await fleet.connect()

    carry = await fleet.carry(player_token)  # the app token your server minted for the player
    if carry.carried:
        fleet.send_actor_update(carry.player_game_token_id, chunk, uuid, state)

    if fleet.refresh_due():  # in your loop
        fleet.apply_refresh(await api.fleet_sessions.refresh(fleet.refresh_input()))

The session is CrowdyCPP's ``replication::FleetSession``, bound as a player's connection is:
its network thread receives, verifies every frame with the fleet token and queues events, and
sends the carries that are due. Python takes events in batches (:class:`FleetBatch`), each row
with the carried players it is for, and sends with the GIL released. The session makes no
Application API call: :meth:`refresh_due` says when to call ``refreshFleetSession``, and
:meth:`apply_refresh` takes the answer.

:class:`AsyncFleetSession` runs in an asyncio loop; :class:`FleetSession` is the blocking twin,
which you :meth:`~FleetSession.poll` from your game loop.

Game token ids and user ids are 64-bit integers here (``int``); every method that takes one
also takes its decimal string, as the Application API returns it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import operator
import re
import select
import socket
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, NamedTuple

import msgspec

from crowdypy import _native
from crowdypy.domains.fleet_sessions import FleetSessionAnswer
from crowdypy.errors import CrowdyRealtimeError, CrowdyReplicationError
from crowdypy.replication import (
    _HANDLER_KEYS,
    _LOG_LEVELS,
    KEEP_SPEAKERS,
    STATUS,
    Assignment,
    ConnState,
    KeepSpeakers,
    Notification,
    NotificationBatch,
    _chunk_rows,
    _column,
    _payload_rows,
    _replication_error,
    _speaker_change,
    _uuid_rows,
)
from crowdypy.wire import (
    CHANNEL_RANGED_MAX_DISTANCE,
    FLEET_TOKEN_CHARS,
    VOXEL_STATE_MAX_BYTES,
    DecayRate,
    ErrorCode,
    FleetCarryStatus,
    MessageType,
)

__all__ = [
    "CARRY_RESULT",
    "HANDLER_NAMES",
    "MIN_SILENCE_TIMEOUT_MS",
    "RELEASED",
    "SEND_REFUSED",
    "SESSION_RELEASED",
    "AsyncFleetSession",
    "FleetBatch",
    "FleetBatchSend",
    "FleetCarryOutcome",
    "FleetCarryResult",
    "FleetNotCarriedError",
    "FleetPlayer",
    "FleetPlayerToken",
    "FleetReleased",
    "FleetSendRefused",
    "FleetSession",
]

_r: Any = _native.replication

#: The row type of a carry's result in a batch (the wire's FLEET_CARRY_RESULT, 51).
CARRY_RESULT: Final[int] = _r.FLEET_CARRY_RESULT_ROW
#: The row type of a carry the server ended (the wire's FLEET_RELEASED, 52).
RELEASED: Final[int] = _r.FLEET_RELEASED_ROW
#: The row type of the server ending the session (the wire's COMMAND_SESSION_RELEASED, 28).
SESSION_RELEASED: Final[int] = _r.FLEET_SESSION_RELEASED_ROW
#: The row type of a verified refusal the session could name a player for. The wire never
#: carries 254.
SEND_REFUSED: Final[int] = _r.FLEET_SEND_REFUSED_ROW

_CARRY_TIMED_OUT: Final[int] = _r.FLEET_CARRY_TIMED_OUT

#: How often a waiting carry looks for its result: the deadline of a carry nothing answered
#: is reported by a poll, and no datagram wakes the loop for it.
_CARRY_CHECK_S = 0.05
#: The shortest silence timeout this session keeps. CrowdyCPP holds the capabilities interval
#: to a third of the timeout and gives each capabilities message 1,000 ms to be acknowledged
#: before it counts the silence. At 3,000 ms the two are equal and the timeout never passes
#: (CrowdyCPP 0.94.0 and 0.94.1; it passes from 3,300 ms), so a shorter one that is not 0 is
#: raised to this.
MIN_SILENCE_TIMEOUT_MS: Final[int] = 4_500

#: How often an asyncio session looks at itself when no event wakes it: a move ordered by the
#: realtime server's silence queues nothing, and neither does the deadline of a carry made
#: again after a move.
_HOUSEKEEPING_S = 1.0

#: The handler names :meth:`AsyncFleetSession.set_handlers` accepts.
HANDLER_NAMES: Final = frozenset(
    [
        *(name for name in _HANDLER_KEYS.values()),
        "any",
        "carry_result",
        "released",
        "send_refused",
        "session_released",
        "move_ordered",
        "error",
    ]
)

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_DECIMAL = re.compile(r"-?[0-9]+")


class FleetCarryOutcome(StrEnum):
    """How a carry ended. The first five are FLEET_CARRY_RESULT's status; the rest are this
    session's own."""

    #: The session carries the player; the result names their user id.
    CARRIED = "carried"
    #: No live token by that id: it expired, the sign-in ended, or it is another app's. The
    #: session asked once more, 250 ms later, before it settled on this: a token minted a
    #: moment ago may not have reached the realtime server yet.
    NO_LIVE_TOKEN = "no_live_token"
    #: The token belongs to another organization's player.
    OTHER_ORGANIZATION = "other_organization"
    #: The proof or its time failed: a wrong token, a replay, or this machine's clock is more
    #: than 30 seconds from the server's.
    PROOF_FAILED = "proof_failed"
    #: The session already carries as many players as it may (``max_players``), or the
    #: realtime server has no place for the carry.
    SESSION_FULL = "session_full"
    #: A status this build does not know; ``wire_status`` has the byte.
    REFUSED = "refused"
    #: No answer came within ``carry_timeout_ms`` of the first send.
    TIMED_OUT = "timed_out"
    #: ``release`` was called for the player before the answer came.
    RELEASED = "released"
    #: The session was closed before the answer came.
    CLOSED = "closed"


_OUTCOMES: Final[dict[int, FleetCarryOutcome]] = {
    FleetCarryStatus.CARRIED: FleetCarryOutcome.CARRIED,
    FleetCarryStatus.NO_LIVE_TOKEN: FleetCarryOutcome.NO_LIVE_TOKEN,
    FleetCarryStatus.OTHER_ORGANIZATION: FleetCarryOutcome.OTHER_ORGANIZATION,
    FleetCarryStatus.PROOF_FAILED: FleetCarryOutcome.PROOF_FAILED,
    FleetCarryStatus.SESSION_FULL: FleetCarryOutcome.SESSION_FULL,
    _CARRY_TIMED_OUT: FleetCarryOutcome.TIMED_OUT,
}


class FleetPlayerToken(NamedTuple):
    """A player's app token, as ``mintManagedPlayerToken`` or ``refreshAppToken`` answered it."""

    #: The player's app token: 64 characters. It proves the carry and is not sent.
    token: str
    #: The player's app token's id.
    game_token_id: int | str
    #: After ``refreshAppToken``: the game token id this session carries the same player under
    #: now. With it the player stays carried throughout: sends for the player name the new id
    #: from the moment the FLEET_CARRY goes out, and the old id's late frames are ignored. It
    #: must name the same player.
    replaces: int | str | None = None

    def __repr__(self) -> str:
        return (
            f"FleetPlayerToken(game_token_id={self.game_token_id!r}, "
            f"replaces={self.replaces!r}, token=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class FleetCarryResult:
    """The result of a carry."""

    player_game_token_id: int
    #: The player's user id when carried (what ``ExecConnection.call_as`` takes); ``None``
    #: otherwise.
    user_id: int | None
    status: FleetCarryOutcome
    #: The status byte the server sent; ``None`` when none came.
    wire_status: int | None

    @property
    def carried(self) -> bool:
        return self.status is FleetCarryOutcome.CARRIED


@dataclass(frozen=True, slots=True)
class FleetReleased:
    """The server ended a carry (FLEET_RELEASED)."""

    player_game_token_id: int
    #: The player's user id when this session knew it; ``None`` otherwise.
    user_id: int | None
    #: :class:`crowdypy.wire.FleetReleaseReason`: 1 another session carries the player (they
    #: moved to another of your servers), 2 the player's sign-in ended, 5 no gameplay input
    #: from the player for the app's idle timeout. Treat a value not listed as the carry
    #: having ended.
    reason: int


@dataclass(frozen=True, slots=True)
class FleetSendRefused:
    """A verified refusal of one send for a carried player, when the sequence number it echoes
    can only be that player's: no send for another player used the same number in the last 3
    seconds. The session changes no carry on it."""

    player_game_token_id: int
    #: The player's user id when the session still holds the player; ``None`` otherwise.
    user_id: int | None
    #: The sequence number the send returned.
    sequence: int
    code: ErrorCode | int

    @property
    def player_token_expired(self) -> bool:
        """The player's own token expired: refresh it and carry again with ``replaces``."""
        return self.code == ErrorCode.TOKEN_EXPIRED


@dataclass(frozen=True, slots=True)
class FleetPlayer:
    """One row of :meth:`AsyncFleetSession.players`."""

    game_token_id: int
    #: ``None`` until a carry is confirmed.
    user_id: int | None
    #: The server confirmed a carry and nothing ended it: sends for the player are accepted.
    carried: bool
    #: A FLEET_CARRY is waiting for its result.
    pending: bool


class FleetBatchSend(NamedTuple):
    """What :meth:`AsyncFleetSession.send_actor_updates` did."""

    #: How many messages were accepted.
    accepted: int
    #: The rows that were not sent because the session does not carry their player now.
    not_carried: list[int]


class FleetNotCarriedError(CrowdyReplicationError):
    """A send named a player this session does not carry now: never carried, a carry still
    waiting for its result, or one that ended. Nothing was sent."""

    def __init__(self, player_game_token_id: int) -> None:
        super().__init__(
            f"this fleet session does not carry player {player_game_token_id} now",
            code="NotCarried",
        )
        self.player_game_token_id = player_game_token_id


def _id(value: int | str, what: str) -> int:
    """``value`` as the 64-bit integer the wire carries: an ``int`` (or anything that is an
    index, such as a numpy integer) or a decimal string."""
    if type(value) is int:  # the hot path: every send names its player
        number = value
    elif isinstance(value, str) and _DECIMAL.fullmatch(value.strip()):
        number = int(value)
    elif isinstance(value, (bool, str)):
        raise _bad_id(value, what)
    else:
        try:
            number = operator.index(value)
        except TypeError:
            raise _bad_id(value, what) from None
    if not _INT64_MIN <= number <= _INT64_MAX:
        raise _bad_id(value, what)
    return number


def _bad_id(value: object, what: str) -> CrowdyReplicationError:
    return CrowdyReplicationError(
        f"{what} must be a whole 64-bit number: {value!r}", code="InvalidArgument"
    )


def _send_refused(exc: Exception, player: int) -> CrowdyReplicationError:
    """What a refused send raises: :class:`FleetNotCarriedError` for a player the session does
    not carry now, the native refusal otherwise."""
    error = _replication_error(exc)
    return FleetNotCarriedError(player) if error.code == "NotCarried" else error


def _silence(timeout_ms: int) -> int:
    timeout = int(timeout_ms)
    return 0 if timeout <= 0 else max(MIN_SILENCE_TIMEOUT_MS, timeout)


def _user(user_id: int) -> int | None:
    return user_id or None


class _Grant(NamedTuple):
    token: str
    game_token_id: int
    app_id: int
    expires_at_ms: int
    max_players: int | None
    server: Assignment


def _grant(session: FleetSessionAnswer | Mapping[str, Any]) -> _Grant:
    """What the native session takes from ``openFleetSession``'s or ``refreshFleetSession``'s
    answer: the struct ``client.fleet_sessions`` returns, or the same fields as the API names
    them (``token``, ``gameTokenId``, ``appId``, ``expiresAt``, ``maxPlayers``, ``server``)."""
    if not isinstance(session, FleetSessionAnswer):
        try:
            fields = dict(session)
            for name in ("gameTokenId", "appId"):  # a BigInt read as a number is taken too
                value = fields.get(name)
                if isinstance(value, int) and not isinstance(value, bool):
                    fields[name] = str(value)
            session = msgspec.convert(fields, FleetSessionAnswer)
        except (msgspec.ValidationError, TypeError, ValueError):
            # Never the value: it holds the fleet token.
            raise CrowdyReplicationError(
                "a fleet session is made from openFleetSession's answer: token, gameTokenId, "
                "appId, expiresAt, maxPlayers and server (ip4, ip6, clientPort)",
                code="InvalidArgument",
            ) from None
    if len(session.token) != FLEET_TOKEN_CHARS or not session.token.isascii():
        raise CrowdyReplicationError(
            f"the fleet token must be {FLEET_TOKEN_CHARS} characters", code="InvalidArgument"
        )
    server = session.server
    if not (server.ip4 or server.ip6) or not 0 < server.client_port <= 0xFFFF:
        raise CrowdyReplicationError(
            "the fleet session's server needs an address and a port", code="InvalidArgument"
        )
    expires_at_ms = 0
    if session.expires_at:
        try:
            expires_at_ms = int(datetime.fromisoformat(session.expires_at).timestamp() * 1000)
        except ValueError:
            raise CrowdyReplicationError(
                "the fleet session's expiresAt is not an ISO 8601 time", code="InvalidArgument"
            ) from None
    return _Grant(
        session.token,
        _id(session.game_token_id, "the fleet gameTokenId"),
        _id(session.app_id, "appId"),
        expires_at_ms,
        session.max_players,
        Assignment(server.ip4 or "", server.ip6 or "", server.client_port),
    )


def _player_token(player: Any) -> tuple[str, int, int]:
    """``(token, game_token_id, replaces)`` from a :class:`FleetPlayerToken`, an app token
    response, or a mapping with the API's names or these."""
    if isinstance(player, Mapping):
        token = player.get("token")
        game_token_id = player.get("game_token_id", player.get("gameTokenId"))
        replaces = player.get("replaces")
    else:
        token = getattr(player, "token", None)
        game_token_id = getattr(player, "game_token_id", None)
        replaces = getattr(player, "replaces", None)
    if not isinstance(token, str) or len(token) != FLEET_TOKEN_CHARS:
        raise CrowdyReplicationError(
            f"a player's app token must be {FLEET_TOKEN_CHARS} characters", code="InvalidArgument"
        )
    if game_token_id is None:
        raise CrowdyReplicationError(
            "a carry needs the player's gameTokenId", code="InvalidArgument"
        )
    return (
        token,
        _id(game_token_id, "the player gameTokenId"),
        0 if replaces is None else _id(replaces, "replaces"),
    )


# ------------------------------------------------------------------ batches


class FleetBatch:
    """The events one poll drained, in columns, with the carried players each is for.

    ``notifications`` is the :class:`crowdypy.replication.NotificationBatch` a player's
    connection would give (zero-copy columns). Beside it, for each row: ``listed`` is 1 when
    the notification arrived inside a FLEET_DELIVERY, which lists the only carried players who
    may receive it, and 0 when it arrived plain, once for the session.

    The session's own events are rows too, by type: :data:`CARRY_RESULT`, :data:`RELEASED`,
    :data:`SESSION_RELEASED`, :data:`SEND_REFUSED`, ``MessageType.GENERIC_ERROR`` and
    :data:`crowdypy.replication.STATUS`. Iterating yields ``(notification, players)`` pairs.
    """

    __slots__ = ("_native", "notifications")

    def __init__(self, native: Any) -> None:
        self._native = native
        self.notifications = NotificationBatch(native.notifications)

    def __len__(self) -> int:
        return len(self._native)

    def __bool__(self) -> bool:
        return len(self._native) > 0

    def __iter__(self) -> Iterator[tuple[Notification, tuple[int, ...] | None]]:
        for row, players in self._native.rows():
            yield Notification(*row), players

    def __getitem__(self, index: int) -> tuple[Notification, tuple[int, ...] | None]:
        if index < 0:
            index += len(self)
        return self.notifications[index], self.recipients(index)

    def recipients(self, index: int) -> tuple[int, ...] | None:
        """The carried players row ``index`` is for (their game token ids), or ``None`` when
        the notification is for the session as a whole: every one of your players in range."""
        result: tuple[int, ...] | None = self._native.recipients(index)
        return result

    @property
    def listed(self) -> Any:
        """uint8, one per event: 1 when its players are listed, 0 when it is for the session."""
        return _column(self._native.listed)

    @property
    def recipient_offsets(self) -> Any:
        """uint32, one more than the events: event ``i``'s players are
        ``recipient_ids[recipient_offsets[i]:recipient_offsets[i + 1]]``."""
        return _column(self._native.recipient_offsets)

    @property
    def recipient_ids(self) -> Any:
        """int64 game token ids, back to back."""
        return _column(self._native.recipient_ids)


# ------------------------------------------------------------------ the session


class _FleetCore:
    """Carrying, sends, polling and handler dispatch, shared by both sessions."""

    def __init__(
        self,
        session: FleetSessionAnswer | Mapping[str, Any],
        *,
        handlers: Mapping[str, Callable[..., Any]] | None = None,
        fleet_own_state: bool = False,
        own_media_echo: bool = False,
        carry_retry_ms: int = 250,
        carry_timeout_ms: int = 5_000,
        max_carries_per_second: int = 50,
        capabilities_interval_ms: int = 15_000,
        silence_timeout_ms: int = 45_000,
        logger: logging.Logger | None = None,
        **transport: Any,
    ) -> None:
        self._logger = logger or logging.getLogger("crowdypy.fleet")
        grant = _grant(session)
        # The realtime servers let a session send its max_players + 32 carries at once, so one
        # that moves to another realtime server can carry all its players again without
        # waiting on the rate.
        transport.setdefault("carry_burst", max(32, grant.max_players or 0))
        try:
            self._native: Any = _r.FleetSession(
                grant.app_id,
                grant.token,
                grant.game_token_id,
                grant.expires_at_ms,
                grant.server.ip4,
                grant.server.ip6,
                grant.server.client_port,
                fleet_own_state=bool(fleet_own_state),
                own_media_echo=bool(own_media_echo),
                carry_resend_ms=max(1, int(carry_retry_ms)),
                carry_timeout_ms=max(1, int(carry_timeout_ms)),
                # The realtime servers drop a session's carries past 64 a second unanswered.
                carries_per_second=max(1, min(63, int(max_carries_per_second))),
                advertise_interval_ms=max(1_000, int(capabilities_interval_ms)),
                silence_timeout_ms=_silence(silence_timeout_ms),
                **transport,
            )
        except (ValueError, TypeError) as exc:
            raise _replication_error(exc) from None
        self._app_id = grant.app_id
        self._game_token_id = grant.game_token_id
        self._max_players = grant.max_players
        self._handlers: dict[str, Callable[..., Any]] = {}
        self.set_handlers(handlers or {})
        self._manual_pump = bool(transport.get("manual_pump", False))
        self._closed = False
        self._move_seen = False
        self._carry_waiters: dict[int, list[Callable[[FleetCarryResult], None]]] = {}
        self._waiters_lock = threading.Lock()
        self._wake_r: socket.socket | None = None
        self._wake_w: socket.socket | None = None

    def __repr__(self) -> str:
        # Never a token: what a log line shows of a session.
        return (
            f"{type(self).__name__}(app_id={self._app_id}, game_token_id={self._game_token_id}, "
            f"state={self.state.name}, server={self.current_server!r}, "
            f"carried={self.carried_count})"
        )

    # ----- what the session is

    @property
    def app_id(self) -> int:
        """The app the session is in."""
        return self._app_id

    @property
    def game_token_id(self) -> int:
        """The session's own game token id: what ``refreshFleetSession`` and
        ``closeFleetSession`` name it by."""
        return self._game_token_id

    @property
    def max_players(self) -> int | None:
        """How many players the session may carry at once, when the answer said."""
        return self._max_players

    @property
    def expires_at(self) -> int | None:
        """When the session ends unless it is refreshed first, in epoch milliseconds; ``None``
        when unknown."""
        _, expires_at_ms = self._native.token_info()
        return expires_at_ms or None

    @property
    def current_server(self) -> Assignment:
        """The realtime server the session is on, or will connect to."""
        return Assignment(*self._native.server())

    @property
    def state(self) -> ConnState:
        """The connection's state. ``INACTIVE`` is the server's idle release of the session
        itself, which nothing reconnects: call ``connect()`` again."""
        return ConnState(self._native.state)

    @property
    def native(self) -> Any:
        """The ``crowdypy._native.replication.FleetSession`` underneath, for benchmarks and
        tools."""
        return self._native

    def set_handlers(self, handlers: Mapping[str, Callable[..., Any]]) -> None:
        """Replace the handlers. Every one is optional, and one that raises is reported to
        ``error`` (or logged) and stops nothing.

        A notification's handler is called with ``(notification, players)``: ``actor_update``,
        ``voxel_update``, ``actor_left``, ``chunk_changed``, ``client_event``,
        ``server_event``, ``generic_spatial``, ``audio``, ``video``, ``text``,
        ``single_actor_message``, ``channel_message``, ``channel_audio``, and ``any`` (each of
        those, before its own handler). ``players`` is a tuple of game token ids when the
        notification arrived inside a FLEET_DELIVERY: deliver it to these players and no
        others (every communication arrives this way). It is ``None`` when the notification
        arrived plain, once for the session: state, for every one of your players in range.

        The session's own:

        - ``generic_error(notification, verified)``: the server refused one send, by its
          sequence number. ``verified`` is true for a refusal that arrived inside a bundle the
          session verified with the fleet token, which is how realtime servers from v0.50.3
          send them. A refusal that arrived alone is 3 bytes with no room for a signature:
          treat it as a hint. The session changes nothing on a refusal.
        - ``send_refused(refused)``: a verified refusal the session could name a player for
          (:class:`FleetSendRefused`), after ``generic_error`` for the same frame.
        - ``carry_result(result)``: every carry the server answered or that timed out,
          including the carries made again after the session moved.
        - ``released(released)``: the server ended a carry (:class:`FleetReleased`).
        - ``move_ordered()``: the session needs another realtime server; ``refresh_due()`` is
          true.
        - ``session_released(reason)``: the realtime server released the session, in a frame
          signed with the fleet token (:class:`crowdypy.wire.SessionReleaseReason`).
        - ``status(state)``: the connection's :class:`crowdypy.replication.ConnState`.
        - ``error(exception)``: a handler raised.
        """
        unknown = set(handlers) - HANDLER_NAMES
        if unknown:
            raise ValueError(f"unknown handler name(s): {', '.join(sorted(unknown))}")
        self._handlers = dict(handlers)

    def stats(self) -> dict[str, Any]:
        """CrowdyCPP's cumulative counters for the session's socket, and the session's own:
        ``hmac_failures``, ``malformed``, ``unsigned_notifications`` and ``wrong_app`` (frames
        dropped before any handler), ``no_live_token_retries``, ``replaced_id_frames``,
        ``session_releases``, ``send_refusals_named``, and messages by type
        (``messages_sent_by_type[49]`` and ``[50]`` count carries and releases)."""
        result: dict[str, Any] = self._native.stats()
        return result

    # ----- keeping the session

    @property
    def move_ordered(self) -> bool:
        """True when the session needs a server from you: the realtime server told it to
        connect elsewhere, released it, or said nothing that verifies for
        ``silence_timeout_ms``. :meth:`apply_refresh` clears it."""
        return bool(self._native.move_ordered)

    def refresh_due(self) -> bool:
        """True when it is time to call ``refreshFleetSession``: a move was ordered
        (:attr:`move_ordered`), or less than half of the session's lifetime is left (12 hours
        into a 24-hour session, counted from when this object was given the token). The
        session makes no call itself: ask this from your own loop."""
        return not self._closed and bool(self._native.refresh_due())

    def refresh_input(self) -> dict[str, Any]:
        """The input of ``refreshFleetSession`` for this session
        (``client.fleet_sessions.refresh``). It names the realtime server the session is on,
        so the session stays there while that server still serves the app. After a move was
        ordered it names none, and the answer's server is the next one.

        ``currentServer`` is an IPv4 address and a port, which is all the Application API
        takes. A session that was given no IPv4 address sends no ``currentServer``, so each
        refresh may answer another server."""
        refresh: dict[str, Any] = {
            "appId": str(self._app_id),
            "gameTokenId": str(self._game_token_id),
        }
        server = self.current_server
        if not self.move_ordered and server.ip4:
            refresh["currentServer"] = {"ip4": server.ip4, "clientPort": server.client_port}
        return refresh

    def apply_refresh(self, answer: FleetSessionAnswer | Mapping[str, Any]) -> None:
        """Take the answer of ``refreshFleetSession``: the new expiry and the realtime server.

        - **The same server:** the carries stand. The session sends its capabilities again
          and no FLEET_CARRY.
        - **Another server**, or the same one after it released the session or went quiet:
          the server holds no carry. The session connects there, sends its capabilities, then
          a new FLEET_CARRY (a new time and proof) for every player, paced under 64 a second.
          Until each is answered, sends for that player raise :class:`FleetNotCarriedError`;
          each answer reaches the ``carry_result`` handler.

        Raises with nothing changed for an answer that is another session's (a different app
        or game token id) or carries no usable token or server."""
        if self._closed:
            raise CrowdyRealtimeError("the fleet session is closed", code="FLEET_CLOSED")
        grant = _grant(answer)
        if grant.app_id != self._app_id or grant.game_token_id != self._game_token_id:
            raise CrowdyReplicationError(
                "the answer is another session's: a refresh keeps the app and the game token id",
                code="InvalidArgument",
            )
        try:
            self._native.apply_refresh(
                grant.token,
                grant.game_token_id,
                grant.expires_at_ms,
                grant.server.ip4,
                grant.server.ip6,
                grant.server.client_port,
            )
        except ValueError as exc:
            raise _replication_error(exc) from None
        if grant.max_players is not None:
            self._max_players = grant.max_players
        self._move_seen = False

    def set_fleet_own_state(self, on: bool) -> None:
        """Start or stop asking for the state this session's own carried players sent, when
        another of its carried players is in range. Off by default: a game server simulates
        its own players. A voxel edit reaches the session that sent it either way."""
        self._native.set_fleet_own_state(bool(on))

    def set_own_media_echo(self, on: bool) -> None:
        """Start or stop asking for a carried speaker's own spatial voice and video inside the
        deliveries."""
        self._native.set_own_media_echo(bool(on))

    # ----- carrying

    def _start_carry(self, player: Any, waiter: Callable[[FleetCarryResult], None]) -> int:
        token, game_token_id, replaces = _player_token(player)
        if self._closed:
            raise CrowdyRealtimeError("the fleet session is closed", code="FLEET_CLOSED")
        with self._waiters_lock:
            self._carry_waiters.setdefault(game_token_id, []).append(waiter)
        try:
            self._native.carry(token, game_token_id, replaces)
        except ValueError as exc:
            self._drop_waiter(game_token_id, waiter)
            raise _replication_error(exc) from None
        return game_token_id

    def _drop_waiter(self, game_token_id: int, waiter: Callable[[FleetCarryResult], None]) -> None:
        with self._waiters_lock:
            waiting = self._carry_waiters.get(game_token_id)
            if waiting is None:
                return
            with contextlib.suppress(ValueError):
                waiting.remove(waiter)
            if not waiting:
                del self._carry_waiters[game_token_id]

    def _settle_carry(self, result: FleetCarryResult) -> None:
        with self._waiters_lock:
            waiters = self._carry_waiters.pop(result.player_game_token_id, ())
        for waiter in waiters:
            waiter(result)

    def carry_nowait(self, player: Any) -> int:
        """Start carrying a player without waiting for the result, which reaches the
        ``carry_result`` handler from a later poll: for a game loop that must not block.
        ``player`` is what ``carry`` takes. Returns the player's game token id."""
        return self._start_carry(player, _ignore)

    def release(self, player_game_token_id: int | str) -> bool:
        """The player left the game: send FLEET_RELEASE and take them out of the table at
        once. A carry still waiting for the player ends ``released``. The message has no
        answer; if it is lost, the carry stays on the platform until another session carries
        the player, their sign-in ends, or they idle out. Returns whether the message left
        (false while the session is not connected; the player is out of the table either
        way)."""
        player = _id(player_game_token_id, "the player gameTokenId")
        try:
            left = bool(self._native.release(player))
        except ValueError as exc:
            raise _replication_error(exc) from None
        self._settle_carry(FleetCarryResult(player, None, FleetCarryOutcome.RELEASED, None))
        return left

    def carries(self, player_game_token_id: int | str) -> bool:
        """True when the server confirmed a carry of this game token id and nothing ended it.
        While a refreshed token's carry waits, true for both ids."""
        return bool(self._native.carries(_id(player_game_token_id, "the player gameTokenId")))

    def players(self) -> list[FleetPlayer]:
        """Every row of the table: carried players and carries still waiting."""
        return [
            FleetPlayer(game_token_id, _user(user_id), carried, pending)
            for game_token_id, user_id, carried, pending in self._native.players()
        ]

    @property
    def carried_count(self) -> int:
        """How many players the server confirmed and nothing ended."""
        return int(self._native.carried_count)

    # ----- sends for a carried player: each returns the message's sequence number (0-255)

    def _send(self, send: Callable[..., Any], player: int | str, *args: Any) -> Any:
        player_id = _id(player, "the player gameTokenId")
        try:
            return send(player_id, *args)
        except ValueError as exc:
            raise _send_refused(exc, player_id) from None

    def _spatial(
        self,
        kind: int,
        player: int | str,
        chunk: Sequence[int],
        uuid: Any,
        payload: Any,
        distance: int,
        decay: int,
    ) -> int:
        # The per-entity path of a server that does not batch: an int in range goes straight
        # to the native send.
        if type(player) is not int or not _INT64_MIN <= player <= _INT64_MAX:
            player = _id(player, "the player gameTokenId")
        try:
            result: int = self._native.send_spatial(
                player, kind, chunk[0], chunk[1], chunk[2], uuid, payload, distance, decay
            )
        except ValueError as exc:
            raise _send_refused(exc, player) from None
        return result

    def send_actor_update(
        self,
        player: int | str,
        chunk: Sequence[int],
        uuid: str | bytes,
        state: Any = b"",
        *,
        distance: int = 8,
        decay: int = DecayRate.EXPONENTIAL,
    ) -> int:
        """An actor update for a carried player: the message that player's own session would
        send, with the player's game token id in its tail and the HMAC keyed with the fleet
        token. The player's permissions, blocks, age rules, rate budget and idle clock apply
        to it. Raises :class:`FleetNotCarriedError`, with nothing sent, for a player the
        session does not carry now."""
        return self._spatial(
            MessageType.ACTOR_UPDATE_REQUEST, player, chunk, uuid, state, distance, decay
        )

    def send_audio_packet(
        self,
        player: int | str,
        chunk: Sequence[int],
        uuid: str | bytes,
        audio: Any,
        *,
        distance: int = 1,
        decay: int = DecayRate.NONE,
    ) -> int:
        return self._spatial(
            MessageType.CLIENT_AUDIO_PACKET, player, chunk, uuid, audio, distance, decay
        )

    def send_video_packet(
        self,
        player: int | str,
        chunk: Sequence[int],
        uuid: str | bytes,
        fragment: Any,
        *,
        distance: int = 1,
        decay: int = DecayRate.NONE,
    ) -> int:
        """One video fragment (header + slice, see :func:`crowdypy.media.fragment_frame`)."""
        return self._spatial(
            MessageType.CLIENT_VIDEO_PACKET, player, chunk, uuid, fragment, distance, decay
        )

    def send_text_packet(
        self,
        player: int | str,
        chunk: Sequence[int],
        uuid: str | bytes,
        text: str | bytes,
        *,
        distance: int = 8,
        decay: int = DecayRate.NONE,
    ) -> int:
        """Proximity text from a carried player. Without the player's ``use_text_chat`` for the
        app the server answers ``UNAUTHORIZED`` for the returned sequence."""
        body = text.encode("utf-8") if isinstance(text, str) else text
        return self._spatial(
            MessageType.CLIENT_TEXT_PACKET, player, chunk, uuid, body, distance, decay
        )

    def send_generic_spatial(
        self,
        player: int | str,
        chunk: Sequence[int],
        uuid: str | bytes,
        payload: Any,
        *,
        distance: int = 8,
        decay: int = DecayRate.NONE,
    ) -> int:
        return self._spatial(
            MessageType.GENERIC_SPATIAL_1, player, chunk, uuid, payload, distance, decay
        )

    def send_voxel_update(
        self,
        player: int | str,
        chunk: Sequence[int],
        uuid: str | bytes,
        voxel: Sequence[int],
        voxel_type: int,
        voxel_state: Any = b"",
        *,
        distance: int = 8,
        decay: int = DecayRate.NONE,
    ) -> int:
        """One voxel edit by a carried player. The position's coordinates and the type are the
        app's signed 16-bit values, and the state is at most
        :data:`crowdypy.wire.VOXEL_STATE_MAX_BYTES`. An accepted edit reaches the session that
        sent it as an ordinary ``voxel_update``."""
        size = memoryview(voxel_state).nbytes
        if size > VOXEL_STATE_MAX_BYTES:
            raise CrowdyReplicationError(
                f"the voxel state is {size} bytes; at most {VOXEL_STATE_MAX_BYTES}",
                code="InvalidArgument",
            )
        result: int = self._send(
            self._native.send_voxel_update,
            player, chunk[0], chunk[1], chunk[2], uuid, voxel[0], voxel[1], voxel[2],
            voxel_type, voxel_state, distance, decay,
        )  # fmt: skip
        return result

    def send_client_event(
        self,
        player: int | str,
        chunk: Sequence[int],
        uuid: str | bytes,
        event_type: int,
        state: Any = b"",
        *,
        distance: int = 8,
        decay: int = DecayRate.NONE,
    ) -> int:
        result: int = self._send(
            self._native.send_client_event,
            player, chunk[0], chunk[1], chunk[2], uuid, event_type, state, distance, decay,
        )  # fmt: skip
        return result

    def send_single_actor_message(
        self, player: int | str, chunk: Sequence[int], uuid: str | bytes, payload: Any
    ) -> int:
        """A direct message from a carried player to one actor: ``chunk`` and ``uuid`` address
        the TARGET."""
        result: int = self._send(
            self._native.send_single_actor_message,
            player, chunk[0], chunk[1], chunk[2], uuid, payload,
        )  # fmt: skip
        return result

    def send_channel_message(
        self, player: int | str, channel_id: int | str, uuid: str | bytes, payload: Any
    ) -> int:
        result: int = self._send(
            self._native.send_channel_message, player, int(channel_id), uuid, payload
        )
        return result

    def send_channel_audio(
        self, player: int | str, channel_id: int | str, uuid: str | bytes, payload: Any
    ) -> int:
        result: int = self._send(
            self._native.send_channel_audio, player, int(channel_id), uuid, payload
        )
        return result

    def send_ranged_channel_message(
        self,
        player: int | str,
        channel_id: int | str,
        uuid: str | bytes,
        payload: Any,
        chunk: Sequence[int],
        max_distance: int,
    ) -> int:
        """A channel publish by a carried player that reaches only the members whose own actor
        is within ``max_distance`` chunks of ``chunk``."""
        if not 0 <= int(max_distance) <= CHANNEL_RANGED_MAX_DISTANCE:
            raise CrowdyReplicationError(
                f"max_distance must be 0..{CHANNEL_RANGED_MAX_DISTANCE}", code="InvalidArgument"
            )
        result: int = self._send(
            self._native.send_ranged_channel_message,
            player, int(channel_id), uuid, payload, chunk[0], chunk[1], chunk[2],
            int(max_distance),
        )  # fmt: skip
        return result

    def send_heartbeat(self, player: int | str, chunk: Sequence[int], uuid: str | bytes) -> int:
        """Idle keep-alive for one of a carried player's actors. Send it every 2 seconds or so
        while the actor is idle, so its presence never lapses: the session sends none by
        itself. A heartbeat is not gameplay input and does not move the player's idle clock."""
        result: int = self._send(
            self._native.send_heartbeat, player, chunk[0], chunk[1], chunk[2], uuid
        )
        return result

    def send_video_frame(
        self,
        player: int | str,
        chunk: Sequence[int],
        uuid: str | bytes,
        frame: Any,
        frame_id: int,
        *,
        codec: int = 0,
        distance: int = 1,
        decay: int = DecayRate.NONE,
    ) -> int:
        """Fragment one encoded frame (JPEG 0, WebP 1) and send every fragment for a carried
        player; returns how many."""
        result: int = self._send(
            self._native.send_video_frame,
            player, chunk[0], chunk[1], chunk[2], uuid, frame, frame_id, codec, distance, decay,
        )  # fmt: skip
        return result

    def send_actor_updates(
        self,
        players: Any,
        chunks: Any,
        uuids: Any,
        states: Any,
        *,
        offsets: Any = None,
        stride: int = 0,
        distance: int = 8,
        decay: int = DecayRate.EXPONENTIAL,
        flush: bool = True,
    ) -> FleetBatchSend:
        """Send one actor update for each of many carried players in one call, releasing the
        GIL once: a game server's tick.

        ``players``: int64 game token ids (a numpy array, ``array('q')`` or bytes) or a list.
        ``chunks``: int64 ``(n, 3)`` or a list of triples. ``uuids``: ``n x 32`` octets or a
        list. ``states``: a list of bodies, or the bodies back to back with int64 ``offsets``
        (``n + 1``) or a fixed ``stride``. A row whose player the session does not carry now
        is skipped and named in the result; any other refusal stops the batch and raises."""
        if isinstance(players, (list, tuple)):
            import array

            players = array.array(
                "q", [_id(player, "the player gameTokenId") for player in players]
            )
        data, list_offsets = _payload_rows(states)
        try:
            accepted, skipped = self._native.send_spatial_batch(
                int(MessageType.ACTOR_UPDATE_REQUEST), players, _chunk_rows(chunks),
                _uuid_rows(uuids), data, list_offsets if list_offsets is not None else offsets,
                stride, distance, decay, flush,
            )  # fmt: skip
        except ValueError as exc:
            raise _replication_error(exc) from None
        return FleetBatchSend(int(accepted), list(skipped))

    def set_speakers(
        self,
        player: int | str,
        *,
        voice: Sequence[str] | KeepSpeakers | None = KEEP_SPEAKERS,
        video: Sequence[str] | KeepSpeakers | None = KEEP_SPEAKERS,
    ) -> None:
        """The speakers whose spatial voice and video a carried player receives. Per kind: a
        list of 32-character actor uuids receives only those (none when empty), ``None``
        everyone in range again, and a kind left out stays as it is. Each carried player has
        their own lists; the session sends them again with each capabilities advertisement
        and after the player is carried again."""
        voice_mode, voice_names = _speaker_change(voice)
        video_mode, video_names = _speaker_change(video)
        self._send(
            self._native.set_speakers, player, voice_mode, voice_names, video_mode, video_names
        )

    def send_session_heartbeat(self) -> int:
        """A keep-alive of the session itself: no actor, under the session's own game token
        id. The capabilities already go out every ``capabilities_interval_ms``; this is for a
        server that wants one sooner."""
        try:
            result: int = self._native.send_session_heartbeat()
        except ValueError as exc:
            raise _replication_error(exc) from None
        return result

    def flush_sends(self) -> None:
        """Put the pending bundle on the wire now (the end of a tick) instead of at the end of
        the bundle window."""
        try:
            self._native.flush_sends()
        except ValueError as exc:
            raise _replication_error(exc) from None

    # ----- receive

    def pump(self, timeout_ms: int = 0) -> int:
        """Manual-pump mode: do network work for up to ``timeout_ms``; returns datagrams read."""
        result: int = self._native.pump(timeout_ms)
        return result

    def _drain(self, max_events: int | None) -> FleetBatch:
        native = self._native.poll() if max_events is None else self._native.poll(max_events)
        for level, line in self._native.drain_logs():
            self._logger.log(_LOG_LEVELS.get(level, logging.INFO), "%s", line)
        batch = FleetBatch(native)
        if native:
            self._dispatch(batch)
        ordered = self.move_ordered
        if ordered and not self._move_seen:
            self._call("move_ordered")
        self._move_seen = ordered
        return batch

    def _dispatch(self, batch: FleetBatch) -> None:
        for event, players in batch:
            kind = event.type
            if kind == CARRY_RESULT:
                player, user_id, status, wire_status = event.extras
                outcome = _OUTCOMES.get(status, FleetCarryOutcome.REFUSED)
                result = FleetCarryResult(
                    player,
                    _user(user_id),
                    outcome,
                    None if outcome is FleetCarryOutcome.TIMED_OUT else wire_status,
                )
                self._settle_carry(result)
                self._call("carry_result", result)
            elif kind == RELEASED:
                player, user_id, _, wire_reason = event.extras
                self._call("released", FleetReleased(player, _user(user_id), wire_reason))
            elif kind == SESSION_RELEASED:
                self._call("session_released", event.extras[1])
            elif kind == SEND_REFUSED:
                player, user_id, code, _ = event.extras
                try:
                    named: ErrorCode | int = ErrorCode(code)
                except ValueError:
                    named = code
                self._call(
                    "send_refused",
                    FleetSendRefused(player, _user(user_id), event.sequence, named),
                )
            elif kind == MessageType.GENERIC_ERROR:
                self._call("generic_error", event, bool(event.extras[1]))
            elif kind == STATUS:
                self._call("status", event.state)
            else:
                self._call("any", event, players)
                key = _HANDLER_KEYS.get(kind)
                if key is not None:
                    self._call(key, event, players)

    def _call(self, name: str, *args: Any) -> None:
        handler = self._handlers.get(name)
        if handler is None:
            return
        try:
            handler(*args)
        except Exception as exc:
            on_error = self._handlers.get("error")
            if on_error is None or name == "error":
                self._logger.exception("fleet session handler %r failed", name)
                return
            try:
                on_error(exc)
            except Exception:
                self._logger.exception("fleet session error handler failed")

    def _end(self) -> None:
        """The session is closed: every carry still waiting ends ``closed``."""
        self._closed = True
        with self._waiters_lock:
            waiting, self._carry_waiters = self._carry_waiters, {}
        for player, waiters in waiting.items():
            result = FleetCarryResult(player, None, FleetCarryOutcome.CLOSED, None)
            for waiter in waiters:
                waiter(result)

    def _open_wake(self) -> None:
        if self._wake_r is None:
            self._wake_r, self._wake_w = socket.socketpair()
            self._wake_r.setblocking(False)
            self._wake_w.setblocking(False)
            self._native.set_wake_fd(self._wake_w.fileno())

    def _close_wake(self) -> None:
        self._native.set_wake_fd(-1)
        for sock in (self._wake_r, self._wake_w):
            if sock is not None:
                sock.close()
        self._wake_r = self._wake_w = None

    def _clear_wake(self) -> None:
        if self._wake_r is None:
            return
        with contextlib.suppress(BlockingIOError, InterruptedError):
            while self._wake_r.recv(4096):
                pass


class AsyncFleetSession(_FleetCore):
    """A fleet session driven by an asyncio loop.

    ``session`` is the answer of ``client.fleet_sessions.open(app_id)``, or the same fields
    from your own HTTP client as the Application API names them. After :meth:`connect`, a task
    on the loop wakes when events are waiting, drains them, calls the handlers, settles
    waiting carries and hands each batch to :meth:`batches` consumers.

    Options: ``handlers`` (:meth:`set_handlers`), ``fleet_own_state``, ``own_media_echo``,
    ``carry_retry_ms`` (250), ``carry_timeout_ms`` (5,000), ``max_carries_per_second`` (50, at
    most 63), ``capabilities_interval_ms`` (15,000, and never more than a third of the silence
    timeout), ``silence_timeout_ms`` (45,000; 0 switches it off, and a shorter one is raised to
    :data:`MIN_SILENCE_TIMEOUT_MS`, 4,500),
    ``logger``, and CrowdyCPP's transport options (``manual_pump``, ``prefer_ipv6``,
    ``session_ready_wait_ms``, ``ring_capacity``, ``socket_recv_buffer_bytes``,
    ``socket_send_buffer_bytes``, ``bundle_sends``, ``bundle_window_ms``, ``carry_burst``).
    """

    def __init__(self, session: FleetSessionAnswer | Mapping[str, Any], **options: Any) -> None:
        super().__init__(session, **options)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: asyncio.Task[None] | None = None
        self._consumers: list[asyncio.Queue[FleetBatch | None]] = []

    async def connect(self) -> None:
        """Open the socket to the realtime server and start reading. The session's
        capabilities are its first message, under its own game token id; carries asked for
        before they leave wait and are sent in order."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._closed = False
        self._open_wake()
        try:
            await asyncio.to_thread(self._native.open)
        except ValueError as exc:
            raise _replication_error(exc) from None
        if self._reader is None or self._reader.done():
            self._reader = loop.create_task(self._read(), name="crowdypy-fleet-reader")

    async def close(self) -> None:
        """Close the socket and stop the network thread. The table is emptied and a carry
        still waiting ends ``closed``. This does not end the session on the platform: that is
        ``client.fleet_sessions.close``."""
        await asyncio.to_thread(self._native.close)
        self._end()
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        self._close_wake()
        for queue in self._consumers:
            queue.put_nowait(None)

    async def __aenter__(self) -> AsyncFleetSession:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def carry(self, player: Any) -> FleetCarryResult:
        """Start carrying a player, and wait for how the carry ended.

        ``player`` is the app token your server minted for them: a :class:`FleetPlayerToken`,
        the response of ``mintManagedPlayerToken`` or ``refreshAppToken`` (its ``token`` and
        ``gameTokenId``), or a mapping with those names. The session sends FLEET_CARRY, which
        names the player's game token id and carries a proof that your server holds the
        player's token, and sends it again every ``carry_retry_ms`` with a new time and proof
        until the result arrives or ``carry_timeout_ms`` passes.

        After ``refreshAppToken`` for a carried player, pass ``replaces``: the game token id
        the player is carried under now. The player stays carried throughout, and every send
        for them names the new id from the moment the new FLEET_CARRY leaves."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[FleetCarryResult] = loop.create_future()

        def settle(result: FleetCarryResult) -> None:
            if not future.done():
                future.set_result(result)

        self._start_carry(player, settle)
        while not future.done():
            done, _ = await asyncio.wait({future}, timeout=_CARRY_CHECK_S)
            if not done:
                self.poll()
        return future.result()

    def poll(self, max_events: int | None = None) -> FleetBatch:
        """Drain now, on this thread (the reader task does this by itself after connect)."""
        batch = self._drain(max_events)
        if batch:
            for queue in self._consumers:
                queue.put_nowait(batch)
        return batch

    async def batches(self) -> AsyncIterator[FleetBatch]:
        """Every non-empty batch from now until :meth:`close`."""
        queue: asyncio.Queue[FleetBatch | None] = asyncio.Queue()
        self._consumers.append(queue)
        try:
            while True:
                batch = await queue.get()
                if batch is None:
                    return
                yield batch
        finally:
            with contextlib.suppress(ValueError):
                self._consumers.remove(queue)

    async def _read(self) -> None:
        loop = asyncio.get_running_loop()
        wake = self._wake_r
        assert wake is not None
        # One read of the wake socket stays pending across the housekeeping passes: it is
        # never cancelled for a timeout, so no wake is lost to a cancelled read.
        waiting: asyncio.Future[bytes] | None = None
        try:
            while True:
                if waiting is None:
                    waiting = asyncio.ensure_future(loop.sock_recv(wake, 4096))
                done, _ = await asyncio.wait({waiting}, timeout=_HOUSEKEEPING_S)
                if done:
                    try:
                        waiting.result()
                    except (OSError, ValueError):
                        return
                    waiting = None
                    self._clear_wake()
                # With nothing read, this pass looks at the move order and the carry deadlines.
                self.poll()
        finally:
            if waiting is not None:
                waiting.cancel()


class FleetSession(_FleetCore):
    """A fleet session for code without an event loop: :meth:`poll` from your game loop every
    tick, or :meth:`wait` for events first. Handlers run on the thread that polls. The options
    are :class:`AsyncFleetSession`'s."""

    def __init__(self, session: FleetSessionAnswer | Mapping[str, Any], **options: Any) -> None:
        super().__init__(session, **options)
        self._backlog: list[FleetBatch] = []

    def connect(self) -> None:
        """Open the socket to the realtime server; see :meth:`AsyncFleetSession.connect`."""
        self._closed = False
        self._open_wake()
        try:
            self._native.open()
        except ValueError as exc:
            raise _replication_error(exc) from None

    def close(self) -> None:
        """Close the socket and stop the network thread; see :meth:`AsyncFleetSession.close`."""
        self._native.close()
        self._end()
        self._close_wake()

    def __enter__(self) -> FleetSession:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def poll(self, max_events: int | None = None) -> FleetBatch:
        """Drain waiting events, calling the handlers on this thread. Call it every tick: it
        also reports the carries nothing answered, and a move the realtime server's silence
        ordered."""
        if self._backlog:
            return self._backlog.pop(0)
        self._clear_wake()
        return self._drain(max_events)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until events are waiting (or ``timeout`` seconds pass); True when they are."""
        if self._backlog:
            return True
        if self._wake_r is None:
            return False
        readable, _, _ = select.select([self._wake_r], [], [], timeout)
        return bool(readable)

    def carry(self, player: Any) -> FleetCarryResult:
        """Start carrying a player and block until the carry ends; see
        :meth:`AsyncFleetSession.carry`. A carry ends within ``carry_timeout_ms`` of its first
        send, which waits for the session to be connected. The batches read while waiting are
        kept for the next :meth:`poll` (their handlers have run). A game loop that must not
        block uses :meth:`carry_nowait`."""
        results: list[FleetCarryResult] = []
        self._start_carry(player, results.append)
        while not results:
            if self._manual_pump:
                self.pump(int(_CARRY_CHECK_S * 1000))
            else:
                self.wait(_CARRY_CHECK_S)
            self._clear_wake()
            batch = self._drain(None)
            if batch:
                self._backlog.append(batch)
        return results[0]


def _ignore(result: FleetCarryResult) -> None:
    del result
