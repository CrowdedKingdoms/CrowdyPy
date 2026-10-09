"""Native UDP replication: CrowdyCPP's connection, driven from Python.

No datagram is handled in Python. CrowdyCPP's network thread receives, verifies each HMAC and
queues events; Python takes them in batches (:class:`NotificationBatch`, whose columns are
views, not copies), sends with the GIL released, and an asyncio loop is woken through a socket
when events are waiting, never by a sleep-poll. Server assignment and token refresh are
GraphQL calls the native side asks Python to make: a small thread serves those requests, so
the network thread waits on a native condition and never on the interpreter.

Most games use ``client.udp`` (:class:`crowdypy.domains.udp.UdpAPI`), which builds one of
these connections for the client's app; build one directly to supply your own
:class:`SessionProvider`.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import logging
import select
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Any, Final, NamedTuple, Protocol

from crowdypy import _native
from crowdypy.errors import CrowdyRealtimeError, CrowdyReplicationError
from crowdypy.wire import CHANNEL_RANGED_MAX_DISTANCE, DecayRate, ErrorCode, MessageType

__all__ = [
    "STATUS",
    "Assignment",
    "AsyncReplicationConnection",
    "ConnState",
    "Notification",
    "NotificationBatch",
    "ReplicationConnection",
    "SessionProvider",
    "TokenMaterial",
    "token_material",
]

_r: Any = _native.replication

#: The row type of a connection-state change in a batch. The wire never carries 255.
STATUS: Final[int] = _r.STATUS_ROW

_LOG_LEVELS = {
    0: logging.DEBUG,
    1: logging.DEBUG,
    2: logging.INFO,
    3: logging.WARNING,
    4: logging.ERROR,
}


class ConnState(IntEnum):
    IDLE = 0
    CONNECTING = 1
    CONNECTED = 2
    RECONNECTING = 3
    FAILED = 4
    CLOSED = 5


class Assignment(NamedTuple):
    """A replication server, from the Game API's ``serverWithLeastClients``."""

    ip4: str
    ip6: str
    client_port: int


class TokenMaterial(NamedTuple):
    """What native UDP needs from an app token: the 64-character HMAC key, the token id every
    message carries, its expiry (epoch ms, 0 = never), and whether a refresh installed it on
    the server the connection already uses (then the socket is kept)."""

    token: str
    game_token_id: int
    expires_at_ms: int = 0
    authorized_on_current_server: bool = False


class _AppToken(Protocol):
    """What :func:`token_material` reads: an ``AppTokenResponse`` from either client."""

    @property
    def token(self) -> str: ...
    @property
    def game_token_id(self) -> str: ...
    @property
    def expires_at(self) -> str: ...
    @property
    def authorized_server(self) -> Any: ...


def token_material(token: _AppToken, current: Assignment | None = None) -> TokenMaterial:
    """The native token material of a minted or refreshed app token."""
    try:
        game_token_id = int(token.game_token_id)
    except ValueError:
        raise CrowdyReplicationError(
            "the app token's gameTokenId is not an integer", code="InvalidArgument"
        ) from None
    if not -(2**63) <= game_token_id < 2**63:
        raise CrowdyReplicationError(
            "the app token's gameTokenId is outside the wire's int64 range", code="InvalidArgument"
        )
    expires_at_ms = 0
    if token.expires_at:
        expires_at_ms = int(datetime.fromisoformat(token.expires_at).timestamp() * 1000)
    server = token.authorized_server
    authorized = (
        current is not None
        and server is not None
        and server.ip4 == current.ip4
        and server.client_port == current.client_port
    )
    return TokenMaterial(token.token, game_token_id, expires_at_ms, authorized)


class SessionProvider(Protocol):
    """What a connection asks of the Game API. Methods may be coroutines (an async client) or
    plain functions (a blocking one); either way they run off the network thread."""

    def assign_server(self) -> Any: ...

    def refresh_token(self, current: Assignment | None) -> Any: ...


