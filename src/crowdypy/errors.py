"""Structured errors raised by CrowdyPy.

Every failure the SDK raises is a :class:`CrowdyError`. Transport problems
(:class:`CrowdyHttpError`, :class:`CrowdyNetworkError`, :class:`CrowdyTimeoutError`)
are distinct from API problems (:class:`CrowdyGraphQLError`), so a network blip can be
retried without retrying a rejected mutation.

Branch on the stable ``extensions["code"]`` (``UNAUTHENTICATED``, ``SCOPE_MISSING``,
``FORBIDDEN``, ``IDEMPOTENCY_CONFLICT``, ``RATE_LIMITED``, ...) rather than on a message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "ACCESS_NOT_GRANTED_CODE",
    "ACCESS_REVOKED_CODE",
    "ACCESS_SUSPENDED_CODE",
    "ACTOR_EXISTS_CODE",
    "APP_PAUSED_CODE",
    "APP_UNAVAILABLE_CODE",
    "WRONG_DATACENTER_CODE",
    "CrowdyAccessRefusal",
    "CrowdyActorExists",
    "CrowdyAppPaused",
    "CrowdyAppUnavailableError",
    "CrowdyError",
    "CrowdyFaultBlame",
    "CrowdyGraphQLError",
    "CrowdyHttpError",
    "CrowdyNetworkError",
    "CrowdyPlayerFault",
    "CrowdyProtocolError",
    "CrowdyRealtimeError",
    "CrowdyReplicationError",
    "CrowdyTimeoutError",
    "CrowdyUserCodeFaultError",
    "GridScopeError",
    "access_refusal_of",
    "actor_exists_of",
    "app_paused_of",
    "is_app_paused",
    "player_fault_of",
]

#: The app's datacenter is not serving clients. Carries no endpoint, on purpose.
APP_UNAVAILABLE_CODE = "APP_UNAVAILABLE"
#: The app lives in another datacenter; the error names the endpoint to move to.
WRONG_DATACENTER_CODE = "WRONG_DATACENTER"
#: An actor with this uuid already exists (HTTP 409); see :func:`actor_exists_of`.
ACTOR_EXISTS_CODE = "ACTOR_EXISTS"
#: The player's access to the app was revoked (HTTP 403).
ACCESS_REVOKED_CODE = "ACCESS_REVOKED"
#: The player is suspended from the app until a time (HTTP 403).
ACCESS_SUSPENDED_CODE = "ACCESS_SUSPENDED"
#: The player holds no access to the app (HTTP 403).
ACCESS_NOT_GRANTED_CODE = "ACCESS_NOT_GRANTED"
#: The app is paused (HTTP 403); see :func:`app_paused_of`.
APP_PAUSED_CODE = "APP_PAUSED"

CrowdyFaultBlame = Literal["PLATFORM", "AUTHOR", "BUDGET"]


class CrowdyError(Exception):
    """Base class for every error the SDK raises."""

    def __init__(self, message: str, *, cause: BaseException | object | None = None) -> None:
        super().__init__(message)
        self.message = message
        #: The underlying cause, if this error wraps another.
        self.cause = cause
        if isinstance(cause, BaseException):
            self.__cause__ = cause


class CrowdyHttpError(CrowdyError):
    """A GraphQL endpoint answered with a non-2xx HTTP status."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class CrowdyGraphQLError(CrowdyError):
    """A 200 response whose ``errors`` array was non-empty: the primary API error."""

    def __init__(self, errors: Sequence[Mapping[str, Any]]) -> None:
        super().__init__("; ".join(str(e.get("message", "")) for e in errors))
        #: Every GraphQL error entry from the response, in server order.
        self.graphql_errors: list[Mapping[str, Any]] = list(errors)

    @property
    def extensions(self) -> Mapping[str, Any] | None:
        """The ``extensions`` of the first error (``code``, ``remediation``, ...)."""
        if not self.graphql_errors:
            return None
        extensions = self.graphql_errors[0].get("extensions")
        return extensions if isinstance(extensions, Mapping) else None

    @property
    def code(self) -> Any:
        """The stable machine-readable code of the first error, or ``None``."""
        extensions = self.extensions
        return extensions.get("code") if extensions else None


