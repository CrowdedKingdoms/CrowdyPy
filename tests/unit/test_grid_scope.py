"""GridScope: the grid calls with the app and grid filled in, and a local box check."""

from __future__ import annotations

import httpx
import pytest

from conftest import MockApi, Sent
from crowdypy import GridBox, GridChunk, GridScopeError
from crowdypy._generated import operations as ops
from crowdypy.client import AsyncCrowdyClient
from crowdypy.sync import CrowdyClient

TOKEN = {
    "token": "grid-token",
    "lowChunk": {"x": "-2", "y": "0", "z": "10"},
    "highChunk": {"x": "2", "y": "3", "z": "9223372036854775807"},
}


def answer(sent: Sent) -> object:
    if sent.operation_name == ops.MINT_GRID_TOKEN.name:
        return {"data": {"mintGridToken": TOKEN}}
    if sent.operation_name == ops.GRID_CHANNELS.name:
        return {"data": {"gridChannels": [{"id": "7"}]}}
    if sent.operation_name == ops.CREATE_GRID_CHANNEL.name:
        return {"data": {"createGridChannel": {"id": "8"}}}
    if sent.operation_name == ops.JOIN_CHANNEL.name:
        return {"data": {"joinChannel": {"id": "8"}}}
    if sent.operation_name == ops.LEAVE_CHANNEL.name:
        return {"data": {"leaveChannel": True}}
    return {"data": {}}


def async_client(api: MockApi) -> AsyncCrowdyClient:
    api.responder = answer
    return AsyncCrowdyClient(
        http_url="https://ck.example.test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
    )


async def test_mint_token_learns_the_box(api: MockApi) -> None:
    grid = async_client(api).grid("42", 5)
    assert grid.bounds is None
    assert not grid.contains((0, 0, 10))
    with pytest.raises(GridScopeError, match="grid box unknown"):
        grid.assert_contains((0, 0, 10))

    token = await grid.mint_token(ttl_seconds=120)
    assert token["token"] == "grid-token"
    assert api.last.variables == {"input": {"appId": "42", "gridId": "5", "ttlSeconds": 120}}
    assert grid.bounds == GridBox(GridChunk(-2, 0, 10), GridChunk(2, 3, 2**63 - 1))
    assert grid.contains({"x": "2", "y": "3", "z": "9223372036854775807"})
    assert grid.contains(GridChunk(-2, 0, 10))
    assert not grid.contains((3, 0, 10))
    with pytest.raises(GridScopeError, match=r"chunk \(3,0,10\) is outside grid 5"):
        grid.assert_contains((3, 0, 10))


async def test_mint_token_leaves_ttl_to_the_server_by_default(api: MockApi) -> None:
    await async_client(api).grid(42, 5).mint_token()
    assert api.last.variables == {"input": {"appId": "42", "gridId": "5"}}


async def test_a_box_passed_in_is_used_without_a_request(api: MockApi) -> None:
    box = GridBox(GridChunk(0, 0, 0), GridChunk(1, 1, 1))
    grid = async_client(api).grid("42", "5", box)
    grid.assert_contains((1, 1, 1))
    assert api.sent == []


async def test_channels_fill_in_the_app_and_grid(api: MockApi) -> None:
    grid = async_client(api).grid("42", "5")
    assert await grid.channels.list() == [{"id": "7"}]
    assert api.last.variables == {"appId": "42", "gridId": "5"}
    assert await grid.channels.create("lobby", members_can_send=False) == {"id": "8"}
    assert api.last.variables == {
        "input": {"appId": "42", "gridId": "5", "name": "lobby", "membersCanSend": False}
    }
    await grid.channels.join("8")
    assert api.last.operation_name == ops.JOIN_CHANNEL.name
    await grid.channels.leave(8)
    assert api.last.operation_name == ops.LEAVE_CHANNEL.name


def test_the_blocking_client_has_the_same_scope(api: MockApi) -> None:
    api.responder = answer
    client = CrowdyClient(
        http_url="https://ck.example.test",
        http_client=httpx.Client(transport=httpx.MockTransport(api.handler)),
    )
    grid = client.grid("42", "5")
    grid.mint_token()
    assert grid.contains((0, 0, 10))
    assert grid.channels.list() == [{"id": "7"}]
    client.close()


def test_grid_scope_error_is_one_class_for_both_clients() -> None:
    from crowdypy._sync.grid_scope import GridScopeError as SyncGridScopeError

    assert SyncGridScopeError is GridScopeError


async def test_a_response_missing_its_root_field_is_a_protocol_error(api: MockApi) -> None:
    from crowdypy.errors import CrowdyProtocolError

    client = async_client(api)
    api.responder = lambda sent: {"data": {}}
    with pytest.raises(CrowdyProtocolError, match="has no 'gridChannels' field"):
        await client.grid("42", "5").channels.list()
