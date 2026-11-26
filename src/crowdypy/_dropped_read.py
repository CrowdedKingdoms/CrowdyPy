"""Which failed reads the SDK's own waits read again: the Studio controller's build wait and
``wait_for_mod_build``. Not exported. CrowdyJS (``src/dropped-read.ts``) and CrowdyCPP
(``crowdy/graphql/dropped_read.hpp``) apply the same rule; change all three together."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from crowdypy.errors import (
    CrowdyHttpError,
    CrowdyNetworkError,
    CrowdyTimeoutError,
    player_fault_of,
    retry_after_ms_of,
)

__all__ = ["is_dropped_read", "retry_after_ms_of"]


def is_dropped_read(error: BaseException) -> bool:
    """Whether a failed read is worth reading again: nothing came back (a network failure, a
    body that does not parse, or a timeout), what came back is not the API's answer (an HTTP
    5xx or 429 without GraphQL errors), or the API said it was busy (a refusal it marks
    retryable and blames on the platform, ``PLATFORM_BUSY``). A GraphQL refusal, any other HTTP
    status, a ``CrowdyProtocolError`` (the SDK refused what came back) and a cancellation are
    answers, and reading again would only repeat them."""
    if isinstance(error, (CrowdyNetworkError, CrowdyTimeoutError)):
        return True
    if isinstance(error, CrowdyHttpError):
        if error.status < 500 and error.status != 429:
            return False
        errors = _graphql_errors_in(error.body)
        return errors is None or _is_busy(errors[0])
    return _is_busy(error)


def _is_busy(value: object) -> bool:
    fault = player_fault_of(value)
    return fault is not None and fault.blame == "PLATFORM" and fault.retryable


def _graphql_errors_in(body: str) -> list[Mapping[str, Any]] | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    errors = parsed.get("errors") if isinstance(parsed, dict) else None
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], Mapping):
        return None
    return errors
