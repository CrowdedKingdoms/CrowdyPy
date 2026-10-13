"""Distance-limited channel messages over native UDP, end to end.

The app owner makes an invite channel with four members, and each member keeps an actor at
its own chunk. A publishes from its chunk: ``max_distance`` 5 reaches only B, exactly 5
chunks away (C, a diagonal neighbour at about 5.66, does not); 6 adds C; 7 adds D. A never
receives its own message, and the members receive the ordinary channel message.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import crowdypy
from crowdypy.replication import Notification
from e2e.conftest import (
    CONFIG,
    RUN,
    Player,
    chunk_band,
    connect_udp,
    owner_client,
    provision_player,
)

OFFSETS = {"A": (0, 0, 0), "B": (3, 4, 0), "C": (4, 4, 0), "D": (0, 0, 7)}


async def _owner_game(app: str) -> tuple[crowdypy.AsyncCrowdyClient, crowdypy.AsyncCrowdyClient]:
    identity = await owner_client()
    minted = await identity.portal.mint_app_token(app)
    game = crowdypy.AsyncCrowdyClient(
        http_url=CONFIG.http_url or minted.game_api_url or CONFIG.api_url,
        discovery_url=minted.discovery_url or CONFIG.api_url,
    )
    game.set_app_token(minted)
    return identity, game


async def test_a_ranged_channel_message_reaches_only_the_members_in_range() -> None:
    app = CONFIG.app_id
    base = chunk_band(4)
    chunks = {
        name: (base[0] + dx, base[1] + dy, base[2] + dz) for name, (dx, dy, dz) in OFFSETS.items()
    }
    actors = {name: uuid.uuid4().hex for name in OFFSETS}
    players: dict[str, Player] = {}
    owner_identity, owner = await _owner_game(app)
    channel_id: str | None = None
    try:
        for name in OFFSETS:
            players[name] = await provision_player(f"py-rc-{name.lower()}")
        channel = await owner.channels.create(
            {
                "appId": app,
                "name": f"py-ranged-{RUN}",
                "membershipPolicy": "invite",
                "membersCanSend": True,
            }
        )
        channel_id = str(channel["groupId"])
        for player in players.values():
            await owner.channels.add_member(channel_id, player.user_id)

        received: dict[str, list[Notification]] = {name: [] for name in OFFSETS}
        sender_errors: list[Notification] = []
        for name, player in players.items():
            await connect_udp(player)
            player.game.udp.subscribe({"channel_message": received[name].append})
        players["A"].game.udp.subscribe({"generic_error": sender_errors.append})

        async def place_all() -> None:
            for name, player in players.items():
                await player.game.udp.send_actor_update(
                    chunks[name], actors[name], b"\0", distance=0
                )
                player.game.udp.flush_sends()

        # The first update into a region can be refused while its grid window loads.
        await place_all()
        await asyncio.sleep(2.5)
        await place_all()
        await asyncio.sleep(1.0)

        ranged_sequences: set[int] = set()

        async def who_receives(max_distance: int) -> list[str]:
            await place_all()
            await asyncio.sleep(0.3)
            payload = f"ranged-{max_distance}-{RUN}".encode()
            sender = players["A"].game.udp
            sequence = await sender.send_ranged_channel_message(
                channel_id, actors["A"], payload, chunk=chunks["A"], max_distance=max_distance
            )
            sender.flush_sends()
            ranged_sequences.add(sequence)
            await asyncio.sleep(3.0)
            got = []
            for name in OFFSETS:
                hits = [n for n in received[name] if n.payload == payload]
                if hits:
                    assert hits[0].channel_id == int(channel_id)
                    assert hits[0].uuid == actors["A"]
                    got.append(name)
            return got

        assert await who_receives(5) == ["B"]
        assert await who_receives(6) == ["B", "C"]
        assert await who_receives(7) == ["B", "C", "D"]
        refused = [e for e in sender_errors if e.sequence in ranged_sequences]
        assert refused == [], [(e.sequence, e.error_code) for e in refused]
    finally:
        if channel_id is not None:
            with contextlib.suppress(crowdypy.CrowdyError):
                await owner.channels.remove(channel_id)
        for player in players.values():
            await player.aclose()
        await owner.aclose()
        await owner_identity.aclose()
