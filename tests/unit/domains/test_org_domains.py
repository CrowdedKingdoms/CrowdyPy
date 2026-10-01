"""The org-admin domains: each method sends its operation with converted variables.

organizations, app_access, apps, billing, payments, quotas, usage, shared_environment,
platform and discovery wrap one operation per method in CrowdyJS, so the contract under
test is the operation, the variables (BigInt arguments as decimal strings, omitted
optionals, idempotency keys under the document's variable name) and the root field handed
back. On top of that, the logic CrowdyJS carries client-side: ``apps.route_for``,
discovery's normalized entries, ``quotas.set``'s scope check, the inline platform
document, ``set_auto_billing``'s kept-or-removed cap, and the ``admin`` facade.
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
from crowdypy.domains.admin import AdminAPI
from crowdypy.domains.app_access import AppAccessAPI
from crowdypy.domains.apps import AppRoute, AppsAPI
from crowdypy.domains.billing import BillingAPI
from crowdypy.domains.discovery import AppEndpoint, DiscoveryDomain
from crowdypy.domains.organizations import OrganizationsAPI
from crowdypy.domains.payments import PaymentsAPI
from crowdypy.domains.platform import INLINE_OPERATIONS, PLATFORM_CONFIG, PlatformAPI
from crowdypy.domains.quotas import QuotasAPI
from crowdypy.domains.shared_environment import SharedEnvironmentAPI
from crowdypy.domains.usage import UsageAPI
from crowdypy.errors import CrowdyError, CrowdyProtocolError
from crowdypy.graphql import AsyncGraphQLClient

#: Each class's public CrowdyJS methods, as the CrowdyJS source names them.
CROWDYJS_METHODS: dict[type[Domain], list[str]] = {
    OrganizationsAPI: [
        "get",
        "bySlug",
        "mine",
        "members",
        "roles",
        "memberRoles",
        "permissions",
        "tokens",
        "create",
        "createToken",
        "updateToken",
        "revokeToken",
        "inviteMember",
        "removeMember",
        "setMemberRoles",
        "createRole",
        "updateRole",
        "deleteRole",
    ],
    AppAccessAPI: [
        "tiers",
        "myAccess",
        "usersByApp",
        "usersByAppConnection",
        "runtimePermissions",
        "grantMemberCandidates",
        "claimFree",
        "grantMine",
        "createTier",
        "updateTier",
        "archiveTier",
        "grant",
        "revoke",
        "defineFeature",
        "features",
        "grantTierFeature",
        "revokeTierFeature",
        "tierFeatures",
    ],
    AppsAPI: [
        "codeAdmissionMode",
        "codeAdmissions",
        "setCodeAdmissionMode",
        "admitCode",
        "revokeCodeAdmission",
        "app",
        "appBySlug",
        "myApps",
        "routeFor",
        "forOrg",
        "marketplace",
        "marketplaceConnection",
        "placeableDatacenters",
        "create",
        "update",
        "archive",
    ],
    BillingAPI: [
        "walletBalance",
        "walletTransactions",
        "appBudget",
        "appBudgets",
        "setAppBudget",
        "walletTransactionsConnection",
    ],
    PaymentsAPI: ["create", "mine", "capturePaypal", "mineConnection"],
    QuotasAPI: ["forOrg", "forApp", "effective", "set", "remove"],
    UsageAPI: ["appGraphqlOperations", "appSummary", "playerPulse"],
    SharedEnvironmentAPI: [
        "plans",
        "freeAppQuota",
        "appSubscription",
        "appRuntimeState",
        "autoBilling",
        "paymentMethods",
        "publishApp",
        "cancelSubscription",
        "setSpendCaps",
        "setReservedThroughput",
        "setAutoBilling",
        "setupPaymentMethod",
        "removePaymentMethod",
    ],
    PlatformAPI: ["config"],
    DiscoveryDomain: ["apps", "app"],
}

#: The document each operation is sent with: the generated one, or the inline one.
DOCUMENTS: dict[str, str] = {
    **{value.name: value.document for value in vars(ops).values() if isinstance(value, Operation)},
    **{op.name: op.document for op in INLINE_OPERATIONS},
}

SINCE = "2026-09-01T00:00:00.000Z"
ORG: dict[str, Any] = {"orgId": "12", "name": "Acme", "slug": "acme", "status": "active"}
MEMBER: dict[str, Any] = {"orgMemberId": "9", "orgId": "12", "userId": "7", "status": "active"}
ROLE: dict[str, Any] = {"orgRoleId": "3", "orgId": "12", "roleName": "builder", "isSystem": False}
TOKEN: dict[str, Any] = {"orgTokenId": "4", "orgId": "12", "label": "ci", "isActive": True}
TIER: dict[str, Any] = {"tierId": "6", "appId": "42", "name": "Default", "isDefault": True}
ACCESS: dict[str, Any] = {"appUserAccessId": "8", "appId": "42", "userId": "7", "tierId": "6"}
FEATURE: dict[str, Any] = {"appId": "42", "featureKey": "boss_room", "description": None}
TIER_FEATURE: dict[str, Any] = {"appId": "42", "tierId": "6", "featureKey": "boss_room"}
ADMISSION: dict[str, Any] = {"admissionId": "0b9e", "appId": "42", "subjectKind": "AUTHOR"}
APP: dict[str, Any] = {
    "appId": "42",
    "orgId": "12",
    "slug": "quest",
    "splitMode": True,
    "deploymentTarget": "DEDICATED",
    "gameApiUrl": "game-api-of-42",
}
PAGE: dict[str, Any] = {"items": [], "pageInfo": {"totalCount": 0, "limit": 50, "offset": 0}}
CONNECTION: dict[str, Any] = {
    "edges": [],
    "pageInfo": {"hasNextPage": False, "endCursor": None},
    "totalCount": 0,
}
WALLET: dict[str, Any] = {"orgId": "12", "balanceMicrousd": "5000000", "holdsMicrousd": "0"}
BUDGET: dict[str, Any] = {"appBudgetId": "2", "appId": "42", "monthlyLimitCents": "5000"}
CHECKOUT: dict[str, Any] = {"checkoutId": "c-1", "status": "PENDING", "externalUrl": "pay-here"}
QUOTA: dict[str, Any] = {"quotaId": "5", "metric": "replication_messages", "limitValue": "1000"}
RUNTIME: dict[str, Any] = {"appId": "42", "runtimeStatus": "ACTIVE", "dailyLimitCents": None}
AUTO_BILLING: dict[str, Any] = {"orgId": "12", "enabled": True, "limitCents": None}
SUBSCRIPTION: dict[str, Any] = {"appId": "42", "planId": "3", "status": "CANCELED"}
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
    #: What the method answers when it is not the root field itself.
    returns: Any = None


CASES: list[Case] = [
    # -- organizations ---------------------------------------------------------------
    Case(OrganizationsAPI, "get", (12,), "Organization", {"id": "12"}, ORG),
    Case(OrganizationsAPI, "by_slug", ("acme",), "OrganizationBySlug", {"slug": "acme"}, None),
    Case(
        OrganizationsAPI,
        "mine",
        (),
        "MyOrganizations",
        {},
        [{"org": ORG, "permissions": ["manage_apps"], "roles": [ROLE]}],
    ),
    Case(OrganizationsAPI, "members", ("12",), "OrgMembers", {"orgId": "12"}, [MEMBER]),
    Case(OrganizationsAPI, "roles", (12,), "OrgRoles", {"orgId": "12"}, [ROLE]),
    Case(OrganizationsAPI, "member_roles", (9,), "MemberRoles", {"orgMemberId": "9"}, [ROLE]),
    Case(
        OrganizationsAPI,
        "permissions",
        (),
        "OrgPermissions",
        {},
        [{"permissionKey": "manage_apps", "category": "apps"}],
    ),
    Case(OrganizationsAPI, "tokens", (12,), "OrgTokens", {"orgId": "12"}, [TOKEN]),
    Case(
        OrganizationsAPI,
        "create",
        (inputs.CreateOrganizationInput(name="Acme", slug="acme"),),
        "CreateOrganization",
        {"input": {"name": "Acme", "slug": "acme"}},
        ORG,
    ),
    Case(
        OrganizationsAPI,
        "create_token",
        (inputs.CreateOrgTokenInput(org_id="12", label="ci"),),
        "CreateOrgToken",
        {"input": {"orgId": "12", "label": "ci"}},
        {**TOKEN, "token": "shown-once"},
    ),
    Case(
        OrganizationsAPI,
        "update_token",
        (4, {"isActive": False}),
        "UpdateOrgToken",
        {"orgTokenId": "4", "input": {"isActive": False}},
        TOKEN,
    ),
    Case(OrganizationsAPI, "revoke_token", ("4",), "RevokeOrgToken", {"orgTokenId": "4"}, True),
    Case(
        OrganizationsAPI,
        "invite_member",
        (inputs.InviteOrgMemberInput(org_id="12", user_id="7"),),
        "InviteOrgMember",
        {"input": {"orgId": "12", "userId": "7"}},
        MEMBER,
    ),
    Case(
        OrganizationsAPI,
        "remove_member",
        (12, "7"),
        "RemoveOrgMember",
        {"orgId": "12", "userId": "7"},
        False,
    ),
    Case(
        OrganizationsAPI,
        "set_member_roles",
        ("12", 7, [3, "4"]),
        "UpdateOrgMemberRoles",
        {"orgId": "12", "userId": "7", "roleIds": ["3", "4"]},
        MEMBER,
    ),
    Case(
        OrganizationsAPI,
        "create_role",
        (inputs.CreateOrgRoleInput(org_id="12", role_name="builder", permissions=["manage_apps"]),),
        "CreateOrgRole",
        {"input": {"orgId": "12", "roleName": "builder", "permissions": ["manage_apps"]}},
        ROLE,
    ),
    Case(
        OrganizationsAPI,
        "update_role",
        (3, inputs.UpdateOrgRoleInput(description=None)),
        "UpdateOrgRole",
        {"orgRoleId": "3", "input": {"description": None}},
        ROLE,
    ),
    Case(OrganizationsAPI, "delete_role", (3,), "DeleteOrgRole", {"orgRoleId": "3"}, True),
    # -- app_access ------------------------------------------------------------------
    Case(AppAccessAPI, "tiers", (42,), "AppAccessTiers", {"appId": "42"}, [TIER]),
    Case(AppAccessAPI, "my_access", ("42",), "MyAppAccess", {"appId": "42"}, None),
    Case(AppAccessAPI, "users_by_app", (42,), "AppUserAccessByApp", {"appId": "42"}, [ACCESS]),
    Case(
        AppAccessAPI,
        "users_by_app",
        (42, "active", 10, 20),
        "AppUserAccessByApp",
        {"appId": "42", "status": "active", "limit": 10, "offset": 20},
        [ACCESS],
    ),
    Case(
        AppAccessAPI,
        "users_by_app_connection",
        (42,),
        "AppUserAccessConnection",
        {"appId": "42", "first": 25, "after": "c-1"},
        CONNECTION,
        {"first": 25, "after": "c-1"},
    ),
    Case(
        AppAccessAPI,
        "runtime_permissions",
        (),
        "RuntimePermissions",
        {},
        ["access", "teleport", "update_voxel_data"],
    ),
    Case(
        AppAccessAPI,
        "grant_member_candidates",
        (42,),
        "AppGrantMemberCandidates",
        {"appId": "42"},
        [{"userId": "7", "email": None, "gamertag": "seven"}],
    ),
    Case(AppAccessAPI, "claim_free", (42,), "ClaimFreeAppAccess", {"appId": "42"}, ACCESS),
    Case(AppAccessAPI, "grant_mine", ("42",), "GrantMyAppAccess", {"appId": "42"}, ACCESS),
    Case(
        AppAccessAPI,
        "create_tier",
        (inputs.CreateAccessTierInput(app_id="42", name="Gold", permission_keys=["access"]),),
        "CreateAccessTier",
        {"input": {"appId": "42", "name": "Gold", "permissionKeys": ["access"]}},
        TIER,
    ),
    Case(
        AppAccessAPI,
        "update_tier",
        (6, {"priceCents": "500"}),
        "UpdateAccessTier",
        {"tierId": "6", "input": {"priceCents": "500"}},
        TIER,
    ),
    Case(
        AppAccessAPI,
        "archive_tier",
        ("6",),
        "ArchiveAccessTier",
        {"tierId": "6"},
        {"tierId": "6", "status": "archived"},
    ),
    Case(
        AppAccessAPI,
        "grant",
        (
            inputs.GrantAppAccessInput(
                app_id="42", user_id="7", tier_id="6", idempotency_key="grant-7"
            ),
        ),
        "GrantAppAccess",
        {"input": {"appId": "42", "userId": "7", "tierId": "6", "idempotencyKey": "grant-7"}},
        ACCESS,
    ),
    Case(
        AppAccessAPI,
        "revoke",
        (42, 7),
        "RevokeAppAccess",
        {"appId": "42", "userId": "7"},
        {**ACCESS, "status": "revoked"},
    ),
    Case(
        AppAccessAPI,
        "define_feature",
        (inputs.DefineAppFeatureInput(app_id="42", feature_key="boss_room"),),
        "DefineAppFeature",
        {"input": {"appId": "42", "featureKey": "boss_room"}},
        FEATURE,
    ),
    Case(AppAccessAPI, "features", (42,), "AppFeatures", {"appId": "42"}, [FEATURE]),
    Case(
        AppAccessAPI,
        "grant_tier_feature",
        ({"appId": "42", "tierId": "6", "featureKey": "boss_room"},),
        "GrantTierFeature",
        {"input": {"appId": "42", "tierId": "6", "featureKey": "boss_room"}},
        TIER_FEATURE,
    ),
    Case(
        AppAccessAPI,
        "revoke_tier_feature",
        (inputs.GrantTierFeatureInput(app_id="42", tier_id="6", feature_key="boss_room"),),
        "RevokeTierFeature",
        {"input": {"appId": "42", "tierId": "6", "featureKey": "boss_room"}},
        True,
    ),
    Case(AppAccessAPI, "tier_features", (42,), "TierFeatures", {"appId": "42"}, [TIER_FEATURE]),
    Case(
        AppAccessAPI,
        "tier_features",
        ("42", 6),
        "TierFeatures",
        {"appId": "42", "tierId": "6"},
        [TIER_FEATURE],
    ),
    # -- apps ------------------------------------------------------------------------
    Case(
        AppsAPI,
        "code_admission_mode",
        (42,),
        "AppCodeAdmissionMode",
        {"appId": "42"},
        "IMPLICIT_ALLOW",
    ),
    Case(
        AppsAPI,
        "code_admissions",
        (42,),
        "AppCodeAdmissions",
        {"appId": "42", "includeRevoked": False},
        [ADMISSION],
    ),
    Case(
        AppsAPI,
        "code_admissions",
        ("42",),
        "AppCodeAdmissions",
        {"appId": "42", "includeRevoked": True},
        [ADMISSION],
        {"include_revoked": True},
    ),
    Case(
        AppsAPI,
        "set_code_admission_mode",
        (42, enums.CodeAdmissionMode.ALLOW_LIST),
        "SetAppCodeAdmissionMode",
        {"appId": "42", "mode": "ALLOW_LIST"},
        "ALLOW_LIST",
    ),
    Case(
        AppsAPI,
        "admit_code",
        (
            inputs.AdmitAppCodeInput(
                app_id="42", subject_kind=enums.CodeAdmissionSubjectKind.AUTHOR, subject_ref="7"
            ),
        ),
        "AdmitAppCode",
        {"input": {"appId": "42", "subjectKind": "AUTHOR", "subjectRef": "7"}},
        ADMISSION,
    ),
    Case(
        AppsAPI,
        "revoke_code_admission",
        (42, "0b9e"),
        "RevokeAppCodeAdmission",
        {"appId": "42", "admissionId": "0b9e"},
        ADMISSION,
    ),
    Case(AppsAPI, "app", (42,), "App", {"appId": "42"}, APP),
    Case(
        AppsAPI,
        "app_by_slug",
        ("acme", "quest"),
        "AppBySlug",
        {"orgSlug": "acme", "appSlug": "quest"},
        APP,
    ),
    Case(AppsAPI, "my_apps", (), "MyApps", {}, [APP]),
    Case(
        AppsAPI,
        "route_for",
        (42,),
        "App",
        {"appId": "42"},
        APP,
        returns=AppRoute(
            app_id="42",
            split_mode=True,
            deployment_target="DEDICATED",
            game_api_url="game-api-of-42",
        ),
    ),
    Case(AppsAPI, "for_org", ("acme",), "AppsForOrg", {"orgSlug": "acme"}, [APP]),
    Case(AppsAPI, "marketplace", (), "MarketplaceApps", {}, PAGE),
    Case(
        AppsAPI,
        "marketplace",
        (inputs.AppMarketplaceFilterInput(query="castle"), 10, 20),
        "MarketplaceApps",
        {"filter": {"query": "castle"}, "limit": 10, "offset": 20},
        PAGE,
    ),
    Case(
        AppsAPI,
        "marketplace_connection",
        (),
        "AppsConnection",
        {"first": 10, "after": "c-1", "filter": {"orgSlug": "acme"}},
        CONNECTION,
        {"first": 10, "after": "c-1", "filter": {"orgSlug": "acme"}},
    ),
    Case(
        AppsAPI,
        "placeable_datacenters",
        (),
        "PlaceableDatacenters",
        {},
        {"placementEnforced": True, "servedBy": "or", "datacenters": [{"code": "or"}]},
    ),
    Case(
        AppsAPI,
        "create",
        (inputs.CreateAppInput(org_id="12", name="Quest", slug="quest", datacenter="or"),),
        "CreateApp",
        {"input": {"orgId": "12", "name": "Quest", "slug": "quest", "datacenter": "or"}},
        APP,
    ),
    Case(
        AppsAPI,
        "update",
        (42, inputs.UpdateAppInput(wilderness_writes_open=False)),
        "UpdateApp",
        {"appId": "42", "input": {"wildernessWritesOpen": False}},
        APP,
    ),
    Case(
        AppsAPI,
        "archive",
        ("42",),
        "ArchiveApp",
        {"appId": "42"},
        {"appId": "42", "status": "ARCHIVED"},
    ),
    # -- billing ---------------------------------------------------------------------
    Case(BillingAPI, "wallet_balance", (12,), "WalletBalance", {"orgId": "12"}, WALLET),
    Case(BillingAPI, "wallet_transactions", (12,), "WalletTransactions", {"orgId": "12"}, [ROW]),
    Case(
        BillingAPI,
        "wallet_transactions",
        ("12", 5, 10),
        "WalletTransactions",
        {"orgId": "12", "limit": 5, "offset": 10},
        [ROW],
    ),
    Case(BillingAPI, "app_budget", (12, 42), "AppBudget", {"orgId": "12", "appId": "42"}, None),
    Case(BillingAPI, "app_budgets", ("12",), "AppBudgets", {"orgId": "12"}, [BUDGET]),
    Case(
        BillingAPI,
        "set_app_budget",
        (12, 42, 5000),
        "SetAppBudget",
        {"orgId": "12", "appId": "42", "monthlyLimitCents": "5000"},
        BUDGET,
    ),
    Case(
        BillingAPI,
        "wallet_transactions_connection",
        (12,),
        "WalletTransactionsConnection",
        {"orgId": "12", "first": 50},
        CONNECTION,
        {"first": 50},
    ),
    # -- payments --------------------------------------------------------------------
    Case(
        PaymentsAPI,
        "create",
        (
            inputs.CreateCheckoutInput(
                provider=enums.PaymentProvider.STRIPE,
                purpose=enums.CheckoutPurpose.ORG_WALLET_TOPUP,
                org_id="12",
                amount_cents="2000",
                idempotency_key="topup-1",
            ),
        ),
        "CreateCheckout",
        {
            "input": {
                "provider": "STRIPE",
                "purpose": "ORG_WALLET_TOPUP",
                "orgId": "12",
                "amountCents": "2000",
                "idempotencyKey": "topup-1",
            }
        },
        CHECKOUT,
    ),
    Case(PaymentsAPI, "mine", (), "MyCheckouts", {}, PAGE),
    Case(PaymentsAPI, "mine", (10,), "MyCheckouts", {"limit": 10}, PAGE),
    Case(
        PaymentsAPI,
        "capture_paypal",
        ("ORDER-1",),
        "CapturePaypalCheckout",
        {"orderId": "ORDER-1"},
        CHECKOUT,
    ),
    Case(
        PaymentsAPI,
        "capture_paypal",
        ("ORDER-1", "capture-once"),
        "CapturePaypalCheckout",
        {"orderId": "ORDER-1", "idempotencyKey": "capture-once"},
        CHECKOUT,
    ),
    Case(
        PaymentsAPI,
        "mine_connection",
        (),
        "MyCheckoutsConnection",
        {"after": "c-1"},
        CONNECTION,
        {"after": "c-1"},
    ),
    # -- quotas ----------------------------------------------------------------------
    Case(QuotasAPI, "for_org", (12,), "QuotasForOrg", {"orgId": "12"}, [QUOTA]),
    Case(QuotasAPI, "for_app", ("42",), "QuotasForApp", {"appId": "42"}, [QUOTA]),
    Case(
        QuotasAPI,
        "effective",
        ("replication_messages",),
        "EffectiveQuota",
        {"metric": "replication_messages"},
        None,
    ),
    Case(
        QuotasAPI,
        "effective",
        ("replication_messages", 12, 42),
        "EffectiveQuota",
        {"metric": "replication_messages", "orgId": "12", "appId": "42"},
        QUOTA,
    ),
    Case(
        QuotasAPI,
        "effective",
        ("replication_messages",),
        "EffectiveQuota",
        {"metric": "replication_messages", "appId": "42"},
        QUOTA,
        {"app_id": 42},
    ),
    Case(
        QuotasAPI,
        "set",
        (inputs.SetQuotaInput(org_id="12", metric="replication_messages", limit_value="1000"),),
        "SetQuota",
        {"input": {"orgId": "12", "metric": "replication_messages", "limitValue": "1000"}},
        QUOTA,
    ),
    Case(
        QuotasAPI,
        "set",
        ({"appId": "42", "tierId": "6", "metric": "replication_messages", "limitValue": "10"},),
        "SetQuota",
        {
            "input": {
                "appId": "42",
                "tierId": "6",
                "metric": "replication_messages",
                "limitValue": "10",
            }
        },
        QUOTA,
    ),
    Case(QuotasAPI, "remove", (5,), "DeleteQuota", {"quotaId": "5"}, True),
    # -- usage -----------------------------------------------------------------------
    Case(
        UsageAPI,
        "app_graphql_operations",
        (12, 42, SINCE),
        "AppGraphqlOperations",
        {"orgId": "12", "appId": "42", "since": SINCE},
        [{"operationName": "Me", "totalOps": "3"}],
    ),
    Case(
        UsageAPI,
        "app_graphql_operations",
        ("12", "42", SINCE, 5),
        "AppGraphqlOperations",
        {"orgId": "12", "appId": "42", "since": SINCE, "limit": 5},
        [],
    ),
    Case(
        UsageAPI,
        "app_summary",
        (12, 42, SINCE),
        "AppUsageSummary",
        {"orgId": "12", "appId": "42", "since": SINCE, "operationLimit": 3},
        {"appId": "42", "graphqlSendBytes": "123456789012"},
        {"operation_limit": 3},
    ),
    Case(
        UsageAPI,
        "player_pulse",
        (12,),
        "PlayerPulse",
        {"orgId": "12"},
        {"orgLivePlayers": 4, "globalLivePlayers": 90},
    ),
    # -- shared_environment ----------------------------------------------------------
    Case(SharedEnvironmentAPI, "plans", (), "SharedEnvPlans", {}, [{"planId": "3"}]),
    Case(
        SharedEnvironmentAPI,
        "free_app_quota",
        (12,),
        "OrgFreeAppQuota",
        {"orgId": "12"},
        {"orgId": "12", "quota": 1, "usedFree": 0},
    ),
    Case(
        SharedEnvironmentAPI,
        "app_subscription",
        (42,),
        "AppSharedSubscription",
        {"appId": "42"},
        None,
    ),
    Case(
        SharedEnvironmentAPI,
        "app_runtime_state",
        ("42",),
        "AppRuntimeState",
        {"appId": "42"},
        RUNTIME,
    ),
    Case(
        SharedEnvironmentAPI,
        "auto_billing",
        (12,),
        "OrgAutoBilling",
        {"orgId": "12"},
        AUTO_BILLING,
    ),
    Case(
        SharedEnvironmentAPI,
        "payment_methods",
        (12,),
        "OrgPaymentMethods",
        {"orgId": "12"},
        [{"paymentMethodId": "8", "brand": "visa", "last4": "4242"}],
    ),
    Case(
        SharedEnvironmentAPI,
        "publish_app",
        (42,),
        "PublishAppToShared",
        {"appId": "42"},
        {"appId": "42", "free": True, "checkout": None},
    ),
    Case(
        SharedEnvironmentAPI,
        "publish_app",
        (42, 3, enums.PaymentProvider.PAYPAL, "done", "back", "publish-42"),
        "PublishAppToShared",
        {
            "appId": "42",
            "planId": "3",
            "provider": "PAYPAL",
            "successUrl": "done",
            "cancelUrl": "back",
            "idempotencyKey": "publish-42",
        },
        {"appId": "42", "free": False, "checkout": CHECKOUT},
    ),
    Case(
        SharedEnvironmentAPI,
        "cancel_subscription",
        (42, "cancel-42"),
        "CancelSharedSubscription",
        {"appId": "42", "idempotencyKey": "cancel-42"},
        SUBSCRIPTION,
    ),
    Case(
        SharedEnvironmentAPI, "set_spend_caps", (42,), "SetAppSpendCaps", {"appId": "42"}, RUNTIME
    ),
    Case(
        SharedEnvironmentAPI,
        "set_spend_caps",
        ("42", 100, "2400"),
        "SetAppSpendCaps",
        {"appId": "42", "hourlyLimitCents": "100", "dailyLimitCents": "2400"},
        RUNTIME,
    ),
    Case(
        SharedEnvironmentAPI,
        "set_reserved_throughput",
        (
            inputs.SetAppReservedThroughputInput(
                org_id="12", app_id="42", reserved_bytes_per_sec="1000000"
            ),
            "reserve-42",
        ),
        "SetAppReservedThroughput",
        {
            "input": {"orgId": "12", "appId": "42", "reservedBytesPerSec": "1000000"},
            "idempotencyKey": "reserve-42",
        },
        {"app": APP, "chargedCents": "1500"},
    ),
    Case(
        SharedEnvironmentAPI,
        "set_reserved_throughput",
        ({"orgId": "12", "appId": "42", "reservedBytesPerSec": "0"},),
        "SetAppReservedThroughput",
        {"input": {"orgId": "12", "appId": "42", "reservedBytesPerSec": "0"}},
        {"app": APP, "chargedCents": "0"},
    ),
    Case(
        SharedEnvironmentAPI,
        "set_auto_billing",
        ("12", True, 50000, 2500, "500", "auto-12"),
        "SetAutoBilling",
        {
            "orgId": "12",
            "enabled": True,
            "limitCents": "50000",
            "rechargeAmountCents": "2500",
            "lowWaterThresholdCents": "500",
            "idempotencyKey": "auto-12",
        },
        AUTO_BILLING,
    ),
    Case(
        SharedEnvironmentAPI,
        "setup_payment_method",
        (12,),
        "SetupSharedPaymentMethod",
        {"orgId": "12"},
        {"clientSecret": "seti_x", "publishableKey": "pk_x"},
    ),
    Case(
        SharedEnvironmentAPI,
        "setup_payment_method",
        (12, "setup-12"),
        "SetupSharedPaymentMethod",
        {"orgId": "12", "idempotencyKey": "setup-12"},
        {"clientSecret": "seti_x", "publishableKey": "pk_x"},
    ),
    Case(
        SharedEnvironmentAPI,
        "remove_payment_method",
        (12, 8),
        "RemoveSharedPaymentMethod",
        {"orgId": "12", "paymentMethodId": "8"},
        True,
    ),
    Case(
        SharedEnvironmentAPI,
        "remove_payment_method",
        ("12", "8", "remove-8"),
        "RemoveSharedPaymentMethod",
        {"orgId": "12", "paymentMethodId": "8", "idempotencyKey": "remove-8"},
        False,
    ),
    # -- platform and discovery ------------------------------------------------------
    Case(
        PlatformAPI,
        "config",
        (),
        "PlatformConfig",
        {},
        {"sharedGameApiUrl": None, "sharedGameApiWsUrl": None, "freeAppsPerOrg": 1},
    ),
    Case(
        DiscoveryDomain,
        "apps",
        ([42, "43"],),
        "AppDiscovery",
        {"appIds": ["42", "43"]},
        [
            {
                "appId": "42",
                "datacenterCode": "or",
                "gameApiUrl": "game-api-or",
                "gameApiWsUrl": "game-ws-or",
            },
            {"appId": "43", "datacenterCode": None, "gameApiUrl": None},
        ],
        returns=[
            AppEndpoint(
                app_id="42",
                datacenter_code="or",
                game_api_url="game-api-or",
                game_api_ws_url="game-ws-or",
            ),
            AppEndpoint(app_id="43"),
        ],
    ),
    Case(
        DiscoveryDomain,
        "app",
        (42,),
        "AppDiscovery",
        {"appIds": ["42"]},
        [{"appId": "42", "datacenterCode": "va", "gameApiUrl": "game-api-va"}],
        returns=AppEndpoint(app_id="42", datacenter_code="va", game_api_url="game-api-va"),
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
    ported = {_snake(name) for name in CROWDYJS_METHODS[domain]}
    assert _public_methods(domain) == ported
    assert {case.method for case in CASES if case.domain is domain} == ported


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case.domain.__name__}.{case.method}")
async def test_sends_operation_and_variables(
    api: MockApi, graphql: AsyncGraphQLClient, case: Case
) -> None:
    api.reply_with_root(case.reply)
    method = getattr(case.domain(graphql), case.method)
    result = await method(*case.args, **case.kwargs)
    assert result == (case.reply if case.returns is None else case.returns)
    assert len(api.sent) == 1
    assert api.last.operation_name == case.operation
    assert api.last.query == DOCUMENTS[case.operation]
    assert api.last.variables == case.variables


def test_platform_config_is_the_one_inline_document() -> None:
    assert INLINE_OPERATIONS == (PLATFORM_CONFIG,)
    assert PLATFORM_CONFIG.name == "PlatformConfig"
    assert PLATFORM_CONFIG.kind == "query"
    assert PLATFORM_CONFIG.root_fields == ("platformConfig",)


@pytest.mark.parametrize(
    "row",
    [
        None,
        {},
        {"appId": 42, "gameApiUrl": "game-api-of-42"},
        {"appId": "42", "splitMode": "yes", "deploymentTarget": 3, "gameApiUrl": ""},
    ],
    ids=["no app", "empty row", "numeric id", "unusable fields"],
)
async def test_route_for_falls_back_to_the_clients_own_endpoint(
    api: MockApi, graphql: AsyncGraphQLClient, row: Any
) -> None:
    api.reply_with_root(row)
    route = await AppsAPI(graphql).route_for(42)
    assert route == AppRoute(
        app_id="42", split_mode=False, deployment_target=None, game_api_url=None
    )
    assert api.last.operation_name == "App"
    assert api.last.variables == {"appId": "42"}


async def test_route_for_keeps_a_shared_app_on_the_shared_endpoint(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    api.reply_with_root({"appId": "42", "splitMode": False, "deploymentTarget": "SHARED"})
    route = await AppsAPI(graphql).route_for("42")
    assert route == AppRoute(app_id="42", deployment_target="SHARED")
    assert route.game_api_url is None


@pytest.mark.parametrize(
    "reply",
    [[], [{"appId": "42", "gameApiUrl": None}], [{"appId": "42", "gameApiUrl": ""}]],
    ids=["no entry", "unplaced", "empty url"],
)
async def test_discovery_app_is_none_without_a_placement(
    api: MockApi, graphql: AsyncGraphQLClient, reply: list[dict[str, Any]]
) -> None:
    api.reply_with_root(reply)
    assert await DiscoveryDomain(graphql).app("42") is None
    assert api.last.operation_name == "AppDiscovery"
    assert api.last.variables == {"appIds": ["42"]}


@pytest.mark.parametrize(
    "rule",
    [
        inputs.SetQuotaInput(metric="replication_messages", limit_value="10"),
        inputs.SetQuotaInput(tier_id="6", metric="replication_messages", limit_value="10"),
        inputs.SetQuotaInput(app_id=None, org_id=None, metric="m", limit_value="10"),
        {"metric": "replication_messages", "limitValue": "10"},
        {"tierId": "6", "appId": None, "orgId": "", "metric": "m", "limitValue": "10"},
        {"app_id": "42", "metric": "m", "limitValue": "10"},
    ],
    ids=["struct", "struct tier only", "struct nulls", "mapping", "mapping empty", "snake keys"],
)
async def test_quotas_set_refuses_a_platform_global_rule_before_any_request(
    api: MockApi, graphql: AsyncGraphQLClient, rule: Any
) -> None:
    with pytest.raises(CrowdyError, match="needs an appId or an orgId"):
        await QuotasAPI(graphql).set(rule)
    assert api.sent == []


async def test_set_auto_billing_keeps_or_removes_the_period_cap(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    shared = SharedEnvironmentAPI(graphql)
    api.reply_with_root(AUTO_BILLING)
    await shared.set_auto_billing(12, True)
    assert api.last.variables == {"orgId": "12", "enabled": True}
    await shared.set_auto_billing(12, True, limit_cents=None)
    assert api.last.variables == {"orgId": "12", "enabled": True, "limitCents": None}
    await shared.set_auto_billing(12, False, recharge_amount_cents=None, idempotency_key="k")
    assert api.last.variables == {"orgId": "12", "enabled": False, "idempotencyKey": "k"}
    assert {sent.operation_name for sent in api.sent} == {"SetAutoBilling"}


def test_admin_groups_the_same_instances(graphql: AsyncGraphQLClient) -> None:
    parts: dict[str, Any] = {
        "organizations": OrganizationsAPI(graphql),
        "apps": AppsAPI(graphql),
        "app_access": AppAccessAPI(graphql),
        "billing": BillingAPI(graphql),
        "payments": PaymentsAPI(graphql),
        "quotas": QuotasAPI(graphql),
        "usage": UsageAPI(graphql),
        "shared_environment": SharedEnvironmentAPI(graphql),
        "grids": object(),
    }
    admin = AdminAPI(**parts)
    for name, instance in parts.items():
        assert getattr(admin, name) is instance
    with pytest.raises(TypeError):
        AdminAPI(*parts.values())


async def test_a_bad_id_is_refused_before_anything_is_sent(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    with pytest.raises(CrowdyProtocolError):
        await OrganizationsAPI(graphql).get("12 OR 1=1")
    with pytest.raises(CrowdyProtocolError):
        await BillingAPI(graphql).set_app_budget(12, 42, "50.5")
    with pytest.raises(CrowdyProtocolError):
        await DiscoveryDomain(graphql).apps(["42", "forty-three"])
    with pytest.raises(CrowdyProtocolError):
        await SharedEnvironmentAPI(graphql).set_auto_billing(12, True, limit_cents="lots")
    assert api.sent == []


async def test_id_sequences_are_not_one_string(api: MockApi, graphql: AsyncGraphQLClient) -> None:
    with pytest.raises(TypeError):
        await OrganizationsAPI(graphql).set_member_roles(12, 7, "34")
    with pytest.raises(TypeError):
        await DiscoveryDomain(graphql).apps("4243")
    assert api.sent == []
