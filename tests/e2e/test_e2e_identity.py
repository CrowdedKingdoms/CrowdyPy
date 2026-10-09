"""Sign-in, minting and the two-token rule, against a live deployment."""

from __future__ import annotations

import pytest

import crowdypy
from e2e.conftest import (
    CONFIG,
    derive_email,
    derive_password,
    ensure_tier,
    owner_client,
    provision_player,
)


async def test_a_player_signs_in_mints_and_refreshes() -> None:
    player = await provision_player("py-auth")
    try:
        me = await player.identity.users.me()
        assert str(me["userId"]) == player.user_id
        assert player.minted.app_id == CONFIG.app_id
        assert player.minted.expires_at
        refreshed = await player.game.refresh_gameplay_token()
        assert len(refreshed.token) == 64
        assert player.game.app_token is not None
        assert player.game.app_token.token == refreshed.token
        assert await player.game.server_status.server_with_least_clients() is not None
    finally:
        await player.aclose()


async def test_a_gameplay_token_waits_for_the_players_consents() -> None:
    email = derive_email("py-consent")
    async with crowdypy.AsyncCrowdyClient(http_url=CONFIG.api_url) as client:
        auth = await client.auth.register(email, derive_password(email))
        assert await client.auth.player_legal_acceptance() is False
        owner = await owner_client()
        try:
            tier = await ensure_tier(owner, CONFIG.app_id)
            await owner.admin.app_access.grant(
                {"appId": CONFIG.app_id, "userId": auth.user.user_id, "tierId": tier}
            )
        finally:
            await owner.aclose()
        with pytest.raises(crowdypy.CrowdyError) as refused:
            await client.portal.mint_app_token(CONFIG.app_id)
        assert crowdypy.is_legal_acceptance_required_error(refused.value)
        assert await client.auth.record_player_consents(
            accept_legal=True, attest_age_of_majority=True
        )
        assert await client.auth.player_legal_acceptance() is True
        minted = await client.portal.mint_app_token(CONFIG.app_id)
        assert len(minted.token) == 64


async def test_a_wrong_password_is_refused() -> None:
    async with crowdypy.AsyncCrowdyClient(http_url=CONFIG.api_url) as client:
        with pytest.raises(crowdypy.CrowdyGraphQLError):
            await client.auth.login(derive_email("py-nobody"), "Wrong-password-1!")


async def test_the_identity_token_is_not_a_gameplay_token() -> None:
    player = await provision_player("py-two-tokens")
    try:
        async with crowdypy.AsyncCrowdyClient(
            http_url=player.minted.game_api_url or CONFIG.api_url
        ) as wrong:
            wrong.set_token(player.identity.get_token())
            with pytest.raises(crowdypy.CrowdyError):
                await wrong.server_status.server_with_least_clients()
    finally:
        await player.aclose()
