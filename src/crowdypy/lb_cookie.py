"""The sticky load-balancer cookie (``cks_ga``).

A client's HTTP requests and its realtime socket must land on the same API instance:
the realtime session is per-process, and a mutation answered by one instance never
reaches a subscription held by another. The load balancer pins a client with this
cookie, so the SDK carries it explicitly on both transports and nothing else.

A draining API instance expires the cookie on its responses so the next balancer
request is placed on an instance that is staying; an expired cookie is forgotten here
the way a browser forgets it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

__all__ = ["LB_COOKIE_NAME", "LbCookieStore"]

LB_COOKIE_NAME = "cks_ga"


class LbCookieStore:
    def __init__(self) -> None:
        self._value: str | None = None

    def header_value(self) -> str | None:
        """The ``Cookie`` header value to send, or ``None`` before the LB has set one."""
        return f"{LB_COOKIE_NAME}={self._value}" if self._value else None

    def clear(self) -> None:
        """Forget the affinity: the next balancer request is placed afresh."""
        self._value = None

    def ingest_set_cookie(self, set_cookie_headers: Iterable[str]) -> None:
        """Remember ``cks_ga`` from a response's ``Set-Cookie`` headers; ignore the rest.

        A response that expires it (an empty value, ``Max-Age`` of zero or less, or an
        ``Expires`` in the past) makes the store forget it.
        """
        for raw in set_cookie_headers:
            pair = raw.split(";", 1)[0].strip()
            if pair.startswith(f"{LB_COOKIE_NAME}="):
                value = pair[len(LB_COOKIE_NAME) + 1 :]
                self._value = None if value == "" or _is_expired(raw) else value

    def get_value(self) -> str | None:
        return self._value


def _is_expired(set_cookie: str, now: datetime | None = None) -> bool:
    """True when a ``Set-Cookie`` line's attributes say the cookie is already gone."""
    current = now or datetime.now(UTC)
    for part in set_cookie.split(";")[1:]:
        name, sep, value = part.partition("=")
        if not sep:
            continue
        name = name.strip().lower()
        value = value.strip()
        if name == "max-age":
            try:
                if int(value) <= 0:
                    return True
            except ValueError:
                continue
        elif name == "expires":
            try:
                at = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            if at <= current:
                return True
    return False
