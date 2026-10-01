"""The sticky load-balancer cookie (``cks_ga``).

A client's HTTP requests and its realtime socket must land on the same API instance:
the realtime session is per-process, and a mutation answered by one instance never
reaches a subscription held by another. The load balancer pins a client with this
cookie, so the SDK carries it explicitly on both transports and nothing else.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["LB_COOKIE_NAME", "LbCookieStore"]

LB_COOKIE_NAME = "cks_ga"


class LbCookieStore:
    def __init__(self) -> None:
        self._value: str | None = None

    def header_value(self) -> str | None:
        """The ``Cookie`` header value to send, or ``None`` before the LB has set one."""
        return f"{LB_COOKIE_NAME}={self._value}" if self._value else None

    def ingest_set_cookie(self, set_cookie_headers: Iterable[str]) -> None:
        """Remember ``cks_ga`` from a response's ``Set-Cookie`` headers; ignore the rest."""
        for raw in set_cookie_headers:
            pair = raw.split(";", 1)[0].strip()
            if pair.startswith(f"{LB_COOKIE_NAME}="):
                self._value = pair[len(LB_COOKIE_NAME) + 1 :]

    def get_value(self) -> str | None:
        return self._value
