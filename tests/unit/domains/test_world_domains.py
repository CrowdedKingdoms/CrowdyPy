"""The world and gameplay domains: each method sends its operation with converted variables.

chunks, voxels, actors, avatars, state, teleport, host, game_apps and grids are thin
wrappers in CrowdyJS (no client-side checks, no decoding), so the contract under test is the
operation, the variables (BigInt arguments as decimal strings, omitted optionals,
idempotency keys where the document puts them) and the root field handed back untouched.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from conftest import MockApi
from crowdypy._generated import inputs
from crowdypy._generated import operations as ops
from crowdypy.domains._base import Domain
from crowdypy.domains.actors import ActorsAPI
from crowdypy.domains.avatars import AvatarsAPI
from crowdypy.domains.chunks import ChunksAPI
from crowdypy.domains.game_apps import GameAppsAPI
from crowdypy.domains.grids import GridsAPI
from crowdypy.domains.host import HostAPI
from crowdypy.domains.input_log import InputLogAPI
from crowdypy.domains.state import StateAPI
from crowdypy.domains.teleport import TeleportAPI
from crowdypy.domains.voxels import VoxelsAPI
from crowdypy.errors import CrowdyProtocolError
from crowdypy.graphql import AsyncGraphQLClient
from crowdypy.utils import decode_base64, encode_base64

UUID = "0123456789abcdef0123456789abcdef"
CHUNK = {"x": "0", "y": "-1", "z": "9223372036854775807"}
VOXEL = {"x": 1, "y": 2, "z": 15}
AT_CHUNK = {"appId": "42", "coordinates": CHUNK}
BLOB = encode_base64(b"\x00\x01\xfe\xff")
GRID = {"appId": "42", "gridId": "5"}
ROW: dict[str, Any] = {"id": "1"}
ROWS = [ROW]

#: The CrowdyJS method set of each class, snake_cased.
PORTED: dict[type[Domain], set[str]] = {
    ChunksAPI: {
        "get",
        "get_lods",
        "by_distance",
        "voxel_list",
        "update",
        "update_state",
        "update_lods",
    },
    VoxelsAPI: {"list", "list_by_distance", "update", "history", "history_connection", "rollback"},
    ActorsAPI: {
        "get",
        "list",
        "list_connection",
        "batch_lookup",
        "create",
        "update",
        "delete",
        "update_state",
    },
    AvatarsAPI: {
        "list_for_user",
        "get",
        "mine",
        "app_state",
        "app_states",
        "create",
        "update",
        "delete",
        "update_state",
        "update_app_state",
    },
    StateAPI: {"get_one", "get_all", "update", "delete"},
    TeleportAPI: {"request"},
    InputLogAPI: {"sessions", "messages"},
    HostAPI: {"get", "am_i_host", "heartbeat"},
    GameAppsAPI: {
        "ownership",
        "assign_ownership",
        "transfer_ownership",
        "user_permissions",
        "nearby_permissions",
        "nearby_grids",
        "permission_limits",
        "open_permissions",
        "group_grants",
        "create_grid",
        "delete_grid",
        "grant_permissions",
        "revoke_permissions",
        "set_permission_limits",
        "set_open_permissions",
        "assign_group",
        "revoke_group",
    },
    GridsAPI: {"mint_token", "create_channel", "channels"},
}


def case(
    domain: type[Domain],
    method: str,
    args: tuple[Any, ...],
    operation: str,
    variables: dict[str, Any],
    *,
    kwargs: dict[str, Any] | None = None,
    reply: Any = ROW,
    label: str = "",
) -> Any:
    name = f"{domain.__name__}.{method}" + (f"[{label}]" if label else "")
    return pytest.param(domain, method, args, kwargs or {}, operation, variables, reply, id=name)


def by_input(
    domain: type[Domain], method: str, operation: str, input: dict[str, Any], reply: Any = ROW
) -> Any:
    return case(domain, method, (input,), operation, {"input": input}, reply=reply)


CASES = [
    # the input log: BigInt ids as decimal strings, paging and filters passed through
    case(InputLogAPI, "sessions", (42,), "InputLogSessions", {"appId": "42"}),
    case(
        InputLogAPI,
        "sessions",
        ("42",),
        "InputLogSessions",
        {"appId": "42", "first": 20, "after": "c1", "filter": {"userId": "7", "messageType": 129}},
        kwargs={"first": 20, "after": "c1", "filter": {"userId": "7", "messageType": 129}},
        label="filtered",
    ),
    case(
        InputLogAPI,
        "messages",
        (42, 9001),
        "InputLogMessages",
        {"appId": "42", "gameTokenId": "9001", "first": 200, "filter": {"messageTypes": [129]}},
        kwargs={"first": 200, "filter": inputs.InputLogMessageFilter(message_types=[129])},
    ),
    # chunks: every method takes its input object as given
    by_input(ChunksAPI, "get", "GetChunk", {**AT_CHUNK, "includeAllLods": True}, reply=None),
    by_input(ChunksAPI, "get_lods", "GetChunkLods", {**AT_CHUNK, "lodLevels": [0, 2]}),
    by_input(
        ChunksAPI,
        "by_distance",
        "GetChunksByDistance",
        {"appId": "42", "centerCoordinate": CHUNK, "maxDistance": 2, "limit": 10},
    ),
    by_input(ChunksAPI, "voxel_list", "GetVoxelList", AT_CHUNK),
    by_input(
        ChunksAPI,
        "update",
        "UpdateChunk",
        {
            **AT_CHUNK,
            "voxels": encode_base64(bytes(4096)),
            "voxelStates": [{"voxelCoord": VOXEL, "voxelType": 7, "state": BLOB}],
        },
    ),
    by_input(ChunksAPI, "update_state", "UpdateChunkState", {**AT_CHUNK, "chunkState": BLOB}),
    by_input(
        ChunksAPI,
        "update_lods",
        "UpdateChunkLods",
        {**AT_CHUNK, "lods": [{"level": 0, "data": BLOB}]},
    ),
    # voxels
    by_input(
        VoxelsAPI, "list", "ListVoxels", {**AT_CHUNK, "since": "2026-09-01T00:00:00Z"}, reply=ROWS
    ),
    by_input(
        VoxelsAPI,
        "list_by_distance",
        "ListVoxelUpdatesByDistance",
        {"appId": "42", "centerCoordinate": CHUNK, "maxDistance": 8},
    ),
    by_input(
        VoxelsAPI,
        "update",
        "UpdateVoxel",
        {**AT_CHUNK, "location": VOXEL, "voxelType": 255, "state": BLOB},
    ),
    case(VoxelsAPI, "history", (42,), "VoxelUpdateHistory", {"appId": "42"}, reply=ROWS),
    case(
        VoxelsAPI,
        "history",
        ("42",),
        "VoxelUpdateHistory",
        {
            "appId": "42",
            "userId": "7",
            "from": "2026-09-01T00:00:00Z",
            "to": "2026-09-02T00:00:00Z",
            "limit": 10,
            "offset": 20,
        },
        kwargs={
            "user_id": 7,
            "from_": "2026-09-01T00:00:00Z",
            "to": "2026-09-02T00:00:00Z",
            "limit": 10,
            "offset": 20,
        },
        reply=ROWS,
        label="filtered",
    ),
    case(VoxelsAPI, "history_connection", (42,), "VoxelUpdateHistoryConnection", {"appId": "42"}),
    case(
        VoxelsAPI,
        "history_connection",
        (42,),
        "VoxelUpdateHistoryConnection",
        {"appId": "42", "userId": "7", "to": "2026-09-02T00:00:00Z", "first": 50, "after": "c1"},
        kwargs={"user_id": "7", "to": "2026-09-02T00:00:00Z", "first": 50, "after": "c1"},
        label="paged",
    ),
    by_input(
        VoxelsAPI,
        "rollback",
        "RollbackVoxelUpdates",
        {
            "appId": "42",
            "userId": "7",
            "from": "2026-09-01T00:00:00Z",
            "to": "2026-09-02T00:00:00Z",
            "dryRun": False,
            "idempotencyKey": "retry-1",
        },
        reply=ROWS,
    ),
    # actors: uuids are Strings, so nothing is converted
    case(ActorsAPI, "get", (UUID,), "Actor", {"uuid": UUID}),
    case(ActorsAPI, "list", (), "Actors", {}, reply=ROWS),
    case(
        ActorsAPI,
        "list",
        ({"appId": "42", "chunk": CHUNK},),
        "Actors",
        {"filter": {"appId": "42", "chunk": CHUNK}},
        reply=ROWS,
        label="filtered",
    ),
    case(ActorsAPI, "list_connection", (), "ActorsConnection", {}),
    case(
        ActorsAPI,
        "list_connection",
        (),
        "ActorsConnection",
        {"first": 10, "after": "c1", "filter": {"appId": "42"}},
        kwargs={"first": 10, "after": "c1", "filter": {"appId": "42"}},
        label="paged",
    ),
    by_input(ActorsAPI, "batch_lookup", "BatchLookupActors", {"uuids": [UUID]}, reply=ROWS),
    by_input(
        ActorsAPI,
        "create",
        "CreateActor",
        {"appId": "42", "uuid": UUID, "chunk": CHUNK, "publicState": BLOB},
    ),
    case(
        ActorsAPI,
        "update",
        (UUID, {"chunk": CHUNK}),
        "UpdateActor",
        {"uuid": UUID, "input": {"chunk": CHUNK}},
    ),
    case(ActorsAPI, "delete", (UUID,), "DeleteActor", {"uuid": UUID}),
    case(
        ActorsAPI,
        "delete",
        (UUID, "retry-1"),
        "DeleteActor",
        {"uuid": UUID, "idempotencyKey": "retry-1"},
        label="idempotent",
    ),
    case(
        ActorsAPI,
        "update_state",
        (UUID, {"privateState": BLOB}),
        "UpdateActorState",
        {"uuid": UUID, "input": {"privateState": BLOB}},
    ),
    # avatars: ids are BigInt
    case(AvatarsAPI, "list_for_user", (7,), "UserAvatars", {"userId": "7"}, reply=ROWS),
    case(AvatarsAPI, "get", ("8",), "AvatarById", {"id": "8"}),
    case(AvatarsAPI, "mine", (), "MyAvatars", {}, reply=ROWS),
    case(
        AvatarsAPI,
        "app_state",
        (42, 8),
        "AvatarAppState",
        {"appId": "42", "avatarId": "8"},
        reply=None,
    ),
    case(
        AvatarsAPI,
        "app_states",
        (42, [8, "9"]),
        "AvatarAppStates",
        {"appId": "42", "avatarIds": ["8", "9"]},
        reply=ROWS,
    ),
    by_input(AvatarsAPI, "create", "CreateAvatar", {"name": "Scout"}),
    case(
        AvatarsAPI,
        "update",
        (8, {"name": "Ranger"}),
        "UpdateAvatar",
        {"id": "8", "input": {"name": "Ranger"}},
    ),
    case(AvatarsAPI, "delete", (8,), "DeleteAvatar", {"id": "8"}),
    case(
        AvatarsAPI,
        "delete",
        (8, "retry-1"),
        "DeleteAvatar",
        {"id": "8", "idempotencyKey": "retry-1"},
        label="idempotent",
    ),
    case(
        AvatarsAPI,
        "update_state",
        (8, {"publicState": BLOB, "privateState": None}),
        "UpdateAvatarState",
        {"id": "8", "input": {"publicState": BLOB, "privateState": None}},
    ),
    by_input(
        AvatarsAPI,
        "update_app_state",
        "UpdateAvatarAppState",
        {"appId": "42", "avatarId": "8", "state": BLOB},
    ),
    # state
    case(StateAPI, "get_one", (42,), "UserAppState", {"appId": "42"}, reply=None),
    case(StateAPI, "get_all", (), "UserAppStates", {}, reply=ROWS),
    by_input(StateAPI, "update", "UpdateUserAppState", {"appId": "42", "state": BLOB}),
    case(StateAPI, "delete", ("42",), "DeleteUserAppState", {"appId": "42"}),
    # teleport
    by_input(
        TeleportAPI,
        "request",
        "TeleportRequest",
        {"appId": "42", "chunkAddress": CHUNK, "voxelAddress": VOXEL, "uuid": UUID},
        reply={"success": False, "errorCode": "UNAUTHORIZED"},
    ),
    # host
    case(HostAPI, "get", (42,), "GameHost", {"appId": "42"}, reply=None),
    case(HostAPI, "am_i_host", ("42",), "AmIGameHost", {"appId": "42"}, reply=True),
    case(
        HostAPI,
        "heartbeat",
        (42,),
        "ActorHeartbeat",
        {"appId": "42"},
        reply={"hostUserId": "7", "actorCount": 2},
    ),
    # game_apps
    case(GameAppsAPI, "ownership", (42, 5), "GridOwnership", GRID, reply=None),
    by_input(GameAppsAPI, "assign_ownership", "AssignGridOwnership", {**GRID, "ownerUserId": "7"}),
    by_input(
        GameAppsAPI, "transfer_ownership", "TransferGridOwnership", {**GRID, "newOwnerUserId": "8"}
    ),
    case(
        GameAppsAPI,
        "user_permissions",
        (42, "5", 7),
        "GridUserPermissions",
        {**GRID, "userId": "7"},
    ),
    by_input(
        GameAppsAPI,
        "nearby_permissions",
        "NearbyGridPermissions",
        {"appId": "42", "userId": "7", "lowChunk": CHUNK, "highChunk": CHUNK},
        reply=ROWS,
    ),
    by_input(
        GameAppsAPI,
        "nearby_grids",
        "NearbyGrids",
        {"appId": "42", "lowChunk": CHUNK, "highChunk": CHUNK},
        reply=ROWS,
    ),
    case(GameAppsAPI, "permission_limits", (42, 5), "GridPermissionLimits", GRID),
    case(GameAppsAPI, "open_permissions", (42, 5), "GridOpenPermissions", GRID),
    by_input(
        GameAppsAPI,
        "set_open_permissions",
        "SetGridOpenPermissions",
        {**GRID, "permissionKeys": ["update_voxel_data"]},
    ),
    case(
        GameAppsAPI,
        "group_grants",
        (42, 5, 3),
        "GridGroupGrants",
        {**GRID, "groupId": "3"},
        reply=ROWS,
    ),
    by_input(
        GameAppsAPI,
        "create_grid",
        "CreateGrid",
        {"appId": "42", "corner1": CHUNK, "corner2": CHUNK},
    ),
    by_input(GameAppsAPI, "delete_grid", "DeleteGrid", GRID),
    by_input(
        GameAppsAPI,
        "grant_permissions",
        "GrantGridPermissions",
        {**GRID, "userId": "7", "permissionKeys": ["update_voxel_data"]},
    ),
    by_input(
        GameAppsAPI,
        "revoke_permissions",
        "RevokeGridPermissions",
        {**GRID, "userId": "7", "idempotencyKey": "retry-1"},
    ),
    by_input(
        GameAppsAPI,
        "set_permission_limits",
        "SetGridPermissionLimits",
        {**GRID, "permissionKeys": []},
    ),
    by_input(
        GameAppsAPI,
        "assign_group",
        "AssignGroupToGrid",
        {**GRID, "groupId": "3", "permissionKeys": ["teleport"]},
        reply=ROWS,
    ),
    by_input(
        GameAppsAPI, "revoke_group", "RevokeGroupFromGrid", {**GRID, "groupId": "3"}, reply=ROWS
    ),
    # grids
    by_input(GridsAPI, "mint_token", "MintGridToken", {**GRID, "ttlSeconds": 600}),
    by_input(GridsAPI, "create_channel", "CreateGridChannel", {**GRID, "name": "plaza"}),
    case(GridsAPI, "channels", (42, 5), "GridChannels", GRID, reply=ROWS),
]


@pytest.mark.parametrize(
    ("domain", "method", "args", "kwargs", "operation", "variables", "reply"), CASES
)
async def test_method_sends_its_operation(
    api: MockApi,
    graphql: AsyncGraphQLClient,
    domain: type[Domain],
    method: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    operation: str,
    variables: dict[str, Any],
    reply: Any,
) -> None:
    api.reply_with_root(reply)
    result = await getattr(domain(graphql), method)(*args, **kwargs)
    assert api.last.operation_name == operation
    assert api.last.query == ops.OPERATIONS[operation].document
    assert api.last.variables == variables
    assert result == reply


@pytest.mark.parametrize("domain", list(PORTED), ids=lambda domain: domain.__name__)
def test_public_methods_are_the_crowdyjs_set(domain: type[Domain]) -> None:
    public = {name for name in vars(domain) if not name.startswith("_")}
    assert public == PORTED[domain]
    assert all(inspect.iscoroutinefunction(getattr(domain, name)) for name in public)


def test_every_ported_method_has_a_case() -> None:
    covered = {(param.values[0], param.values[1]) for param in CASES}
    assert covered == {(domain, name) for domain, names in PORTED.items() for name in names}


async def test_input_structs_encode_under_their_graphql_names(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    api.reply_with_root(ROWS)
    await VoxelsAPI(graphql).rollback(
        inputs.RollbackVoxelUpdatesInput(
            app_id="42",
            user_id="7",
            from_="2026-09-01T00:00:00Z",
            to="2026-09-02T00:00:00Z",
            dry_run=True,
            idempotency_key="retry-1",
        )
    )
    assert api.last.variables == {
        "input": {
            "appId": "42",
            "userId": "7",
            "from": "2026-09-01T00:00:00Z",
            "to": "2026-09-02T00:00:00Z",
            "dryRun": True,
            "idempotencyKey": "retry-1",
        }
    }

    api.reply_with_root(ROW)
    await ChunksAPI(graphql).get(
        inputs.GetChunkInput(app_id="42", coordinates=inputs.ChunkCoordinatesInput(**CHUNK))
    )
    assert api.last.variables == {"input": AT_CHUNK}

    await ActorsAPI(graphql).list_connection(first=5, filter=inputs.ActorFilterInput(uuid=UUID))
    assert api.last.variables == {"first": 5, "filter": {"uuid": UUID}}


async def test_state_blobs_stay_base64_both_ways(api: MockApi, graphql: AsyncGraphQLClient) -> None:
    blob = encode_base64(b"\x00\xffsave")
    api.reply_with_root({"appId": "42", "state": blob})
    row = await StateAPI(graphql).update(inputs.CreateUserAppStateInput(app_id="42", state=blob))
    assert api.last.variables == {"input": {"appId": "42", "state": blob}}
    assert row["state"] == blob
    assert decode_base64(row["state"]) == b"\x00\xffsave"


async def test_am_i_host_is_a_bool(api: MockApi, graphql: AsyncGraphQLClient) -> None:
    api.reply_with_root(False)
    assert await HostAPI(graphql).am_i_host(42) is False


@pytest.mark.parametrize(
    "call",
    [
        lambda graphql: HostAPI(graphql).get("app-42"),
        lambda graphql: GameAppsAPI(graphql).user_permissions(42, 5, "7.0"),
        lambda graphql: AvatarsAPI(graphql).app_states(42, ["8", "nine"]),
        lambda graphql: VoxelsAPI(graphql).history(42, user_id="me"),
    ],
    ids=["host.get", "game_apps.user_permissions", "avatars.app_states", "voxels.history"],
)
async def test_bigint_arguments_are_refused_before_sending(
    api: MockApi,
    graphql: AsyncGraphQLClient,
    call: Callable[[AsyncGraphQLClient], Awaitable[Any]],
) -> None:
    with pytest.raises(CrowdyProtocolError, match="not a decimal BigInt"):
        await call(graphql)
    assert api.sent == []
