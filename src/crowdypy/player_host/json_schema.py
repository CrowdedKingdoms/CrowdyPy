"""Bounded JSON Schema and canonical JSON (CrowdyJS's ``player-host/json-schema``).

The schema dialect is CrowdyJS's closed subset: every object sets
``additionalProperties: false``, every string, number and array is bounded, and ``oneOf``
must match exactly one branch. Lengths count UTF-16 units and integers stop at 2**53, as in
JavaScript, so a value passes here exactly when it passes in CrowdyJS.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from decimal import Decimal
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Literal

from crowdypy.player_host.agent_errors import CrowdyAgentError, CrowdyAgentErrorCode

__all__ = [
    "FORBIDDEN_AGENT_AUTHORITY_FIELDS",
    "JsonSchema",
    "assert_bounded_json_schema",
    "canonical_json",
    "deep_freeze",
    "digest_canonical_json",
    "is_decimal_string",
    "sha256_digest",
    "validate_json_schema_value",
]

#: A schema in CrowdyJS's dialect, as a mapping.
JsonSchema = Mapping[str, Any]
Direction = Literal["INPUT", "OUTPUT", "SCHEMA"]

_MAX_DEPTH = 12
_MAX_NODES = 4_096
_MAX_BYTES = 1_048_576
_SAFE = 2**53 - 1
_DECIMAL = re.compile(r"(0|-[1-9][0-9]*|[1-9][0-9]*)")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", re.IGNORECASE
)
_DATE_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z")

FORBIDDEN_AGENT_AUTHORITY_FIELDS: tuple[str, ...] = (
    "userId",
    "ownerUserId",
    "appId",
    "projectId",
    "gridId",
    "sessionId",
    "runId",
    "toolCallId",
    "clientEpoch",
    "leaseId",
    "lease",
    "approval",
    "approvalGrant",
    "argumentHash",
    "descriptorDigest",
    "idempotencyKey",
    "deadline",
    "token",
    "authorization",
    "headers",
    "endpoint",
    "url",
    "permissions",
    "authority",
)
_FORBIDDEN = frozenset(
    re.sub(r"[^A-Za-z0-9]", "", f).lower() for f in FORBIDDEN_AGENT_AUTHORITY_FIELDS
)


# ---------------------------------------------------------------------- canonical JSON


def canonical_json(value: Any) -> str:
    """Keys sorted (by UTF-16 units), no whitespace, strings escaped as ``JSON.stringify``
    escapes them. Non-finite numbers, cycles and non-JSON values are refused."""
    return _canonical(value, set())


def sha256_digest(value: str | bytes) -> str:
    """``sha256:`` and the hex digest of the UTF-8 text (or the bytes)."""
    data = value.encode("utf-8", "surrogatepass") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_canonical_json(value: Any) -> str:
    return sha256_digest(canonical_json(value))


def deep_freeze(value: Any) -> Any:
    """A read-only copy: mappings become ``MappingProxyType``, lists tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(v) for v in value)
    return value


def is_decimal_string(value: str) -> bool:
    """A canonical base-10 integer string (``0``, ``-12``, never ``-0`` or ``012``)."""
    return _DECIMAL.fullmatch(value) is not None


def _canonical(value: Any, ancestors: set[int]) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _js_number(value)
    if not isinstance(value, (list, tuple, Mapping)):
        raise CrowdyAgentError(
            "AGENT_TOOL_INPUT_INVALID",
            f"Non-JSON value of type {type(value).__name__} is forbidden",
        )
    if id(value) in ancestors:
        raise CrowdyAgentError("AGENT_TOOL_INPUT_INVALID", "Cyclic JSON is forbidden")
    ancestors.add(id(value))
    try:
        if isinstance(value, (list, tuple)):
            return "[" + ",".join(_canonical(entry, ancestors) for entry in value) + "]"
        if not all(isinstance(key, str) for key in value):
            raise CrowdyAgentError(
                "AGENT_TOOL_INPUT_INVALID", "Only plain JSON objects are allowed"
            )
        keys = sorted(value, key=_utf16_key)
        return (
            "{"
            + ",".join(
                f"{json.dumps(key, ensure_ascii=False)}:{_canonical(value[key], ancestors)}"
                for key in keys
            )
            + "}"
        )
    finally:
        ancestors.discard(id(value))


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def _js_number(value: float) -> str:
    """``JSON.stringify`` of a number."""
    if isinstance(value, int):
        if abs(value) <= _SAFE:
            return str(value)
        value = float(value)
    if not math.isfinite(value):
        raise CrowdyAgentError("AGENT_TOOL_INPUT_INVALID", "Non-finite numbers are forbidden")
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) <= _SAFE:
        return str(int(value))
    text = repr(value)
    if "e" not in text:
        return text
    mantissa, exponent = text.split("e")
    power = int(exponent)
    if -7 < power < 21:
        return format(Decimal(text), "f")  # JavaScript stays positional in this range
    return f"{mantissa.removesuffix('.0')}e{'-' if power < 0 else '+'}{abs(power)}"