# ------------------------------------------------------------------ events


_HANDLER_KEYS: dict[int, str] = {
    MessageType.ACTOR_UPDATE_NOTIFICATION: "actor_update",
    MessageType.VOXEL_UPDATE_NOTIFICATION: "voxel_update",
    MessageType.CLIENT_AUDIO_NOTIFICATION: "audio",
    MessageType.CLIENT_VIDEO_NOTIFICATION: "video",
    MessageType.ACTOR_LEFT_NOTIFICATION: "actor_left",
    MessageType.CLIENT_TEXT_NOTIFICATION: "text",
    MessageType.CLIENT_EVENT_NOTIFICATION: "client_event",
    MessageType.SERVER_EVENT_NOTIFICATION: "server_event",
    MessageType.GENERIC_SPATIAL_1: "generic_spatial",
    MessageType.SINGLE_ACTOR_MESSAGE: "single_actor_message",
    MessageType.CHANNEL_MESSAGE_NOTIFICATION: "channel_message",
    MessageType.GENERIC_ERROR: "generic_error",
    STATUS: "status",
}

#: The handler names :meth:`AsyncReplicationConnection.subscribe` accepts.
HANDLER_NAMES: Final = frozenset([*_HANDLER_KEYS.values(), "any"])


@dataclass(frozen=True, slots=True)
class Notification:
    """One event. ``extras`` holds what the type adds; the properties name it."""

    type: int
    app_id: int
    chunk: tuple[int, int, int]
    uuid: str
    payload: bytes
    epoch_ms: int
    sequence: int
    extras: tuple[int, int, int, int]

    @property
    def message_type(self) -> MessageType | None:
        try:
            return MessageType(self.type)
        except ValueError:
            return None

    @property
    def voxel(self) -> tuple[int, int, int]:
        """A voxel update's voxel coordinates within the chunk (its state is ``payload``)."""
        return self.extras[0], self.extras[1], self.extras[2]

    @property
    def voxel_type(self) -> int:
        return self.extras[3]

    @property
    def event_type(self) -> int:
        """A client or server event's type (its state is ``payload``)."""
        return self.extras[0]

    @property
    def left_reason(self) -> int:
        """An actor-left's reason (0 STALE)."""
        return self.extras[0]

    @property
    def channel_id(self) -> int:
        """A channel message's channel (the sender is ``uuid``)."""
        return self.extras[0]

    @property
    def error_code(self) -> ErrorCode | int:
        """A generic error's code; ``sequence`` is the failed send's."""
        try:
            return ErrorCode(self.extras[0])
        except ValueError:
            return self.extras[0]

    @property
    def state(self) -> ConnState:
        """A status row's connection state."""
        return ConnState(self.extras[0])


_numpy: Any = None
_numpy_checked = False


def _column(view: Any) -> Any:
    global _numpy, _numpy_checked
    if not _numpy_checked:
        try:
            _numpy = importlib.import_module("numpy")
        except ImportError:  # pragma: no cover - numpy is an optional extra
            _numpy = None
        _numpy_checked = True
    return _numpy.asarray(view) if _numpy is not None else memoryview(view)


