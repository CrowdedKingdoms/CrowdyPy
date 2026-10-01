"""PKCE (RFC 7636, S256) for the portal's authorization-code flow."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Literal

__all__ = ["PkcePair", "generate_pkce_pair", "generate_state"]


@dataclass(frozen=True, slots=True)
class PkcePair:
    verifier: str
    challenge: str
    method: Literal["S256"] = "S256"


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> PkcePair:
    """A fresh verifier (32 random octets) and its S256 challenge."""
    verifier = _base64url(secrets.token_bytes(32))
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return PkcePair(verifier=verifier, challenge=challenge)


def generate_state() -> str:
    """An unguessable ``state`` value (16 random octets) to bind a redirect to its start."""
    return _base64url(secrets.token_bytes(16))
