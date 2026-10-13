"""The input log read by the app's owner, against a live deployment.

Read-only: it lists whatever the app has recorded, which may be nothing (turning replay logging
on needs a funded wallet, and this suite leaves the app's settings alone). What it proves: the
Game API accepts the sessions document's ``missingRecords`` selection (one before the release
after v2.40.2 refuses it), one session's inputs page when there is one, and a cancelled read
stops.
"""

from __future__ import annotations

import asyncio

import pytest

import crowdypy
from e2e.conftest import CONFIG, owner_client


async def _owner_game() -> tuple[crowdypy.AsyncCrowdyClient, crowdypy.AsyncCrowdyClient]:
    owner = await owner_client()
    minted = await owner.portal.mint_app_token(CONFIG.app_id)
    game = crowdypy.AsyncCrowdyClient(
        http_url=CONFIG.http_url or minted.game_api_url or CONFIG.api_url,
        discovery_url=minted.discovery_url or CONFIG.api_url,
    )
    game.set_app_token(minted)
    return owner, game


async def test_the_owner_reads_sessions_with_missing_records() -> None:
    owner, game = await _owner_game()
    try:
        page = await game.input_log.sessions(CONFIG.app_id, first=5)
        assert isinstance(page["edges"], list)
        assert isinstance(page["pageInfo"]["hasNextPage"], bool)
        for edge in page["edges"]:
            # A count, or None for a session recorded before the count existed.
            assert "missingRecords" in edge["node"]
        if page["edges"]:
            token_id = page["edges"][0]["node"]["gameTokenId"]
            messages = await game.input_log.messages(CONFIG.app_id, token_id, first=20)
            assert isinstance(messages["edges"], list)
            assert isinstance(messages["pageInfo"]["hasNextPage"], bool)
    finally:
        await game.aclose()
        await owner.aclose()


async def test_a_cancelled_read_stops() -> None:
    owner, game = await _owner_game()
    try:
        reading = asyncio.create_task(game.input_log.sessions(CONFIG.app_id, first=200))
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        # The client is still usable for the next read.
        page = await game.input_log.sessions(CONFIG.app_id, first=1)
        assert isinstance(page["edges"], list)
    finally:
        await game.aclose()
        await owner.aclose()
