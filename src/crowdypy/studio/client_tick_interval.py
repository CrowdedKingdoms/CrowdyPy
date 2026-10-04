"""A CLIENT half's tick cadence, from ``[package.metadata.crowdy] tick_interval_ms`` in its
Cargo.toml (CrowdyJS's ``parseClientTickIntervalMs``)."""

from __future__ import annotations

import re

__all__ = [
    "DEFAULT_CLIENT_TICK_INTERVAL_MS",
    "MAX_CLIENT_TICK_INTERVAL_MS",
    "MIN_CLIENT_TICK_INTERVAL_MS",
    "parse_client_tick_interval_ms",
]

DEFAULT_CLIENT_TICK_INTERVAL_MS = 1_000
MIN_CLIENT_TICK_INTERVAL_MS = 16
MAX_CLIENT_TICK_INTERVAL_MS = 1_000

_LINE = re.compile(r"^[ \t]*tick_interval_ms[ \t]*=[ \t]*(\d+)[ \t]*$", re.MULTILINE)


def parse_client_tick_interval_ms(cargo_toml: str | None) -> int:
    """The interval in milliseconds, clamped to 16-1000; 1000 when the line is absent."""
    match = _LINE.search(cargo_toml or "")
    if not match:
        return DEFAULT_CLIENT_TICK_INTERVAL_MS
    value = int(match[1])
    if value <= 0 or value > 2**53 - 1:
        return DEFAULT_CLIENT_TICK_INTERVAL_MS
    return min(MAX_CLIENT_TICK_INTERVAL_MS, max(MIN_CLIENT_TICK_INTERVAL_MS, value))
