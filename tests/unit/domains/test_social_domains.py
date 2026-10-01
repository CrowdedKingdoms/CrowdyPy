"""The social and wallet domains: each method sends its operation with converted variables.

channels, teams, marketplace and player_wallet are thin wrappers in CrowdyJS (no decoding,
no client-side checks), so the contract under test is the operation, the variables (BigInt
arguments as decimal strings, omitted optionals, idempotency keys under the document's
variable name) and the root field handed back untouched. On top of that: the one argument
whose omission and explicit null differ (``set_auto_billing``'s ``limit_cents``) and the
deprecated ``app_markup_accrued``.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from conftest import MockApi
from crowdypy._generated import enums, inputs
from crowdypy._generated import operations as ops
from crowdypy._operation import Operation
from crowdypy.domains._base import Domain
from crowdypy.domains.channels import ChannelsAPI
from crowdypy.domains.marketplace import APP_LISTING_VERSIONS, MarketplaceAPI
from crowdypy.domains.player_wallet import PlayerWalletAPI
from crowdypy.domains.teams import TeamsAPI
from crowdypy.errors import CrowdyProtocolError
from crowdypy.graphql import AsyncGraphQLClient

#: Each class's public CrowdyJS methods, as the CrowdyJS source names them.
CROWDYJS_METHODS: dict[type[Domain], list[str]] = {
    ChannelsAPI: [
        "mine",
        "list",
        "get",
        "members",
        "roles",
        "policy",
        "create",
        "update",
        "remove",
        "setPolicy",
        "join",
        "requestToJoin",
        "leave",
        "addMember",
        "removeMember",
        "setMemberRoles",
        "createRole",
        "updateRole",
        "deleteRole",
    ],
    TeamsAPI: [
        "mine",
        "list",
        "get",
        "members",
        "roles",
        "policy",
        "create",
        "update",
        "remove",
        "setPolicy",
        "join",
        "requestToJoin",
        "leave",
        "addMember",
        "removeMember",
        "setMemberRoles",
        "createRole",
        "updateRole",
        "deleteRole",
    ],
    MarketplaceAPI: [
        "gridClaimPolicy",
        "gridClaimRequests",
        "claimGridOwnership",
        "claimGridChunk",
        "releaseClaimedGrid",
        "decideGridClaim",
        "issueGridClaimInvite",
        "admissionQueue",
        "appListings",
        "appAcquisitions",
        "transferListing",
        "setListingStatus",
        "setGridClaimPolicy",
    ],
    PlayerWalletAPI: [
        "balance",
        "transactions",
        "charges",
        "spendCaps",
        "setSpendCap",
        "autoBilling",
        "setAutoBilling",
        "beginCardSetup",
        "runtimeStates",
        "rateMarkup",
        "setRateMarkup",
        "appPlayerUsage",
        "appMarkupAccrued",
        "appMarkupAccruedMicrousd",
    ],
}

GENERATED: dict[str, Operation] = {
    **{value.name: value for value in vars(ops).values() if isinstance(value, Operation)},
    APP_LISTING_VERSIONS.name: APP_LISTING_VERSIONS,
}

#: Methods CrowdyCPP carries and CrowdyJS does not, under their Python names.
CROWDYCPP_METHODS: dict[type[Domain], set[str]] = {MarketplaceAPI: {"app_listing_versions"}}

GROUP: dict[str, Any] = {"groupId": "5", "appId": "42", "name": "general"}
MEMBER: dict[str, Any] = {"groupMemberId": "9", "groupId": "5", "userId": "7", "status": "active"}
ROLE: dict[str, Any] = {"groupRoleId": "3", "groupId": "5", "roleName": "speaker"}
POLICY: dict[str, Any] = {"appId": "42", "creationPolicy": "member", "maxMembers": None}
MEMBERSHIP: dict[str, Any] = {"group": GROUP, "roles": [ROLE], "permissions": ["send_messages"]}
LISTING: dict[str, Any] = {"listingId": "L-1", "appId": "42", "status": "ACTIVE"}
CHUNK = {"x": "-2", "y": "3", "z": "7"}
ROW: dict[str, Any] = {"id": "1"}


@dataclass(frozen=True)
class Case:
    domain: type[Domain]
    method: str
    args: tuple[Any, ...]
    operation: str
    variables: dict[str, Any]
    reply: Any
    kwargs: Mapping[str, Any] = field(default_factory=dict)


def _group_cases(domain: type[Domain], noun: str, plural: str) -> list[Case]:
    """The methods channels and teams share: the same shape over each one's own operations."""
    return [
        Case(domain, "mine", (42,), f"My{plural}", {"appId": "42"}, [MEMBERSHIP]),
        Case(domain, "list", ("42",), plural, {"appId": "42"}, [GROUP]),
        Case(domain, "get", (5,), noun, {"groupId": "5"}, GROUP),
        Case(domain, "members", ("5",), f"{noun}Members", {"groupId": "5"}, [MEMBER]),
        Case(domain, "roles", (5,), f"{noun}Roles", {"groupId": "5"}, [ROLE]),
        Case(domain, "policy", (42,), f"{noun}Policy", {"appId": "42"}, POLICY),
        Case(
            domain,
            "update",
            ({"groupId": "5", "description": None},),
            f"Update{noun}",
            {"input": {"groupId": "5", "description": None}},
            GROUP,
        ),
        Case(domain, "join", (5,), f"Join{noun}", {"groupId": "5"}, MEMBER),
        Case(domain, "request_to_join", ("5",), f"RequestToJoin{noun}", {"groupId": "5"}, MEMBER),
        Case(
            domain,
            "add_member",
            (5, 7),
            f"Add{noun}Member",
            {"groupId": "5", "userId": "7"},
            MEMBER,
        ),
        Case(
            domain,
            "remove_member",
            ("5", 7),
            f"Remove{noun}Member",
            {"groupId": "5", "userId": "7"},
            True,
        ),
        Case(
            domain,
            "set_member_roles",
            (inputs.SetMemberRolesInput(group_id="5", user_id="7", role_ids=["3"]),),
            f"Set{noun}MemberRoles",
            {"input": {"groupId": "5", "userId": "7", "roleIds": ["3"]}},
            MEMBER,
        ),
        Case(
            domain,
            "create_role",
            (inputs.CreateGroupRoleInput(group_id="5", role_name="speaker", rank=2),),
            f"Create{noun}Role",
            {"input": {"groupId": "5", "roleName": "speaker", "rank": 2}},
            ROLE,
        ),
        Case(
            domain,
            "update_role",
            ({"groupRoleId": "3", "permissions": []},),
            f"Update{noun}Role",
            {"input": {"groupRoleId": "3", "permissions": []}},
            ROLE,
        ),
        Case(domain, "delete_role", (3,), f"Delete{noun}Role", {"groupRoleId": "3"}, True),
    ]


