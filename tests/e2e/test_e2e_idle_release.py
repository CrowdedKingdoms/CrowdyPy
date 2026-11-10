"""The replication server releases an idle player, and the native connection stays released.

The app's idle timeout is lowered to a minute for the test and put back afterwards. The
connection's own heartbeats keep the transport alive; they are not gameplay, so the release
still comes.
"""

from __future__ import annotations

import asyncio

import pytest

from crowdypy.replication import ConnState
from e2e.conftest import (
    CONFIG,
    chunk_band,
    connect_udp,
    eventually,
    owner_client,
    provision_player,
    warm_up,
)

IDLE_S = 60


@pytest.mark.slow
async def test_an_idle_player_is_released_and_not_reconnected() -> None:
    owner = await owner_client()
    app = await owner.apps.app(CONFIG.app_id)
    assert app is not None
    before = app.get("idleTimeoutSeconds")
    player = await provision_player("py-idle")
    try:
        updated = await owner.apps.update(CONFIG.app_id, {"idleTimeoutSeconds": IDLE_S})
        assert updated["effectiveIdleTimeoutSeconds"] == IDLE_S
        await connect_udp(player)
        assert await warm_up(player, chunk_band(9))
        connection = player.game.udp.connection
        assert connection is not None
        reassignments = connection.stats().get("reconnects", 0)

        released = await eventually(
            lambda: connection.state == ConnState.INACTIVE, timeout_s=IDLE_S + 45, every_s=1.0
        )
        assert released, f"never released: {connection.state!r}"
        status = await player.game.udp.connection_status()
        assert (status.connected, status.state) == (False, ConnState.INACTIVE)

        await asyncio.sleep(20)  # nothing puts the player back on a server
        assert connection.state == ConnState.INACTIVE
        assert connection.stats().get("reconnects", 0) == reassignments
    finally:
        await owner.apps.update(CONFIG.app_id, {"idleTimeoutSeconds": before})
        await owner.aclose()
        await player.aclose()
