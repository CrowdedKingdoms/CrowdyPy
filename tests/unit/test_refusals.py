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
    CrowdyAccountBlocked,
    CrowdyActorExists,
    CrowdyAppPaused,
    CrowdyGraphQLError,
    CrowdyLegalAcceptanceRequired,
    CrowdyMinecraftVerificationFailed,
    CrowdyMissingLegalDocument,
    CrowdyTerritoryNotSupported,
    access_refusal_of,
    account_blocked_of,
    actor_exists_of,
    age_required_of,
    age_requirement_not_met_of,
    app_paused_of,
    group_member_banned_of,
    is_app_paused,
    legal_acceptance_required_of,
    minecraft_verification_failed_of,
    minecraft_verification_unavailable_of,
    parental_consent_unavailable_of,
    safety_log_required_of,
    social_limit_reached_of,
    territory_not_supported_of,
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


def test_account_blocked_and_group_member_banned_are_read_from_an_error_or_an_entry() -> None:
    timed = CrowdyGraphQLError(
        [
            {
                "message": "This account is blocked until …",
                "extensions": {
                    "code": "ACCOUNT_BLOCKED",
                    "blockedUntil": "2026-10-15T12:00:00.000Z",
                },
            }
        ]
    )
    assert account_blocked_of(timed) == CrowdyAccountBlocked("2026-10-15T12:00:00.000Z")
    assert account_blocked_of(
        {"message": "m", "extensions": {"code": "ACCOUNT_BLOCKED"}}
    ) == CrowdyAccountBlocked(None)
    assert account_blocked_of({"message": "m", "extensions": {"code": "FORBIDDEN"}}) is None
    assert account_blocked_of(ValueError("x")) is None
    banned = CrowdyGraphQLError(
        [
            {"message": "a", "extensions": {"code": "BAD_USER_INPUT"}},
            {"message": "b", "extensions": {"code": "GROUP_MEMBER_BANNED"}},
        ]
    )
    assert group_member_banned_of(banned) is True
    assert group_member_banned_of({"message": "m", "extensions": {"code": "FORBIDDEN"}}) is False
    assert crowdypy.ACCOUNT_BLOCKED_CODE == "ACCOUNT_BLOCKED"
    assert crowdypy.GROUP_MEMBER_BANNED_CODE == "GROUP_MEMBER_BANNED"
    assert crowdypy.EXEC_CLOSE_ACCESS_ENDED == 4403


# The teen safety refusals (ck-api v2.60.0, CrowdyJS 18.24.0).


def test_legal_acceptance_required_of_lists_the_missing_documents() -> None:
    error = refused(
        code="LEGAL_ACCEPTANCE_REQUIRED",
        missingDocuments=[
            {"documentId": "player-terms", "version": "player-terms-2026-11-01", "appId": None},
            {"documentId": "rules", "version": "rules-2", "appId": "42"},
            {"documentId": "addendum", "version": "a-1", "appId": 43},
            {"documentId": "no-version"},
            "junk",
        ],
    )
    assert legal_acceptance_required_of(error) == CrowdyLegalAcceptanceRequired(
        (
            CrowdyMissingLegalDocument("player-terms", "player-terms-2026-11-01", None),
            CrowdyMissingLegalDocument("rules", "rules-2", "42"),
            CrowdyMissingLegalDocument("addendum", "a-1", "43"),
        )
    )
    assert legal_acceptance_required_of(refused(code="LEGAL_ACCEPTANCE_REQUIRED")) == (
        CrowdyLegalAcceptanceRequired(())
    )
    assert legal_acceptance_required_of(refused(code="ORG_LEGAL_ACCEPTANCE_REQUIRED")) is None
    assert legal_acceptance_required_of(ValueError("LEGAL_ACCEPTANCE_REQUIRED")) is None


def test_the_age_refusals_are_booleans_and_never_surface_an_age() -> None:
    assert age_required_of(refused(code="AGE_REQUIRED")) is True
    assert age_required_of(refused(code="AGE_REQUIREMENT_NOT_MET")) is False
    not_met = refused(code="AGE_REQUIREMENT_NOT_MET", minimumAge=14, ageBracket="13_15")
    assert age_requirement_not_met_of(not_met) is True
    assert age_requirement_not_met_of({"message": "m", "extensions": {"code": "AGE_REQUIRED"}}) is (
        False
    )
    assert age_requirement_not_met_of(None) is False


def test_territory_and_minecraft_refusals_carry_their_detail() -> None:
    assert territory_not_supported_of(
        refused(code="TERRITORY_NOT_SUPPORTED", country="DE")
    ) == CrowdyTerritoryNotSupported("DE")
    assert territory_not_supported_of(
        refused(code="TERRITORY_NOT_SUPPORTED", country="")
    ) == CrowdyTerritoryNotSupported(None)
    assert territory_not_supported_of(refused(code="FORBIDDEN")) is None
    assert minecraft_verification_failed_of(
        refused(code="MINECRAFT_VERIFICATION_FAILED", reason="not_joined")
    ) == CrowdyMinecraftVerificationFailed("not_joined")
    assert minecraft_verification_failed_of(
        refused(code="MINECRAFT_VERIFICATION_FAILED")
    ) == CrowdyMinecraftVerificationFailed(None)
    assert minecraft_verification_unavailable_of(refused(code="MINECRAFT_VERIFICATION_UNAVAILABLE"))
    assert not minecraft_verification_unavailable_of(refused(code="MINECRAFT_VERIFICATION_FAILED"))


def test_safety_log_consent_and_social_limit_refusals() -> None:
    later = CrowdyGraphQLError(
        [
            {"message": "a", "extensions": {"code": "BAD_USER_INPUT"}},
            {"message": "b", "extensions": {"code": "SOCIAL_LIMIT_REACHED"}},
        ]
    )
    assert social_limit_reached_of(later) is True
    assert safety_log_required_of(refused(code="SAFETY_LOG_REQUIRED")) is True
    assert parental_consent_unavailable_of(refused(code="PARENTAL_CONSENT_UNAVAILABLE")) is True
    assert not safety_log_required_of(refused(code="PARENTAL_CONSENT_UNAVAILABLE"))
    assert not parental_consent_unavailable_of(ValueError("x"))


def test_the_teen_safety_codes_are_on_the_package_root() -> None:
    codes = {
        "LEGAL_ACCEPTANCE_REQUIRED_CODE": "LEGAL_ACCEPTANCE_REQUIRED",
        "AGE_REQUIRED_CODE": "AGE_REQUIRED",
        "AGE_REQUIREMENT_NOT_MET_CODE": "AGE_REQUIREMENT_NOT_MET",
        "TERRITORY_NOT_SUPPORTED_CODE": "TERRITORY_NOT_SUPPORTED",
        "SAFETY_LOG_REQUIRED_CODE": "SAFETY_LOG_REQUIRED",
        "PARENTAL_CONSENT_UNAVAILABLE_CODE": "PARENTAL_CONSENT_UNAVAILABLE",
        "SOCIAL_LIMIT_REACHED_CODE": "SOCIAL_LIMIT_REACHED",
        "MINECRAFT_VERIFICATION_FAILED_CODE": "MINECRAFT_VERIFICATION_FAILED",
        "MINECRAFT_VERIFICATION_UNAVAILABLE_CODE": "MINECRAFT_VERIFICATION_UNAVAILABLE",
    }
    assert {name: getattr(crowdypy, name) for name in codes} == codes
    assert crowdypy.age_requirement_not_met_of is age_requirement_not_met_of
    assert crowdypy.legal_acceptance_required_of is legal_acceptance_required_of
