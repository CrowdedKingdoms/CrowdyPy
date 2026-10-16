"""The kit's observable value: ``get`` the current value, ``subscribe`` to hear each new one.
CrowdyJS ``src/kit/store.ts``."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from typing import Any, Protocol

__all__ = ["KitStore", "WritableKitStore"]


class KitStore[T](Protocol):
    def get(self) -> T: ...

    def subscribe(self, listener: Callable[[Any], Any]) -> Callable[[], None]:
        """Calls ``listener`` with every new value; returns the function that stops it."""
        ...


class WritableKitStore[T]:
    """A :class:`KitStore` its owner sets."""

    def __init__(self, initial: T) -> None:
        self._value = initial
        self._listeners: list[Callable[[T], Any]] = []
        self._lock = threading.Lock()

    def get(self) -> T:
        return self._value

    def subscribe(self, listener: Callable[[T], Any]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def stop() -> None:
            with self._lock, contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return stop

    def set(self, value: T) -> None:
        with self._lock:
            self._value = value
            listeners = list(self._listeners)
        for listener in listeners:
            with contextlib.suppress(Exception):  # a listener's exception is its own
                listener(value)
