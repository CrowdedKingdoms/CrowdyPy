"""The player-host contract's values (CrowdyJS's ``player-host/types``): what a game
advertises and observes about the player it controls. Values cross as JSON, so these are
``TypedDict``s; coordinates, distances and health are decimal strings."""

from __future__ import annotations

from typing import Literal, NotRequired, Protocol, TypedDict

from crowdypy.player_host.agent_errors import AgentErrorV1
from crowdypy.player_host.agent_types import (
    CrowdyAgentApprovalPolicy,
    CrowdyAgentPreemptionReason,
    CrowdyAgentToolRisk,
)

__all__ = [
    "GameCommandResultV1",
    "GameCommandV1",
    "GameObservationActorV1",
    "GameObservationInventoryV1",
    "GameObservationV1",
    "ObserveRequestV1",
    "PlayerHostAdapterV1",
    "PlayerHostCapabilitiesV1",
    "PlayerHostCommandCapabilityV1",
    "PlayerHostCommandKind",
    "PlayerHostLeaseScope",
    "PlayerHostLookV1",
    "PlayerHostVector3V1",
    "ValidatedGateV1",
]


class PlayerHostVector3V1(TypedDict):
    x: str
    y: str
    z: str


class PlayerHostLookV1(TypedDict):
    yaw: str
    pitch: str


PlayerHostLeaseScope = Literal[
    "observe",
    "locomotion",
    "interact",
    "craft",
    "combat",
    "communicate",
    "travel",
    "grid",
    "trust_consent",
    "commerce",
]
PlayerHostCommandKind = Literal[
    "MOVE",
    "LOOK",
    "STOP",
    "INVENTORY_SELECT",
    "INVENTORY_CONSUME",
    "INVENTORY_TRANSFER",
    "INTERACT",
    "CRAFT",
    "MOUNT",
    "COMBAT_ATTACK",
    "CHAT_SEND",
    "TRAVEL_TELEPORT",
]


class PlayerHostCommandCapabilityV1(TypedDict):
    kind: PlayerHostCommandKind
    toolName: str
    requiredScope: NotRequired[PlayerHostLeaseScope]
    risk: CrowdyAgentToolRisk
    approval: CrowdyAgentApprovalPolicy
    rateLimitPerSecond: float


class _ObservationLimits(TypedDict):
    maxAgeMs: int
    maxNearbyActors: int
    maxNearbyVoxels: int


class PlayerHostCapabilitiesV1(TypedDict):
    contractVersion: Literal["crowdy.player-host/1"]
    gameId: str
    revision: str
    controlledEntityId: str
    commands: list[PlayerHostCommandCapabilityV1]
    observation: _ObservationLimits
    advertisedAt: str


class ObserveRequestV1(TypedDict):
    detail: Literal["MINIMAL", "STANDARD", "TACTICAL"]
    maxNearbyActors: int
    maxNearbyVoxels: int


class GameObservationActorV1(TypedDict):
    actorId: str
    kind: Literal["PLAYER", "NPC", "MOB", "OBJECT", "VEHICLE"]
    position: PlayerHostVector3V1
    distance: str
    disposition: Literal["SELF", "FRIENDLY", "NEUTRAL", "HOSTILE", "UNKNOWN"]
    label: NotRequired[str]
    health: NotRequired[str]


class _InventorySlot(TypedDict):
    slot: int
    itemId: str
    quantity: int
    usable: bool


class GameObservationInventoryV1(TypedDict):
    selectedSlot: int
    slots: list[_InventorySlot]
    craftableRecipeIds: list[str]


class _Player(TypedDict):
    position: PlayerHostVector3V1
    velocity: PlayerHostVector3V1
    look: PlayerHostLookV1
    health: str
    alive: bool


class _ControlledEntity(TypedDict):
    kind: Literal["PLAYER", "MOUNT", "VEHICLE"]
    position: PlayerHostVector3V1
    velocity: PlayerHostVector3V1


class _Target(TypedDict):
    targetId: str
    kind: Literal["ACTOR", "VOXEL", "OBJECT", "NONE"]
    distance: str


class _Grid(TypedDict):
    gridRef: str
    low: PlayerHostVector3V1
    high: PlayerHostVector3V1
    effectiveScopes: list[PlayerHostLeaseScope]


class _Voxel(TypedDict):
    position: PlayerHostVector3V1
    material: str
    interaction: Literal["NONE", "MINE", "PLACE", "USE"]


class _InputState(TypedDict):
    modalOpen: bool
    textInputFocused: bool
    humanInputActive: bool


class GameObservationV1(TypedDict):
    contractVersion: Literal["crowdy.game-observation/1"]
    observationId: str
    capabilityRevision: str
    controlledEntityId: str
    observedAt: str
    expiresAt: str
    player: _Player
    controlledEntity: _ControlledEntity
    target: NotRequired[_Target]
    inventory: NotRequired[GameObservationInventoryV1]
    grid: NotRequired[_Grid]
    nearbyActors: list[GameObservationActorV1]
    nearbyVoxels: list[_Voxel]
    inputState: _InputState


#: One of the commands in ``GAME_COMMAND_SCHEMAS_V1``, as JSON (``kind`` selects which).
GameCommandV1 = dict[str, object]


class _Detail(TypedDict):
    name: str
    value: str


class GameCommandResultV1(TypedDict):
    contractVersion: Literal["crowdy.game-command-result/1"]
    status: Literal["SUCCEEDED", "FAILED", "DENIED", "OUTCOME_UNKNOWN"]
    commandKind: PlayerHostCommandKind
    observationId: NotRequired[str]
    details: NotRequired[list[_Detail]]
    error: NotRequired[AgentErrorV1]


class ValidatedGateV1(TypedDict):
    contractVersion: Literal["crowdy.validated-gate/1"]
    clientEpoch: str
    leaseId: NotRequired[str]
    scopes: list[PlayerHostLeaseScope]
    contextVersion: str
    observationId: NotRequired[str]
    validatedAt: str


class PlayerHostAdapterV1(Protocol):
    """What a game implements so tooling can read the controlled player and its
    surroundings. Observation is the supported use: ``dispatch`` exists in the contract,
    but no SDK orchestrator drives it."""

    contract_version: Literal["crowdy.player-host/1"]

    async def capabilities(self) -> PlayerHostCapabilitiesV1: ...

    async def observe(self, request: ObserveRequestV1) -> GameObservationV1: ...

    async def dispatch(
        self, command: GameCommandV1, gate: ValidatedGateV1
    ) -> GameCommandResultV1: ...

    def clear_agent_intent(self, reason: CrowdyAgentPreemptionReason) -> None: ...