CASES: list[Case] = [
    # -- channels --------------------------------------------------------------------
    *_group_cases(ChannelsAPI, "Channel", "Channels"),
    Case(
        ChannelsAPI,
        "create",
        (inputs.CreateChannelInput(app_id="42", name="news", members_can_send=False),),
        "CreateChannel",
        {"input": {"appId": "42", "name": "news", "membersCanSend": False}},
        GROUP,
    ),
    Case(ChannelsAPI, "remove", (5,), "DeleteChannel", {"groupId": "5"}, True),
    Case(
        ChannelsAPI,
        "set_policy",
        (inputs.SetChannelPolicyInput(app_id="42", creation_policy="member", max_members=None),),
        "SetChannelPolicy",
        {"input": {"appId": "42", "creationPolicy": "member", "maxMembers": None}},
        POLICY,
    ),
    Case(ChannelsAPI, "leave", ("5",), "LeaveChannel", {"groupId": "5"}, False),
    # -- teams -----------------------------------------------------------------------
    *_group_cases(TeamsAPI, "Team", "Teams"),
    Case(
        TeamsAPI,
        "create",
        ({"appId": "42", "name": "Red Squad"},),
        "CreateTeam",
        {"input": {"appId": "42", "name": "Red Squad"}},
        GROUP,
    ),
    Case(TeamsAPI, "remove", (5,), "DeleteTeam", {"groupId": "5"}, True),
    Case(
        TeamsAPI,
        "remove",
        ("5", "delete-5-once"),
        "DeleteTeam",
        {"groupId": "5", "idempotencyKey": "delete-5-once"},
        True,
    ),
    Case(
        TeamsAPI,
        "set_policy",
        ({"appId": "42", "defaultMembershipPolicy": "request"},),
        "SetTeamPolicy",
        {"input": {"appId": "42", "defaultMembershipPolicy": "request"}},
        POLICY,
    ),
    Case(TeamsAPI, "leave", (5,), "LeaveTeam", {"groupId": "5"}, True),
    Case(
        TeamsAPI,
        "leave",
        (5,),
        "LeaveTeam",
        {"groupId": "5", "idempotencyKey": "leave-5"},
        True,
        {"idempotency_key": "leave-5"},
    ),
    # -- marketplace -----------------------------------------------------------------
    Case(
        MarketplaceAPI,
        "grid_claim_policy",
        (2,),
        "MarketplaceGridClaimPolicy",
        {"appId": "2"},
        "SELF_CLAIM",
    ),
    Case(
        MarketplaceAPI,
        "grid_claim_requests",
        ("2",),
        "MarketplaceGridClaimRequests",
        {"appId": "2"},
        [{"requestId": "r-1", "status": "PENDING"}],
    ),
    Case(
        MarketplaceAPI,
        "claim_grid_ownership",
        (2, 42),
        "MarketplaceClaimGridOwnership",
        {"appId": "2", "gridId": "42"},
        {"policy": "APPROVAL", "ownershipAssigned": False, "claimRequestId": "r-1"},
    ),
    Case(
        MarketplaceAPI,
        "claim_grid_chunk",
        ("2", inputs.ChunkCoordinatesInput(x="-2", y="3", z="7")),
        "MarketplaceClaimGridChunk",
        {"appId": "2", "chunk": CHUNK},
        {"gridId": "42", "lowChunk": CHUNK, "highChunk": CHUNK, "policy": "SELF_CLAIM"},
    ),
    Case(
        MarketplaceAPI,
        "claim_grid_chunk",
        (2, CHUNK),
        "MarketplaceClaimGridChunk",
        {"appId": "2", "chunk": CHUNK},
        {"gridId": "42"},
    ),
    Case(
        MarketplaceAPI,
        "release_claimed_grid",
        (2, "42"),
        "MarketplaceReleaseClaimedGrid",
        {"appId": "2", "gridId": "42"},
        {"gridId": "42", "released": True},
    ),
    Case(
        MarketplaceAPI,
        "decide_grid_claim",
        (2, "r-1", True),
        "MarketplaceDecideGridClaim",
        {"appId": "2", "requestId": "r-1", "approve": True},
        {"requestId": "r-1", "status": "APPROVED"},
    ),
    Case(
        MarketplaceAPI,
        "issue_grid_claim_invite",
        (2, 42, 7),
        "MarketplaceIssueGridClaimInvite",
        {"appId": "2", "gridId": "42", "inviteeUserId": "7"},
        True,
    ),
    Case(
        MarketplaceAPI,
        "admission_queue",
        (2,),
        "MarketplaceAdmissionQueue",
        {"appId": "2"},
        [{"listing": LISTING, "admissionState": "PENDING"}],
    ),
    Case(
        MarketplaceAPI,
        "app_listings",
        (2,),
        "MarketplaceAppListings",
        {"appId": "2"},
        [LISTING],
    ),
    Case(
        MarketplaceAPI,
        "app_listings",
        (2,),
        "MarketplaceAppListings",
        {"appId": "2", "includeDelisted": True},
        [LISTING],
        {"include_delisted": True},
    ),
    Case(
        MarketplaceAPI,
        "app_acquisitions",
        ("2",),
        "MarketplaceAppAcquisitions",
        {"appId": "2"},
        [{"acquisitionId": "a-1", "listingId": "L-1"}],
    ),
    Case(
        MarketplaceAPI,
        "transfer_listing",
        (
            inputs.TransferPlayerCodeListingInput(
                app_id="2",
                listing_id="L-1",
                to_owner_kind=enums.PlayerCodeOwnerKind.ORG,
                to_owner_ref="11",
            ),
        ),
        "MarketplaceTransferListing",
        {"input": {"appId": "2", "listingId": "L-1", "toOwnerKind": "ORG", "toOwnerRef": "11"}},
        LISTING,
    ),
    Case(
        MarketplaceAPI,
        "set_listing_status",
        (2, "L-1", enums.PlayerCodeListingStatus.KILLED),
        "MarketplaceSetListingStatus",
        {"appId": "2", "listingId": "L-1", "status": "KILLED"},
        {**LISTING, "status": "KILLED"},
    ),
    Case(
        MarketplaceAPI,
        "set_grid_claim_policy",
        (2, "SELF_CLAIM"),
        "MarketplaceSetGridClaimPolicy",
        {"appId": "2", "policy": "SELF_CLAIM"},
        "SELF_CLAIM",
    ),
    Case(
        MarketplaceAPI,
        "set_grid_claim_policy",
        (2, enums.GridClaimPolicy.APPROVAL, [7, "8"]),
        "MarketplaceSetGridClaimPolicy",
        {"appId": "2", "policy": "APPROVAL", "approverUserIds": ["7", "8"]},
        "APPROVAL",
    ),
    # -- player wallet ---------------------------------------------------------------
    Case(
        PlayerWalletAPI,
        "balance",
        (),
        "PlayerWalletBalance",
        {},
        {"walletId": "1", "balanceMicrousd": "2500000", "holdsMicrousd": "0"},
    ),
    Case(PlayerWalletAPI, "transactions", (), "PlayerWalletTransactions", {}, [ROW]),
    Case(
        PlayerWalletAPI,
        "transactions",
        (20, 40),
        "PlayerWalletTransactions",
        {"limit": 20, "offset": 40},
        [ROW],
    ),
    Case(PlayerWalletAPI, "charges", (), "PlayerUsageCharges", {}, [ROW]),
    Case(
        PlayerWalletAPI,
        "charges",
        (42,),
        "PlayerUsageCharges",
        {"appId": "42", "limit": 5},
        [ROW],
        {"limit": 5},
    ),
    Case(PlayerWalletAPI, "spend_caps", (), "PlayerSpendCaps", {}, [ROW]),
    Case(
        PlayerWalletAPI,
        "set_spend_cap",
        ("app", 42, 500, "15000"),
        "SetPlayerSpendCap",
        {"scope": "app", "appId": "42", "dailyLimitCents": "500", "monthlyLimitCents": "15000"},
        [ROW],
    ),
    Case(
        PlayerWalletAPI,
        "set_spend_cap",
        ("global",),
        "SetPlayerSpendCap",
        {"scope": "global"},
        [],
    ),
    Case(PlayerWalletAPI, "auto_billing", (), "PlayerAutoBilling", {}, {"enabled": False}),
    Case(
        PlayerWalletAPI,
        "set_auto_billing",
        (True, 20000, 1000, "200"),
        "SetPlayerAutoBilling",
        {
            "enabled": True,
            "limitCents": "20000",
            "rechargeAmountCents": "1000",
            "lowWaterThresholdCents": "200",
        },
        {"enabled": True},
    ),
    Case(
        PlayerWalletAPI,
        "begin_card_setup",
        (),
        "BeginPlayerCardSetup",
        {},
        {"clientSecret": "seti_x", "publishableKey": "pk_x"},
    ),
    Case(PlayerWalletAPI, "runtime_states", (), "PlayerRuntimeStates", {}, [ROW]),
    Case(PlayerWalletAPI, "rate_markup", (42,), "PlayerRateMarkup", {"appId": "42"}, 250),
    Case(
        PlayerWalletAPI,
        "set_rate_markup",
        ("42", 300),
        "SetPlayerRateMarkup",
        {"appId": "42", "markupBps": 300},
        300,
    ),
    Case(PlayerWalletAPI, "app_player_usage", (42,), "AppPlayerUsage", {"appId": "42"}, [ROW]),
    Case(
        PlayerWalletAPI,
        "app_player_usage",
        (42,),
        "AppPlayerUsage",
        {"appId": "42", "hours": 744},
        [ROW],
        {"hours": 744},
    ),
    Case(
        PlayerWalletAPI,
        "app_markup_accrued_microusd",
        (42,),
        "AppPlayerMarkupAccruedMicrousd",
        {"appId": "42"},
        "123456789",
    ),
    Case(
        MarketplaceAPI,
        "app_listing_versions",
        (42, "l-1"),
        "MarketplaceAppListingVersions",
        {"appId": "42", "listingId": "l-1"},
        [{"versionId": "v-1"}],
    ),
]


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _public_methods(domain: type[Domain]) -> set[str]:
    return {
        name
        for name, value in vars(domain).items()
        if not name.startswith("_") and inspect.iscoroutinefunction(value)
    }


