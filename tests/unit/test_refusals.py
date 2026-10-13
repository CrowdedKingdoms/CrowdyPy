"""The refusals and gates CrowdyJS 18.7.0 reads: ACTOR_EXISTS, the three access refusals,
APP_PAUSED, the runtime gate on token responses and the bootstrap, and the public player
profiles a game shows instead of another user's private fields."""

from __future__ import annotations

from typing import Any

import pytest

import crowdypy
from conftest import MockApi
from crowdypy.auth_state import AuthState
from crowdypy.domains.portal import AppRuntimeGate, AppTokenResponse, PortalAPI
from crowdypy.domains.users import PLAYER_PROFILES_MAX, UsersAPI
from crowdypy.errors import (
    CrowdyAccessRefusal,
    CrowdyActorExists,
    CrowdyAppPaused,
    CrowdyGraphQLError,
    access_refusal_of,
    actor_exists_of,
    app_paused_of,
    is_app_paused,
)
from crowdypy.graphql import AsyncGraphQLClient


def refused(**extensions: Any) -> CrowdyGraphQLError:
    return CrowdyGraphQLError([{"message": "refused", "extensions": extensions}])


def test_actor_exists_of_reads_owned_by_caller() -> None:
    assert actor_exists_of(refused(code="ACTOR_EXISTS", ownedByCaller=True)) == CrowdyActorExists(
        True
    )
    assert actor_exists_of(refused(code="ACTOR_EXISTS")) == CrowdyActorExists(None)
    assert actor_exists_of(refused(code="ACTOR_EXISTS", ownedByCaller="yes")) == CrowdyActorExists(
        None
    )
    assert actor_exists_of(refused(code="FORBIDDEN")) is None
    assert actor_exists_of(ValueError("x")) is None
    assert actor_exists_of(None) is None


def test_access_refusal_of_reads_the_three_codes_and_suspended_until() -> None:
    assert access_refusal_of(
        refused(code="ACCESS_SUSPENDED", suspendedUntil="2026-10-11T00:00:00.000Z")
    ) == CrowdyAccessRefusal("ACCESS_SUSPENDED", "2026-10-11T00:00:00.000Z")
    assert access_refusal_of(refused(code="ACCESS_REVOKED")) == CrowdyAccessRefusal(
        "ACCESS_REVOKED"
    )
    raw = {"message": "m", "extensions": {"code": "ACCESS_NOT_GRANTED"}}
    assert access_refusal_of(raw) == CrowdyAccessRefusal("ACCESS_NOT_GRANTED")
    assert access_refusal_of(refused(code="APP_PAUSED")) is None
    assert access_refusal_of({"message": "no extensions"}) is None


def test_app_paused_of_reads_the_reason_also_from_a_later_entry() -> None:
    error = CrowdyGraphQLError(
        [
            {"message": "a", "extensions": {"code": "BAD_USER_INPUT"}},
            {"message": "b", "extensions": {"code": "APP_PAUSED", "reason": "spend_cap"}},
        ]
    )
    assert app_paused_of(error) == CrowdyAppPaused("spend_cap")
    assert app_paused_of(refused(code="APP_PAUSED")) == CrowdyAppPaused(None)
    assert app_paused_of(refused(code="APP_UNAVAILABLE")) is None


def test_is_app_paused_is_any_status_but_active_and_false_without_a_gate() -> None:
    assert not is_app_paused({"status": "ACTIVE", "reason": None})
    assert not is_app_paused(AppRuntimeGate(status="ACTIVE"))
    for status in ("GRACE", "DENIED", "SUSPENDED"):
        assert is_app_paused({"status": status, "reason": "insufficient_funds"})
        assert is_app_paused(AppRuntimeGate(status=status, reason="spend_cap"))
    assert not is_app_paused(None)


def test_the_codes_and_readers_are_on_the_package_root() -> None:
    assert crowdypy.APP_PAUSED_CODE == "APP_PAUSED"
    assert crowdypy.ACTOR_EXISTS_CODE == "ACTOR_EXISTS"
    assert {
        crowdypy.ACCESS_REVOKED_CODE,
        crowdypy.ACCESS_SUSPENDED_CODE,
        crowdypy.ACCESS_NOT_GRANTED_CODE,
    } == {"ACCESS_REVOKED", "ACCESS_SUSPENDED", "ACCESS_NOT_GRANTED"}
    assert crowdypy.is_app_paused is is_app_paused
    assert crowdypy.AppRuntimeGate is AppRuntimeGate


MINTED: dict[str, Any] = {
    "token": "t" * 64,
    "gameTokenId": "9",
    "appId": "42",
    "expiresAt": "2026-10-10T12:00:00.000Z",
    "runtimeGate": {"status": "DENIED", "reason": "insufficient_funds"},
}


async def test_every_token_mutation_selects_the_runtime_gate(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    api.reply_with_root(MINTED)
    portal = PortalAPI(graphql, session)
    for token in (
        await portal.mint_app_token(42),
        await portal.exchange_code("code", "verifier"),
        await portal.refresh(),
        await portal.refresh({"ip4": "203.0.113.5", "clientPort": 4000}),
    ):
        assert token.runtime_gate == AppRuntimeGate(status="DENIED", reason="insufficient_funds")
        assert is_app_paused(token.runtime_gate), "a paused app still mints"
    assert all("runtimeGate { status reason }" in sent.query for sent in api.sent)


async def test_a_token_from_a_server_without_the_gate_reads_as_not_paused(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    api.reply_with_root({key: value for key, value in MINTED.items() if key != "runtimeGate"})
    token = await PortalAPI(graphql, session).mint_app_token("42")
    assert isinstance(token, AppTokenResponse)
    assert token.runtime_gate is None
    assert not is_app_paused(token.runtime_gate)


PROFILE = {"userId": "7", "gamertag": "seven", "disambiguation": "0001"}


async def test_player_profiles_read_public_fields_in_one_call(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    users = UsersAPI(graphql)
    api.reply_with_root(PROFILE)
    assert await users.player_profile(7) == PROFILE
    assert (api.last.operation_name, api.last.variables) == ("PlayerProfile", {"userId": "7"})
    api.reply_with_root([PROFILE])
    assert await users.player_profiles([7, "8"]) == [PROFILE]
    assert (api.last.operation_name, api.last.variables) == (
        "PlayerProfiles",
        {"userIds": ["7", "8"]},
    )


async def test_player_profiles_refuses_more_than_100_ids_and_sends_nothing_for_none(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    users = UsersAPI(graphql)
    assert PLAYER_PROFILES_MAX == 100
    assert await users.player_profiles([]) == []
    with pytest.raises(ValueError, match="at most 100 ids: 101"):
        await users.player_profiles(range(101))
    with pytest.raises(TypeError):
        await users.player_profiles("123")
    assert api.sent == []
