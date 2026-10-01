"""Coalescing one in-flight operation among concurrent callers.

``AsyncSingleFlight`` is what the async client uses; scripts/unasync.py maps the name to
``SingleFlight``, the thread-safe twin the blocking client uses. Several things notice the
same condition at once (a dead endpoint, an expiring token) and must share one attempt
rather than each acting on it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any

__all__ = ["AsyncSingleFlight", "SingleFlight", "run_now", "run_soon"]


class AsyncSingleFlight[T]:
    def __init__(self) -> None:
        self._pending: asyncio.Future[T] | None = None

    @property
    def active(self) -> bool:
        return self._pending is not None

    async def run(self, factory: Callable[[], Awaitable[T]]) -> T:
        if self._pending is not None:
            return await asyncio.shield(self._pending)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        self._pending = future
        try:
            result = await factory()
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # consumed: a failure nobody else awaited is not "never retrieved"
            raise
        else:
            future.set_result(result)
            return result
        finally:
            self._pending = None

    async def wait(self) -> None:
        """Wait for the in-flight operation, if any, ignoring its outcome."""
        pending = self._pending
        if pending is not None:
            try:
                await asyncio.shield(pending)
            except Exception:
                return


class _Flight[T]:
    __slots__ = ("done", "error", "value")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: T | None = None
        self.error: BaseException | None = None


class SingleFlight[T]:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flight: _Flight[T] | None = None

    @property
    def active(self) -> bool:
        return self._flight is not None

    def run(self, factory: Callable[[], T]) -> T:
        with self._lock:
            flight = self._flight
            leader = flight is None
            if flight is None:
                flight = self._flight = _Flight()
        if not leader:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            return flight.value  # type: ignore[return-value]
        try:
            flight.value = factory()
            return flight.value
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            with self._lock:
                self._flight = None
            flight.done.set()

    def wait(self) -> None:
        flight = self._flight
        if flight is not None:
            flight.done.wait()


_background: set[asyncio.Task[Any]] = set()
_logger = logging.getLogger("crowdypy")


def run_soon(job: Callable[[], Awaitable[Any]]) -> None:
    """Start ``job`` on the running loop without waiting for it; a failure is logged.

    scripts/unasync.py maps this to :func:`run_now`, which runs the job inline in the
    blocking client.
    """

    async def guarded() -> None:
        try:
            await job()
        except Exception:
            _logger.exception("background job failed")

    task = asyncio.get_running_loop().create_task(guarded())
    _background.add(task)
    task.add_done_callback(_background.discard)


def run_now(job: Callable[[], Any]) -> None:
    """Run ``job`` now; a failure is logged (the blocking twin of :func:`run_soon`)."""
    try:
        job()
    except Exception:
        _logger.exception("background job failed")
