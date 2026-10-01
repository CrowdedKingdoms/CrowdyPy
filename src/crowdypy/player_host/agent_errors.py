"""The agent error contract (CrowdyJS's ``player-host/agent-errors``): closed codes, and
messages sanitized before anyone sees them (control characters blanked, secrets after
``bearer``, ``token``, ``secret`` or ``api key`` redacted, at most 512 UTF-16 units)."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict, get_args

from crowdypy.errors import CrowdyError

__all__ = [
    "CROWDY_AGENT_ERROR_CODES",
    "AgentErrorV1",
    "CrowdyAgentError",
    "CrowdyAgentErrorCode",
    "CrowdyAgentOutcomeUnknownError",
    "to_agent_error",
]

CrowdyAgentErrorCode = Literal[
    "AGENT_DISABLED",
    "AGENT_UNAUTHENTICATED",
    "AGENT_PERMISSION_DENIED",
    "AGENT_SCOPE_DENIED",
    "AGENT_CONTEXT_CHANGED",
    "AGENT_CONTEXT_STALE",
    "AGENT_SESSION_NOT_FOUND",
    "AGENT_SESSION_CLOSED",
    "AGENT_RUN_ALREADY_ACTIVE",
    "AGENT_RUN_NOT_ACTIVE",
    "AGENT_CANCELLED",
    "AGENT_PREEMPTED",
    "AGENT_OPERATOR_KILLED",
    "AGENT_DISCONNECTED",
    "AGENT_CLIENT_REATTACHED",
    "AGENT_CLIENT_EPOCH_STALE",
    "AGENT_EVENT_CURSOR_INVALID",
    "AGENT_EVENT_GAP",
    "AGENT_MODEL_NOT_ALLOWED",
    "AGENT_PROVIDER_POLICY_UNSATISFIED",
    "AGENT_PROVIDER_UNAVAILABLE",
    "AGENT_PROVIDER_OUTPUT_INVALID",
    "AGENT_PROVIDER_USAGE_UNAVAILABLE",
    "AGENT_BUDGET_EXHAUSTED",
    "AGENT_QUOTA_EXHAUSTED",
    "AGENT_RATE_LIMITED",
    "AGENT_TOOL_UNKNOWN",
    "AGENT_TOOL_VERSION_UNSUPPORTED",
    "AGENT_TOOL_INPUT_INVALID",
    "AGENT_TOOL_OUTPUT_INVALID",
    "AGENT_TOOL_DESCRIPTOR_INVALID",
    "AGENT_TOOL_FAILED",
    "AGENT_TOOL_TIMEOUT",
    "AGENT_TOOL_OUTCOME_UNKNOWN",
    "AGENT_HOST_UNAVAILABLE",
    "AGENT_HOST_CAPABILITY_CHANGED",
    "AGENT_OBSERVATION_STALE",
    "AGENT_CONTROL_TARGET_CHANGED",
    "AGENT_PARALLEL_TOOL_CALLS_UNSUPPORTED",
    "AGENT_APPROVAL_REQUIRED",
    "AGENT_APPROVAL_MISMATCH",
    "AGENT_APPROVAL_EXPIRED",
    "AGENT_APPROVAL_DENIED",
    "AGENT_APPROVAL_REVOKED",
    "AGENT_LEASE_REQUIRED",
    "AGENT_LEASE_EXPIRED",
    "AGENT_LEASE_REVOKED",
    "AGENT_LEASE_SCOPE_MISSING",
    "AGENT_IDEMPOTENCY_CONFLICT",
    "AGENT_CHECKPOINT_NOT_FOUND",
    "CROWDY_STUDIO_REVISION_CONFLICT",
]
CROWDY_AGENT_ERROR_CODES: tuple[CrowdyAgentErrorCode, ...] = get_args(CrowdyAgentErrorCode)


class AgentErrorV1(TypedDict):
    code: CrowdyAgentErrorCode
    message: str
    retryable: bool
    remediation: NotRequired[str]
    field: NotRequired[str]
    requiredScope: NotRequired[str]


class CrowdyAgentError(CrowdyError):
    """An agent-facing failure with a closed ``code``; ``to_json()`` is its wire form."""

    def __init__(
        self,
        code: CrowdyAgentErrorCode,
        message: str,
        *,
        retryable: bool = False,
        remediation: str | None = None,
        field: str | None = None,
        required_scope: str | None = None,
        cause: BaseException | object | None = None,
    ) -> None:
        super().__init__(_sanitize(message), cause=cause)
        self.code: CrowdyAgentErrorCode = code
        self.retryable = retryable
        self.remediation = _sanitize(remediation) if remediation else None
        self.field = _utf16_prefix(field, 256) if field is not None else None
        self.required_scope = (
            _utf16_prefix(required_scope, 80) if required_scope is not None else None
        )

    def to_json(self) -> AgentErrorV1:
        out: AgentErrorV1 = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.remediation:
            out["remediation"] = self.remediation
        if self.field:
            out["field"] = self.field
        if self.required_scope:
            out["requiredScope"] = self.required_scope
        return out


class CrowdyAgentOutcomeUnknownError(CrowdyAgentError):
    """The effect may have happened: inspect the current state before continuing."""

    def __init__(
        self, message: str = "The tool effect may have occurred; inspect current state"
    ) -> None:
        super().__init__(
            "AGENT_TOOL_OUTCOME_UNKNOWN",
            message,
            remediation="Inspect the current project or game state before continuing.",
        )


def to_agent_error(
    error: Any, fallback: CrowdyAgentErrorCode = "AGENT_TOOL_FAILED"
) -> AgentErrorV1:
    """An error's agent form; anything but a :class:`CrowdyAgentError` is opaque."""
    if isinstance(error, CrowdyAgentError):
        return error.to_json()
    return {"code": fallback, "message": "Agent operation failed", "retryable": False}


