"""The kit's way to the app's hubs. CrowdyJS ``src/kit/runtime.ts``."""

from __future__ import annotations

import asyncio
import contextlib
import random
import threading
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from crowdypy._concurrency import AsyncSingleFlight
from crowdypy.domains.exec import CrowdyExecError
from crowdypy.kit.protocol import KitError, new_action_id, with_action_id

if TYPE_CHECKING:
    from crowdypy.domains.exec import ExecAPI
    from crowdypy.exec_gateway import AsyncExecConnection, ExecPush

__all__ = ["KIT_SUBSCRIPTIONS_MAX", "KitRuntime"]

#: Subscriptions one connection holds at most (the gateway refuses the next).
KIT_SUBSCRIPTIONS_MAX = 256

_UNKNOWN = frozenset({"DeadlineExceeded", "Unavailable", "Moved"})
_DID_NOT_RUN = frozenset({"Busy", "RateLimited"})


class _Budget:
    """A token bucket: ``capacity`` tokens a window, refilled continuously."""

    def __init__(self, capacity: int, window_ms: float) -> None:
        self._capacity = capacity
        self._window = window_ms / 1000
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def wait(self) -> float:
        """0 when a token was taken; otherwise how long to wait for one, in seconds."""
        with self._lock:
            now = time.monotonic()
            if now < self._blocked_until:
                return self._blocked_until - now
            self._tokens = min(
                self._capacity,
                self._tokens + (now - self._last) * self._capacity / self._window,
            )
            self._last = now
            if self._tokens >= 1:
                self._tokens -= 1
                return 0.0
            return (1 - self._tokens) * self._window / self._capacity

    def hold(self, seconds: float) -> None:
        """The gateway said to wait: nothing is sent until ``seconds`` from now."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)
            self._tokens = 0.0


class KitRuntime:
    """One exec connection shared by every kit module, opened when first needed. It paces what
    it sends to a budget below the gateway's limit, sends a call again when that is safe (it did
    not run, or it carries an action id the hub answers once), counts subscriptions under the
    gateway's cap, and turns a hub's coded refusal into a :class:`KitError`.

    ``connection`` uses one you opened (the kit does not close it); otherwise ``exec`` opens
    one, as a developer with ``developer=True`` (an identity session with ``manage_compute``).
    ``connect`` holds the connection's options (``node_type``, ``key``, ``call_timeout``).
    ``call_budget`` calls a ``budget_window_ms`` keeps the kit under the gateway's 120 per
    10 s; ``retries`` is how many times a call is sent again; ``player_id`` matches
    ``changed`` messages (``who_am_i`` is asked when it is absent).
    """

    def __init__(
        self,
        app_id: str | int,
        exec: ExecAPI | None = None,
        *,
        connection: AsyncExecConnection | None = None,
        connect: dict[str, Any] | None = None,
        developer: bool = False,
        call_budget: int = 100,
        budget_window_ms: float = 10_000,
        retries: int = 2,
        player_id: str | int | None = None,
        who_am_i: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        self.app_id = str(app_id)
        self._exec = exec
        self._given = connection
        self._connect = dict(connect or {})
        self._developer = developer
        self._budget = _Budget(max(1, call_budget), max(1.0, budget_window_ms))
        self._retries = max(0, retries)
        self._player_id = None if player_id is None else str(player_id)
        self._who_am_i = who_am_i
        self._conn: AsyncExecConnection | None = None
        self._opening: AsyncSingleFlight[AsyncExecConnection] = AsyncSingleFlight()
        self._ended: str | None = None
        self._subs: dict[tuple[str, str, str], int] = {}
        self._lock = threading.Lock()
        self._reconnect: list[Callable[[str], Any]] = []
        self._access_ended: list[Callable[[str], Any]] = []

    async def _take(self) -> None:
        while True:
            wait = self._budget.wait()
            if wait <= 0:
                return
            await asyncio.sleep(wait)

    async def connection(self) -> AsyncExecConnection:
        """The connection, opened on first use."""
        if self._ended is not None:
            raise CrowdyExecError("Denied", f"access ended: {self._ended}")
        if self._conn is not None:
            return self._conn
        return await self._opening.run(self._open)

    async def _open(self) -> AsyncExecConnection:
        conn = self._given
        if conn is None:
            if self._exec is None:
                raise CrowdyExecError("Unavailable", "the kit has no exec client to connect with")
            if self._developer:
                conn = await self._exec.connect_as_developer(self.app_id, **self._connect)
            else:
                conn = await self._exec.connect(self.app_id, **self._connect)
        conn.on_reconnect(self._reconnected)
        conn.on_access_ended(self._ended_with)
        self._conn = conn
        return conn

    def _reconnected(self, host: str) -> None:
        for listener in list(self._reconnect):
            with contextlib.suppress(Exception):  # a listener's exception is its own
                listener(host)

    def _ended_with(self, reason: str) -> None:
        self._ended = reason or "access ended"
        for listener in list(self._access_ended):
            with contextlib.suppress(Exception):
                listener(reason)

    async def player_id(self) -> str | None:
        """The player's id, from the options or ``who_am_i``; ``None`` when neither knows it."""
        if self._player_id is None and self._who_am_i is not None:
            with contextlib.suppress(Exception):
                self._player_id = await self._who_am_i()
        return self._player_id

    async def call(
        self,
        node_type: str,
        key: str,
        method: str,
        args: Any = None,
        *,
        action: bool | str = False,
        timeout: float | None = None,
    ) -> Any:
        """Calls a hub's endpoint with ``args`` as msgpack and returns the decoded reply.

        ``action`` sends an ``action_id`` so a resend is answered once: ``True`` for a fresh
        one, or the id to use (a deliberate retry of an earlier attempt). Raises
        :class:`KitError` for a hub's coded refusal, :class:`CrowdyExecError` for the
        platform's.
        """
        action_id = (
            new_action_id() if action is True else action if isinstance(action, str) else None
        )
        sent = with_action_id(args, action_id) if action_id else args
        attempt = 0
        while True:
            await self._take()
            conn = await self.connection()
            try:
                return await conn.call(node_type, key, method, sent, timeout=timeout)
            except CrowdyExecError as exc:
                err = KitError.from_error(exc)
                if (
                    attempt < self._retries
                    and not isinstance(err, KitError)
                    and exc.status != "AppError"
                ):
                    if exc.status == "Busy" and exc.rate_limited:
                        self._budget.hold((exc.retry_after_ms or 1000) / 1000)
                        attempt += 1
                        continue
                    if exc.status in _DID_NOT_RUN or (action_id and exc.status in _UNKNOWN):
                        await asyncio.sleep(
                            min(2.0, 0.2 * 2**attempt) * (0.5 + random.random() / 2)
                        )
                        attempt += 1
                        continue
                raise err from None

    async def subscribe(
        self, node_type: str, key: str, topic: str, on_push: Callable[[ExecPush], Any]
    ) -> Callable[[], Awaitable[None]]:
        """Receives what a hub publishes on ``topic``. Returns the (async) function that stops
        this handler, and the subscription with the last one. Raises :class:`KitError`
        ``limit`` past :data:`KIT_SUBSCRIPTIONS_MAX` subscriptions."""
        sub = (node_type, key, topic)
        with self._lock:
            held = self._subs.get(sub, 0)
            if held == 0 and len(self._subs) >= KIT_SUBSCRIPTIONS_MAX:
                raise KitError(
                    "limit", f"a connection holds at most {KIT_SUBSCRIPTIONS_MAX} subscriptions"
                )
        if held == 0:
            await self._take()
        conn = await self.connection()
        stop = await conn.subscribe(node_type, key, topic, on_push)
        with self._lock:
            self._subs[sub] = self._subs.get(sub, 0) + 1
        stopped = False

        async def unsubscribe() -> None:
            nonlocal stopped
            if stopped:
                return
            stopped = True
            with self._lock:
                left = self._subs.get(sub, 1) - 1
                if left > 0:
                    self._subs[sub] = left
                else:
                    self._subs.pop(sub, None)
            if left <= 0:
                await self._take()
            await stop()

        return unsubscribe

    @property
    def subscriptions(self) -> int:
        """How many distinct topics the kit subscribes to."""
        return len(self._subs)

    def on_reconnect(self, listener: Callable[[str], Any]) -> Callable[[], None]:
        """Called with the new host after the connection reconnects; kit mirrors resync then."""
        self._reconnect.append(listener)
        return lambda: self._reconnect.remove(listener) if listener in self._reconnect else None

    def on_access_ended(self, listener: Callable[[str], Any]) -> Callable[[], None]:
        """Called once when the player's access ended (close code 4403); nothing reconnects."""
        self._access_ended.append(listener)
        return lambda: (
            self._access_ended.remove(listener) if listener in self._access_ended else None
        )

    async def close(self) -> None:
        """Closes the connection the kit opened (not one it was given)."""
        conn, self._conn = self._conn, None
        if conn is not None and self._given is None:
            await conn.close()
        with self._lock:
            self._subs.clear()