class CrowdyAppUnavailableError(CrowdyGraphQLError):
    """The app's own datacenter is not serving clients.

    Nothing the client can do fixes it, so stop retrying in a loop and show
    ``message``, which the server writes to be shown to a player as-is. It names no
    endpoint; do not fall back to a cached one, because that one is in the
    datacenter that is down.
    """

    @property
    def app_id(self) -> str | None:
        value = (self.extensions or {}).get("appId")
        return value if isinstance(value, str) else None

    @property
    def app_datacenter(self) -> str | None:
        """Diagnostic only; do not show it to a player or try to reach it."""
        value = (self.extensions or {}).get("appDatacenter")
        return value if isinstance(value, str) else None

    @property
    def retryable(self) -> bool:
        return (self.extensions or {}).get("retryable") is not False


@dataclass(frozen=True, slots=True)
class CrowdyPlayerFault:
    """What the platform says about a failure in app-authored code."""

    #: A stable, enumerated reason (an open set: keep a default branch).
    code: str
    #: Whose problem it is.
    blame: CrowdyFaultBlame
    #: Whether repeating the identical call could succeed with nothing else changing.
    retryable: bool
    #: Why an open circuit opened, when the server knows (``watchdog_timeout``).
    cause: str | None = None


def _fault_fields(code: Any, blame: Any, retryable: Any, cause: Any) -> CrowdyPlayerFault:
    return CrowdyPlayerFault(
        code=code if isinstance(code, str) else "PLATFORM_ERROR",
        blame=blame,
        retryable=retryable is True,
        cause=cause if isinstance(cause, str) and cause else None,
    )


def _fault_from_extensions(extensions: Mapping[str, Any] | None) -> CrowdyPlayerFault | None:
    if not extensions or not isinstance(extensions.get("blame"), str):
        return None
    return _fault_fields(
        extensions.get("code"),
        extensions["blame"],
        extensions.get("retryable"),
        extensions.get("cause"),
    )


class CrowdyUserCodeFaultError(CrowdyGraphQLError):
    """A call into code the platform did not write failed, with blame attributed.

    The message is platform-authored and safe to show; you are expected to replace it
    with your own wording, branching on :attr:`fault`.
    """

    @property
    def fault(self) -> CrowdyPlayerFault:
        extensions = self.extensions or {}
        return _fault_fields(
            extensions.get("code"),
            extensions.get("blame", "PLATFORM"),
            extensions.get("retryable"),
            extensions.get("cause"),
        )

    @property
    def blame(self) -> CrowdyFaultBlame:
        return self.fault.blame

    @property
    def retryable(self) -> bool:
        return self.fault.retryable


def player_fault_of(value: object) -> CrowdyPlayerFault | None:
    """Read the platform's attribution from either carrier, so a game branches once.

    Accepts a raised :class:`CrowdyGraphQLError`, a result carrying ``fault`` in band,
    or one raw GraphQL error entry. ``None`` means "not a question about whose code
    failed".
    """
    if isinstance(value, CrowdyGraphQLError):
        return _fault_from_extensions(value.extensions)
    if not isinstance(value, Mapping):
        return None
    fault = value.get("fault")
    if isinstance(fault, Mapping) and isinstance(fault.get("blame"), str):
        return _fault_fields(
            fault.get("code"), fault["blame"], fault.get("retryable"), fault.get("cause")
        )
    extensions = value.get("extensions")
    if isinstance(extensions, Mapping):
        return _fault_from_extensions(extensions)
    return None


@dataclass(frozen=True, slots=True)
class CrowdyActorExists:
    """An ``ACTOR_EXISTS`` refusal: creating an actor whose uuid is taken."""

    #: ``True``: the actor is already yours (a retried create; read it rather than failing).
    #: ``False``: another player holds the uuid. ``None``: the server did not say.
    owned_by_caller: bool | None


@dataclass(frozen=True, slots=True)
class CrowdyAccessRefusal:
    """A refusal of the player's own access to an app. Retrying fixes none of them."""

    code: Literal["ACCESS_REVOKED", "ACCESS_SUSPENDED", "ACCESS_NOT_GRANTED"]
    #: On ``ACCESS_SUSPENDED``: when access returns by itself (ISO-8601).
    suspended_until: str | None = None


@dataclass(frozen=True, slots=True)
class CrowdyAppPaused:
    """An ``APP_PAUSED`` refusal: the app's runtime gate is not ``ACTIVE``."""

    #: ``insufficient_funds``, ``spend_cap`` or ``subscription_lapsed`` (an open set), or
    #: ``None`` when the server did not say.
    reason: str | None