class NotificationBatch:
    """The events one poll drained, in columns.

    The column properties are zero-copy views (numpy arrays when numpy is installed,
    ``memoryview`` otherwise) that keep the batch alive. Iterating builds a
    :class:`Notification` per event, for code that wants one object at a time.
    """

    __slots__ = ("_native",)

    def __init__(self, native: Any) -> None:
        self._native = native

    def __len__(self) -> int:
        return len(self._native)

    def __bool__(self) -> bool:
        return len(self._native) > 0

    def __iter__(self) -> Iterator[Notification]:
        for row in self._native.rows():
            yield Notification(*row)

    def __getitem__(self, index: int) -> Notification:
        if index < 0:
            index += len(self)
        return Notification(*self._native.row(index))

    @property
    def types(self) -> Any:
        return _column(self._native.types)

    @property
    def app_ids(self) -> Any:
        return _column(self._native.app_ids)

    @property
    def chunks(self) -> Any:
        """int64, one ``(x, y, z)`` row per event."""
        return _column(self._native.chunks)

    @property
    def uuids(self) -> Any:
        """uint8, 32 octets per event (NUL-padded)."""
        return _column(self._native.uuids)

    @property
    def epoch_ms(self) -> Any:
        return _column(self._native.epoch_ms)

    @property
    def sequences(self) -> Any:
        return _column(self._native.sequences)

    @property
    def extras(self) -> Any:
        """int64, four per event (see :class:`Notification`)."""
        return _column(self._native.extras)

    @property
    def payload_offsets(self) -> Any:
        """uint32, one more than the events: event ``i``'s payload is
        ``payload_data[payload_offsets[i]:payload_offsets[i + 1]]``."""
        return _column(self._native.payload_offsets)

    @property
    def payload_data(self) -> Any:
        return _column(self._native.payload_data)

    def payload(self, index: int) -> bytes:
        result: bytes = self._native.payload(index)
        return result

    def uuid(self, index: int) -> str:
        result: str = self._native.uuid(index)
        return result

    def match(self, sequence: int, uuid: str | bytes) -> int:
        """The row answering a send: its echo from ``uuid``, or the error naming its sequence."""
        result: int = self._native.match(sequence, uuid)
        return result


# ------------------------------------------------------------------ the provider server


class _ProviderServer:
    """Serves the native side's assignment and refresh requests on a thread of its own."""

    def __init__(self, bridge: Any, provider: SessionProvider, logger: logging.Logger) -> None:
        self._bridge = bridge
        self._provider = provider
        self._logger = logger
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._timeout = 30.0

    def start(self, loop: asyncio.AbstractEventLoop | None) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._loop = loop
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name="crowdypy-replication-provider", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stopping.is_set():
            request = self._bridge.next(250)
            if request is None:
                if self._bridge.closed:  # disconnected: next() would return at once
                    self._stopping.wait(0.05)
                continue
            request_id, kind, current = request
            try:
                if kind == _r.ASSIGN:
                    assignment = self._call(self._provider.assign_server)
                    self._bridge.answer_assignment(
                        request_id,
                        assignment.ip4 or "",
                        assignment.ip6 or "",
                        assignment.client_port,
                    )
                else:
                    server = Assignment(*current) if current is not None else None
                    token = self._call(self._provider.refresh_token, server)
                    self._bridge.answer_token(
                        request_id,
                        token.token,
                        token.game_token_id,
                        token.expires_at_ms,
                        token.authorized_on_current_server,
                    )
            except BaseException as exc:  # the thread must outlive any one failure
                self._logger.warning(
                    "replication %s request failed: %s",
                    "assignment" if kind == _r.ASSIGN else "token refresh",
                    exc,
                )
                code = _r.ERRC["Timeout"] if isinstance(exc, TimeoutError) else _r.ERRC["Rejected"]
                self._bridge.answer_error(request_id, code)

    def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        result = fn(*args)
        if inspect.isawaitable(result):
            if self._loop is None:
                raise RuntimeError("an async session provider needs the connection's event loop")
            return asyncio.run_coroutine_threadsafe(_awaited(result), self._loop).result(
                self._timeout
            )
        return result


async def _awaited(awaitable: Any) -> Any:
    return await awaitable


# ------------------------------------------------------------------ the connection


def _sequence_timeout(timeout: float) -> CrowdyRealtimeError:
    return CrowdyRealtimeError(
        f"no answer to the send within {timeout:g}s", code="UDP_SEQUENCE_TIMEOUT", retryable=True
    )


