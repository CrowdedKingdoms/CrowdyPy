"""The balancer cookie is forgotten when a draining instance expires it, or on clear()."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from crowdypy.lb_cookie import LbCookieStore

PINNED = "cks_ga=abc123; Path=/; HttpOnly; Secure; SameSite=None"


@pytest.mark.parametrize(
    "expiring",
    [
        # Exactly what a draining game-api sends.
        "cks_ga=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; "
        "HttpOnly; Secure; SameSite=None",
        "cks_ga=old; Path=/; Max-Age=0",
        "cks_ga=old; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
        "cks_ga=; Path=/",
    ],
)
def test_an_expired_cookie_is_forgotten(expiring: str) -> None:
    store = LbCookieStore()
    store.ingest_set_cookie([PINNED])
    assert store.get_value() == "abc123"
    store.ingest_set_cookie([expiring])
    assert store.header_value() is None


def test_a_cookie_that_expires_later_is_kept_and_clear_forgets_it() -> None:
    store = LbCookieStore()
    later = format_datetime(datetime.now(UTC) + timedelta(hours=1), usegmt=True)
    store.ingest_set_cookie([f"cks_ga=fresh; Path=/; Expires={later}"])
    assert store.get_value() == "fresh"
    store.clear()
    assert store.header_value() is None
