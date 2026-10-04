"""The client's auth state: the session store under the name the docs use."""

from __future__ import annotations

from collections.abc import Callable

from .session import SessionListener, SessionStore, TokenStore

__all__ = ["AuthState", "AuthStateListener"]

AuthStateListener = SessionListener


class AuthState(SessionStore):
    """The session store every sub-client of one client shares."""

    def __init__(self, token_store: TokenStore | None = None) -> None:
        super().__init__(token_store)

    def subscribe(self, listener: AuthStateListener) -> Callable[[], None]:
        return self.on_change(listener)
