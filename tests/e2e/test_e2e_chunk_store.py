"""The chunk store over a live session (0.32.0): a chunk written with ``updateChunk`` loads and
hydrates with the server's times, an edit settles with its echo, a catch-up over every tracked
chunk merges what was recorded, and an evicted chunk is forgotten."""

from __future__ import annotations

import time

from crowdypy.stores import _chunk_input, _iso, create_world_session
from crowdypy.utils import encode_base64
from e2e.conftest import CONFIG, chunk_band, connect_udp, eventually, provision_player, warm_up


async def test_loads_hydrate_and_catch_up_with_the_servers_times() -> None:
    player = await provision_player("py-chunks")
    chunk = chunk_band(7)
    try:
        await connect_udp(player)
        assert await warm_up(player, chunk)
        await player.game.chunks.update(
            {
                "appId": CONFIG.app_id,
                "coordinates": _chunk_input(chunk),
                "voxels": encode_base64(bytes([3]) * 4096),
            }
        )
        session = create_world_session(
            player.game,
            CONFIG.app_id,
            chunk_options={"hydrate_voxel_states": True, "write_back_interval_ms": None},
        )
        try:
            await session.chunks.ensure_around(chunk, 0)
            assert session.chunks.voxel_type_at(chunk, 0, 0, 0) == 3
            now_ms = int(time.time() * 1000)
            stored_at = session.chunks.confirmed_at(chunk, 0, 0, 0)
            assert stored_at is not None and abs(stored_at - now_ms) < 120_000

            session.self.join(chunk, b"py")
            session.chunks.set_voxel(chunk, 1, 2, 3, 9)

            def settled() -> bool:
                session.tick()
                return session.chunks.edits_in_flight == 0

            assert await eventually(settled, timeout_s=15)
            assert session.chunks.voxel_type_at(chunk, 1, 2, 3) == 9
            edited_at = session.chunks.confirmed_at(chunk, 1, 2, 3)
            assert edited_at is not None and abs(edited_at - now_ms) < 120_000

            assert await session.chunks.catch_up(_iso(now_ms - 60_000))
            assert session.chunks.voxel_type_at(chunk, 1, 2, 3) == 9
            assert await session.chunks.hydrate(chunk)
            assert session.chunks.voxel_type_at(chunk, 1, 2, 3) == 9

            assert session.chunks.evict(chunk)
            assert session.chunks.get(chunk) is None
        finally:
            session.dispose()
    finally:
        await player.aclose()
