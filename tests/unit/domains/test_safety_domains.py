"""The teen safety surface (ck-api v2.60.0, CrowdyJS 18.24.0): the player's own safety controls
(``client.safety``), the moderators' reports (``client.moderation``), and what auth, apps,
platform and portal gained for age, territory and documents.

``safety`` and ``moderation`` are thin wrappers in CrowdyJS, so the contract under test is the
operation, the variables (BigInt ids as decimal strings, omitted optionals) and the root field
handed back untouched. Auth and portal declare their documents inline, as CrowdyJS does.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import crowdypy
from conftest import MockApi
from crowdypy._generated import operations as ops
from crowdypy._operation import Operation
from crowdypy.auth_state import AuthState
from crowdypy.domains import auth as auth_module
from crowdypy.domains import portal as portal_module
from crowdypy.domains._base import Domain
from crowdypy.domains.apps import AppsAPI
from crowdypy.domains.auth import AuthAPI, PlayerAge
from crowdypy.domains.moderation import ModerationAPI
from crowdypy.domains.portal import (
    AcceptedLegalDocument,
    AppTokenResponse,
    MinecraftChallenge,
    PortalAPI,
)
from crowdypy.domains.safety import SafetyAPI
from crowdypy.graphql import AsyncGraphQLClient

#: Each class's public CrowdyJS methods, as the CrowdyJS source names them.
CROWDYJS_METHODS: dict[type[Domain], list[str]] = {
    SafetyAPI: [
        "commSettings",
        "setCommSettings",
        "requestParentalConsent",
        "contacts",
        "contactRequests",
        "requestContact",
        "acceptContact",
        "removeContact",
        "blocks",
        "block",
        "unblock",
        "fileReport",
    ],
    ModerationAPI: ["reports", "resolveReport", "escalateReport"],
}

SETTINGS: dict[str, Any] = {
    "appId": "42",
    "isMinor": False,
    "voiceSend": "EVERYONE",
    "voiceReceive": "CONTACTS",
    "videoSend": "EVERYONE",
    "videoReceive": "EVERYONE",
    "choices": {
        "voiceSend": None,
        "voiceReceive": "CONTACTS",
        "videoSend": None,
        "videoReceive": None,
    },
    "voiceAllowed": "EVERYONE",
    "videoAllowed": "EVERYONE",
    "voiceConsent": False,
    "videoConsent": False,
    "updatedAt": "2026-10-30T12:00:00.000Z",
}
CONTACT: dict[str, Any] = {"userId": "7", "state": "PENDING", "createdAt": "t", "acceptedAt": None}
REQUEST: dict[str, Any] = {"userId": "8", "direction": "INCOMING", "createdAt": "t"}
BLOCK: dict[str, Any] = {"userId": "7", "createdAt": "t"}
REPORT: dict[str, Any] = {"reportId": "900", "status": "OPEN", "sessions": ["11"]}


@dataclass(frozen=True)
class Case:
    domain: type[Domain]
    method: str
    args: tuple[Any, ...]
    operation: str
    variables: dict[str, Any]
    reply: Any
    kwargs: Mapping[str, Any] = field(default_factory=dict)


CASES: list[Case] = [
    # -- safety ----------------------------------------------------------------------
    Case(SafetyAPI, "comm_settings", (42,), "MyCommSettings", {"appId": "42"}, SETTINGS),
    Case(
        SafetyAPI,
        "set_comm_settings",
        ("42",),
        "SetMyCommSettings",
        {"appId": "42", "input": {"voiceReceive": "CONTACTS", "videoSend": "OFF"}},
        SETTINGS,
        {"voice_receive": "CONTACTS", "video_send": "OFF"},
    ),
    Case(
        SafetyAPI,
        "request_parental_consent",
        (42, "VOICE", "parent@example.com"),
        "RequestParentalConsent",
        {"appId": "42", "feature": "VOICE", "parentEmail": "parent@example.com"},
        {"appId": "42", "feature": "VOICE", "status": "NOT_REQUIRED"},
    ),
    Case(SafetyAPI, "contacts", (42,), "MyContacts", {"appId": "42"}, [CONTACT]),
    Case(SafetyAPI, "contact_requests", (42,), "ContactRequests", {"appId": "42"}, [REQUEST]),
    Case(
        SafetyAPI,
        "request_contact",
        (42, 7),
        "RequestContact",
        {"appId": "42", "userId": "7"},
        CONTACT,
    ),
    Case(
        SafetyAPI,
        "accept_contact",
        ("42", "8"),
        "AcceptContact",
        {"appId": "42", "userId": "8"},
        {**CONTACT, "userId": "8", "state": "ACCEPTED"},
    ),
    Case(
        SafetyAPI, "remove_contact", (42, 7), "RemoveContact", {"appId": "42", "userId": "7"}, True
    ),
    Case(SafetyAPI, "blocks", (42,), "MyBlocks", {"appId": "42"}, [BLOCK]),
    Case(SafetyAPI, "block", (42, 7), "BlockPlayer", {"appId": "42", "userId": "7"}, BLOCK),
    Case(SafetyAPI, "unblock", (42, 7), "UnblockPlayer", {"appId": "42", "userId": "7"}, False),
    Case(
        SafetyAPI,
        "file_report",
        (42, 7, "HARASSMENT", "2026-10-30T12:00:00.000Z"),
        "FileSafetyReport",
        {
            "input": {
                "appId": "42",
                "reportedUserId": "7",
                "reason": "HARASSMENT",
                "occurredAt": "2026-10-30T12:00:00.000Z",
            }
        },
        {"reportId": "900", "filedAt": "2026-10-30T12:01:00.000Z"},
    ),
    # -- moderation ------------------------------------------------------------------
    Case(ModerationAPI, "reports", (42,), "SafetyReports", {"appId": "42"}, [REPORT]),
    Case(
        ModerationAPI,
        "reports",
        ("42",),
        "SafetyReports",
        {"appId": "42", "status": "OPEN", "first": 10},
        [REPORT],
        {"status": "OPEN", "first": 10},
    ),
    Case(
        ModerationAPI,
        "resolve_report",
        (42, 900, "warned"),
        "ResolveSafetyReport",
        {"appId": "42", "reportId": "900", "outcome": "warned"},
        {**REPORT, "status": "RESOLVED"},
    ),
    Case(
        ModerationAPI,
        "escalate_report",
        (42, "900", "NCMEC-1"),
        "EscalateSafetyReport",
        {"appId": "42", "reportId": "900", "ncmecReportId": "NCMEC-1"},
        {**REPORT, "status": "ESCALATED"},
    ),
]

GENERATED: dict[str, Operation] = {
    value.name: value for value in vars(ops).values() if isinstance(value, Operation)
}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


@pytest.mark.parametrize("domain", list(CROWDYJS_METHODS), ids=lambda domain: domain.__name__)
def test_method_set_is_crowdyjs_snake_cased_and_every_method_is_covered(
    domain: type[Domain],
) -> None:
    ported = {_snake(name) for name in CROWDYJS_METHODS[domain]}
    public = {
        name
        for name, value in vars(domain).items()
        if not name.startswith("_") and inspect.iscoroutinefunction(value)
    }
    assert public == ported
    assert {case.method for case in CASES if case.domain is domain} == ported


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case.domain.__name__}.{case.method}")
async def test_sends_operation_and_variables(
    api: MockApi, graphql: AsyncGraphQLClient, case: Case
) -> None:
    api.reply_with_root(case.reply)
    result = await getattr(case.domain(graphql), case.method)(*case.args, **case.kwargs)
    assert result == case.reply
    assert len(api.sent) == 1
    assert api.last.operation_name == case.operation
    assert api.last.query == GENERATED[case.operation].document
    assert api.last.variables == case.variables


async def test_a_report_takes_an_aware_datetime_and_its_optional_fields(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    api.reply_with_root({"reportId": "901", "filedAt": "t"})
    safety = SafetyAPI(graphql)
    when = datetime(2026, 10, 30, 7, 0, tzinfo=timezone(timedelta(hours=-5)))
    await safety.file_report(
        42, 7, "OTHER", when, reported_actor_uuid="ab" * 16, details="griefing my build"
    )
    assert api.last.variables["input"] == {
        "appId": "42",
        "reportedUserId": "7",
        "reason": "OTHER",
        "occurredAt": "2026-10-30T12:00:00.000Z",
        "reportedActorUuid": "ab" * 16,
        "details": "griefing my build",
    }
    with pytest.raises(ValueError, match="timezone-aware"):
        await safety.file_report(42, 7, "OTHER", datetime(2026, 10, 30, 12, 0))
    assert len(api.sent) == 1


async def test_set_comm_settings_with_nothing_sends_an_empty_choice(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    api.reply_with_root(SETTINGS)
    await SafetyAPI(graphql).set_comm_settings(42)
    assert api.last.variables == {"appId": "42", "input": {}}


def test_the_clients_expose_safety_and_moderation_and_admin_shares_it() -> None:
    client = crowdypy.AsyncCrowdyClient(http_url="https://ck.example.test")
    assert isinstance(client.safety, SafetyAPI)
    assert isinstance(client.moderation, ModerationAPI)
    assert client.admin.moderation is client.moderation
    blocking = crowdypy.CrowdyClient(http_url="https://ck.example.test")
    assert {"comm_settings", "file_report"} <= set(dir(blocking.safety))
    assert blocking.admin.moderation is blocking.moderation
    assert crowdypy.SafetyAPI is SafetyAPI
    assert crowdypy.ModerationAPI is ModerationAPI


# ---------------------------------------------------------------------- auth


def inline(module: Any, name: str) -> Operation:
    return next(op for op in module.INLINE_OPERATIONS if op.name == name)


async def test_register_sends_the_age_screen_s_answers(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    api.reply_with_root({"token": "s" * 40, "gameTokenId": "1", "user": {"userId": "5"}})
    auth = AuthAPI(graphql, session)
    await auth.register(
        "p@example.com",
        "pw-123456",
        birth_date="2011-04-20",
        country="US",
        parental_confirmation=True,
    )
    assert api.last.variables == {
        "registerUserInput": {
            "email": "p@example.com",
            "password": "pw-123456",
            "birthDate": "2011-04-20",
            "country": "US",
            "parentalConfirmation": True,
        }
    }
    await auth.register("q@example.com", "pw-123456", accept_legal=True)
    assert api.last.variables["registerUserInput"] == {
        "email": "q@example.com",
        "password": "pw-123456",
        "acceptLegal": True,
    }


async def test_record_player_consents_keeps_the_old_document_for_the_old_call(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    api.reply_with_root(True)
    auth = AuthAPI(graphql, session)
    assert await auth.record_player_consents(accept_legal=True, attest_age_of_majority=True)
    assert api.last.operation_name == "RecordPlayerConsents"
    assert api.last.query == inline(auth_module, "RecordPlayerConsents").document
    assert "$attestAgeOfMajority: Boolean!" in api.last.query
    assert api.last.variables == {"acceptLegal": True, "attestAgeOfMajority": True}

    await auth.record_player_consents(accept_legal=True, app_id=42, method="IN_GAME")
    assert api.last.operation_name == "RecordPlayerConsentsForApp"
    assert api.last.variables == {"acceptLegal": True, "appId": "42", "method": "IN_GAME"}
    await auth.record_player_consents(accept_legal=True)
    assert api.last.operation_name == "RecordPlayerConsentsForApp"
    assert api.last.variables == {"acceptLegal": True}
    await auth.record_player_consents(accept_legal=True, attest_age_of_majority=True, app_id="9")
    assert api.last.variables == {"acceptLegal": True, "attestAgeOfMajority": True, "appId": "9"}


async def test_player_legal_acceptance_names_the_app_only_when_given(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    api.reply_with_root(False)
    auth = AuthAPI(graphql, session)
    assert await auth.player_legal_acceptance() is False
    assert (api.last.operation_name, api.last.variables) == ("PlayerLegalAcceptance", {})
    await auth.player_legal_acceptance(app_id=42)
    assert (api.last.operation_name, api.last.variables) == (
        "PlayerLegalAcceptanceForApp",
        {"appId": "42"},
    )


async def test_record_player_age_returns_the_bracket_and_country(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    api.reply_with_root({"userId": "5", "ageBracket": "18_plus", "country": "US"})
    auth = AuthAPI(graphql, session)
    age = await auth.record_player_age(birth_date="1990-01", country="US")
    assert age == PlayerAge(user_id="5", age_bracket="18_plus", country="US")
    assert api.last.operation_name == "RecordPlayerAge"
    assert api.last.variables == {"input": {"birthDate": "1990-01", "country": "US"}}
    await auth.record_player_age(birth_date="2011-04-20", country="US", parental_confirmation=True)
    assert api.last.variables["input"]["parentalConfirmation"] is True


DOCUMENT = {
    "documentId": "player-terms",
    "version": "player-terms-2026-11-01",
    "title": "Player Terms",
    "url": "https://crowdedkingdoms.com/player-terms.html",
    "appId": None,
}


async def test_the_documents_and_the_developer_consents(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    auth = AuthAPI(graphql, session)
    api.reply_with_root([DOCUMENT])
    assert await auth.player_legal_documents() == [DOCUMENT]
    assert (api.last.operation_name, api.last.variables) == ("PlayerLegalDocuments", {})
    assert await auth.app_required_documents(42) == [DOCUMENT]
    assert (api.last.operation_name, api.last.variables) == (
        "AppRequiredDocuments",
        {"appId": "42"},
    )
    assert await auth.developer_legal_documents() == [DOCUMENT]
    assert api.last.operation_name == "DeveloperLegalDocuments"
    assert "documentId version title url appId" in api.last.query
    api.reply_with_root(True)
    assert await auth.developer_legal_acceptance() is True
    assert api.last.operation_name == "DeveloperLegalAcceptance"
    assert await auth.record_developer_consents(accept_developer_terms=True) is True
    assert (api.last.operation_name, api.last.variables) == (
        "RecordDeveloperConsents",
        {"acceptDeveloperTerms": True},
    )


# ---------------------------------------------------------------------- apps and platform


async def test_recording_is_none_for_an_unknown_app(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    api.reply_with_root(None)
    assert await AppsAPI(graphql).recording(43) is None
    assert (api.last.operation_name, api.last.variables) == ("AppRecording", {"appId": "43"})


def test_the_app_selections_carry_the_safety_log_and_the_ages() -> None:
    for operation in (ops.APP, ops.UPDATE_APP):
        for field_name in (
            "replayLoggingRecordsVoice",
            "replayLoggingRecordsVideo",
            "minPlayerAge",
            "effectiveMinPlayerAge",
        ):
            assert field_name in operation.document, (operation.name, field_name)
        assert re.search(r"recording \{\s*enabled\s+voice\s+video\s*\}", operation.document)
    assert re.search(r"\bageBracket\b[\s\S]*\bcountry\b", ops.ME.document)


# ---------------------------------------------------------------------- portal: Minecraft

MINTED: dict[str, Any] = {
    "token": "t" * 64,
    "gameTokenId": "9",
    "appId": "42",
    "expiresAt": "2026-10-30T12:00:00.000Z",
}


async def test_minecraft_sign_in_holds_the_token_and_sends_the_documents(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    portal = PortalAPI(graphql, session)
    api.reply_with_root({"serverId": "a" * 40, "expiresAt": "2026-10-30T12:02:00.000Z"})
    challenge = await portal.minecraft_challenge(42)
    assert challenge == MinecraftChallenge(
        server_id="a" * 40, expires_at="2026-10-30T12:02:00.000Z"
    )
    assert (api.last.operation_name, api.last.variables) == ("MinecraftChallenge", {"appId": "42"})

    api.reply_with_root(MINTED)
    token = await portal.sign_in_with_minecraft(
        app_id=42,
        username="Steve",
        server_id=challenge.server_id,
        birth_date="2000-02",
        country="US",
        accepted_documents=[
            AcceptedLegalDocument(document_id="player-terms", version="player-terms-1"),
            {"documentId": "minecraft-addendum", "version": "minecraft-addendum-1", "x": 1},
        ],
    )
    assert isinstance(token, AppTokenResponse)
    assert session.get_token() == "t" * 64
    assert api.last.operation_name == "SignInWithMinecraft"
    assert api.last.query == inline(portal_module, "SignInWithMinecraft").document
    assert api.last.variables == {
        "input": {
            "appId": "42",
            "username": "Steve",
            "serverId": "a" * 40,
            "birthDate": "2000-02",
            "country": "US",
            "acceptedDocuments": [
                {"documentId": "player-terms", "version": "player-terms-1"},
                {"documentId": "minecraft-addendum", "version": "minecraft-addendum-1"},
            ],
        }
    }


def test_the_new_names_are_on_the_package_root() -> None:
    assert crowdypy.PlayerAge is PlayerAge
    assert crowdypy.MinecraftChallenge is MinecraftChallenge
    assert crowdypy.AcceptedLegalDocument is AcceptedLegalDocument
    assert "ageYears" in crowdypy.ManagedPlayerAssertionClaims.__annotations__
    assert {"age_years", "country"} <= set(
        crowdypy.inputs.MintManagedPlayerTokenInput.__struct_fields__
    )
