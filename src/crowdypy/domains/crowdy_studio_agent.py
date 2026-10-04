"""Agentic Crowdy Studio's metered model endpoint and policy (``client.crowdy_studio_agent``).

The model endpoint itself is REST (``GET /v1/model/models``, ``POST
/v1/model/chat/completions``, bearer = the app token) and is what an in-browser Studio agent
spends tokens through. These GraphQL companions read what it recorded and gate whether it
may run: the player's provider consent and own usage (``use_studio_agent``), and the app's
policy and sanitized usage (``view_compute_diagnostics``; ``manage_compute`` to change it).
Mirrors CrowdyCPP's ``CrowdyStudioAgentAPI``. CrowdyJS's in-browser agent sends the consent
and model-usage roots itself; its portable API sends none of the seven.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from crowdypy._operation import inline_operation
from crowdypy.domains._base import Domain, omit_none
from crowdypy.utils import bigint

__all__ = ["CrowdyStudioAgentAPI"]

_CONSENT_FIELDS = "appId consented consentedAt"
_LIMIT_FIELDS = (
    "providerRequests inputTokens outputTokens reasoningTokens totalTokens "
    "providerCostMicrousd toolCalls compiles"
)
_POLICY_FIELDS = (
    "fragment CrowdyStudioAgentPolicyFields on CrowdyStudioAgentPolicy { kind appId enabled "
    "killSwitch operatorKillSwitch disableReasonCode disableReason allowedModelIds "
    "allowedToolNames allowedModes allowedRiskClasses "
    f"turnLimits {{ {_LIMIT_FIELDS} toolRounds wallClockMs concurrentProviderRequests }} "
    f"sessionLimits {{ {_LIMIT_FIELDS} concurrentRuns }} "
    f"playerDayLimits {{ {_LIMIT_FIELDS} concurrentSessions concurrentRunsPerApp }} "
    "retention { assistantChunkHours detailedContextHours sessionDataDays usageDays } "
    "privacy { requireZdr denyDataCollection allowPrivateSource requirePrivateSourceConsent "
    "persistProviderBodies } "
    "funding { billingMode payerKind rateCardId walletDebitEnabled } "
    "capabilityGaps { mode code detail } "
    "revision platformRevision appRevision effectiveRevision createdAt updatedAt }"
)
_USAGE_RECORD_FIELDS = (
    "usageId appId userId sessionId runId provider providerGenerationId resolvedModelId "
    "accountingStatus requestCount promptTokens completionTokens reasoningTokens cachedTokens "
    "nativePromptTokens nativeCompletionTokens nativeReasoningTokens nativeCachedTokens "
    "toolCalls toolRounds compileCount wallClockMs reservedCostMicrousd providerCostUsd "
    "upstreamInferenceCostUsd zdrEnforced dataCollectionDenied platformPolicyRevision "
    "appPolicyRevision billingMode payerKind occurredAt ingestedAt"
)
_USAGE_SUMMARY_FIELDS = (
    "requestCount promptTokens completionTokens reasoningTokens cachedTokens toolCalls "
    "toolRounds compileCount wallClockMs providerCostUsd"
)

PROVIDER_CONSENT = inline_operation(
    "CrowdyStudioProviderConsent",
    "query",
    "crowdyStudioProviderConsent",
    "query CrowdyStudioProviderConsent($appId: BigInt!) { crowdyStudioProviderConsent(appId: "
    f"$appId) {{ {_CONSENT_FIELDS} }} }}",
)
SET_PROVIDER_CONSENT = inline_operation(
    "CrowdyStudioSetProviderConsent",
    "mutation",
    "crowdyStudioSetProviderConsent",
    "mutation CrowdyStudioSetProviderConsent($input: SetCrowdyStudioProviderConsentInput!) { "
    f"crowdyStudioSetProviderConsent(input: $input) {{ {_CONSENT_FIELDS} }} }}",
)
MODEL_USAGE = inline_operation(
    "CrowdyStudioModelUsage",
    "query",
    "crowdyStudioModelUsage",
    "query CrowdyStudioModelUsage($appId: BigInt!, $limit: Int) { crowdyStudioModelUsage(appId: "
    "$appId, limit: $limit) { appId payerKind todayRequests todayChargeMicrousd "
    "dayLimitMicrousd recent { usageId payerKind status requestedModel resolvedModel "
    "promptTokens completionTokens reasoningTokens chargeMicrousd occurredAt client } } }",
)
POLICY = inline_operation(
    "CrowdyStudioAgentPolicy",
    "query",
    "crowdyStudioAgentPolicy",
    "query CrowdyStudioAgentPolicy($appId: BigInt!) { crowdyStudioAgentPolicy(appId: $appId) "
    "{ ...CrowdyStudioAgentPolicyFields } }\n" + _POLICY_FIELDS,
)
EFFECTIVE_POLICY = inline_operation(
    "CrowdyStudioAgentEffectivePolicy",
    "query",
    "crowdyStudioAgentEffectivePolicy",
    "query CrowdyStudioAgentEffectivePolicy($appId: BigInt!) { "
    "crowdyStudioAgentEffectivePolicy(appId: $appId) { ...CrowdyStudioAgentPolicyFields } }\n"
    + _POLICY_FIELDS,
)
USAGE = inline_operation(
    "CrowdyStudioAgentUsage",
    "query",
    "crowdyStudioAgentUsage",
    "query CrowdyStudioAgentUsage($appId: BigInt!, $since: DateTime, $until: DateTime, "
    "$limit: Int) { crowdyStudioAgentUsage(appId: $appId, since: $since, until: $until, "
    f"limit: $limit) {{ records {{ {_USAGE_RECORD_FIELDS} }} summary {{ {_USAGE_SUMMARY_FIELDS} }} "
    "since until } }",
)
SET_POLICY = inline_operation(
    "CrowdyStudioAgentSetPolicy",
    "mutation",
    "setCrowdyStudioAgentPolicy",
    "mutation CrowdyStudioAgentSetPolicy($input: SetCrowdyStudioAgentAppPolicyInput!) { "
    "setCrowdyStudioAgentPolicy(input: $input) { ...CrowdyStudioAgentPolicyFields } }\n"
    + _POLICY_FIELDS,
)
INLINE_OPERATIONS = (
    PROVIDER_CONSENT,
    SET_PROVIDER_CONSENT,
    MODEL_USAGE,
    POLICY,
    EFFECTIVE_POLICY,
    USAGE,
    SET_POLICY,
)


class CrowdyStudioAgentAPI(Domain):
    """The Studio agent's consent, model usage and app policy."""

    async def provider_consent(self, app_id: str | int) -> dict[str, Any]:
        """Whether the signed-in player lets their project source reach a model provider in
        this app. The model endpoint refuses project context without it."""
        result: dict[str, Any] = await self._request(PROVIDER_CONSENT, {"appId": bigint(app_id)})
        return result

    async def set_provider_consent(self, app_id: str | int, consented: bool) -> dict[str, Any]:
        """Record or revoke that consent."""
        result: dict[str, Any] = await self._request(
            SET_PROVIDER_CONSENT, {"input": {"appId": bigint(app_id), "consented": consented}}
        )
        return result

    async def model_usage(self, app_id: str | int, limit: int | None = None) -> dict[str, Any]:
        """The signed-in player's own model usage today against the policy's ceiling, and
        the most recent requests (``limit`` of them)."""
        result: dict[str, Any] = await self._request(
            MODEL_USAGE, omit_none({"appId": bigint(app_id), "limit": limit})
        )
        return result

    async def policy(self, app_id: str | int) -> dict[str, Any]:
        """The app's own agent policy, or a disabled deny-all projection when it has none."""
        result: dict[str, Any] = await self._request(POLICY, {"appId": bigint(app_id)})
        return result

    async def effective_policy(self, app_id: str | int) -> dict[str, Any]:
        """The policy as enforced: the platform's switches and limits resolved with the
        app's, failing closed."""
        result: dict[str, Any] = await self._request(EFFECTIVE_POLICY, {"appId": bigint(app_id)})
        return result

    async def usage(
        self,
        app_id: str | int,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """The app's sanitized platform-funded agent usage over a bounded window (ISO-8601
        ``since`` and ``until``)."""
        result: dict[str, Any] = await self._request(
            USAGE,
            omit_none({"appId": bigint(app_id), "since": since, "until": until, "limit": limit}),
        )
        return result

    async def set_policy(self, input: Mapping[str, Any]) -> dict[str, Any]:
        """Create or patch the app's agent policy (``SetCrowdyStudioAgentAppPolicyInput``,
        camelCase). Omitted values stay; a first policy starts disabled. Needs
        ``manage_compute``."""
        result: dict[str, Any] = await self._request(SET_POLICY, {"input": dict(input)})
        return result
