"""The teen safety surface (ck-api v2.60.0) against a live deployment: sign-up with an age and
a country, the refusals a tier still admitting adults only can give, the documents, a player's
voice and video scopes, contacts, blocks, a report and its moderation, and an org-managed
player's asserted age and country.

The managed-player scenarios need an organization's server credential: an org token with
``players:mint`` (``CROWDY_E2E_ORG_TOKEN``) and one of that organization's apps
(``CROWDY_E2E_MANAGED_APP_ID``); unset, they skip. A tier admitting adults only (the floor at
18) refuses every minor, so the minor's defaults, parental consent and the safety-log refusal
are left to the unit suites there.
"""

from __future__ import annotations

import os
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
            accept_legal=True, attest_age_of_majority=True, app_id=CONFIG.app_id, method="CLICKWRAP"
        )

        policies = {p["countryCode"]: p for p in await client.platform.territory_policies()}
        assert policies["DE"]["status"] == "blocked"
        assert policies["KR"]["status"] == "open"


async def test_an_age_or_a_country_the_platform_refuses_makes_no_account() -> None:
    under = await register_raw(
        derive_email("py-young"),
        acceptLegal=True,
        birthDate=_years_ago(14),
        country="US",
        parentalConfirmation=True,
    )
    young = _refusal(under)
    assert crowdypy.age_requirement_not_met_of(young), young.graphql_errors
    assert not crowdypy.territory_not_supported_of(young)

    blocked = _refusal(
        await register_raw(
            derive_email("py-de"),
            acceptLegal=True,
            attestAgeOfMajority=True,
            birthDate="1990-05-15",
            country="DE",
        )
    )
    territory = crowdypy.territory_not_supported_of(blocked)
    assert territory == crowdypy.CrowdyTerritoryNotSupported("DE"), blocked.graphql_errors


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


@managed
async def test_a_managed_player_carries_the_age_and_country_its_organization_asserts() -> None:
    external_id = _external_id("adult")
    payload = await _org_mint(externalId=external_id, ageYears=30, country="US")
    assert not payload.get("errors"), payload.get("errors")
    minted = payload["data"]["mintManagedPlayerToken"]
    async with crowdypy.AsyncCrowdyClient(
        http_url=minted.get("gameApiUrl") or CONFIG.api_url
    ) as game:
        game.set_app_token(game.portal.adopt_app_token(minted))
        me = await game.users.me()
        try:
            assert (me["ageBracket"], me["country"]) == ("18_plus", "US")
            assert (await game.safety.comm_settings(MANAGED_APP_ID))["isMinor"] is False
            assert len((await game.refresh_gameplay_token()).token) == 64
        finally:
            async with crowdypy.AsyncCrowdyClient(http_url=CONFIG.api_url) as org:
                org.set_token(ORG_TOKEN)
                assert await org.managed_players.delete(me["orgId"], external_id)


@managed
async def test_a_managed_player_under_the_age_or_in_a_blocked_country_is_refused() -> None:
    young = _refusal(await _org_mint(externalId=_external_id("child"), ageYears=12, country="US"))
    assert crowdypy.age_requirement_not_met_of(young), young.graphql_errors
    blocked = _refusal(await _org_mint(externalId=_external_id("de"), ageYears=30, country="DE"))
    assert crowdypy.territory_not_supported_of(blocked) == crowdypy.CrowdyTerritoryNotSupported(
        "DE"
    ), blocked.graphql_errors