@pytest.mark.parametrize("domain", list(CROWDYJS_METHODS), ids=lambda domain: domain.__name__)
def test_method_set_is_crowdyjs_snake_cased_and_every_method_is_covered(
    domain: type[Domain],
) -> None:
    ported = {_snake(name) for name in CROWDYJS_METHODS[domain]} | CROWDYCPP_METHODS.get(
        domain, set()
    )
    assert _public_methods(domain) == ported
    covered = {case.method for case in CASES if case.domain is domain}
    if domain is PlayerWalletAPI:
        covered.add("app_markup_accrued")  # test_app_markup_accrued_is_deprecated
    assert covered == ported


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case.domain.__name__}.{case.method}")
async def test_sends_operation_and_variables(
    api: MockApi, graphql: AsyncGraphQLClient, case: Case
) -> None:
    api.reply_with_root(case.reply)
    method = getattr(case.domain(graphql), case.method)
    result = await method(*case.args, **case.kwargs)
    assert result == case.reply
    assert len(api.sent) == 1
    assert api.last.operation_name == case.operation
    assert api.last.query == GENERATED[case.operation].document
    assert api.last.variables == case.variables


async def test_set_auto_billing_keeps_or_removes_the_ceiling(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    wallet = PlayerWalletAPI(graphql)
    api.reply_with_root({"enabled": True})
    await wallet.set_auto_billing(True)
    assert api.last.variables == {"enabled": True}
    await wallet.set_auto_billing(False, limit_cents=None)
    assert api.last.variables == {"enabled": False, "limitCents": None}
    await wallet.set_auto_billing(True, recharge_amount_cents=2500)
    assert api.last.variables == {"enabled": True, "rechargeAmountCents": "2500"}
    assert {sent.operation_name for sent in api.sent} == {"SetPlayerAutoBilling"}


async def test_app_markup_accrued_is_deprecated(api: MockApi, graphql: AsyncGraphQLClient) -> None:
    api.reply_with_root("1234")
    with pytest.warns(DeprecationWarning, match="app_markup_accrued_microusd"):
        accrued = await PlayerWalletAPI(graphql).app_markup_accrued(42)
    assert accrued == "1234"
    assert api.last.operation_name == "AppPlayerMarkupAccrued"
    assert api.last.query == ops.APP_PLAYER_MARKUP_ACCRUED.document
    assert api.last.variables == {"appId": "42"}


async def test_a_bad_id_is_refused_before_anything_is_sent(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    with pytest.raises(CrowdyProtocolError):
        await TeamsAPI(graphql).remove("5 OR 1=1", "key")
    with pytest.raises(CrowdyProtocolError):
        await ChannelsAPI(graphql).add_member("5", "seven")
    with pytest.raises(CrowdyProtocolError):
        await PlayerWalletAPI(graphql).set_spend_cap("app", app_id="42", daily_limit_cents="1.5")
    assert api.sent == []


async def test_approver_ids_must_be_a_sequence_not_one_string(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    with pytest.raises(TypeError):
        await MarketplaceAPI(graphql).set_grid_claim_policy(2, "APPROVAL", "78")
    assert api.sent == []