def _answered(event: Notification) -> Notification:
    if event.type != MessageType.GENERIC_ERROR:
        return event
    code = event.error_code
    name = code.name if isinstance(code, ErrorCode) else str(code)
    raise CrowdyRealtimeError(f"the server refused the send: {name}", code=name, retryable=False)


def _replication_error(exc: Exception) -> CrowdyReplicationError:
    message = str(exc)
    code, _, detail = message.partition(": ")
    return CrowdyReplicationError(detail or message, code=code if detail else None)


def _uuid_rows(uuids: Any) -> Any:
    if isinstance(uuids, (list, tuple)):
        rows = bytearray(32 * len(uuids))
        for i, value in enumerate(uuids):
            raw = value.encode("ascii") if isinstance(value, str) else bytes(value)
            if len(raw) > 32:
                raise CrowdyReplicationError(
                    "an actor uuid is at most 32 octets", code="InvalidArgument"
                )
            rows[32 * i : 32 * i + len(raw)] = raw
        return rows
    return uuids


def _chunk_rows(chunks: Any) -> Any:
    if isinstance(chunks, (list, tuple)):
        import array

        flat = array.array("q")
        for chunk in chunks:
            flat.extend((int(chunk[0]), int(chunk[1]), int(chunk[2])))
        return flat
    return chunks


def _payload_rows(payloads: Any) -> tuple[Any, Any]:
    """``(data, offsets)`` from a list of bodies; other inputs pass through without offsets."""
    if isinstance(payloads, (list, tuple)):
        import array

        offsets = array.array("q", [0])
        total = 0
        for body in payloads:
            total += len(body)
            offsets.append(total)
        return b"".join(bytes(b) for b in payloads), offsets
    return payloads, None


