"""The agent vocabulary the player-host contract shares (CrowdyJS's ``agent-types``)."""

from __future__ import annotations

from typing import Literal, TypedDict

__all__ = [
    "CrowdyAgentApprovalPolicy",
    "CrowdyAgentLeaseV1",
    "CrowdyAgentPreemptionReason",
    "CrowdyAgentToolRisk",
]

CrowdyAgentToolRisk = Literal[
    "READ_ONLY",
    "ROUTINE_WRITE",
    "WORLD_CONTROL",
    "DESTRUCTIVE",
    "TRUST_CONSENT",
    "ECONOMIC",
    "IRREVERSIBLE",
]
CrowdyAgentApprovalPolicy = Literal["NONE", "REQUIRED", "CONDITIONAL"]
#: Why an agent's intent was cleared: the closed preemption vocabulary.
CrowdyAgentPreemptionReason = Literal[
    "HUMAN_INPUT",
    "HUMAN_EDIT",
    "HUMAN_STOP",
    "ESCAPE",
    "DEATH",
    "CONTEXT_CHANGED",
    "PERMISSION_CHANGED",
    "ADMISSION_CHANGED",
    "CONTROL_TARGET_CHANGED",
    "DISCONNECTED",
    "CLIENT_REATTACHED",
    "QUOTA_FAILURE",
    "BUDGET_FAILURE",
    "OPERATOR_KILL",
    "LEASE_EXPIRED",
    "SESSION_CLOSED",
]


class CrowdyAgentLeaseV1(TypedDict, total=False):
    leaseId: str
    leaseType: Literal["WORKSPACE", "PLAY"]
    scopes: list[str]
    expiresAt: str
    lastHeartbeatAt: str
    revokedReason: str