# ---------------------------------------------------------------------- sanitizing

_CONTROL = {*range(0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F}
_SPACES = {
    0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B),
    0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF,
}  # fmt: skip


def _sanitize(value: str) -> str:
    cleaned = "".join(" " if ord(ch) in _CONTROL else ch for ch in value)
    return _utf16_prefix(_js_trim(_redact(cleaned)), 512) or "Agent operation failed"


def _js_trim(value: str) -> str:
    start, end = 0, len(value)
    while start < end and ord(value[start]) in _SPACES:
        start += 1
    while end > start and ord(value[end - 1]) in _SPACES:
        end -= 1
    return value[start:end]


def _utf16_prefix(value: str, units: int) -> str:
    """``value.slice(0, units)`` in JavaScript: a cut through a surrogate pair keeps the
    lone high surrogate, as JavaScript does."""
    encoded = value.encode("utf-16-le", "surrogatepass")
    if len(encoded) <= units * 2:
        return value
    return encoded[: units * 2].decode("utf-16-le", "surrogatepass")


def _redact(value: str) -> str:
    parts: list[str] = []
    i = plain_start = 0
    while i < len(value):
        end = _secret_span_end(value, i)
        if end > i:
            if i > plain_start:
                parts.append(value[plain_start:i])
            parts.append("[redacted]")
            i = plain_start = end
            continue
        i += 1
    if plain_start == 0:
        return value
    if plain_start < len(value):
        parts.append(value[plain_start:])
    return "".join(parts)


def _secret_span_end(value: str, index: int) -> int:
    after_keyword = _keyword_end(value, index)
    if after_keyword < 0:
        return -1
    j = after_keyword
    while j < len(value) and ord(value[j]) in _SPACES:
        j += 1
    if j < len(value) and value[j] in ":=":
        k = j + 1
        while k < len(value) and ord(value[k]) in _SPACES:
            k += 1
        with_separator = _token_end(value, k)
        if with_separator > 0:
            return with_separator
    return _token_end(value, j)


def _keyword_end(value: str, index: int) -> int:
    if index > 0 and _word_char(value[index - 1]):
        return -1
    for word in ("bearer", "token", "secret"):
        if _equals_ascii(value, index, word):
            return index + len(word)
    if not _equals_ascii(value, index, "api"):
        return -1
    separator = index + 3
    if (
        separator < len(value)
        and value[separator] in "_ -"
        and _equals_ascii(value, separator + 1, "key")
    ):
        return separator + 4
    if _equals_ascii(value, index + 3, "key"):
        return index + 6
    return -1


def _token_end(value: str, start: int) -> int:
    k = start
    while k < len(value) and ord(value[k]) not in _SPACES and value[k] not in ",;":
        k += 1
    return k if k > start else -1


def _equals_ascii(value: str, index: int, word: str) -> bool:
    if index + len(word) > len(value):
        return False
    return all(value[index + k].lower() == word[k] for k in range(len(word)))


def _word_char(ch: str) -> bool:
    return ch == "_" or ("0" <= ch <= "9") or ("A" <= ch <= "Z") or ("a" <= ch <= "z")
