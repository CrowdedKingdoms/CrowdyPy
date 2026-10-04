"""The World Stores' GraphQL side: host election, save state, avatar state and chunk
persistence, against httpx's mock transport, over a real native session."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from crowdypy.client import AsyncCrowdyClient
from crowdypy.replication import Assignment, AsyncReplicationConnection, TokenMaterial
from crowdypy.stores import WorldSessionCore, create_world_session
from crowdypy.utils import decode_base64, encode_base64

TOKEN = "t" * 64
QUIET = {"session_ready_wait_ms": 0, "advertise_capabilities": False}


class Api:
    """Answers each operation from ``roots`` by its first root field; a list of answers is
    used in order (an exception instance becomes a GraphQL error), the last one repeating."""

    def __init__(self) -> None:
        self.roots: dict[str, Any] = {}
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query: str = body["query"]
        root = query[query.index("{") + 1 :].strip().split("(")[0].split("{")[0].strip()
        self.sent.append((root, body.get("variables") or {}))
        answer = self.roots.get(root)
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, dict) and "__error__" in answer:
            return httpx.Response(200, json={"data": None, "errors": [answer["__error__"]]})
        return httpx.Response(200, json={"data": {root: answer}})

    def calls(self, root: str) -> list[dict[str, Any]]:
        return [variables for name, variables in self.sent if name == root]


def error(code: str, **extensions: Any) -> dict[str, Any]:
    return {"__error__": {"message": code.lower(), "extensions": {"code": code, **extensions}}}


class Provider:
    def __init__(self, port: int) -> None:
        self.port = port

    def assign_server(self) -> Assignment:
        return Assignment("127.0.0.1", "", self.port)

    def refresh_token(self, current: Assignment | None) -> TokenMaterial:
        raise RuntimeError


@pytest.fixture
async def setup() -> AsyncIterator[tuple[Api, AsyncCrowdyClient, AsyncReplicationConnection]]:
    api = Api()
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    client = AsyncCrowdyClient(
        http_url="https://ck.example.test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(api.handle)),
    )
    connection = AsyncReplicationConnection(
        Provider(server.getsockname()[1]), TokenMaterial(TOKEN, 42), app_id=7, **QUIET
    )
    await connection.connect()
    yield api, client, connection
    await connection.close()
    await client.aclose()
    server.close()


async def until(condition: Callable[[], Any], session: WorldSessionCore | None = None) -> None:
    for _ in range(300):
        if condition():
            return
        if session is not None:
            session.tick()
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


async def test_host_tracker_reports_changes_and_keeps_the_host_on_failure(setup: Any) -> None:
    api, client, connection = setup
    api.roots["actorHeartbeat"] = [
        {"hostUserId": "u1"},
        error("PLATFORM_BUSY"),
        {"hostUserId": "u2"},
    ]
    session = create_world_session(
        client, 7, connection=connection, host={"my_user_id": "u2", "interval_ms": 20}
    )
    assert session.host is not None
    seen: list[str | None] = []
    session.host.on_host_changed(seen.append)
    await until(lambda: session.host is not None and session.host.host_user_id == "u1", session)
    await until(lambda: len(api.calls("actorHeartbeat")) >= 3, session)
    await until(lambda: session.host is not None and session.host.host_user_id == "u2", session)
    assert seen[-1] == "u2"
    assert "u1" in seen
    assert session.host.is_host
    session.dispose()


async def test_save_state_loads_sets_and_saves(setup: Any) -> None:
    api, client, connection = setup
    api.roots["userAppState"] = {"state": encode_base64(json.dumps({"level": 3}).encode())}
    api.roots["updateUserAppState"] = {"appId": "7"}
    session = create_world_session(client, 7, connection=connection, save=True)
    assert session.save is not None
    assert await session.save.load() == {"level": 3}
    session.save.patch({"coins": 10})
    assert session.save.dirty
    await session.save.save()
    assert not session.save.dirty
    assert session.save.last_saved_at is not None
    written = api.calls("updateUserAppState")[0]["input"]
    assert written["appId"] == "7"
    assert json.loads(decode_base64(written["state"])) == {"level": 3, "coins": 10}
    session.dispose()


async def test_avatar_state_binds_your_first_avatar(setup: Any) -> None:
    api, client, connection = setup
    api.roots["myAvatars"] = [[{"avatarId": "99"}]]  # one answer: the list
    api.roots["avatar"] = {
        "publicState": encode_base64(b'{"name": "Ada"}'),
        "privateState": encode_base64(b'{"gold": 5}'),
    }
    api.roots["avatarAppState"] = {"state": encode_base64(b'{"xp": 1}')}
    api.roots["updateAvatarAppState"] = {"avatarId": "99"}
    session = create_world_session(client, 7, connection=connection, avatar=True)
    assert session.avatar is not None
    await session.avatar.load()
    assert session.avatar.avatar_id == "99"
    assert session.avatar.public_state == {"name": "Ada"}
    assert session.avatar.private_state == {"gold": 5}
    assert session.avatar.app_state == {"xp": 1}
    await session.avatar.set_app_state({"xp": 2})
    written = api.calls("updateAvatarAppState")[0]["input"]
    assert (written["avatarId"], json.loads(decode_base64(written["state"]))) == ("99", {"xp": 2})
    session.dispose()


async def test_chunks_hydrate_by_distance_and_write_back_with_refusals(setup: Any) -> None:
    api, client, connection = setup
    voxels = bytes([3]) * 4096
    api.roots["getChunksByDistance"] = {
        "chunks": [{"coordinates": {"x": "0", "y": "0", "z": "0"}, "voxels": encode_base64(voxels)}]
    }
    api.roots["updateChunk"] = [error("PLATFORM_BUSY"), {"appId": "7"}]
    generated: list[tuple[int, int, int]] = []

    def worldgen(coord: tuple[int, int, int]) -> bytes:
        generated.append(coord)
        return bytes([7]) * 4096

    session = WorldSessionCore(
        client,
        7,
        connection,
        chunk_options={"on_missing": worldgen, "write_back_interval_ms": None},
    )
    failures: list[Any] = []
    session.chunks.on_write_back_failed(failures.append)
    await session.chunks.ensure_around((0, 0, 0), 1)

    request = api.calls("getChunksByDistance")[0]["input"]
    assert (request["maxDistance"], request["limit"]) == (1, 27)
    loaded = session.chunks.get((0, 0, 0))
    assert loaded is not None
    assert loaded.load_state == "loaded"
    assert session.chunks.voxel_type_at((0, 0, 0), 5, 5, 5) == 3
    assert len(generated) == 26  # every requested chunk the server never stored
    assert session.chunks.pending_write_backs == 26
    assert session.chunks.voxel_type_at((1, 1, 1), 0, 0, 0) == 7

    assert await session.chunks.flush() == []  # the busy answer was retried
    assert session.chunks.pending_write_backs == 0
    assert len(api.calls("updateChunk")) == 27
    assert decode_base64(api.calls("updateChunk")[-1]["input"]["voxels"]) == bytes([7]) * 4096
    seeded = session.chunks.get((1, 1, 1))
    assert seeded is not None
    assert (seeded.load_state, seeded.dirty) == ("loaded", False)

    api.roots["updateChunk"] = [error("FORBIDDEN", httpStatus=403)]
    session.chunks.seed((9, 9, 9), bytes(4096))
    failed = await session.chunks.flush()
    assert [(f.coord, f.reason, f.attempts) for f in failed] == [((9, 9, 9), "refused", 1)]
    assert failures == failed
    session.dispose()