class _ConnectionCore:
    """Sends, polling and handler dispatch, shared by the async and blocking connections."""

    def __init__(
        self,
        provider: SessionProvider,
        token: TokenMaterial,
        *,
        app_id: int | str,
        logger: logging.Logger | None = None,
        subscriptions: list[Mapping[str, Callable[[Notification], Any]]] | None = None,
        **options: Any,
    ) -> None:
        self._logger = logger or logging.getLogger("crowdypy.replication")
        try:
            self._native: Any = _r.Connection(
                int(app_id), token.token, token.game_token_id, token.expires_at_ms, **options
            )
        except (ValueError, TypeError) as exc:
            raise _replication_error(exc) from None
        self.app_id = str(app_id)
        self._server = _ProviderServer(self._native.bridge, provider, self._logger)
        #: Subscribed handler maps; shared with ``client.udp`` so they outlive a reconnect.
        self._handlers = subscriptions if subscriptions is not None else []
        self._wake_r: socket.socket | None = None
        self._wake_w: socket.socket | None = None

    # ----- state

    @property
    def state(self) -> ConnState:
        return ConnState(self._native.state)

    @property
    def native(self) -> Any:
        """The ``crowdypy._native.replication.Connection`` underneath, for benchmarks and tools."""
        return self._native

    def assignment(self) -> Assignment:
        return Assignment(*self._native.assignment())

    def stats(self) -> dict[str, Any]:
        """CrowdyCPP's cumulative counters: a local diagnostic, not a bill (egress is what is
        billed, measured at the platform's interface)."""
        result: dict[str, Any] = self._native.stats()
        return result

    def set_token(self, token: TokenMaterial) -> None:
        """Rotate the token material after a refresh made outside the connection."""
        try:
            self._native.set_token(
                token.token,
                token.game_token_id,
                token.expires_at_ms,
                token.authorized_on_current_server,
            )
        except ValueError as exc:
            raise _replication_error(exc) from None

    def request_reassignment(self) -> None:
        """Ask for a fresh server assignment on the next housekeeping pass (after a datacenter
        move, so the UDP session follows the API to the app's datacenter)."""
        self._native.request_reassignment()

    # ----- sends: each returns the message's sequence number (0-255)

    def send_actor_update(
        self,
        chunk: Sequence[int],
        uuid: str | bytes,
        state: Any = b"",
        *,
        distance: int = 8,
        decay: int = DecayRate.EXPONENTIAL,
    ) -> int:
        return self._spatial(MessageType.ACTOR_UPDATE_REQUEST, chunk, uuid, state, distance, decay)

    def send_audio_packet(
        self,
        chunk: Sequence[int],
        uuid: str | bytes,
        audio: Any,
        *,
        distance: int = 1,
        decay: int = DecayRate.NONE,
    ) -> int:
        return self._spatial(MessageType.CLIENT_AUDIO_PACKET, chunk, uuid, audio, distance, decay)

    def send_video_packet(
        self,
        chunk: Sequence[int],
        uuid: str | bytes,
        fragment: Any,
        *,
        distance: int = 1,
        decay: int = DecayRate.NONE,
    ) -> int:
        """One video fragment (header + slice, see :func:`crowdypy.media.fragment_frame`)."""
        return self._spatial(
            MessageType.CLIENT_VIDEO_PACKET, chunk, uuid, fragment, distance, decay
        )

    def send_text_packet(
        self,
        chunk: Sequence[int],
        uuid: str | bytes,
        text: str | bytes,
        *,
        distance: int = 8,
        decay: int = DecayRate.NONE,
    ) -> int:
        body = text.encode("utf-8") if isinstance(text, str) else text
        return self._spatial(MessageType.CLIENT_TEXT_PACKET, chunk, uuid, body, distance, decay)

    def send_generic_spatial(
        self,
        chunk: Sequence[int],
        uuid: str | bytes,
        payload: Any,
        *,
        distance: int = 8,
        decay: int = DecayRate.NONE,
    ) -> int:
        return self._spatial(MessageType.GENERIC_SPATIAL_1, chunk, uuid, payload, distance, decay)

    def send_voxel_update(
        self,
        chunk: Sequence[int],
        uuid: str | bytes,
        voxel: Sequence[int],
        voxel_type: int,
        voxel_state: Any = b"",
        *,
        distance: int = 8,
        decay: int = DecayRate.NONE,
    ) -> int:
        try:
            result: int = self._native.send_voxel_update(
                chunk[0], chunk[1], chunk[2], uuid, voxel[0], voxel[1], voxel[2],
                voxel_type, voxel_state, distance, decay,
            )  # fmt: skip
        except ValueError as exc:
            raise _replication_error(exc) from None
        return result

    def send_client_event(
        self,
        chunk: Sequence[int],
        uuid: str | bytes,
        event_type: int,
        state: Any = b"",
        *,
        distance: int = 8,
        decay: int = DecayRate.NONE,
    ) -> int:
        try:
            result: int = self._native.send_client_event(
                chunk[0], chunk[1], chunk[2], uuid, event_type, state, distance, decay
            )
        except ValueError as exc:
            raise _replication_error(exc) from None
        return result

    def send_single_actor_message(
        self, chunk: Sequence[int], uuid: str | bytes, payload: Any
    ) -> int:
        """A direct message to one actor: ``chunk`` and ``uuid`` address the TARGET."""
        try:
            result: int = self._native.send_single_actor_message(
                chunk[0], chunk[1], chunk[2], uuid, payload
            )
        except ValueError as exc:
            raise _replication_error(exc) from None
        return result

    def send_channel_message(self, channel_id: int | str, uuid: str | bytes, payload: Any) -> int:
        try:
            result: int = self._native.send_channel_message(int(channel_id), uuid, payload)
        except ValueError as exc:
            raise _replication_error(exc) from None
        return result

    def send_ranged_channel_message(
        self,
        channel_id: int | str,
        uuid: str | bytes,
        payload: Any,
        chunk: Sequence[int],
        max_distance: int,
    ) -> int:
        """A channel publish that reaches only the members whose own actor is within
        ``max_distance`` chunks of ``chunk`` (straight-line distance, inclusive). The origin
        is the connection's app; members receive an ordinary channel message."""
        if not 0 <= int(max_distance) <= CHANNEL_RANGED_MAX_DISTANCE:
            raise CrowdyReplicationError(
                f"max_distance must be 0..{CHANNEL_RANGED_MAX_DISTANCE}", code="InvalidArgument"
            )
        try:
            result: int = self._native.send_ranged_channel_message(
                int(channel_id), uuid, payload, chunk[0], chunk[1], chunk[2], int(max_distance)
            )
        except ValueError as exc:
            raise _replication_error(exc) from None
        return result

    def send_heartbeat(self, chunk: Sequence[int], uuid: str | bytes) -> int:
        """Idle keep-alive for your own actor; every ~2 s while idle keeps presence alive."""
        try:
            result: int = self._native.send_heartbeat(chunk[0], chunk[1], chunk[2], uuid)
        except ValueError as exc:
            raise _replication_error(exc) from None
        return result

    def send_video_frame(
        self,
        chunk: Sequence[int],
        uuid: str | bytes,
        frame: Any,
        frame_id: int,
        *,
        codec: int = 0,
        distance: int = 1,
        decay: int = DecayRate.NONE,
    ) -> int:
        """Fragment one encoded frame (JPEG 0, WebP 1) and send every fragment; returns how
        many. A frame needing more than 16 fragments is refused whole."""
        try:
            result: int = self._native.send_video_frame(
                chunk[0], chunk[1], chunk[2], uuid, frame, frame_id, codec, distance, decay
            )
        except ValueError as exc:
            raise _replication_error(exc) from None
        return result

    def send_actor_updates(
        self,
        chunks: Any,
        uuids: Any,
        states: Any,
        *,
        offsets: Any = None,
        stride: int = 0,
        distance: int = 8,
        decay: int = DecayRate.EXPONENTIAL,
        flush: bool = True,
    ) -> int:
        """Send many actor updates in one call, releasing the GIL once.

        ``chunks``: int64 ``(n, 3)`` (a numpy array, ``array('q')`` or bytes) or a list of
        triples. ``uuids``: ``n x 32`` octets or a list. ``states``: a list of bodies, or the
        bodies back to back with int64 ``offsets`` (``n + 1``) or a fixed ``stride``. Returns
        how many were accepted.
        """
        return self.send_spatial_batch(
            MessageType.ACTOR_UPDATE_REQUEST, chunks, uuids, states,
            offsets=offsets, stride=stride, distance=distance, decay=decay, flush=flush,
        )  # fmt: skip

    def send_spatial_batch(
        self,
        message_type: int,
        chunks: Any,
        uuids: Any,
        payloads: Any,
        *,
        offsets: Any = None,
        stride: int = 0,
        distance: int = 8,
        decay: int = DecayRate.NONE,
        flush: bool = True,
    ) -> int:
        """:meth:`send_actor_updates` for any one spatial type (actor, audio, video, text,
        generic)."""
        data, list_offsets = _payload_rows(payloads)
        try:
            result: int = self._native.send_spatial_batch(
                int(message_type), _chunk_rows(chunks), _uuid_rows(uuids), data,
                list_offsets if list_offsets is not None else offsets,
                stride, distance, decay, flush,
            )  # fmt: skip
        except ValueError as exc:
            raise _replication_error(exc) from None
        return result

    def flush_sends(self) -> None:
        """Put the pending bundle on the wire now (end of a frame) instead of at the end of the
        bundle window."""
        try:
            self._native.flush_sends()
        except ValueError as exc:
            raise _replication_error(exc) from None

    def _spatial(
        self, kind: int, chunk: Sequence[int], uuid: Any, payload: Any, distance: int, decay: int
    ) -> int:
        try:
            result: int = self._native.send_spatial(
                kind, chunk[0], chunk[1], chunk[2], uuid, payload, distance, decay
            )
        except ValueError as exc:
            raise _replication_error(exc) from None
        return result

    # ----- receive

    def subscribe(
        self, handlers: Mapping[str, Callable[[Notification], Any]]
    ) -> Callable[[], None]:
        """Call ``handlers[name](notification)`` for each event: ``actor_update``,
        ``voxel_update``, ``audio``, ``video``, ``actor_left``, ``text``, ``client_event``,
        ``server_event``, ``generic_spatial``, ``single_actor_message``, ``channel_message``,
        ``generic_error``, ``status``, and ``any`` (every event but status). Returns the
        unsubscribe function. One object per event: prefer batches on a hot path."""
        unknown = set(handlers) - HANDLER_NAMES
        if unknown:
            raise ValueError(f"unknown handler name(s): {', '.join(sorted(unknown))}")
        entry = dict(handlers)
        self._handlers.append(entry)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._handlers.remove(entry)

        return unsubscribe

    def pump(self, timeout_ms: int = 0) -> int:
        """Manual-pump mode: do network work for up to ``timeout_ms``; returns datagrams read."""
        result: int = self._native.pump(timeout_ms)
        return result

    def _drain(self, max_events: int | None) -> NotificationBatch:
        native = self._native.poll() if max_events is None else self._native.poll(max_events)
        for level, line in self._native.drain_logs():
            self._logger.log(_LOG_LEVELS.get(level, logging.INFO), "%s", line)
        batch = NotificationBatch(native)
        if self._handlers and native:
            self._dispatch(batch)
        return batch

    def _dispatch(self, batch: NotificationBatch) -> None:
        for event in batch:
            key = _HANDLER_KEYS.get(event.type)
            for handlers in list(self._handlers):
                if event.type != STATUS:
                    every = handlers.get("any")
                    if every is not None:
                        self._call_handler(every, event)
                handler = handlers.get(key) if key else None
                if handler is not None:
                    self._call_handler(handler, event)

    def _call_handler(self, handler: Callable[[Notification], Any], event: Notification) -> None:
        try:
            handler(event)
        except Exception:
            self._logger.exception("replication handler failed")

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


