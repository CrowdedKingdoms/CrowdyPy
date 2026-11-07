"""The teen safety surface (ck-api v2.60.0 and v2.61.0) against a live deployment: sign-up with an
age and a country, a sign-up from a blocked country refused before any account exists, the
documents, a player's voice and video scopes, contacts, blocks, a report and its moderation,
what existing accounts read (whether the app's organization appointed a safety moderator, a
player's own records in the input log, the territory list's regions), and an org-managed
player's asserted age, country and region.

A tier's sign-ups are limited and shared (dev: 30 an hour per address, every attempt counts),
so mind which tests register: the sign-up test one account (deleted at the end), the two-player
scenarios two (reused within a run). The existing-account, Minecraft and managed-player
scenarios register nobody. The two-player scenarios also need an owner that may administer the
app's organization: on a tier that asks for the developer documents, one that has accepted them
(``auth.developer_legal_acceptance()``), or its ``grant`` is refused. The existing-account scenario also needs ``CROWDY_E2E_PLAYER_EMAIL`` /
``CROWDY_E2E_PLAYER_PASSWORD``, an existing player outside the app's organization; unset, it
skips.

``CROWDY_E2E_UNDER13_SIGNUP=1`` also checks that a sign-up under 13 is refused. Since ck-api
v2.61.0 an age refusal at sign-up or at ``record_player_age`` blocks the same address and user
agent from both for 24 hours, so never set it on a machine others register from; no other test
declares an age the platform refuses.

The managed-player scenarios need an organization's server credential: an org token with
``players:mint`` and ``players:manage`` (``CROWDY_E2E_ORG_TOKEN``) and one of that
organization's apps (``CROWDY_E2E_MANAGED_APP_ID``); unset, they skip. A managed refusal locks
only that player (its organization and external id), and every run uses fresh ids. The region
cases need a blocked region on the territory list (Ukraine's sanctioned regions since v2.61.0).
The minor's defaults, parental consent and the safety-log and safety-moderator refusals need a
minor's account, so they are left to the unit suites.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

import crowdypy
from crowdypy.graphql import graphql_endpoint
from e2e.conftest import (
    CONFIG,
    derive_email,
    ensure_tier,
    identity_client,
    owner_client,
    provision_player,
    register_raw,
)


def _refusal(payload: dict[str, Any]) -> crowdypy.CrowdyGraphQLError:
    assert payload.get("errors"), f"expected a refusal, got {payload.get('data')}"
    return crowdypy.CrowdyGraphQLError(payload["errors"])


def _years_ago(years: int) -> str:
    today = datetime.now(UTC).date()
    return f"{today.year - years:04d}-{today.month:02d}-01"


async def test_sign_up_with_an_age_and_a_country_and_the_documents() -> None:
    email = derive_email("py-age")
    payload = await register_raw(
        email,
        acceptLegal=True,
        attestAgeOfMajority=True,
        birthDate="1990-05-15",
        country="US",
    )
    assert not payload.get("errors"), payload["errors"] if payload.get("errors") else None
    async with crowdypy.AsyncCrowdyClient(http_url=CONFIG.api_url) as client:
        client.set_token(payload["data"]["register"]["token"])
        try:
            me = await client.users.me()
            assert (me["ageBracket"], me["country"]) == ("18_plus", "US")
            with pytest.raises(crowdypy.CrowdyGraphQLError) as again:
                await client.auth.record_player_age(birth_date="1990-05", country="US")
            assert again.value.code == "CONFLICT"

            documents = await client.auth.player_legal_documents()
            assert documents
            assert all({"documentId", "version", "title", "url"} <= set(doc) for doc in documents)
            assert isinstance(await client.auth.app_required_documents(CONFIG.app_id), list)
            assert isinstance(await client.auth.developer_legal_documents(), list)
            assert await client.auth.player_legal_acceptance(app_id=CONFIG.app_id) is True
            assert await client.auth.record_player_consents(
                accept_legal=True,
                attest_age_of_majority=True,
                app_id=CONFIG.app_id,
                method="CLICKWRAP",
            )

            policies = await client.platform.territory_policies()
            countries = {p["countryCode"]: p for p in policies if p["regionCode"] is None}
            assert countries["CU"]["status"] == "blocked"
            assert countries["KR"]["status"] == "open"
        finally:
            # The account is this test's alone: no later suite signs it in.
            with contextlib.suppress(crowdypy.CrowdyError):
                await client.users.delete_my_account()


async def test_an_age_or_a_country_the_platform_refuses_makes_no_account() -> None:
    if os.environ.get("CROWDY_E2E_UNDER13_SIGNUP") == "1":
        under = await register_raw(
            derive_email("py-young"),
            acceptLegal=True,
            birthDate=_years_ago(10),
            country="US",
            parentalConfirmation=True,
        )
        young = _refusal(under)
        assert crowdypy.age_requirement_not_met_of(young), young.graphql_errors
        assert not crowdypy.territory_not_supported_of(young)

    blocked = _refusal(
        await register_raw(
            derive_email("py-cu"),
            acceptLegal=True,
            attestAgeOfMajority=True,
            birthDate="1990-05-15",
            country="CU",
        )
    )
    territory = crowdypy.territory_not_supported_of(blocked)
    assert territory == crowdypy.CrowdyTerritoryNotSupported("CU"), blocked.graphql_errors


async def test_scopes_contacts_and_blocks_between_two_players() -> None:
    a = await provision_player("py-a")
    b = await provision_player("py-b")
    app = CONFIG.app_id
    try:
        settings = await a.game.safety.comm_settings(app)
        assert settings["isMinor"] is False
        assert settings["voiceAllowed"] == "EVERYONE"
        lowered = await a.game.safety.set_comm_settings(app, voice_receive="CONTACTS")
        assert lowered["voiceReceive"] == "CONTACTS"
        assert lowered["choices"]["voiceReceive"] == "CONTACTS"
        restored = await a.game.safety.set_comm_settings(app, voice_receive="EVERYONE")
        assert restored["voiceReceive"] == "EVERYONE"

        asked = await a.game.safety.request_contact(app, b.user_id)
        assert (str(asked["userId"]), asked["state"]) == (b.user_id, "PENDING")
        incoming = await b.game.safety.contact_requests(app)
        assert {(str(r["userId"]), r["direction"]) for r in incoming} == {(a.user_id, "INCOMING")}
        outgoing = await a.game.safety.contact_requests(app)
        assert {(str(r["userId"]), r["direction"]) for r in outgoing} == {(b.user_id, "OUTGOING")}
        accepted = await b.game.safety.accept_contact(app, a.user_id)
        assert accepted["state"] == "ACCEPTED"
        assert b.user_id in {str(c["userId"]) for c in await a.game.safety.contacts(app)}
        assert await a.game.safety.remove_contact(app, b.user_id) is True
        assert await a.game.safety.contacts(app) == []
        assert await b.game.safety.contacts(app) == []

        blocked = await a.game.safety.block(app, b.user_id)
        assert str(blocked["userId"]) == b.user_id
        assert {str(x["userId"]) for x in await a.game.safety.blocks(app)} == {b.user_id}
        # The blocked player is not told: its request answers PENDING and nothing is stored.
        assert (await b.game.safety.request_contact(app, a.user_id))["state"] == "PENDING"
        assert await a.game.safety.contact_requests(app) == []
        assert await a.game.safety.unblock(app, b.user_id) is True
        assert await a.game.safety.blocks(app) == []
        assert await a.game.safety.unblock(app, b.user_id) is False
    finally:
        await a.aclose()
        await b.aclose()


async def test_recording_consent_and_a_report_its_moderator_resolves() -> None:
    a = await provision_player("py-a")
    b = await provision_player("py-b")
    app = CONFIG.app_id
    owner = await owner_client()
    try:
        recording = await a.game.apps.recording(app)
        assert recording is not None
        assert {type(recording[k]) for k in ("enabled", "voice", "video")} == {bool}

        consent = await a.game.safety.request_parental_consent(
            app, "VOICE", "crowdy-e2e-parent@test.invalid"
        )
        assert consent["status"] == "NOT_REQUIRED"

        receipt = await a.game.safety.file_report(
            app,
            b.user_id,
            "OTHER",
            datetime.now(UTC) - timedelta(minutes=1),
            details="CrowdyPy e2e: an automated test report; resolve it.",
        )
        report_id = str(receipt["reportId"])
        assert receipt["filedAt"]

        reports = await owner.moderation.reports(app, status="OPEN", first=50)
        mine = next(r for r in reports if str(r["reportId"]) == report_id)
        assert str(mine["reporterUserId"]) == a.user_id
        assert str(mine["reportedUserId"]) == b.user_id
        assert mine["reason"] == "OTHER"
        resolved = await owner.moderation.resolve_report(app, report_id, "CrowdyPy e2e cleanup")
        assert resolved["status"] == "RESOLVED"
    finally:
        await owner.aclose()
        await a.aclose()
        await b.aclose()


async def test_minecraft_sign_in_is_closed_to_an_app_that_is_not_crowded_kingdoms_own() -> None:
    async with crowdypy.AsyncCrowdyClient(http_url=CONFIG.api_url) as client:
        with pytest.raises(crowdypy.CrowdyGraphQLError) as refused:
            await client.portal.minecraft_challenge(CONFIG.app_id)
        assert refused.value.code in {"FORBIDDEN", "FEATURE_DISABLED"}


# ---------------------------------------------------------------------- existing accounts

PLAYER_EMAIL = os.environ.get("CROWDY_E2E_PLAYER_EMAIL", "").strip()
PLAYER_PASSWORD = os.environ.get("CROWDY_E2E_PLAYER_PASSWORD", "").strip()
existing = pytest.mark.skipif(
    not (PLAYER_EMAIL and PLAYER_PASSWORD and CONFIG.owner_password),
    reason="existing accounts: set CROWDY_E2E_PLAYER_EMAIL, CROWDY_E2E_PLAYER_PASSWORD and "
    "CROWDY_E2E_OWNER_PASSWORD",
)


@existing
async def test_existing_accounts_read_the_moderator_their_own_records_and_the_regions() -> None:
    app = CONFIG.app_id
    owner = await owner_client()
    player, player_id = await identity_client(PLAYER_EMAIL, PLAYER_PASSWORD)
    game: crowdypy.AsyncCrowdyClient | None = None
    try:
        # A member of the app's organization reads whether it appointed a safety moderator;
        # an outsider reads None.
        as_member = await owner.apps.app(app)
        assert as_member is not None
        assert isinstance(as_member["hasSafetyModerator"], bool), as_member
        as_outsider = await player.apps.app(app)
        assert as_outsider is not None
        assert as_outsider["hasSafetyModerator"] is None

        # The player's own records: the documents the tier asks for now, a token, its sessions.
        if not await player.auth.player_legal_acceptance(app_id=app):
            await player.auth.record_player_consents(accept_legal=True, app_id=app)
        try:
            minted = await player.portal.mint_app_token(app)
        except crowdypy.CrowdyGraphQLError as error:
            refusal = crowdypy.access_refusal_of(error)
            if refusal is None or refusal.code != "ACCESS_NOT_GRANTED":
                raise
            tier = await ensure_tier(owner, app)
            await owner.admin.app_access.grant({"appId": app, "userId": player_id, "tierId": tier})
            minted = await player.portal.mint_app_token(app)
        game = crowdypy.AsyncCrowdyClient(
            http_url=CONFIG.http_url or minted.game_api_url or CONFIG.api_url,
            discovery_url=minted.discovery_url or CONFIG.api_url,
        )
        game.set_app_token(minted)
        page = await game.input_log.sessions(app, first=5)
        assert isinstance(page["edges"], list)
        assert {str(edge["node"]["userId"]) for edge in page["edges"]} <= {player_id}

        # Every territory row has a regionCode, and a region's names its country.
        policies = await player.platform.territory_policies()
        assert all("regionCode" in p for p in policies)
        for policy in policies:
            if policy["regionCode"] is not None:
                assert re.fullmatch(
                    rf"{policy['countryCode']}-[A-Z0-9]{{1,3}}", policy["regionCode"]
                ), policy
    finally:
        if game is not None:
            await game.aclose()
        for client in (player, owner):
            with contextlib.suppress(crowdypy.CrowdyError):
                await client.auth.logout()
            await client.aclose()


# ---------------------------------------------------------------------- org-managed players

ORG_TOKEN = os.environ.get("CROWDY_E2E_ORG_TOKEN", "").strip()
MANAGED_APP_ID = os.environ.get("CROWDY_E2E_MANAGED_APP_ID", "").strip()
managed = pytest.mark.skipif(
    not (ORG_TOKEN and MANAGED_APP_ID),
    reason="managed players: set CROWDY_E2E_ORG_TOKEN and CROWDY_E2E_MANAGED_APP_ID",
)

_MINT = (
    "mutation M($i: MintManagedPlayerTokenInput!) { mintManagedPlayerToken(input: $i) "
    "{ token gameTokenId appId expiresAt gameApiUrl gameApiWsUrl discoveryUrl launchUrl } }"
)


async def _org_mint(**fields: Any) -> dict[str, Any]:
    """The organization's server: ``mintManagedPlayerToken`` with its org token. No SDK wraps
    it, so this one request goes direct, and only the app token it returns reaches a client."""
    variables = {"i": {"appId": MANAGED_APP_ID, **fields}}
    async with httpx.AsyncClient(timeout=60.0) as http:
        res = await http.post(
            graphql_endpoint(CONFIG.api_url) or CONFIG.api_url,
            json={"query": _MINT, "variables": variables},
            headers={"authorization": f"Bearer {ORG_TOKEN}"},
        )
    payload: dict[str, Any] = res.json()
    return payload


def _external_id(tag: str) -> str:
    return f"crowdypy-e2e-{tag}-{secrets.token_hex(4)}"


async def _admitted(external_id: str, **fields: Any) -> dict[str, Any]:
    payload = await _org_mint(externalId=external_id, **fields)
    assert not payload.get("errors"), (external_id, payload.get("errors"))
    minted: dict[str, Any] = payload["data"]["mintManagedPlayerToken"]
    return minted


@managed
async def test_managed_players_carry_what_their_organization_asserts_or_are_never_made() -> None:
    adult = _external_id("adult")
    minted = await _admitted(adult, ageYears=30, country="US")
    created, refused = [adult], []
    async with (
        crowdypy.AsyncCrowdyClient(http_url=minted.get("gameApiUrl") or CONFIG.api_url) as game,
        crowdypy.AsyncCrowdyClient(http_url=CONFIG.api_url) as org,
    ):
        org.set_token(ORG_TOKEN)
        game.set_app_token(game.portal.adopt_app_token(minted))
        me = await game.users.me()
        try:
            assert me["userType"] == "org_managed"
            assert (me["ageBracket"], me["country"]) == ("18_plus", "US")
            assert (await game.safety.comm_settings(MANAGED_APP_ID))["isMinor"] is False
            assert len((await game.refresh_gameplay_token()).token) == 64

            # Refused before a player exists: under the youngest age admitted, a blocked country.
            young = _external_id("child")
            refusal = _refusal(await _org_mint(externalId=young, ageYears=12, country="US"))
            assert crowdypy.age_requirement_not_met_of(refusal), refusal.graphql_errors
            cuban = _external_id("cu")
            refusal = _refusal(await _org_mint(externalId=cuban, ageYears=30, country="CU"))
            assert crowdypy.territory_not_supported_of(
                refusal
            ) == crowdypy.CrowdyTerritoryNotSupported("CU"), refusal.graphql_errors
            refused += [young, cuban]

            # A country checked by region (ck-api v2.61.0): a blocked region and no region are
            # refused, and another region of it is admitted.
            policies = await game.platform.territory_policies()
            blocked_region = next(
                (p for p in policies if p["regionCode"] and p["status"] == "blocked"), None
            )
            if blocked_region is not None:
                country = blocked_region["countryCode"]
                in_region, no_region = _external_id("in-region"), _external_id("no-region")
                refusal = _refusal(
                    await _org_mint(
                        externalId=in_region,
                        ageYears=30,
                        country=country,
                        region=blocked_region["regionCode"],
                    )
                )
                assert crowdypy.territory_not_supported_of(refusal), refusal.graphql_errors
                refusal = _refusal(
                    await _org_mint(externalId=no_region, ageYears=30, country=country)
                )
                assert crowdypy.territory_not_supported_of(refusal), refusal.graphql_errors
                refused += [in_region, no_region]
                rows = [p for p in policies if p["countryCode"] == country]
                open_country = all(p["status"] != "blocked" for p in rows if not p["regionCode"])
                if (
                    country == "UA"
                    and open_country
                    and "UA-30" not in {p["regionCode"] for p in rows}
                ):
                    kyiv = _external_id("ua-30")
                    await _admitted(kyiv, ageYears=30, country="UA", region="UA-30")
                    created.append(kyiv)

            for external_id in refused:
                assert await org.managed_players.get(me["orgId"], external_id) is None, external_id
        finally:
            for external_id in created:
                assert await org.managed_players.delete(me["orgId"], external_id), external_id
