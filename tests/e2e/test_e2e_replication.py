"""Native UDP replication between two players, end to end."""

from __future__ import annotations

import uuid

import numpy as np

from crowdypy.replication import Notification
from e2e.conftest import Player, chunk_band, connect_udp, eventually, warm_up


async def _connected(a: Player, b: Player, chunk: tuple[int, int, int]) -> None:
    await connect_udp(a)
    await connect_udp(b)
    assert await warm_up(a, chunk)
    assert await warm_up(b, chunk)


async def test_actor_updates_reach_the_other_player(player_pair: tuple[Player, Player]) -> None:
    a, b = player_pair
    chunk = chunk_band(1)
    await _connected(a, b, chunk)
    seen: list[Notification] = []
    b.game.udp.subscribe({"actor_update": seen.append})
    actor = uuid.uuid4().hex

    def arrived() -> bool:
        return any(n.uuid == actor and n.payload == b"pose-1" for n in seen)

    for _ in range(20):
        await a.game.udp.send_actor_update(chunk, actor, b"pose-1")
        a.game.udp.flush_sends()
        if await eventually(arrived, timeout_s=0.5):
            break
    assert arrived()
    echo = await a.game.udp.send_actor_update_and_wait(chunk, actor, b"pose-2")
    assert echo.uuid == actor
    assert tuple(echo.chunk) == chunk


async def test_a_batched_frame_reaches_the_other_player(player_pair: tuple[Player, Player]) -> None:
    a, b = player_pair
    chunk = chunk_band(2)
    await _connected(a, b, chunk)
    seen: set[str] = set()
    b.game.udp.subscribe({"actor_update": lambda n: seen.add(n.uuid)})
    actors = [uuid.uuid4().hex for _ in range(16)]
    chunks = np.array([chunk] * len(actors), dtype=np.int64)
    uuids = np.frombuffer("".join(actors).encode(), dtype=np.uint8).reshape(len(actors), 32)
    poses = np.zeros((len(actors), 8), dtype=np.uint8)
    for _ in range(20):
        a.game.udp.connection.send_actor_updates(chunks, uuids, poses, stride=8)
        a.game.udp.flush_sends()
        if await eventually(lambda: set(actors) <= seen, timeout_s=0.5):
            break
    assert set(actors) <= seen


async def test_voxel_updates_and_client_events_reach_the_other_player(
    player_pair: tuple[Player, Player],
) -> None:
    a, b = player_pair
    chunk = chunk_band(3)
    await _connected(a, b, chunk)
    voxels: list[Notification] = []
    events: list[Notification] = []
    b.game.udp.subscribe({"voxel_update": voxels.append, "client_event": events.append})
    actor = uuid.uuid4().hex
    for _ in range(20):
        await a.game.udp.send_voxel_update(chunk, actor, (1, 2, 3), 5)
        await a.game.udp.send_client_event(chunk, actor, 7, b"hello")
        a.game.udp.flush_sends()
        if await eventually(lambda: bool(voxels) and bool(events), timeout_s=0.5):
            break
    assert any(n.voxel == (1, 2, 3) and n.voxel_type == 5 for n in voxels)
    assert any(n.event_type == 7 and n.payload == b"hello" for n in events)