# ---------------------------------------------------------------------- validation


def validate_json_schema_value(
    schema: JsonSchema,
    value: Any,
    *,
    direction: Direction | None = None,
    max_depth: int = _MAX_DEPTH,
    max_nodes: int = _MAX_NODES,
    max_bytes: int = _MAX_BYTES,
) -> None:
    """Raise :class:`CrowdyAgentError` (``AGENT_TOOL_INPUT_INVALID``, or ``..._OUTPUT_...``
    for ``direction="OUTPUT"``) naming the first field that fails."""
    try:
        size = len(canonical_json(value).encode("utf-8", "surrogatepass"))
    except CrowdyAgentError as error:
        raise _failure(direction, "$", error.message) from error
    if size > max_bytes:
        raise _failure(direction, "$", f"encoded value is {size} bytes; maximum is {max_bytes}")
    state = _State(max_nodes, max_depth, direction)
    _validate(schema, value, "$", 0, state)


def assert_bounded_json_schema(schema: JsonSchema, *, reject_authority_fields: bool = True) -> None:
    """Raise ``AGENT_TOOL_DESCRIPTOR_INVALID`` unless ``schema`` is in the bounded dialect
    (and, by default, names no caller-authority field such as ``userId`` or ``token``)."""
    _inspect(schema, "$", 0, set(), reject_authority_fields)


class _State:
    __slots__ = ("ancestors", "direction", "max_depth", "max_nodes", "nodes")

    def __init__(self, max_nodes: int, max_depth: int, direction: Direction | None) -> None:
        self.nodes = 0
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self.direction = direction
        self.ancestors: set[int] = set()

    def branch(self) -> _State:
        copy = _State(self.max_nodes, self.max_depth, self.direction)
        copy.nodes = self.nodes
        copy.ancestors = set(self.ancestors)
        return copy


def _validate(schema: JsonSchema, value: Any, path: str, depth: int, state: _State) -> None:
    state.nodes += 1
    if state.nodes > state.max_nodes:
        raise _failure(state.direction, path, "value has too many nodes")
    if depth > state.max_depth:
        raise _failure(state.direction, path, "value is too deeply nested")
    if "oneOf" in schema:
        matches = 0
        for alternative in schema["oneOf"]:
            try:
                _validate(alternative, value, path, depth, state.branch())
                matches += 1
            except CrowdyAgentError:
                pass
        if matches != 1:
            raise _failure(
                state.direction, path, f"must match exactly one schema (matched {matches})"
            )
        return
    if "const" in schema and not _same(value, schema["const"]):
        raise _failure(state.direction, path, f"must equal {_js_string(schema['const'])}")
    if "enum" in schema and not any(_same(entry, value) for entry in schema["enum"]):
        raise _failure(state.direction, path, "contains an unknown enum value")
    kind = schema.get("type")
    if kind == "null":
        if value is not None:
            raise _failure(state.direction, path, "must be null")
        return
    if kind == "boolean":
        if not isinstance(value, bool):
            raise _failure(state.direction, path, "must be a boolean")
        return
    if kind == "string":
        _validate_string(schema, value, path, state)
        return
    if kind in ("number", "integer"):
        _validate_number(schema, kind, value, path, state)
        return
    if kind == "array":
        _validate_array(schema, value, path, depth, state)
        return
    if not isinstance(value, Mapping) or not all(isinstance(k, str) for k in value):
        raise _failure(state.direction, path, "must be a plain object")
    if kind != "object":
        raise _failure(state.direction, path, "schema type mismatch")
    properties: Mapping[str, Any] = schema["properties"]
    unknown = next((key for key in value if key not in properties), None)
    if unknown is not None:
        raise _failure(state.direction, f"{path}.{unknown}", "unknown fields are forbidden")
    if ("minProperties" in schema and len(value) < schema["minProperties"]) or len(
        value
    ) > schema.get("maxProperties", len(properties)):
        raise _failure(state.direction, path, "object property count is out of bounds")
    for key in schema["required"]:
        if key not in value:
            raise _failure(state.direction, f"{path}.{key}", "required field is missing")
    _enter(value, path, state)
    try:
        for key in value:
            _validate(properties[key], value[key], f"{path}.{key}", depth + 1, state)
    finally:
        state.ancestors.discard(id(value))


