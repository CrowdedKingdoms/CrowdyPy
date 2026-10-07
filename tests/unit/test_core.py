"""Sessions, token stores, PKCE, estates, redirects and small helpers."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from pathlib import Path

import pytest

from crowdypy.auth_state import AuthState
from crowdypy.datacenter_redirect import move_from_error, move_from_errors
from crowdypy.errors import CrowdyProtocolError
from crowdypy.estate import is_same_estate
from crowdypy.pkce import generate_pkce_pair, generate_state
from crowdypy.session import FileTokenStore, MemoryTokenStore, SessionStore
from crowdypy.utils import SequenceAllocator, bigint, generate_crowdy_uuid, validate_crowdy_uuid


def test_session_persists_notifies_and_restores() -> None:
    store = MemoryTokenStore()
    session = SessionStore(store)
    seen: list[str | None] = []
    unsubscribe = session.on_change(seen.append)
    session.set_token("a")
    session.set_token("a")
    session.clear()
    unsubscribe()
    session.set_token("b")
    assert seen == [None, "a", None]
    assert store.get() == "b"
    fresh = AuthState(store)
    assert fresh.restore() == "b"
    assert fresh.get_token() == "b"


def test_restore_does_not_write_back() -> None:
    class Recording(MemoryTokenStore):
        writes = 0

        def set(self, token: str) -> None:
            Recording.writes += 1
            super().set(token)

    store = Recording("x")
    SessionStore(store).restore()
    assert Recording.writes == 0


def test_file_token_store_is_atomic_and_private(tmp_path: Path) -> None:
    path = FileTokenStore.session_path(tmp_path, "https://ck.example.test")
    assert path.name == "crowdy-session-ck.example.test"
    assert FileTokenStore.app_path(tmp_path, 93718654971904).name == "crowdy-app-93718654971904"
    store = FileTokenStore(path)
    assert store.get() is None
    store.set("secret-token")
    assert store.get() == "secret-token"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [path.name]
    store.clear()
    store.clear()
    assert store.get() is None


def test_session_path_ignores_scheme() -> None:
    a = FileTokenStore.session_path("/d", "https://ck.example.test")
    b = FileTokenStore.session_path("/d", "ck.example.test")
    assert a == b


def test_pkce_pair_is_s256() -> None:
    pair = generate_pkce_pair()
    assert pair.method == "S256"
    digest = hashlib.sha256(pair.verifier.encode()).digest()
    assert pair.challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert len(pair.verifier) == 43
    assert generate_state() != generate_state()


@pytest.mark.parametrize(
    ("current", "candidate", "same"),
    [
        ("wss://ck.example.com/graphql", "wss://ck-va.example.com/realtime", True),
        ("https://ck-or.dev.example.com", "https://ck-va.dev.example.com", True),
        ("wss://ck.example.com", "wss://ck.example.com.evil.net", False),
        ("wss://ck.example.com", "wss://evil.example@ck.attacker.net", False),
        ("wss://localhost", "wss://otherhost", False),
        ("not a url", "wss://ck.example.com", False),
    ],
)
def test_estate(current: str, candidate: str, same: bool) -> None:
    assert is_same_estate(current, candidate) is same


def test_redirect_needs_code_and_endpoint() -> None:
    assert move_from_error({"extensions": {"code": "WRONG_DATACENTER"}}) is None
    assert move_from_error({"extensions": {"code": "OTHER", "gameApiUrl": "https://x"}}) is None
    move = move_from_errors(
        [
            {"extensions": {}},
            {"extensions": {"code": "WRONG_DATACENTER", "gameApiUrl": " https://x "}},
        ]
    )
    assert move is not None
    assert move.game_api_url == "https://x"


def test_sequence_wraps_at_256() -> None:
    seq = SequenceAllocator(seed=255)
    assert [seq.next(), seq.next(), seq.next()] == [255, 0, 1]


def test_uuid_helpers() -> None:
    uuid = generate_crowdy_uuid()
    assert len(uuid) == 32
    validate_crowdy_uuid(uuid)
    with pytest.raises(CrowdyProtocolError):
        validate_crowdy_uuid("short")


def test_bigint() -> None:
    assert bigint(93718654971904) == "93718654971904"
    assert bigint(" -42 ") == "-42"
    with pytest.raises(CrowdyProtocolError):
        bigint("12a")
    with pytest.raises(TypeError):
        bigint(True)
