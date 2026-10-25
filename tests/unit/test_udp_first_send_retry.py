"""``*_and_wait``: a session's first awaited send is retried once when a realtime server older
than v0.42.0 refuses it (``UNAUTHORIZED``) or, for a send the server echoes, drops it
(``UDP_SEQUENCE_TIMEOUT``). Audio, text and events succeed by timing out, so their timeout is
never retried; later sends in a session are not retried; a reassignment or a new connect starts
a new session. Driven through a fake connection: the native core is not involved."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from crowdypy._sync.domains.udp import UdpAPI as SyncUdpAPI
from crowdypy.domains.udp import UdpAPI
from crowdypy.errors import CrowdyRealtimeError
from crowdypy.replication import (
    AsyncReplicationConnection,
    ConnState,
    ReplicationConnection,
    _SessionChanged,
)

CHUNK = (0, 0, 0)
ME = "a" * 32


class FakeConnection:
    """Answers each awaited send from ``replies`` in order: echo, refuse, silent, or moved (a
    reassignment's session connected while it waited, which ends only an echoed send's wait)."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.sent: list[int] = []
        self.reconnects = 0

    def stats(self) -> dict[str, Any]:
        return {"reconnects": self.reconnects}

    def flush_sends(self) -> None:
        pass

    def _send(self, *_args: Any, **_kwargs: Any) -> int:
        sequence = len(self.sent)
        self.sent.append(sequence)
        return sequence

    send_actor_update = _send
    send_voxel_update = _send
    send_text_packet = _send

    def _answer(self, sequence: int, end_on_new_session: bool) -> dict[str, int]:
        reply = self.replies.pop(0) if self.replies else "silent"
        if reply == "echo":
            return {"sequence": sequence}
        if reply == "refuse":
            raise CrowdyRealtimeError("refused", code="UNAUTHORIZED")
        if reply == "moved" and end_on_new_session:
            self.reconnects += 1
            raise _SessionChanged
        raise CrowdyRealtimeError("timed out", code="UDP_SEQUENCE_TIMEOUT")


class AsyncFake(FakeConnection):
    async def _await_sequence(
        self, sequence: int, uuid: str, timeout: float, *, end_on_new_session: bool
    ) -> Any:
        return self._answer(sequence, end_on_new_session)


class SyncFake(FakeConnection):
    def _await_sequence(
        self, sequence: int, uuid: str, timeout: float, *, end_on_new_session: bool
    ) -> Any:
        return self._answer(sequence, end_on_new_session)


def api_with(connection: FakeConnection) -> UdpAPI:
    api = UdpAPI(None)  # type: ignore[arg-type]
    api._connection = connection  # type: ignore[assignment]
    return api


async def test_a_refused_first_send_is_retried_once_with_a_fresh_sequence() -> None:
    fake = AsyncFake(["refuse", "echo"])
    answer = await api_with(fake).send_actor_update_and_wait(CHUNK, ME, b"", timeout=0.1)
    assert fake.sent == [0, 1]
    assert answer == {"sequence": 1}


async def test_a_dropped_first_send_is_retried_once() -> None:
    fake = AsyncFake(["silent", "echo"])
    answer = await api_with(fake).send_voxel_update_and_wait(CHUNK, ME, (1, 2, 3), 4)
    assert fake.sent == [0, 1]
    assert answer == {"sequence": 1}


async def test_the_retry_happens_once() -> None:
    fake = AsyncFake(["refuse", "refuse"])
    with pytest.raises(CrowdyRealtimeError) as raised:
        await api_with(fake).send_actor_update_and_wait(CHUNK, ME)
    assert raised.value.code == "UNAUTHORIZED"
    assert len(fake.sent) == 2


async def test_later_sends_in_the_session_are_not_retried() -> None:
    fake = AsyncFake(["echo", "refuse", "echo"])
    api = api_with(fake)
    await api.send_actor_update_and_wait(CHUNK, ME)
    with pytest.raises(CrowdyRealtimeError):
        await api.send_actor_update_and_wait(CHUNK, ME)
    assert len(fake.sent) == 2


async def test_a_reassignment_or_a_new_connect_starts_a_new_session() -> None:
    fake = AsyncFake(["echo", "refuse", "echo", "silent", "echo"])
    api = api_with(fake)
    await api.send_actor_update_and_wait(CHUNK, ME)
    fake.reconnects += 1
    await api.send_actor_update_and_wait(CHUNK, ME)
    assert len(fake.sent) == 3
    api._generation += 1
    await api.send_actor_update_and_wait(CHUNK, ME)
    assert len(fake.sent) == 5


async def test_a_text_timeout_is_its_success_and_answers_for_the_session() -> None:
    fake = AsyncFake(["silent", "refuse", "echo"])
    api = api_with(fake)
    with pytest.raises(CrowdyRealtimeError) as raised:
        await api.send_text_packet_and_wait(CHUNK, ME, "hi", timeout=0.1)
    assert raised.value.code == "UDP_SEQUENCE_TIMEOUT"
    assert len(fake.sent) == 1
    with pytest.raises(CrowdyRealtimeError):
        await api.send_actor_update_and_wait(CHUNK, ME)
    assert len(fake.sent) == 2


