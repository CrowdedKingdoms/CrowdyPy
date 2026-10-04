"""Which hosts a server may redirect this client to.

A redirect (a reconnect directive, a datacenter move) is followed only within the same
registrable estate: the same host, or the same last two DNS labels. The rule matches
CrowdyJS's and CrowdyCPP's ``isSameEstate`` exactly, including the refusal of a bare
single-label host and the userinfo trick (``wss://evil.example@ck.example.com``).
"""

from __future__ import annotations

from urllib.parse import urlsplit

__all__ = ["estate_hostname", "is_same_estate"]


def estate_hostname(url: str) -> str | None:
    """The lowercased host of an absolute URL, or ``None`` when it has none."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    host = parts.hostname
    if not host:
        return None
    return f"[{host}]" if ":" in host else host.lower()


def _site(host: str) -> str:
    return ".".join(host.split(".")[-2:])


def is_same_estate(current: str, candidate: str) -> bool:
    a = estate_hostname(current)
    b = estate_hostname(candidate)
    if a is None or b is None:
        return False
    if a == b:
        return True
    return _site(a) == _site(b) and "." in _site(a)