def _validate_string(schema: JsonSchema, value: Any, path: str, state: _State) -> None:
    if not isinstance(value, str):
        raise _failure(state.direction, path, "must be a string")
    length = len(value.encode("utf-16-le", "surrogatepass")) // 2
    low, high = schema.get("minLength"), schema.get("maxLength")
    if (low is not None and length < low) or (high is not None and length > high):
        bound = "bounded" if high is None else _js_number(high)
        raise _failure(
            state.direction, path, f"string length must be {_js_number(low or 0)}..{bound}"
        )
    if schema.get("pattern") and not _pattern(schema["pattern"]).search(value):
        raise _failure(state.direction, path, "does not match the required pattern")
    if schema.get("format") == "uuid" and not _UUID.fullmatch(value):
        raise _failure(state.direction, path, "must be a UUID")
    if schema.get("format") == "date-time" and not _utc_date_time(value):
        raise _failure(state.direction, path, "must be a UTC date-time")


def _validate_number(schema: JsonSchema, kind: str, value: Any, path: str, state: _State) -> None:
    number = _finite_number(value)
    if number is None or (kind == "integer" and not _safe_integer(number)):
        raise _failure(state.direction, path, f"must be a finite {kind}")
    low, high = schema.get("minimum"), schema.get("maximum")
    if (low is not None and number < low) or (high is not None and number > high):
        raise _failure(
            state.direction, path, f"must be within {_js_string(low)}..{_js_string(high)}"
        )


def _validate_array(schema: JsonSchema, value: Any, path: str, depth: int, state: _State) -> None:
    if not isinstance(value, (list, tuple)):
        raise _failure(state.direction, path, "must be an array")
    low = schema.get("minItems")
    if (low is not None and len(value) < low) or len(value) > schema["maxItems"]:
        raise _failure(
            state.direction,
            path,
            f"array length must be {_js_number(low or 0)}..{_js_number(schema['maxItems'])}",
        )
    if schema.get("uniqueItems"):
        rendered = [canonical_json(entry) for entry in value]
        if len(set(rendered)) != len(rendered):
            raise _failure(state.direction, path, "array items must be unique")
    _enter(value, path, state)
    try:
        for index, entry in enumerate(value):
            _validate(schema["items"], entry, f"{path}[{index}]", depth + 1, state)
    finally:
        state.ancestors.discard(id(value))


def _enter(value: Any, path: str, state: _State) -> None:
    if id(value) in state.ancestors:
        raise _failure(state.direction, path, "cyclic values are forbidden")
    state.ancestors.add(id(value))


