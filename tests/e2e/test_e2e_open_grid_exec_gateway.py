"""Open grids and the ck-exec gateway check, as a throwaway org admin (CrowdyJS's
open-grid-and-exec-gateway suite). Registers two throwaway accounts and an app, archived
afterwards; needs CROWDY_E2E_THROWAWAY_OWNER=1."""

from __future__ import annotations

import asyncio
import os
import random
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

import crowdypy
from crowdypy.domains.exec import CrowdyExecError, exec_gateway_refusal
from crowdypy.exec_gateway import AsyncExecConnection
from e2e.conftest import CONFIG

pytestmark = pytest.mark.skipif(
    os.environ.get("CROWDY_E2E_THROWAWAY_OWNER") != "1",
    reason="set CROWDY_E2E_THROWAWAY_OWNER=1 (registers throwaway accounts)",
)


def rid() -> str:
    return secrets.token_hex(4)


def chunk(x: int, y: int, z: int) -> dict[str, str]:
    return {"x": str(x), "y": str(y), "z": str(z)}


async def patiently[T](work: Callable[[], Awaitable[T]], attempts: int = 6) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return await work()
        except crowdypy.CrowdyGraphQLError as error:
            if error.code != "PLATFORM_BUSY" or attempt >= attempts:
                raise
            await asyncio.sleep(0.5 * attempt)
    raise AssertionError("unreachable")


async def until[T](
    read: Callable[[], Awaitable[T]], ok: Callable[[T], bool], seconds: float = 30
) -> T:
    deadline = asyncio.get_running_loop().time() + seconds
    last = await read()
    while not ok(last) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.5)
        last = await read()
    return last


@dataclass
class World:
    app_id: str
    owner: crowdypy.AsyncCrowdyClient
    owner_game: crowdypy.AsyncCrowdyClient
    player_id: str
    player: crowdypy.AsyncCrowdyClient
    player_game: crowdypy.AsyncCrowdyClient


async def _account(kind: str) -> tuple[crowdypy.AsyncCrowdyClient, str]:
    client = crowdypy.AsyncCrowdyClient(http_url=CONFIG.api_url)
    email = f"crowdypy-e2e-{kind}-{rid()}@test.invalid"
    auth = await patiently(lambda: client.auth.register(email, f"Aa1!e2e-{rid()}{rid()}"))
    return client, auth.user.user_id


async def _game(identity: crowdypy.AsyncCrowdyClient, app_id: str) -> crowdypy.AsyncCrowdyClient:
    minted = await patiently(lambda: identity.portal.mint_app_token(app_id))
    game = crowdypy.AsyncCrowdyClient(
        http_url=minted.game_api_url or CONFIG.api_url,
        discovery_url=minted.discovery_url or CONFIG.api_url,
    )
    game.set_app_token(minted)
    return game


@pytest.fixture
async def world() -> AsyncIterator[World]:
    owner, _ = await _account("owner")
    tag = rid()
    org = await owner.admin.organizations.create(
        {"name": f"e2e open grids {tag}", "slug": f"e2e-open-grids-{tag}"}
    )
    placeable = (await owner.admin.apps.placeable_datacenters()).get("datacenters") or []
    datacenter = next((dc["code"] for dc in placeable if dc.get("placeable")), None)
    assert datacenter, "the tier can place an app"
    app = await owner.admin.apps.create(
        {
            "orgId": str(org["orgId"]),
            "name": f"e2e-open-grids-{tag}",
            "slug": f"e2e-open-grids-{tag}",
            "datacenter": datacenter,
        }
    )
    app_id = str(app["appId"])
    player, player_id = await _account("player")
    built = World(
        app_id, owner, await _game(owner, app_id), player_id, player, await _game(player, app_id)
    )
    try:
        yield built
    finally:
        try:
            await owner.admin.apps.archive(app_id)
        except crowdypy.CrowdyError as error:
            print(f"[e2e] could not archive app {app_id}: {error}")
        for client in (built.owner, built.owner_game, built.player, built.player_game):
            await client.aclose()


async def test_an_org_admin_opens_a_grid_reads_it_back_and_closes_it(world: World) -> None:
    grids = world.owner_game.game_apps
    await until(
        lambda: _succeeds(world.player_game.server_status.game_client_bootstrap(world.app_id)), bool
    )
    x = 200 + random.randrange(1000)
    created = await until(
        lambda: grids.create_grid(
            {"appId": world.app_id, "corner1": chunk(x, 0, x), "corner2": chunk(x + 3, 0, x + 3)}
        ),
        lambda result: result.get("error") != "NO_MATCHING_GRID_ASSIGNMENT",
    )
    assert created.get("error") == "NO_ERROR", created
    grid_id = str(created["grid"]["grid_id"])
    assert (await grids.open_permissions(world.app_id, grid_id))["permissionKeys"] == []

    opened = await grids.set_open_permissions(
        {
            "appId": world.app_id,
            "gridId": grid_id,
            "permissionKeys": ["update_voxel_data", "access"],
        }
    )
    assert str(opened["gridId"]) == grid_id
    assert opened["permissionKeys"] == ["access", "update_voxel_data"]
    keys = ["access", "update_voxel_data"]
    assert (await grids.open_permissions(world.app_id, grid_id))["permissionKeys"] == keys
    held = await until(
        lambda: grids.user_permissions(world.app_id, grid_id, world.player_id),
        lambda p: all(k in p["permissionKeys"] for k in keys),
    )
    assert "update_voxel_data" in held["permissionKeys"], held

    with pytest.raises(crowdypy.CrowdyGraphQLError) as refused:
        await grids.set_open_permissions(
            {"appId": world.app_id, "gridId": grid_id, "permissionKeys": ["write_server_code"]}
        )
    assert refused.value.code == "BAD_REQUEST"
    assert "player-code keys" in refused.value.message
    assert (await grids.open_permissions(world.app_id, grid_id))["permissionKeys"] == keys

    closed = await grids.set_open_permissions(
        {"appId": world.app_id, "gridId": grid_id, "permissionKeys": []}
    )
    assert closed["permissionKeys"] == []
    after = await until(
        lambda: grids.user_permissions(world.app_id, grid_id, world.player_id),
        lambda p: "update_voxel_data" not in p["permissionKeys"],
    )
    assert "update_voxel_data" not in after["permissionKeys"], after


async def test_the_tier_s_gateway_passes_the_check_and_a_tampered_token_is_denied(
    world: World,
) -> None:
    exec_api = world.player_game.exec
    try:
        endpoint = await exec_api.endpoint(world.app_id)
    except crowdypy.CrowdyError as error:
        if "not configured on this tier" in str(error) or "Cannot query field" in str(error):
            pytest.skip(f"no ck-exec here: {error}")
        raise
    api = world.player_game.graphql.endpoint
    assert exec_gateway_refusal(api, endpoint.gateway_url) is None, endpoint.gateway_url
    assert endpoint.gateway_url.startswith("wss://")

    connection = await exec_api.connect(world.app_id)
    try:
        assert await connection.ping() >= 0
    finally:
        await connection.close()

    tampered = f"{endpoint.token[:-4]}AAAA"
    with pytest.raises(CrowdyExecError) as refused:
        await AsyncExecConnection.open(endpoint.gateway_url, tampered)
    assert refused.value.status == "Denied", refused.value.message
    assert "the gateway refused the connection (HTTP 401" in refused.value.message


async def _succeeds(work: Awaitable[Any]) -> bool:
    try:
        await work
    except crowdypy.CrowdyError:
        return False
    return True