class AsyncReplicationConnection(_ConnectionCore):
    """A connection driven by an asyncio loop.

    After :meth:`connect`, a task on the loop wakes when events are waiting, drains them,
    dispatches subscribed handlers, resolves ``*_and_wait`` sends and hands each batch to
    :meth:`batches` consumers.
    """

    def __init__(self, provider: SessionProvider, token: TokenMaterial, **kwargs: Any) -> None:
        super().__init__(provider, token, **kwargs)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: asyncio.Task[None] | None = None
        self._consumers: list[asyncio.Queue[NotificationBatch | None]] = []
        self._waiters: list[tuple[int, str | bytes, asyncio.Future[Notification]]] = []

    async def connect(self) -> None:
        """Assign a server (through the provider), open the socket and start reading."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._open_wake()
        self._server.start(loop)
        try:
            await asyncio.to_thread(self._native.connect)
        except ValueError as exc:
            raise _replication_error(exc) from None
        if self._reader is None or self._reader.done():
            self._reader = loop.create_task(self._read(), name="crowdypy-replication-reader")

    async def disconnect(self) -> None:
        """Close the socket and stop the network thread. Handlers and subscriptions stay, so
        :meth:`connect` resumes with them (what a gameplay-token rotation does)."""
        await asyncio.to_thread(self._native.disconnect)
        await asyncio.to_thread(self._server.stop)
        self._fail_waiters(CrowdyRealtimeError("the connection closed", code="CLOSED"))

    async def close(self) -> None:
        """Disconnect and release everything; batch consumers see the end of the stream."""
        await self.disconnect()
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        self._close_wake()
        for queue in self._consumers:
            queue.put_nowait(None)

    async def __aenter__(self) -> AsyncReplicationConnection:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def poll(self, max_events: int | None = None) -> NotificationBatch:
        """Drain now, on this thread (the reader task does this by itself after connect)."""
        batch = self._drain(max_events)
        self._deliver(batch)
        return batch

    async def batches(self) -> AsyncIterator[NotificationBatch]:
        """Every non-empty batch from now until :meth:`close`."""
        queue: asyncio.Queue[NotificationBatch | None] = asyncio.Queue()
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

    async def wait_for_sequence(
        self, sequence: int, uuid: str | bytes, timeout: float = 5.0
    ) -> Notification:
        """The echo of send ``sequence`` from ``uuid`` (only actor and voxel updates echo).

        As in CrowdyJS, a server error for that sequence raises :class:`CrowdyRealtimeError`
        with the error's name as ``code`` (``UNAUTHORIZED``, ...), and no answer within
        ``timeout`` seconds raises one with ``code == "UDP_SEQUENCE_TIMEOUT"``.
        """
        future = self._register(sequence, uuid)
        return await self._settle(future, timeout)

    def _register(self, sequence: int, uuid: str | bytes) -> asyncio.Future[Notification]:
        loop = self._loop or asyncio.get_running_loop()
        future: asyncio.Future[Notification] = loop.create_future()
        self._waiters.append((sequence, uuid, future))
        return future

    async def _settle(self, future: asyncio.Future[Notification], timeout: float) -> Notification:
        try:
            event = await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise _sequence_timeout(timeout) from None
        finally:
            self._waiters = [w for w in self._waiters if w[2] is not future]
        return _answered(event)

    async def _read(self) -> None:
        loop = asyncio.get_running_loop()
        wake = self._wake_r
        assert wake is not None
        while True:
            try:
                await loop.sock_recv(wake, 4096)
            except (OSError, ValueError):
                return
            self._clear_wake()
            batch = self._drain(None)
            self._deliver(batch)

    def _deliver(self, batch: NotificationBatch) -> None:
        if not batch:
            return
        if self._waiters:
            for sequence, uuid, future in list(self._waiters):
                if future.done():
                    continue
                row = batch.match(sequence, uuid)
                if row >= 0:
                    future.set_result(batch[row])
        for queue in self._consumers:
            queue.put_nowait(batch)

    def _fail_waiters(self, error: BaseException) -> None:
        for _, _, future in self._waiters:
            if not future.done():
                future.set_exception(error)
        self._waiters.clear()


class ReplicationConnection(_ConnectionCore):
    """A connection for code without an event loop: :meth:`poll` from your game loop, or
    :meth:`wait` for events first."""

    def __init__(self, provider: SessionProvider, token: TokenMaterial, **kwargs: Any) -> None:
        super().__init__(provider, token, **kwargs)
        self._backlog: list[NotificationBatch] = []

    def connect(self) -> None:
        """Assign a server (through the provider), open the socket. Blocks for one round trip."""
        self._open_wake()
        self._server.start(None)
        try:
            self._native.connect()
        except ValueError as exc:
            raise _replication_error(exc) from None

    def disconnect(self) -> None:
        """Close the socket and stop the network thread; handlers and subscriptions stay."""
        self._native.disconnect()
        self._server.stop()

    def close(self) -> None:
        self.disconnect()
        self._close_wake()

    def __enter__(self) -> ReplicationConnection:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def poll(self, max_events: int | None = None) -> NotificationBatch:
        """Drain waiting events, dispatching subscribed handlers on this thread."""
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

    def wait_for_sequence(
        self, sequence: int, uuid: str | bytes, timeout: float = 5.0
    ) -> Notification:
        """The echo of send ``sequence`` from ``uuid``; see the async connection. The batches
        read while waiting are kept for the next :meth:`poll`."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _sequence_timeout(timeout)
            self.wait(remaining)
            self._clear_wake()
            batch = self._drain(None)
            if not batch:
                continue
            self._backlog.append(batch)
            row = batch.match(sequence, uuid)
            if row < 0:
                continue
            return _answered(batch[row])
