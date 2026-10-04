"""GraphQL subscriptions over ``graphql-transport-ws``.

Every subscription a client opens shares one WebSocket to the client's GraphQL WebSocket
endpoint, authenticated as CrowdyJS's is (``Authorization: Bearer`` in the connection
parameters, with the app id, and the sticky load-balancer cookie on the upgrade).
Replication does not use this: ``client.udp`` is the native connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import AsyncIterator, Callable, Mapping
from typing import TYPE_CHECKING, Any

import msgspec

from crowdypy.errors import CrowdyGraphQLError, CrowdyRealtimeError

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

    from crowdypy.lb_cookie import LbCookieStore

__all__ = ["GraphQLSubscriptions"]

_PROTOCOL = "graphql-transport-ws"
_DONE = object()


class GraphQLSubscriptions:
    def __init__(
        self,
        url: str,
        get_token: Callable[[], str | None],
        *,
        app_id: str | None = None,
        lb_cookie_store: LbCookieStore | None = None,
        open_timeout: float = 10.0,
        ack_timeout: float = 10.0,
    ) -> None:
        self.url = url
        self._get_token = get_token
        self._app_id = app_id
        self._lb_cookie_store = lb_cookie_store
        self._open_timeout = open_timeout
        self._ack_timeout = ack_timeout
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._streams: dict[str, asyncio.Queue[Any]] = {}
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()

    @classmethod
    def for_client(
        cls, client: Any, *, app_id: str | None = None, **options: Any
    ) -> GraphQLSubscriptions:
        """Subscriptions on an :class:`~crowdypy.AsyncCrowdyClient`'s WebSocket endpoint,
        with its token, its load-balancer cookie and (by default) its app token's app."""
        if not client.ws_endpoint:
            raise CrowdyRealtimeError("the client has no WebSocket endpoint", code="NO_ENDPOINT")
        app = app_id or (client.app_token.app_id if client.app_token is not None else None)
        return cls(
            client.ws_endpoint,
            client.get_token,
            app_id=app,
            lb_cookie_store=client.lb_cookie_store,
            **options,
        )

    async def connect(self) -> None:
        """Open the socket and complete the protocol's handshake (``connection_ack``)."""
        async with self._lock:
            if self._ws is not None:
                return
            from websockets.asyncio.client import connect
            from websockets.exceptions import InvalidHandshake

            headers = {}
            cookie = self._lb_cookie_store.header_value() if self._lb_cookie_store else None
            if cookie:
                headers["Cookie"] = cookie
            try:
                ws = await connect(
                    self.url, subprotocols=[_PROTOCOL], additional_headers=headers,  # type: ignore[list-item]
                    open_timeout=self._open_timeout, max_size=None,
                )  # fmt: skip
            except (InvalidHandshake, OSError, TimeoutError) as exc:
                raise CrowdyRealtimeError(
                    f"could not open {self.url}: {exc}", code="CONNECT_FAILED", retryable=True
                ) from exc
            params: dict[str, str] = {}
            token = self._get_token()
            if token:
                params["Authorization"] = f"Bearer {token}"
            if self._app_id is not None:
                params["appId"] = self._app_id
            await ws.send(
                msgspec.json.encode({"type": "connection_init", "payload": params}).decode()
            )
            try:
                ack = msgspec.json.decode(await asyncio.wait_for(ws.recv(), self._ack_timeout))
            except Exception as exc:
                await ws.close()
                raise CrowdyRealtimeError(
                    "no connection_ack from the server", code="CONNECT_FAILED", retryable=True
                ) from exc
            if ack.get("type") != "connection_ack":
                await ws.close()
                raise CrowdyRealtimeError(
                    f"the server refused the connection: {ack}", code="AUTH_REQUIRED"
                )
            self._ws = ws
            self._reader = asyncio.ensure_future(self._read(ws))

    async def subscribe(
        self,
        document: str,
        variables: Mapping[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> AsyncIterator[Any]:
        """Each result's ``data``, until the server completes the subscription. GraphQL
        errors raise :class:`CrowdyGraphQLError`.

        Python finalizes an abandoned async generator only later, so to stop one subscription
        promptly, iterate it inside ``contextlib.aclosing(...)``; :meth:`close` stops them all.
        """
        await self.connect()
        ws = self._ws
        assert ws is not None
        sid = str(next(self._ids))
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._streams[sid] = queue
        payload: dict[str, Any] = {"query": document}
        if variables is not None:
            payload["variables"] = dict(variables)
        if operation_name is not None:
            payload["operationName"] = operation_name
        await ws.send(
            msgspec.json.encode({"id": sid, "type": "subscribe", "payload": payload}).decode()
        )
        finished = False
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    finished = True
                    return
                if isinstance(item, BaseException):
                    finished = True
                    raise item
                yield item
        finally:
            self._streams.pop(sid, None)
            if not finished and self._ws is ws:
                with contextlib.suppress(Exception):
                    await ws.send(msgspec.json.encode({"id": sid, "type": "complete"}).decode())

    async def _read(self, ws: ClientConnection) -> None:
        from websockets.exceptions import ConnectionClosed

        reason: BaseException = CrowdyRealtimeError(
            "the subscription socket closed", code="CONNECTION_CLOSED", retryable=True
        )
        try:
            async for raw in ws:
                message = msgspec.json.decode(raw)
                kind = message.get("type")
                if kind == "ping":
                    await ws.send('{"type":"pong"}')
                    continue
                queue = self._streams.get(str(message.get("id")))
                if queue is None:
                    continue
                if kind == "next":
                    result = message.get("payload") or {}
                    if result.get("errors"):
                        queue.put_nowait(CrowdyGraphQLError(result["errors"]))
                    else:
                        queue.put_nowait(result.get("data"))
                elif kind == "error":
                    queue.put_nowait(CrowdyGraphQLError(message.get("payload") or []))
                elif kind == "complete":
                    queue.put_nowait(_DONE)
        except ConnectionClosed:
            pass
        finally:
            if self._ws is ws:
                self._ws = None
            for queue in self._streams.values():
                queue.put_nowait(reason)

    async def close(self) -> None:
        """Stop every open subscription and close the socket."""
        ws, self._ws = self._ws, None
        if ws is not None:
            for sid in list(self._streams):
                with contextlib.suppress(Exception):
                    await ws.send(msgspec.json.encode({"id": sid, "type": "complete"}).decode())
            await ws.close()
        if self._reader is not None:
            with contextlib.suppress(Exception):
                await self._reader
