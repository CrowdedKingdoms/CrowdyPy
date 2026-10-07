"""The SDK is for normal clients: players, developers and org-admins.

It wraps no root field only a super-admin or a platform operator can call (the operator's
decision of 2026-09-28, the rule CrowdyJS and CrowdyCPP carry too); platform tooling calls
those fields directly. Two nets, because the schema has no structured marker for either
role: the fields the Game API guards for those roles, and any root field whose description
says it is operator- or super-admin-only. Every document counts: the copied operations
(all of them ship, in the generated module) and the inline documents in the package.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
    build_schema,
    parse,
    validate,
)

from crowdypy._operation import Operation

ROOT = Path(__file__).resolve().parents[2]

# Same list as CrowdyCPP tests/parity/sdk-audience.test.mjs (Game API dev da1179f9).
PLATFORM_ONLY = {
    # super-admin
    "setSuperAdmin", "setOperator", "setEarlyAccessOverride", "updateUserType", "forceLogoutUser",
    "usersPaginated", "usersConnection", "checkouts", "checkoutsConnection", "paymentEvents",
    "paymentEventsConnection", "setOrgStatus", "setAppVisibility",
    # operator
    "cpBillingCreditOverbill", "cpSetCrowdyStudioAgentAppKill", "cpSetCrowdyStudioAgentPlatformPolicy",
    "creditOrgWallet", "forgetEmailDeliverability", "reinstateOrganization", "retireOrganization",
    "runSharedUsageBillingTick", "sendTestEmail", "setBillingRate", "setHostedGameListing",
    "setOrgBillingExempt", "takeDownHostedGame", "agentRateCards", "allHostedGames",
    "billingExemptOrgs", "billingRateCard", "cpBillingInvariantRuns", "cpBillingReconciliations",
    "cpBillingWriteOffs", "cpCrowdyStudioAgentCatalog", "cpCrowdyStudioAgentPlatformPolicy",
    "emailDeliverability", "emailDeliveryConfig", "retiredOrganizations",
}  # fmt: skip
PLATFORM_DESCRIPTION = re.compile(
    r"\b(operator|super[- ]?admins?)( only\b|:)|\brestricted to super[- ]?admins?\b|\brequires a super[- ]?admin\b",
    re.I,
)


def _package_modules() -> list[tuple[str, object]]:
    """Every module of the INSTALLED package (wheel tests run without the source tree)."""
    import crowdypy

    modules = []
    for info in sorted(pkgutil.walk_packages(crowdypy.__path__, "crowdypy."), key=lambda i: i.name):
        if "._sync" in info.name or "._generated" in info.name or info.name.endswith("_native"):
            continue
        modules.append((info.name, importlib.import_module(info.name)))
    return modules


def _inline_operations() -> list[tuple[str, Operation]]:
    return [
        (rel, op)
        for rel, module in _package_modules()
        for op in getattr(module, "INLINE_OPERATIONS", ())
    ]


def _documents() -> list[tuple[str, str]]:
    from crowdypy.rediscover import BOOTSTRAP_QUERY

    docs = [
        (p.relative_to(ROOT).as_posix(), p.read_text(encoding="utf-8"))
        for p in sorted((ROOT / "operations").rglob("*.graphql"))
    ]
    docs.extend((rel, op.document) for rel, op in _inline_operations())
    docs.append(("crowdypy.rediscover", BOOTSTRAP_QUERY))
    return docs


def _wrapped_root_fields() -> dict[str, str]:
    parsed = [(where, parse(text)) for where, text in _documents()]
    fragments: dict[str, FragmentDefinitionNode] = {}
    for _, doc in parsed:
        for definition in doc.definitions:
            if isinstance(definition, FragmentDefinitionNode):
                fragments[definition.name.value] = definition
    out: dict[str, str] = {}

    def visit(selection_set: SelectionSetNode, where: str, seen: frozenset[str]) -> None:
        for sel in selection_set.selections:
            if isinstance(sel, FieldNode):
                if not sel.name.value.startswith("__"):
                    out.setdefault(sel.name.value, where)
            elif isinstance(sel, InlineFragmentNode):
                visit(sel.selection_set, where, seen)
            elif isinstance(sel, FragmentSpreadNode) and sel.name.value not in seen:
                frag = fragments.get(sel.name.value)
                if frag:
                    visit(frag.selection_set, where, seen | {sel.name.value})

    for where, doc in parsed:
        for definition in doc.definitions:
            if isinstance(definition, OperationDefinitionNode):
                visit(definition.selection_set, where, frozenset())
    return out


def _root_descriptions() -> dict[str, str]:
    schema = build_schema((ROOT / "schema.gql").read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for gql_type in (schema.query_type, schema.mutation_type, schema.subscription_type):
        if gql_type is None:
            continue
        for name, field in gql_type.fields.items():
            out[name] = " ".join((field.description or "").split())
    return out


def test_no_document_selects_a_platform_only_root_field() -> None:
    wrapped = _wrapped_root_fields()
    assert len(wrapped) > 100, f"only {len(wrapped)} wrapped root fields found; the scan is broken"
    described = {name for name, d in _root_descriptions().items() if PLATFORM_DESCRIPTION.search(d)}
    offenders = sorted(f"{n} ({w})" for n, w in wrapped.items() if n in PLATFORM_ONLY | described)
    assert offenders == [], "the SDK wraps platform-only root fields; remove them"


def test_the_platform_only_list_names_real_root_fields() -> None:
    roots = _root_descriptions()
    assert sorted(n for n in PLATFORM_ONLY if n not in roots) == []


def test_the_description_net_tells_platform_from_org_admin() -> None:
    roots = _root_descriptions()
    for name in (
        "setOrgStatus",
        "retireOrganization",
        "emailDeliverability",
        "allHostedGames",
        "checkouts",
    ):
        assert PLATFORM_DESCRIPTION.search(roots.get(name, "")), (
            f"{name} should read as platform-only"
        )
    for name in ("orgMembers", "inviteOrgMember", "createApp", "myCheckouts"):
        assert not PLATFORM_DESCRIPTION.search(roots.get(name, "")), (
            f"{name} should read as org-admin"
        )


def test_inline_documents_are_valid_and_named_as_declared() -> None:
    schema = build_schema((ROOT / "schema.gql").read_text(encoding="utf-8"))
    checked = 0
    for rel, op in _inline_operations():
        doc = parse(op.document)
        errors = validate(schema, doc)
        assert not errors, f"{rel}: {op.name}: {errors[0].message}"
        definition = next(d for d in doc.definitions if isinstance(d, OperationDefinitionNode))
        assert definition.name, f"{rel}: {op.name} has no name"
        assert definition.name.value == op.name, f"{rel}: {op.name} misnamed"
        assert definition.operation.value == op.kind, f"{rel}: {op.name} kind"
        first = definition.selection_set.selections[0]
        assert isinstance(first, FieldNode)
        assert (first.alias or first.name).value == op.root_fields[0], f"{rel}: {op.name} root"
        checked += 1
    assert checked >= 25, f"only {checked} inline documents found; the scan is broken"
