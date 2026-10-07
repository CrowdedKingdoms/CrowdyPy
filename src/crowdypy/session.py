"""Token persistence and the per-client session store.

Two credentials, two scopes. An identity **session** token is one per API origin and
is shared by every game that talks to it; an app-scoped **gameplay** token is one per
app and authorises only that app. Name their files with :meth:`FileTokenStore.session_path`
and :meth:`FileTokenStore.app_path` rather than inventing a scheme: keying a session by
anything per-game is the bug that left players signed in to one game anonymous to the
next.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "FileTokenStore",
    "MemoryTokenStore",
    "SessionListener",
    "SessionStore",
    "TokenStore",
]

SessionListener = Callable[[str | None], None]


@runtime_checkable
class TokenStore(Protocol):
    """Pluggable token persistence. ``get`` returns ``None`` when nothing is stored."""

    def get(self) -> str | None: ...

    def set(self, token: str) -> None: ...

    def clear(self) -> None: ...


class MemoryTokenStore:
    """Keeps the token in memory only; nothing outlives the process."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token

    def get(self) -> str | None:
        return self._token

    def set(self, token: str) -> None:
        self._token = token

    def clear(self) -> None:
        self._token = None


def _slug(value: str) -> str:
    """CrowdyCPP's FileTokenStore slug: scheme dropped, unsafe characters to ``_``."""
    scheme = value.find("://")
    if scheme != -1:
        value = value[scheme + 3 :]
    out = "".join(c if (c.isascii() and (c.isalnum() or c in ".-_")) else "_" for c in value)
    return out.rstrip("_")


class FileTokenStore:
    """Stores one token in one file, readable by the owner only (mode 0600).

    Writes are atomic (a temporary file in the same directory, then a rename), so a
    crash mid-write never leaves a truncated token behind.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    @staticmethod
    def session_path(directory: str | os.PathLike[str], api_origin: str) -> Path:
        """The file for an IDENTITY session token: one per origin, shared by every game."""
        return Path(directory) / f"crowdy-session-{_slug(api_origin)}"

    @staticmethod
    def app_path(directory: str | os.PathLike[str], app_id: str | int) -> Path:
        """The file for an app-scoped GAMEPLAY token: one per app, never shared."""
        return Path(directory) / f"crowdy-app-{_slug(str(app_id))}"

    def get(self) -> str | None:
        try:
            token = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return None
        return token[0] if token and token[0] else None

    def set(self, token: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".crowdy-", dir=self.path.parent)
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class SessionStore:
    """The one token a client holds, observed by its HTTP transport and realtime sockets.

    Every request reads the token fresh from here, so HTTP and realtime auth never drift
    within a client. Listeners fire on every change, including clears (``None``).
    """

    def __init__(self, token_store: TokenStore | None = None) -> None:
        self._token: str | None = None
        self._listeners: list[SessionListener] = []
        self._token_store = token_store

    def restore(self) -> str | None:
        """Load the persisted token (without writing it back) and return it."""
        token = self._token_store.get() if self._token_store is not None else None
        self.set_token(token, persist=False)
        return token

    def get_token(self) -> str | None:
        return self._token

    def set_token(self, token: str | None, *, persist: bool = True) -> None:
        if token == self._token:
            return
        self._token = token
        if persist and self._token_store is not None:
            if token:
                self._token_store.set(token)
            else:
                self._token_store.clear()
        for listener in list(self._listeners):
            listener(token)

    def clear(self) -> None:
        self.set_token(None)

    def on_change(self, listener: SessionListener) -> Callable[[], None]:
        """Observe token changes; ``listener`` is called immediately with the current one."""
        self._listeners.append(listener)
        listener(self._token)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe
