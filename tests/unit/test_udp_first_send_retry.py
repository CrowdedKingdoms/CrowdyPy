"""``*_and_wait``: a session's first awaited send is retried once when a realtime server older
than v0.42.0 refuses it (``UNAUTHORIZED``) or, for a send the server echoes, drops it
(``UDP_SEQUENCE_TIMEOUT``). Audio, text and events succeed by timing out, so their timeout is
never retried; later sends in a session are not retried; a reassignment or a new connect starts
a new session. Driven through a fake connection: the native core is not involved."""

from __future__ import annotations

from typing import Any

import pytest

from crowdypy._sync.domains.udp import UdpAPI as SyncUdpAPI
from crowdypy.domains.udp import UdpAPI
from crowdypy.errors import CrowdyRealtimeError

CHUNK = (0, 0, 0)
ME = "a" * 32


class FakeConnection:
    """Answers each awaited send from ``replies`` in order: echo, refuse or silent."""

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

    def _answer(self, sequence: int) -> dict[str, int]:
        reply = self.replies.pop(0) if self.replies else "silent"
        if reply == "echo":
            return {"sequence": sequence}
        if reply == "refuse":
            raise CrowdyRealtimeError("refused", code="UNAUTHORIZED")
        raise CrowdyRealtimeError("timed out", code="UDP_SEQUENCE_TIMEOUT")


class AsyncFake(FakeConnection):
    async def wait_for_sequence(self, sequence: int, uuid: str, timeout: float) -> Any:
        return self._answer(sequence)


class SyncFake(FakeConnection):
    def wait_for_sequence(self, sequence: int, uuid: str, timeout: float) -> Any:
        return self._answer(sequence)


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
