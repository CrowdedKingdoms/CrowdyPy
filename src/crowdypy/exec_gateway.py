"""The ck-exec gateway connection: call methods on, and subscribe to topics of, the server
halves of mods (CrowdyJS's ``ExecConnection``).

One WebSocket to the gateway of the host ``client.exec.endpoint()`` names, binary frames
(:func:`encode_exec_frame` / :func:`decode_exec_frame`, checked against the platform's
shared frame fixture), msgpack payloads. A dropped connection is redialed with backoff and
its subscriptions renewed; a ``Moved`` reply redials to the new host and retries the call.

:class:`AsyncExecConnection` runs in an asyncio loop; :class:`ExecConnection` is the blocking
twin, with a reader thread (push handlers run on it).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import msgspec

from crowdypy.domains.exec import CrowdyExecError, ExecEndpoint, ExecStatus, exec_status

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection as AsyncSocket
    from websockets.sync.client import ClientConnection as SyncSocket

__all__ = [
    "AsyncExecConnection",
    "ExecCall",
    "ExecConnection",
    "ExecPing",
    "ExecPong",
    "ExecPush",
    "ExecReply",
    "ExecSubscribe",
    "decode_exec_frame",
    "encode_exec_frame",
]

_CALL_TIMEOUT = 10.0
_OPEN_TIMEOUT = 10.0
_MAX_BACKOFF = 5.0


# ------------------------------------------------------------------ frames


@dataclass(frozen=True, slots=True)
class ExecCall:
    rid: int
    node_type: str
    key: str
    method: str
    payload: bytes = b""


@dataclass(frozen=True, slots=True)
class ExecSubscribe:
    rid: int
    node_type: str
    key: str
    topic: str
    #: Send an unsubscribe frame instead.
    unsubscribe: bool = False


@dataclass(frozen=True, slots=True)
class ExecPing:
    nonce: int


@dataclass(frozen=True, slots=True)
class ExecReply:
    rid: int
    status: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class ExecPush:
    """A topic push. ``value`` is the msgpack-decoded payload (``None`` until delivered, or
    when it did not decode)."""

    node_type: str
    key: str
    topic: str
    payload: bytes
    value: Any = None


@dataclass(frozen=True, slots=True)
class ExecPong:
    nonce: int


def _str(value: str, limit: int, what: str) -> bytes:
    data = value.encode("utf-8")
    if len(data) > limit:
        raise ValueError(f"{what} is longer than {limit} bytes")
    return data


def encode_exec_frame(frame: ExecCall | ExecSubscribe | ExecPing) -> bytes:
    if isinstance(frame, ExecPing):
        return b"\x04" + frame.nonce.to_bytes(4, "little")
    node = _str(frame.node_type, 0xFF, "node type")
    key = _str(frame.key, 0xFFFF, "key")
    head = bytes([len(node)]) + node + len(key).to_bytes(2, "little") + key
    if isinstance(frame, ExecCall):
        method = _str(frame.method, 0xFF, "method")
        return (
            b"\x01"
            + frame.rid.to_bytes(4, "little")
            + head
            + bytes([len(method)])
            + method
            + bytes(frame.payload)
        )
    topic = _str(frame.topic, 0xFF, "topic")
    tag = b"\x03" if frame.unsubscribe else b"\x02"
    return tag + frame.rid.to_bytes(4, "little") + head + bytes([len(topic)]) + topic


def decode_exec_frame(data: bytes) -> ExecReply | ExecPush | ExecPong:
    """A frame from the gateway; :class:`CrowdyExecError` (``BadRequest``) when malformed."""
    view = memoryview(data)
    at = 0

    def take(n: int) -> memoryview:
        nonlocal at
        if at + n > len(view):
            raise CrowdyExecError("BadRequest", "truncated frame from the gateway")
        chunk = view[at : at + n]
        at += n
        return chunk

    def text(n: int) -> str:
        try:
            return bytes(take(n)).decode("utf-8")
        except UnicodeDecodeError:
            raise CrowdyExecError("BadRequest", "a frame from the gateway is not UTF-8") from None

    tag = take(1)[0]
    if tag == 0x81:
        rid = int.from_bytes(take(4), "little")
        status = take(1)[0]
        return ExecReply(rid, status, bytes(view[at:]))
    if tag == 0x82:
        node = text(take(1)[0])
        key = text(int.from_bytes(take(2), "little"))
        topic = text(take(1)[0])
        return ExecPush(node, key, topic, bytes(view[at:]))
    if tag == 0x84:
        return ExecPong(int.from_bytes(take(4), "little"))
    raise CrowdyExecError("BadRequest", f"unknown frame type 0x{tag:x} from the gateway")


def _decode_value(payload: bytes) -> Any:
    try:
        return msgspec.msgpack.decode(payload)
    except msgspec.DecodeError:
        return None


def _url(endpoint: ExecEndpoint) -> str:
    return f"{endpoint.gateway_url.rstrip('/')}/v1/connect?token={quote(endpoint.token, safe='')}"


def _closed_status(error: BaseException) -> ExecStatus:
    received = getattr(error, "rcvd", None)
    return "Denied" if received is not None and received.code == 4401 else "Unavailable"


def _refused(error: BaseException) -> CrowdyExecError | None:
    """A refused upgrade as the gateway put it: ``401`` (a token it will not take) is
    ``Denied``, anything else ``Unavailable``, with the body as the reason."""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        return None
    body = getattr(response, "body", None) or b""
    reason = " ".join(body.decode("utf-8", errors="replace").split())[:500]
    return CrowdyExecError(
        "Denied" if status == 401 else "Unavailable",
        f"the gateway refused the connection (HTTP {status}{f': {reason}' if reason else ''})",
    )


def _status_error(status: int, payload: bytes) -> CrowdyExecError:
    return CrowdyExecError(exec_status(status), payload.decode("utf-8", errors="replace"))


@dataclass
class _Shared:
    """What both connections track: subscriptions, listeners and request ids."""

    subs: dict[tuple[str, str, str], list[Callable[[ExecPush], Any]]] = field(default_factory=dict)
    listeners: list[Callable[[str], Any]] = field(default_factory=list)
    next_rid: int = 1

    def rid(self) -> int:
        rid = self.next_rid
        self.next_rid = 1 if self.next_rid >= 0xFFFFFFFF else self.next_rid + 1
        return rid

    def deliver(self, push: ExecPush) -> None:
        handlers = self.subs.get((push.node_type, push.key, push.topic))
        if not handlers:
            return
        delivered = ExecPush(
            push.node_type, push.key, push.topic, push.payload, _decode_value(push.payload)
        )
        for handler in list(handlers):
            with contextlib.suppress(Exception):  # a handler's exception is its own
                handler(delivered)


# ------------------------------------------------------------------ asyncio


class AsyncExecConnection:
    """A gateway connection in an asyncio loop. Get one from ``client.exec.connect(app_id)``
    (a player) or ``connect_as_developer``, or :meth:`open` for a known gateway and token."""

    def __init__(
        self,
        dial: Callable[[], Awaitable[ExecEndpoint]],
        *,
        call_timeout: float = _CALL_TIMEOUT,
        reconnect: bool = True,
        open_timeout: float = _OPEN_TIMEOUT,
    ) -> None:
        self._dial = dial
        self._call_timeout = call_timeout
        self._reconnect_enabled = reconnect
        self._open_timeout = open_timeout
        self._ws: AsyncSocket | None = None
        self._endpoint: ExecEndpoint | None = None
        self._ready: asyncio.Task[None] | None = None
        self._pending: dict[int, tuple[Any, asyncio.Future[tuple[int, bytes, Any]]]] = {}
        self._shared = _Shared()
        self._closed = False
        self._backoff = 0.25
        self._tasks: set[asyncio.Task[Any]] = set()

    @classmethod
    async def open(cls, gateway_url: str, token: str, **options: Any) -> AsyncExecConnection:
        """Connect to a gateway with a token you already hold (no reconnect)."""

        async def dial() -> ExecEndpoint:
            return ExecEndpoint(gateway_url=gateway_url, token=token, host="")

        connection = cls(dial, **{**options, "reconnect": False})
        await connection.connect()
        return connection

    @property
    def host(self) -> str:
        """The host the connection is on ("" before connecting, or for :meth:`open`)."""
        return self._endpoint.host if self._endpoint is not None else ""

    def on_reconnect(self, listener: Callable[[str], Any]) -> Callable[[], None]:
        """Called with the new host after every reconnect or move."""
        self._shared.listeners.append(listener)
        return lambda: (
            self._shared.listeners.remove(listener) if listener in self._shared.listeners else None
        )

    async def connect(self) -> None:
        if self._ready is None:
            self._ready = asyncio.ensure_future(self._dial_once())
        try:
            await asyncio.shield(self._ready)
        except BaseException:
            self._ready = None
            raise

    async def _dial_once(self) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidHandshake

        endpoint = await self._dial()
        try:
            ws = await connect(_url(endpoint), open_timeout=self._open_timeout, max_size=None)
        except ConnectionClosed as exc:
            raise CrowdyExecError(
                _closed_status(exc), f"the gateway closed the connection ({exc})"
            ) from exc
        except (InvalidHandshake, OSError, TimeoutError) as exc:
            raise _refused(exc) or CrowdyExecError(
                "Unavailable", f"connecting to {endpoint.gateway_url} failed: {exc}"
            ) from exc
        self._ws = ws
        self._endpoint = endpoint
        self._backoff = 0.25
        self._spawn(self._read(ws))

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read(self, ws: AsyncSocket) -> None:
        from websockets.exceptions import ConnectionClosed

        status: ExecStatus = "Unavailable"
        why = "the connection to the execution host closed"
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    self._on_message(ws, message)
        except ConnectionClosed as exc:
            status, why = _closed_status(exc), f"the gateway closed the connection ({exc})"
        finally:
            self._on_closed(ws, status, why)

    def _on_message(self, ws: Any, data: bytes) -> None:
        try:
            frame = decode_exec_frame(data)
        except CrowdyExecError:
            return
        if isinstance(frame, ExecPush):
            self._shared.deliver(frame)
            return
        rid = frame.rid if isinstance(frame, ExecReply) else frame.nonce
        waiting = self._pending.pop(rid, None)
        if waiting is None or waiting[1].done():
            return
        status, payload = (
            (frame.status, frame.payload) if isinstance(frame, ExecReply) else (0, b"")
        )
        waiting[1].set_result((status, payload, waiting[0]))

    def _reject(self, ws: Any, why: str, status: ExecStatus = "Unavailable") -> None:
        for rid, (owner, future) in list(self._pending.items()):
            if owner is ws:
                del self._pending[rid]
                if not future.done():
                    future.set_exception(CrowdyExecError(status, why))

    def _on_closed(self, ws: Any, status: ExecStatus, why: str) -> None:
        self._reject(ws, why, status)
        if self._ws is not ws:
            return
        self._ws = None
        self._ready = None
        if not self._closed and self._reconnect_enabled:
            self._spawn(self._reconnect())

    async def _reconnect(self) -> None:
        while not self._closed:
            try:
                await self.connect()
                await self._renew()
                for listener in list(self._shared.listeners):
                    listener(self.host)
                return
            except Exception:
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, _MAX_BACKOFF)

    async def _redial(self, origin: Any) -> None:
        if self._ws is origin:
            self._ws = None
            self._ready = None
            self._reject(origin, "the connection moved to another host")
            if origin is not None:
                with contextlib.suppress(Exception):
                    await origin.close()
            await self.connect()
            await self._renew()
            for listener in list(self._shared.listeners):
                listener(self.host)
            return
        await self.connect()

    async def _renew(self) -> None:
        for node_type, key, topic in list(self._shared.subs):
            await self._request(ExecSubscribe(0, node_type, key, topic))

    async def _request(
        self, frame: ExecCall | ExecSubscribe | ExecPing, timeout: float | None = None
    ) -> tuple[int, bytes, Any]:
        if self._closed:
            raise CrowdyExecError("Unavailable", "the connection is closed")
        await self.connect()
        ws = self._ws
        if ws is None:
            raise CrowdyExecError("Unavailable", "not connected")
        rid = self._shared.rid()
        if isinstance(frame, ExecPing):
            frame = ExecPing(rid)
        elif isinstance(frame, ExecCall):
            frame = ExecCall(rid, frame.node_type, frame.key, frame.method, frame.payload)
        else:
            frame = ExecSubscribe(rid, frame.node_type, frame.key, frame.topic, frame.unsubscribe)
        future: asyncio.Future[tuple[int, bytes, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = (ws, future)
        try:
            await ws.send(encode_exec_frame(frame))
        except Exception as exc:
            self._pending.pop(rid, None)
            raise CrowdyExecError(
                _closed_status(exc), "sending to the execution host failed", exc
            ) from exc
        wait = timeout if timeout is not None else self._call_timeout
        try:
            return await asyncio.wait_for(future, wait)
        except TimeoutError:
            self._pending.pop(rid, None)
            raise CrowdyExecError(
                "DeadlineExceeded", f"no reply in {int(wait * 1000)} ms"
            ) from None

    async def call_raw(
        self,
        node_type: str,
        key: str,
        method: str,
        payload: bytes = b"",
        *,
        timeout: float | None = None,
    ) -> bytes:
        """Call ``method`` with raw payload bytes; returns the reply's bytes."""
        frame = ExecCall(0, node_type, key, method, payload)
        try:
            status, data, ws = await self._request(frame, timeout)
        except CrowdyExecError as exc:
            if not self._reconnect_enabled or exc.status != "Unavailable":
                raise
            status, data, ws = await self._request(frame, timeout)
        if self._reconnect_enabled and exec_status(status) == "Moved":
            await self._redial(ws)
            status, data, ws = await self._request(frame, timeout)
        if exec_status(status) != "Ok":
            raise _status_error(status, data)
        return data

    async def call(
        self,
        node_type: str,
        key: str,
        method: str,
        args: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Call ``method`` with msgpack-encoded ``args``; returns the decoded reply."""
        reply = await self.call_raw(
            node_type, key, method, msgspec.msgpack.encode(args), timeout=timeout
        )
        return msgspec.msgpack.decode(reply)

    async def subscribe(
        self, node_type: str, key: str, topic: str, on_push: Callable[[ExecPush], Any]
    ) -> Callable[[], Awaitable[None]]:
        """Receive ``topic``'s pushes; returns the (async) unsubscribe function."""
        subscription = (node_type, key, topic)
        handlers = self._shared.subs.setdefault(subscription, [])
        first = not handlers
        handlers.append(on_push)
        if first:
            try:
                status, data, _ = await self._request(ExecSubscribe(0, node_type, key, topic))
                if exec_status(status) != "Ok":
                    raise _status_error(status, data)
            except BaseException:
                handlers.remove(on_push)
                if not handlers:
                    del self._shared.subs[subscription]
                raise

        async def unsubscribe() -> None:
            current = self._shared.subs.get(subscription)
            if not current or on_push not in current:
                return
            current.remove(on_push)
            if current:
                return
            del self._shared.subs[subscription]
            if self._ws is not None:
                with contextlib.suppress(CrowdyExecError):
                    await self._request(ExecSubscribe(0, node_type, key, topic, unsubscribe=True))

        return unsubscribe

    async def ping(self) -> float:
        """The round trip to the host, in milliseconds."""
        start = time.monotonic()
        await self._request(ExecPing(0))
        return (time.monotonic() - start) * 1000

    async def close(self) -> None:
        self._closed = True
        ws, self._ws = self._ws, None
        self._ready = None
        if ws is not None:
            await ws.close()
        for task in list(self._tasks):
            task.cancel()


# ------------------------------------------------------------------ blocking


class _Slot:
    __slots__ = ("done", "error", "result", "ws")

    def __init__(self, ws: Any) -> None:
        self.done = threading.Event()
        self.result: tuple[int, bytes] | None = None
        self.error: BaseException | None = None
        self.ws = ws


class ExecConnection:
    """The blocking gateway connection: the same methods as :class:`AsyncExecConnection`,
    with a reader thread. Push handlers and reconnect listeners run on that thread."""

    def __init__(
        self,
        dial: Callable[[], ExecEndpoint],
        *,
        call_timeout: float = _CALL_TIMEOUT,
        reconnect: bool = True,
        open_timeout: float = _OPEN_TIMEOUT,
    ) -> None:
        self._dial = dial
        self._call_timeout = call_timeout
        self._reconnect_enabled = reconnect
        self._open_timeout = open_timeout
        self._ws: SyncSocket | None = None
        self._endpoint: ExecEndpoint | None = None
        self._pending: dict[int, _Slot] = {}
        self._shared = _Shared()
        self._lock = threading.RLock()
        self._closed = False
        self._backoff = 0.25

    @classmethod
    def open(cls, gateway_url: str, token: str, **options: Any) -> ExecConnection:
        connection = cls(
            lambda: ExecEndpoint(gateway_url=gateway_url, token=token, host=""),
            **{**options, "reconnect": False},
        )
        connection.connect()
        return connection

    @property
    def host(self) -> str:
        return self._endpoint.host if self._endpoint is not None else ""

    def on_reconnect(self, listener: Callable[[str], Any]) -> Callable[[], None]:
        self._shared.listeners.append(listener)
        return lambda: (
            self._shared.listeners.remove(listener) if listener in self._shared.listeners else None
        )

    def connect(self) -> None:
        with self._lock:
            if self._ws is not None:
                return
            from websockets.exceptions import ConnectionClosed, InvalidHandshake
            from websockets.sync.client import connect

            endpoint = self._dial()
            try:
                ws = connect(_url(endpoint), open_timeout=self._open_timeout, max_size=None)
                # The connection outlives any with-block; entering it says so (websockets 17.1+).
                ws.__enter__()
            except ConnectionClosed as exc:
                raise CrowdyExecError(
                    _closed_status(exc), f"the gateway closed the connection ({exc})"
                ) from exc
            except (InvalidHandshake, OSError, TimeoutError) as exc:
                raise _refused(exc) or CrowdyExecError(
                    "Unavailable", f"connecting to {endpoint.gateway_url} failed: {exc}"
                ) from exc
            self._ws = ws
            self._endpoint = endpoint
            self._backoff = 0.25
            threading.Thread(
                target=self._read, args=(ws,), name="crowdypy-exec-reader", daemon=True
            ).start()

    def _read(self, ws: SyncSocket) -> None:
        from websockets.exceptions import ConnectionClosed

        status: ExecStatus = "Unavailable"
        why = "the connection to the execution host closed"
        try:
            for message in ws:
                if isinstance(message, bytes):
                    self._on_message(message)
        except ConnectionClosed as exc:
            status, why = _closed_status(exc), f"the gateway closed the connection ({exc})"
        finally:
            self._on_closed(ws, status, why)

    def _on_message(self, data: bytes) -> None:
        try:
            frame = decode_exec_frame(data)
        except CrowdyExecError:
            return
        if isinstance(frame, ExecPush):
            self._shared.deliver(frame)
            return
        rid = frame.rid if isinstance(frame, ExecReply) else frame.nonce
        with self._lock:
            slot = self._pending.pop(rid, None)
        if slot is None:
            return
        slot.result = (frame.status, frame.payload) if isinstance(frame, ExecReply) else (0, b"")
        slot.done.set()

    def _reject(self, ws: Any, why: str, status: ExecStatus = "Unavailable") -> None:
        with self._lock:
            mine = [(rid, slot) for rid, slot in self._pending.items() if slot.ws is ws]
            for rid, _ in mine:
                del self._pending[rid]
        for _, slot in mine:
            slot.error = CrowdyExecError(status, why)
            slot.done.set()

    def _on_closed(self, ws: Any, status: ExecStatus, why: str) -> None:
        self._reject(ws, why, status)
        with self._lock:
            if self._ws is not ws:
                return
            self._ws = None
        if not self._closed and self._reconnect_enabled:
            threading.Thread(
                target=self._reconnect, name="crowdypy-exec-reconnect", daemon=True
            ).start()

    def _reconnect(self) -> None:
        while not self._closed:
            try:
                self.connect()
                self._renew()
                for listener in list(self._shared.listeners):
                    listener(self.host)
                return
            except Exception:
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, _MAX_BACKOFF)

    def _redial(self, origin: Any) -> None:
        with self._lock:
            moved = self._ws is origin
            if moved:
                self._ws = None
        if moved:
            self._reject(origin, "the connection moved to another host")
            with contextlib.suppress(Exception):
                origin.close()
            self.connect()
            self._renew()
            for listener in list(self._shared.listeners):
                listener(self.host)
            return
        self.connect()

    def _renew(self) -> None:
        for node_type, key, topic in list(self._shared.subs):
            self._request(ExecSubscribe(0, node_type, key, topic))

    def _request(
        self, frame: ExecCall | ExecSubscribe | ExecPing, timeout: float | None = None
    ) -> tuple[int, bytes, Any]:
        if self._closed:
            raise CrowdyExecError("Unavailable", "the connection is closed")
        self.connect()
        with self._lock:
            ws = self._ws
            if ws is None:
                raise CrowdyExecError("Unavailable", "not connected")
            rid = self._shared.rid()
            slot = _Slot(ws)
            self._pending[rid] = slot
        if isinstance(frame, ExecPing):
            frame = ExecPing(rid)
        elif isinstance(frame, ExecCall):
            frame = ExecCall(rid, frame.node_type, frame.key, frame.method, frame.payload)
        else:
            frame = ExecSubscribe(rid, frame.node_type, frame.key, frame.topic, frame.unsubscribe)
        try:
            ws.send(encode_exec_frame(frame))
        except Exception as exc:
            with self._lock:
                self._pending.pop(rid, None)
            raise CrowdyExecError(
                _closed_status(exc), "sending to the execution host failed", exc
            ) from exc
        wait = timeout if timeout is not None else self._call_timeout
        if not slot.done.wait(wait):
            with self._lock:
                self._pending.pop(rid, None)
            raise CrowdyExecError("DeadlineExceeded", f"no reply in {int(wait * 1000)} ms")
        if slot.error is not None:
            raise slot.error
        assert slot.result is not None
        return slot.result[0], slot.result[1], ws

    def call_raw(
        self,
        node_type: str,
        key: str,
        method: str,
        payload: bytes = b"",
        *,
        timeout: float | None = None,
    ) -> bytes:
        frame = ExecCall(0, node_type, key, method, payload)
        try:
            status, data, ws = self._request(frame, timeout)
        except CrowdyExecError as exc:
            if not self._reconnect_enabled or exc.status != "Unavailable":
                raise
            status, data, ws = self._request(frame, timeout)
        if self._reconnect_enabled and exec_status(status) == "Moved":
            self._redial(ws)
            status, data, ws = self._request(frame, timeout)
        if exec_status(status) != "Ok":
            raise _status_error(status, data)
        return data

    def call(
        self,
        node_type: str,
        key: str,
        method: str,
        args: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        reply = self.call_raw(node_type, key, method, msgspec.msgpack.encode(args), timeout=timeout)
        return msgspec.msgpack.decode(reply)

    def subscribe(
        self, node_type: str, key: str, topic: str, on_push: Callable[[ExecPush], Any]
    ) -> Callable[[], None]:
        subscription = (node_type, key, topic)
        handlers = self._shared.subs.setdefault(subscription, [])
        first = not handlers
        handlers.append(on_push)
        if first:
            try:
                status, data, _ = self._request(ExecSubscribe(0, node_type, key, topic))
                if exec_status(status) != "Ok":
                    raise _status_error(status, data)
            except BaseException:
                handlers.remove(on_push)
                if not handlers:
                    del self._shared.subs[subscription]
                raise

        def unsubscribe() -> None:
            current = self._shared.subs.get(subscription)
            if not current or on_push not in current:
                return
            current.remove(on_push)
            if current:
                return
            del self._shared.subs[subscription]
            if self._ws is not None:
                with contextlib.suppress(CrowdyExecError):
                    self._request(ExecSubscribe(0, node_type, key, topic, unsubscribe=True))

        return unsubscribe

    def ping(self) -> float:
        start = time.monotonic()
        self._request(ExecPing(0))
        return (time.monotonic() - start) * 1000

    def close(self) -> None:
        self._closed = True
        with self._lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            ws.close()