def _inspect(schema: JsonSchema, path: str, depth: int, seen: set[int], reject: bool) -> None:
    if depth > _MAX_DEPTH:
        raise _schema_failure(path, "schema is too deeply nested")
    if id(schema) in seen:
        raise _schema_failure(path, "recursive schemas are forbidden")
    seen.add(id(schema))
    if "oneOf" in schema:
        alternatives = schema["oneOf"]
        if not 2 <= len(alternatives) <= 16:
            raise _schema_failure(path, "oneOf must contain 2 to 16 bounded alternatives")
        for index, entry in enumerate(alternatives):
            _inspect(entry, f"{path}.oneOf[{index}]", depth + 1, seen, reject)
        seen.discard(id(schema))
        return
    if schema.get("enum"):
        values = schema["enum"]
        if len(values) > 128:
            raise _schema_failure(path, "enum must contain 1 to 128 values")
        if len({canonical_json(entry) for entry in values}) != len(values):
            raise _schema_failure(path, "enum values must be unique")
    elif "enum" in schema and schema["enum"] is not None:
        raise _schema_failure(path, "enum must contain 1 to 128 values")
    kind = schema.get("type")
    if kind == "object":
        if schema.get("additionalProperties") is not False:
            raise _schema_failure(path, "every object must set additionalProperties:false")
        keys = list(schema["properties"])
        most_properties = schema.get("maxProperties", len(keys))
        if len(keys) > 128 or most_properties > len(keys) or most_properties < 0:
            raise _schema_failure(path, "object property bounds are invalid")
        required = list(schema["required"])
        if len(set(required)) != len(required):
            raise _schema_failure(path, "required fields must be unique")
        for name in required:
            if name not in schema["properties"]:
                raise _schema_failure(path, f"required field {name} has no schema")
        for key, child in schema["properties"].items():
            if reject and re.sub(r"[^A-Za-z0-9]", "", key).lower() in _FORBIDDEN:
                raise _schema_failure(
                    f"{path}.properties.{key}", "caller authority fields are forbidden"
                )
            _inspect(child, f"{path}.properties.{key}", depth + 1, seen, reject)
    elif kind == "array":
        most: Any = schema.get("maxItems")
        least: Any = schema.get("minItems")
        if (
            not _safe_integer(most)
            or most < 0
            or most > 10_000
            or (least is not None and (not _safe_integer(least) or least < 0 or least > most))
        ):
            raise _schema_failure(path, "array item bounds are required and invalid")
        _inspect(schema["items"], f"{path}.items", depth + 1, seen, reject)
    elif kind == "string":
        most = schema.get("maxLength")
        least = schema.get("minLength")
        if (
            most is None
            or not _safe_integer(most)
            or most < 0
            or most > _MAX_BYTES
            or (least is not None and (not _safe_integer(least) or least < 0 or least > most))
        ):
            raise _schema_failure(path, "every string must have valid min/max length bounds")
        if schema.get("pattern") is not None:
            try:
                _pattern(schema["pattern"])
            except re.error as error:
                raise _schema_failure(path, "string pattern is invalid") from error
    elif kind in ("number", "integer"):
        low, high = _finite_number(schema.get("minimum")), _finite_number(schema.get("maximum"))
        if low is None or high is None or low > high:
            raise _schema_failure(path, "every number must have finite minimum and maximum")
    seen.discard(id(schema))


# ---------------------------------------------------------------------- JavaScript semantics


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _safe_integer(value: Any) -> bool:
    number = _finite_number(value)
    if number is None:
        return False
    if isinstance(number, float) and not number.is_integer():
        return False
    return abs(number) <= _SAFE


def _same(left: Any, right: Any) -> bool:
    """``Object.is`` on JSON values."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if left == 0 and right == 0:
            return math.copysign(1, left) == math.copysign(1, right)
        return left == right
    return type(left) is type(right) and left == right


def _js_string(value: Any) -> str:
    """``String(value)``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if math.isfinite(value):
            return _js_number(value)
        return "NaN" if value != value else "Infinity" if value > 0 else "-Infinity"
    return str(value)


@lru_cache(maxsize=256)
def _pattern(source: str) -> re.Pattern[str]:
    """A schema's JavaScript pattern for Python: ``$`` ends the string (no newline grace)."""
    out, escaped, in_class = [], False, False
    for ch in source:
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == "\\":
            out.append(ch)
            escaped = True
        elif ch == "[":
            in_class = True
            out.append(ch)
        elif ch == "]":
            in_class = False
            out.append(ch)
        elif ch == "$" and not in_class:
            out.append(r"\Z")
        else:
            out.append(ch)
    return re.compile("".join(out))


def _utc_date_time(value: str) -> bool:
    """V8's ``Date.parse`` on the strict pattern: any day 1-31 in any month, and 24:00:00
    (as the next midnight) but never a 60th minute or second."""
    match = _DATE_TIME.fullmatch(value)
    if match is None:
        return False
    month, day, hour, minute, second = (int(match[i]) for i in range(2, 7))
    if not (1 <= month <= 12 and 1 <= day <= 31 and minute <= 59 and second <= 59):
        return False
    if hour == 24:
        return minute == 0 and second == 0 and not (match[7] or "").strip("0")
    return hour <= 23


def _failure(direction: Direction | None, field: str, message: str) -> CrowdyAgentError:
    code: CrowdyAgentErrorCode = (
        "AGENT_TOOL_OUTPUT_INVALID"
        if direction == "OUTPUT"
        else "AGENT_TOOL_DESCRIPTOR_INVALID"
        if direction == "SCHEMA"
        else "AGENT_TOOL_INPUT_INVALID"
    )
    return CrowdyAgentError(code, f"{field}: {message}", field=field)


def _schema_failure(path: str, message: str) -> CrowdyAgentError:
    return _failure("SCHEMA", path, message)
