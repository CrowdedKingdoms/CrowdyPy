"""World Stores over two live sessions: presence and voxel edits land in the other
player's stores natively."""

from __future__ import annotations

from crowdypy.stores import create_world_session
from e2e.conftest import CONFIG, Player, chunk_band, connect_udp, eventually, warm_up


async def test_two_sessions_see_each_other_and_share_voxel_edits(
    player_pair: tuple[Player, Player],
) -> None:
    a, b = player_pair
    chunk = chunk_band(4)
    await connect_udp(a)
    await connect_udp(b)
    assert await warm_up(a, chunk)
    assert await warm_up(b, chunk)
    alice = create_world_session(a.game, CONFIG.app_id)
    bob = create_world_session(b.game, CONFIG.app_id)
    try:
        for session in (alice, bob):
            session.chunks.seed(chunk, bytes(4096), write_back=False)
        alice.self.join(chunk, b"alice")
        bob.self.join(chunk, b"bob")

        def present() -> bool:
            alice.tick()
            bob.tick()
            return (
                bob.actors.get(alice.actor_uuid) is not None
                and alice.actors.get(bob.actor_uuid) is not None
            )

        assert await eventually(present, timeout_s=20)

        def edited() -> bool:
            alice.tick()
            bob.tick()
            return bob.chunks.voxel_type_at(chunk, 1, 2, 3) == 9

        for _ in range(10):
            alice.chunks.set_voxel(chunk, 1, 2, 3, 9)
            if await eventually(edited, timeout_s=1.5):
                break
        assert bob.chunks.voxel_type_at(chunk, 1, 2, 3) == 9
        assert alice.chunks.voxel_type_at(chunk, 1, 2, 3) == 9
    finally:
        alice.dispose()
        bob.dispose()