async def test_a_refused_first_text_is_retried_once() -> None:
    fake = AsyncFake(["refuse", "silent"])
    with pytest.raises(CrowdyRealtimeError) as raised:
        await api_with(fake).send_text_packet_and_wait(CHUNK, ME, "hi", timeout=0.1)
    assert raised.value.code == "UDP_SEQUENCE_TIMEOUT"
    assert len(fake.sent) == 2


def test_the_sync_client_retries_the_same_way() -> None:
    fake = SyncFake(["refuse", "echo", "refuse"])
    api = SyncUdpAPI(None)  # type: ignore[arg-type]
    api._connection = fake  # type: ignore[assignment]
    assert api.send_actor_update_and_wait(CHUNK, ME) == {"sequence": 1}
    with pytest.raises(CrowdyRealtimeError):
        api.send_actor_update_and_wait(CHUNK, ME)
    assert len(fake.sent) == 3


# An echoed send in flight across a reassignment: its echo went to the session it left, so it
# is resent once the new session is connected, not at its timeout (OI-2026-10-25-002). The
# first send confirms the session, so the first-send rule is not what retries.


async def test_an_echoed_send_waiting_across_a_reassignment_is_resent() -> None:
    fake = AsyncFake(["echo", "moved", "echo"])
    api = api_with(fake)
    await api.send_actor_update_and_wait(CHUNK, ME)
    answer = await api.send_actor_update_and_wait(CHUNK, ME)
    assert fake.sent == [0, 1, 2]
    assert answer == {"sequence": 2}


async def test_the_resend_after_a_new_session_is_the_one_retry() -> None:
    fake = AsyncFake(["echo", "moved", "silent"])
    api = api_with(fake)
    await api.send_actor_update_and_wait(CHUNK, ME)
    with pytest.raises(CrowdyRealtimeError) as raised:
        await api.send_voxel_update_and_wait(CHUNK, ME, (1, 2, 3), 4)
    assert raised.value.code == "UDP_SEQUENCE_TIMEOUT"
    assert len(fake.sent) == 3


async def test_text_is_not_resent_on_a_new_session() -> None:
    fake = AsyncFake(["echo", "moved"])
    api = api_with(fake)
    await api.send_actor_update_and_wait(CHUNK, ME)
    with pytest.raises(CrowdyRealtimeError) as raised:
        await api.send_text_packet_and_wait(CHUNK, ME, "hi", timeout=0.1)
    assert raised.value.code == "UDP_SEQUENCE_TIMEOUT"
    assert len(fake.sent) == 2


def test_the_sync_client_resends_across_a_reassignment() -> None:
    fake = SyncFake(["echo", "moved", "echo"])
    api = SyncUdpAPI(None)  # type: ignore[arg-type]
    api._connection = fake  # type: ignore[assignment]
    api.send_actor_update_and_wait(CHUNK, ME)
    assert api.send_actor_update_and_wait(CHUNK, ME) == {"sequence": 2}


class _BareAsync(AsyncReplicationConnection):
    """The connection's wait, without a native core: ``moves`` is its reassignment count."""

    def __init__(self, state: ConnState = ConnState.CONNECTED) -> None:
        self.moves = 0
        self._waiters = []
        self._loop = None
        self._native = SimpleNamespace(state=int(state), stats=lambda: {"reconnects": self.moves})


class _BareSync(ReplicationConnection):
    def __init__(self) -> None:
        self.moves = 0
        self._backlog = []
        self._native = SimpleNamespace(
            state=int(ConnState.CONNECTED), stats=lambda: {"reconnects": self.moves}
        )

    def wait(self, timeout: float | None = None) -> bool:
        time.sleep(timeout or 0)
        return False

    def _clear_wake(self) -> None:
        pass

    def _drain(self, max_events: int | None) -> Any:
        return []


async def test_the_connection_ends_the_wait_when_a_new_session_connects() -> None:
    connection = _BareAsync()

    async def reassigned_later() -> None:
        await asyncio.sleep(0.1)
        connection.moves += 1

    later = asyncio.ensure_future(reassigned_later())
    started = time.monotonic()
    with pytest.raises(_SessionChanged):
        await connection._await_sequence(7, ME, 5.0, end_on_new_session=True)
    await later
    assert time.monotonic() - started < 1.0
    assert connection._waiters == []


async def test_a_new_session_that_is_not_connected_yet_does_not_end_the_wait() -> None:
    connection = _BareAsync(ConnState.CONNECTING)
    connection.moves = 1
    with pytest.raises(CrowdyRealtimeError) as raised:
        await connection._await_sequence(7, ME, 0.2, end_on_new_session=True)
    assert raised.value.code == "UDP_SEQUENCE_TIMEOUT"


def test_the_blocking_connection_ends_the_wait_the_same_way() -> None:
    connection = _BareSync()
    connection.moves = 1  # a reassignment after the wait began is what counts
    with pytest.raises(CrowdyRealtimeError):
        connection._await_sequence(7, ME, 0.2, end_on_new_session=True)
    original = connection._reassignments
    calls = iter(range(1000))
    connection._reassignments = lambda: 2 if next(calls) > 2 else original()  # type: ignore[method-assign]
    started = time.monotonic()
    with pytest.raises(_SessionChanged):
        connection._await_sequence(7, ME, 5.0, end_on_new_session=True)
    assert time.monotonic() - started < 1.0
