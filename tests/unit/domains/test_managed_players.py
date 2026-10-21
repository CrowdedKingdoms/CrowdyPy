"""Org-managed players (CrowdyJS 18.16.0, dev only).

The device holds the player's own app token and never the organization's org token, so the
SDK carries the device's two steps (``portal.adopt_app_token``,
``portal.exchange_managed_player_assertion``) and the organization's administration
(``managed_players``), and no org-token mint.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from conftest import MockApi
from crowdypy import AsyncCrowdyClient
from crowdypy.auth_state import AuthState
from crowdypy.domains import portal as portal_module
from crowdypy.domains.managed_players import ManagedPlayersAPI, private_jwk_member
from crowdypy.domains.organizations import OrganizationsAPI
from crowdypy.domains.portal import AppTokenResponse, PortalAPI
from crowdypy.errors import (
    CrowdyAssertionRejected,
    assertion_rejected_of,
    managed_player_not_allowed_of,
)
from crowdypy.graphql import AsyncGraphQLClient

ROOT = Path(__file__).resolve().parents[3]

MINTED: dict[str, Any] = {
    "token": "c" * 64,
    "gameTokenId": "9",
    "appId": "7",
    "expiresAt": "2026-10-22T00:00:00.000Z",
}


async def test_adopt_holds_the_app_token_the_organization_minted(
    graphql: AsyncGraphQLClient, session: AuthState, api: MockApi
) -> None:
    portal = PortalAPI(graphql, session)
    token = portal.adopt_app_token({**MINTED, "token": "a" * 64})
    assert isinstance(token, AppTokenResponse)
    assert session.get_token() == "a" * 64
    assert api.sent == []


@pytest.mark.parametrize(
    "response",
    [{**MINTED, "token": "cko_" + "b" * 64}, {**MINTED, "token": ""}, {}],
)
async def test_adopt_refuses_an_org_token_and_a_missing_one(
    graphql: AsyncGraphQLClient, session: AuthState, response: dict[str, Any]
) -> None:
    portal = PortalAPI(graphql, session)
    with pytest.raises(ValueError):
        portal.adopt_app_token(response)
    assert session.get_token() is None


async def test_exchange_trades_the_assertion_and_holds_the_token(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    api.reply_with_root(MINTED)
    token = await PortalAPI(graphql, session).exchange_managed_player_assertion(7, "h.p.s")
    assert token.token == "c" * 64
    assert session.get_token() == "c" * 64
    assert api.last.operation_name == "ExchangeManagedPlayerAssertion"
    assert "exchangeManagedPlayerAssertion(input: $input)" in api.last.query
    assert api.last.variables == {"input": {"appId": "7", "assertion": "h.p.s"}}


async def test_each_admin_field_is_sent_with_its_arguments(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    api.reply_with_root(True)
    players = ManagedPlayersAPI(graphql)
    await players.list(3, search="acct-", first=20)
    await players.get(3, "acct-1")
    await players.provision({"orgId": "3", "players": [{"externalId": "acct-1"}]})
    await players.update({"orgId": "3", "externalId": "acct-1", "disabled": True})
    await players.end_sessions(3, "acct-1")
    await players.delete(3, "acct-1")
    await players.settings(3)
    await players.signing_keys(3)
    await players.revoke_signing_key(3, 11)
    await players.set_app_mods(7, False)
    assert [(sent.operation_name, sent.variables) for sent in api.sent] == [
        ("ManagedPlayers", {"orgId": "3", "search": "acct-", "first": 20}),
        ("ManagedPlayer", {"orgId": "3", "externalId": "acct-1"}),
        (
            "ProvisionManagedPlayers",
            {"input": {"orgId": "3", "players": [{"externalId": "acct-1"}]}},
        ),
        (
            "UpdateManagedPlayer",
            {"input": {"orgId": "3", "externalId": "acct-1", "disabled": True}},
        ),
        ("EndManagedPlayerSessions", {"orgId": "3", "externalId": "acct-1"}),
        ("DeleteManagedPlayer", {"orgId": "3", "externalId": "acct-1"}),
        ("OrgManagedPlayerSettings", {"orgId": "3"}),
        ("OrgSigningKeys", {"orgId": "3"}),
        ("RevokeOrgSigningKey", {"orgId": "3", "keyId": "11"}),
        ("SetAppManagedPlayerMods", {"appId": "7", "enabled": False}),
    ]


async def test_app_mods_enabled_reads_the_switch_and_is_false_for_an_unseen_app(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    players = ManagedPlayersAPI(graphql)
    api.reply_with_root({"appId": "7", "managedPlayerModsEnabled": True})
    assert await players.app_mods_enabled(7) is True
    api.reply_with_root(None)
    assert await players.app_mods_enabled(7) is False


async def test_register_signing_key_sends_jwk_text_and_refuses_a_private_key(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    api.reply_with_root({"keyId": "11"})
    players = ManagedPlayersAPI(graphql)
    base = {"orgId": "3", "kid": "k1", "algorithm": "EdDSA", "issuer": "https://id.example"}
    await players.register_signing_key(
        {**base, "publicJwk": {"kty": "OKP", "crv": "Ed25519", "x": "abc"}}
    )
    assert api.last.variables["input"]["publicJwk"] == '{"kty":"OKP","crv":"Ed25519","x":"abc"}'
    with pytest.raises(ValueError, match="private member 'd'"):
        await players.register_signing_key(
            {**base, "publicJwk": '{"kty":"OKP","crv":"Ed25519","x":"abc","d":"secret"}'}
        )
    assert len(api.sent) == 1
    assert private_jwk_member({"kty": "RSA", "n": "n", "e": "AQAB", "p": "s"}) == "p"
    assert private_jwk_member("not json") is None


async def test_revoke_token_sends_end_sessions_only_when_asked(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    api.reply_with_root(True)
    orgs = OrganizationsAPI(graphql)
    await orgs.revoke_token(5)
    await orgs.revoke_token(5, end_sessions=True)
    assert [sent.variables for sent in api.sent] == [
        {"orgTokenId": "5"},
        {"orgTokenId": "5", "endSessions": True},
    ]


def test_the_refusal_readers() -> None:
    rejected = {"message": "no", "extensions": {"code": "ASSERTION_REJECTED", "reason": "replayed"}}
    not_allowed = {"message": "no", "extensions": {"code": "MANAGED_PLAYER_NOT_ALLOWED"}}
    assert assertion_rejected_of(rejected) == CrowdyAssertionRejected("replayed")
    assert assertion_rejected_of(not_allowed) is None
    assert managed_player_not_allowed_of(not_allowed) is True
    assert managed_player_not_allowed_of(rejected) is False


def test_the_client_exposes_managed_players_at_the_top_and_under_admin() -> None:
    client = AsyncCrowdyClient(http_url="https://ck.example.test")
    assert isinstance(client.managed_players, ManagedPlayersAPI)
    assert client.admin.managed_players is client.managed_players


def test_no_operation_mints_with_an_org_token() -> None:
    operations = "".join(p.read_text() for p in (ROOT / "operations").rglob("*.graphql"))
    assert "managedPlayers(" in operations
    assert "mintManagedPlayerToken" not in operations
    # The installed module: a wheel test run has no src/ tree.
    portal = Path(portal_module.__file__).read_text()
    assert re.search(r"mintManagedPlayerToken\s*\(", portal) is None
