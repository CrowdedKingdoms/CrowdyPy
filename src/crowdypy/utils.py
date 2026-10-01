"""Small helpers shared across the SDK."""

from __future__ import annotations

import base64
import secrets
from collections.abc import Mapping
from typing import Any

from .errors import CrowdyProtocolError

__all__ = [
    "SequenceAllocator",
    "bigint",
    "decode_base64",
    "encode_base64",
    "generate_crowdy_uuid",
    "validate_chunk_coordinates",
    "validate_crowdy_uuid",
]

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


class SequenceAllocator:
    """uint8 sequence numbers for correlating sends with server echoes and errors.

    Correlation only, never idempotency: the counter wraps at 256.
    """

    def __init__(self, seed: int = 1) -> None:
        self._next = seed & 0xFF

    def next(self) -> int:
        value = self._next
        self._next = (self._next + 1) & 0xFF
        return value


def generate_crowdy_uuid() -> str:
    """A random actor uuid: 32 lowercase hex characters (not an RFC 4122 UUID)."""
    return secrets.token_hex(16)


def validate_crowdy_uuid(uuid: str) -> None:
    if len(uuid.encode("utf-8")) != 32:
        raise CrowdyProtocolError("Crowdy UUID must be exactly 32 bytes when UTF-8 encoded")


def encode_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_base64(value: str) -> bytes:
    return base64.b64decode(value)


def bigint(value: int | str) -> str:
    """A GraphQL ``BigInt`` argument: decimal strings on the wire, as everywhere on the platform."""
    if isinstance(value, bool):
        raise TypeError("a BigInt cannot be a bool")
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if not text or not (text.lstrip("-").isdigit()):
        raise CrowdyProtocolError(f"not a decimal BigInt: {value!r}")
    return text


def validate_chunk_coordinates(chunk: Mapping[str, Any]) -> None:
    for axis in ("x", "y", "z"):
        value = int(chunk[axis])
        if value < _INT64_MIN or value > _INT64_MAX:
            raise CrowdyProtocolError(f"Chunk coordinate {axis} is outside signed int64 range")