def _extensions_with_code(value: object, code: str) -> Mapping[str, Any] | None:
    """The extensions of the first error entry carrying ``code``, from a raised
    :class:`CrowdyGraphQLError` or one raw GraphQL error entry."""
    if isinstance(value, CrowdyGraphQLError):
        entries: Sequence[Any] = value.graphql_errors
    elif isinstance(value, Mapping):
        entries = [value]
    else:
        return None
    for entry in entries:
        extensions = entry.get("extensions") if isinstance(entry, Mapping) else None
        if isinstance(extensions, Mapping) and extensions.get("code") == code:
            return extensions
    return None


def actor_exists_of(value: object) -> CrowdyActorExists | None:
    """An ``ACTOR_EXISTS`` refusal from a raised error or a raw error entry, or ``None``."""
    extensions = _extensions_with_code(value, ACTOR_EXISTS_CODE)
    if extensions is None:
        return None
    owned = extensions.get("ownedByCaller")
    return CrowdyActorExists(owned if isinstance(owned, bool) else None)


def access_refusal_of(value: object) -> CrowdyAccessRefusal | None:
    """An access refusal (``ACCESS_REVOKED``, ``ACCESS_SUSPENDED`` or ``ACCESS_NOT_GRANTED``,
    all HTTP 403) from a raised error or a raw error entry, or ``None``. None of them is
    fixed by retrying; tell the player."""
    codes: tuple[Literal["ACCESS_SUSPENDED", "ACCESS_REVOKED", "ACCESS_NOT_GRANTED"], ...] = (
        "ACCESS_SUSPENDED",
        "ACCESS_REVOKED",
        "ACCESS_NOT_GRANTED",
    )
    for code in codes:
        extensions = _extensions_with_code(value, code)
        if extensions is None:
            continue
        until = extensions.get("suspendedUntil")
        return CrowdyAccessRefusal(code, until if isinstance(until, str) and until else None)
    return None


def app_paused_of(value: object) -> CrowdyAppPaused | None:
    """An ``APP_PAUSED`` refusal (HTTP 403: the app's runtime gate is not ``ACTIVE``) from a
    raised error or a raw error entry, or ``None``. Show the player that the world is paused
    instead of retrying in a loop."""
    extensions = _extensions_with_code(value, APP_PAUSED_CODE)
    if extensions is None:
        return None
    reason = extensions.get("reason")
    return CrowdyAppPaused(reason if isinstance(reason, str) and reason else None)


def is_app_paused(gate: object) -> bool:
    """Whether an app's runtime gate (``AppTokenResponse.runtime_gate``, the
    ``runtimeGate`` of ``server_status.game_client_bootstrap``) says the app is paused: any
    status but ``ACTIVE``. A missing gate (an older server) reads as not paused. A paused app
    still mints tokens, so check before entering the world."""
    if gate is None:
        return False
    status = gate.get("status") if isinstance(gate, Mapping) else getattr(gate, "status", None)
    return status != "ACTIVE"


class CrowdyNetworkError(CrowdyError):
    """A failure before any HTTP response arrived (DNS, TLS, refused). Retryable."""

    def __init__(self, cause: BaseException | object) -> None:
        super().__init__(f"Network error: {cause}", cause=cause)


class CrowdyTimeoutError(CrowdyError):
    """An HTTP request exceeded the configured timeout.

    Safe to retry for idempotent operations and for mutations sent with an
    ``idempotency_key``: the server replays the first result.
    """

    def __init__(self, timeout_ms: float) -> None:
        super().__init__(f"Request timed out after {timeout_ms:g}ms")
        self.timeout_ms = timeout_ms


class CrowdyRealtimeError(CrowdyError):
    """A realtime failure: a subscription refused or dropped, or a wait timed out."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        cause: BaseException | object | None = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.code = code
        self.retryable = retryable


class CrowdyReplicationError(CrowdyError):
    """The native replication client refused an operation.

    ``code`` is the CrowdyCPP error name: ``NotConnected``, ``InvalidArgument``, ``Timeout``,
    ``SocketError``, ``WouldBlock`` (the kernel's send buffer was full and nothing was sent;
    retry shortly), ...
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class GridScopeError(CrowdyError):
    """Raised locally, before any request, when a ``GridScope`` call would leave the grid."""


class CrowdyProtocolError(CrowdyError):
    """A response or input failed the SDK's structural validation."""
