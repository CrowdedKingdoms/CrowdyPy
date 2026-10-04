"""Compiler diagnostics from a ck-exec build log: rustc's human lines, its compact
``path:line:col: level: message`` lines, and ``--message-format=json`` lines, attributed to
the SERVER or CLIENT target. CrowdyJS's ``parseRustcDiagnostics``, checked against the
shared fixture.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

import msgspec

from crowdypy.domains.crowdy_studio import CrowdyStudioTarget, normalize_crowdy_studio_path

__all__ = [
    "CrowdyStudioDiagnostic",
    "CrowdyStudioDiagnosticSeverity",
    "CrowdyStudioDiagnosticSource",
    "parse_rustc_diagnostics",
]

CrowdyStudioDiagnosticSeverity = Literal["error", "warning", "info", "hint"]
CrowdyStudioDiagnosticSource = Literal["rustc", "local-advisory", "runtime"]


class CrowdyStudioDiagnostic(
    msgspec.Struct, rename="camel", frozen=True, kw_only=True, omit_defaults=True
):
    target: CrowdyStudioTarget
    path: str
    line: int
    column: int
    end_line: int | None = None
    end_column: int | None = None
    severity: CrowdyStudioDiagnosticSeverity
    message: str
    code: str | None = None
    source: CrowdyStudioDiagnosticSource


# JavaScript's ``.`` stops at every line terminator, not only ``\n``.
_DOT = r"[^\n\r\u2028\u2029]"
_HEADER = re.compile(rf"\s*(error|warning|info|note)(?:\[([^\]]+)\])?:\s*({_DOT}+?)\s*")
_ARROW = re.compile(rf"\s*-->\s+({_DOT}+?):(\d+):(\d+)(?:-(\d+))?\s*")
_COMPACT = re.compile(
    rf"\s*({_DOT}+?):(\d+):(\d+):\s*(error|warning|info|note)(?:\[([^\]]+)\])?:\s*({_DOT}+?)\s*"
)
_TARGET_DIR = re.compile(rf"(?:^|/)(server|client)/({_DOT}+)$", re.IGNORECASE)
_TO_CARGO = re.compile(r"^.*/(?=Cargo\.toml$)", re.DOTALL)
_SAFE = 2**53 - 1


def parse_rustc_diagnostics(
    output: str | None, default_target: CrowdyStudioTarget
) -> list[CrowdyStudioDiagnostic]:
    """Every diagnostic in a build log, deduplicated. A location under a ``server/`` or
    ``client/`` directory names its target; any other is ``default_target``'s."""
    if not output:
        return []
    found: list[CrowdyStudioDiagnostic] = []
    pending: dict[str, Any] | None = None
    for line in re.sub(r"\r\n?", "\n", output).split("\n"):
        spans = _json_diagnostics(line, default_target)
        if spans:
            found.extend(spans)
            pending = None
            continue
        header = _HEADER.fullmatch(line)
        if header:
            pending = {"severity": _severity(header[1]), "message": header[3], "code": header[2]}
            continue
        arrow = _ARROW.fullmatch(line)
        if arrow and pending:
            target, path = _location(arrow[1], default_target)
            found.append(
                CrowdyStudioDiagnostic(
                    target=target,
                    path=path,
                    line=int(arrow[2]),
                    column=int(arrow[3]),
                    end_column=int(arrow[4]) if arrow[4] else None,
                    source="rustc",
                    **pending,
                )
            )
            pending = None
            continue
        compact = _COMPACT.fullmatch(line)
        if compact:
            target, path = _location(compact[1], default_target)
            found.append(
                CrowdyStudioDiagnostic(
                    target=target,
                    path=path,
                    line=int(compact[2]),
                    column=int(compact[3]),
                    severity=_severity(compact[4]),
                    message=compact[6],
                    code=compact[5],
                    source="rustc",
                )
            )
    seen: set[tuple[Any, ...]] = set()
    unique: list[CrowdyStudioDiagnostic] = []
    for diagnostic in found:
        key = (
            diagnostic.target,
            diagnostic.path,
            diagnostic.line,
            diagnostic.column,
            diagnostic.severity,
            diagnostic.code or "",
            diagnostic.message,
        )
        if key not in seen:
            seen.add(key)
            unique.append(diagnostic)
    return unique


def _reject_constant(name: str) -> Any:
    raise ValueError(name)  # JSON.parse has no NaN or Infinity


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value) if abs(value) <= _SAFE else None


def _json_diagnostics(
    line: str, default_target: CrowdyStudioTarget
) -> list[CrowdyStudioDiagnostic]:
    if not line.lstrip().startswith("{"):
        return []
    try:
        value = json.loads(line, parse_constant=_reject_constant)
    except ValueError:
        return []
    if not isinstance(value, dict):
        return []
    message = value["message"] if isinstance(value.get("message"), dict) else value
    if not isinstance(message.get("message"), str):
        return []
    level = _severity(message["level"]) if isinstance(message.get("level"), str) else "error"
    raw_code = message.get("code")
    code = (
        raw_code["code"]
        if isinstance(raw_code, dict) and isinstance(raw_code.get("code"), str)
        else None
    )
    spans: list[Any] = message["spans"] if isinstance(message.get("spans"), list) else []
    out: list[CrowdyStudioDiagnostic] = []
    for span in spans:
        if not isinstance(span, dict) or span.get("is_primary") is not True:
            continue
        line_start, column_start = (
            _safe_int(span.get("line_start")),
            _safe_int(span.get("column_start")),
        )
        if not isinstance(span.get("file_name"), str) or line_start is None or column_start is None:
            continue
        target, path = _location(span["file_name"], default_target)
        out.append(
            CrowdyStudioDiagnostic(
                target=target,
                path=path,
                line=line_start,
                column=column_start,
                end_line=_safe_int(span.get("line_end")),
                end_column=_safe_int(span.get("column_end")),
                severity=level,
                message=message["message"],
                code=code,
                source="rustc",
            )
        )
    return out


def _location(value: str, default_target: CrowdyStudioTarget) -> tuple[CrowdyStudioTarget, str]:
    path = value.strip().replace("\\", "/")
    target = default_target
    match = _TARGET_DIR.search(path)
    if match:
        target = "SERVER" if match[1].upper() == "SERVER" else "CLIENT"
        path = match[2]
    else:
        source = path.rfind("/src/")
        if source >= 0:
            path = path[source + 1 :]
        elif "/" in path:
            path = _TO_CARGO.sub("", path, count=1)
    return target, normalize_crowdy_studio_path(path)


def _severity(value: str) -> CrowdyStudioDiagnosticSeverity:
    if value == "warning":
        return "warning"
    if value in ("info", "note", "help"):
        return "info"
    return "error"
