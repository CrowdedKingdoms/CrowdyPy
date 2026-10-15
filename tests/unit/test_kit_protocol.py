"""The Game Kit against ck-exec's golden record of its starter hubs (``tests/fixtures/kit-protocol.json``,
a copy of ck-exec ``crates/ckx-kit-protocol/kit-protocol.json`` by way of CrowdyJS):

- the protocol's own cases: kit messages, refusal codes, shard keys, realtime events and actor
  states read as ck-exec wrote them;
- each starter's script replayed through a gateway that answers only the next recorded step:
  every call a kit module makes must be that step's method and arguments from that step's
  player, and it gets the hub's real reply; the hub's real topic messages are pushed to whoever
  subscribes. CrowdyJS's ``game-kit.test.mjs`` and CrowdyCPP's ``kit_runtime_test`` play the
  same scripts;
- the runtime's resends and pacing, private state, the topology's checks, emotes, and the
  blocking client's kit over the same gateway.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import msgspec
import pytest
from websockets.asyncio.server import ServerConnection, serve

from crowdypy.domains.exec import CrowdyExecError
from crowdypy.exec_gateway import AsyncExecConnection, ExecConnection
from crowdypy.kit import (
    EVENT_EMOTE,
    KIT_SUBSCRIPTIONS_MAX,
    CollectionMirror,
    GameKitClient,
    KitError,
    KitRuntime,
    PrivateState,
    decode_kit_actor_state,
    kit_actor_lanes,
    kit_error_code,
    kit_manifest_problems,
    parse_ability_event,
    parse_contact_damage,
    parse_control_point_event,
    parse_emote_event,
    parse_encounter_event,
    parse_engine_event,
    parse_kit_refusal,
    parse_kit_server_event,
    parse_movement_violation,
    parse_possession_event,
    parse_proposal_event,
    parse_race_timing_event,
    parse_score_event,
    parse_shard_key,
    parse_turn_event,
    parse_weather_event,
    parse_zone_change_event,
    session_turn,
    shard_key,
    shard_keys_around,
    split_kit_message,
)

RECORD = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kit-protocol.json").read_text(
        encoding="utf-8"
    )
)


def starter(crate: str) -> dict[str, Any]:
    return next(s for s in RECORD["starters"] if s["crate"] == crate)


def body_of(value: dict[str, Any]) -> dict[str, Any]:
    msg = split_kit_message(value)
    assert msg is not None
    return msg[1]


def without_action_id(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    rest = {k: v for k, v in value.items() if k != "action_id"}
    return rest or None


def same(a: Any, b: Any) -> bool:
    """Deep equality, numbers by value (a hub's 2 and a fixture's 2.0 are one number), map keys
    as text (the record writes a hub's integer keys, such as session scores by player, as text;
    msgpack hands Python the integers)."""
    if isinstance(a, dict) and isinstance(b, dict):
        a = {str(k): v for k, v in a.items()}
        b = {str(k): v for k, v in b.items()}
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return float(a) == float(b)
    return bool(a == b)


# ---- the gateway's frames ----


def read_frame(message: bytes) -> dict[str, Any]:
    tag = message[0]
    rid = int.from_bytes(message[1:5], "little")
    if tag == 0x04:
        return {"kind": "ping", "rid": rid}
    at = 5
    node_type = message[at + 1 : at + 1 + message[at]].decode()
    at += 1 + message[at]
    key_len = int.from_bytes(message[at : at + 2], "little")
    key = message[at + 2 : at + 2 + key_len].decode()
    at += 2 + key_len
    name = message[at + 1 : at + 1 + message[at]].decode()
    payload = message[at + 1 + message[at] :]
    kind = {0x01: "call", 0x02: "subscribe", 0x03: "unsubscribe"}[tag]
    return {
        "kind": kind,
        "rid": rid,
        "node_type": node_type,
        "key": key,
        "name": name,
        "payload": payload,
    }


def reply(rid: int, status: int, payload: bytes = b"") -> bytes:
    return b"\x81" + rid.to_bytes(4, "little") + bytes([status]) + payload


def push(node_type: str, key: str, topic: str, payload: bytes) -> bytes:
    k = key.encode()
    return (
        b"\x82" + bytes([len(node_type)]) + node_type.encode() + len(k).to_bytes(2, "little") + k
        + bytes([len(topic)]) + topic.encode() + payload
    )  # fmt: skip


STATUS = {"Ok": 0, "AppError": 1, "Busy": 2, "Moved": 3, "NotFound": 4, "DeadlineExceeded": 5,
          "Denied": 6, "RateLimited": 7, "Unavailable": 8}  # fmt: skip


class Replay:
    """A gateway that plays one starter's recorded script: it answers a call only when it is the
    next step (method, arguments without the action id, caller), with the recorded reply, and
    pushes the step's published messages to the topics callers subscribe to."""

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.at = 0
        self.refused: list[str] = []
        self.peers: list[tuple[ServerConnection, dict[str, int], set[str]]] = []
        self.url = ""

    def topic_key(self, topic: str) -> str:
        return f"{self.script['node_type']}\0{self.script['key']}\0{topic}"

    async def publish(self, published: list[dict[str, Any]]) -> None:
        for m in published:
            for ws, _, subs in self.peers:
                if self.topic_key(m["topic"]) in subs:
                    await ws.send(
                        push(
                            self.script["node_type"],
                            self.script["key"],
                            m["topic"],
                            bytes.fromhex(m["msgpack"]),
                        )
                    )

    async def handle(self, ws: ServerConnection) -> None:
        path = ws.request.path if ws.request is not None else ""
        kind, ident = path.split("token=", 1)[-1].split("-")
        caller = {kind: int(ident)}
        subs: set[str] = set()
        self.peers.append((ws, caller, subs))
        async for message in ws:
            assert isinstance(message, bytes)
            f = read_frame(message)
            if f["kind"] == "ping":
                await ws.send(b"\x84" + message[1:5])
                continue
            if f["kind"] in ("subscribe", "unsubscribe"):
                key = f"{f['node_type']}\0{f['key']}\0{f['name']}"
                (subs.add if f["kind"] == "subscribe" else subs.discard)(key)
                await ws.send(reply(f["rid"], 0))
                continue
            steps = self.script["steps"]
            step = steps[self.at] if self.at < len(steps) else None
            args = without_action_id(msgspec.msgpack.decode(f["payload"]) if f["payload"] else None)
            fits = (
                step is not None
                and step.get("call") == f["name"]
                and f["node_type"] == self.script["node_type"]
                and f["key"] == self.script["key"]
                and step.get("caller") == caller
                and same(without_action_id(step.get("args")), args)
            )
            if not fits:
                why = f"step {self.at} is {step and (step.get('call'), step.get('caller'), step.get('args'))}, not {f['name']} {args} from {caller}"
                self.refused.append(why)
                await ws.send(reply(f["rid"], 11, why.encode()))
                continue
            self.at += 1
            if step["ok"]:
                await ws.send(reply(f["rid"], 0, bytes.fromhex(step["reply_msgpack"])))
            else:
                await ws.send(
                    reply(f["rid"], STATUS.get(step["status"], 1), step["error"].encode())
                )
            await self.publish(step["published"])

    async def server_step(self) -> None:
        step = self.script["steps"][self.at]
        assert "advance_ms" in step or "instance" in (step.get("caller") or {}), step
        self.at += 1
        await self.publish(step["published"])

    def done(self) -> None:
        assert self.refused == []
        assert self.at == len(self.script["steps"])


async def kit_for(gw: Replay, kind: str, ident: int) -> GameKitClient:
    conn = await AsyncExecConnection.open(gw.url, f"{kind}-{ident}", call_timeout=3.0)
    return GameKitClient(
        "77", None,  # type: ignore[arg-type]
        connection=conn, player_id=ident if kind == "player" else None,
    )  # fmt: skip


async def until(what: str, check: Callable[[], bool], timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


async def code(awaitable: Any) -> str:
    with pytest.raises(KitError) as caught:
        await awaitable
    assert caught.value.status == "AppError"
    return caught.value.code


@pytest.fixture
async def replay(request: pytest.FixtureRequest) -> AsyncIterator[Replay]:
    gw = Replay(starter(request.param))
    async with serve(gw.handle, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        gw.url = f"ws://127.0.0.1:{port}"
        yield gw


# ---- the protocol's own cases ----


def test_kit_messages_read_as_ck_exec_writes_them() -> None:
    for m in RECORD["messages"]:
        value = msgspec.msgpack.decode(bytes.fromhex(m["msgpack"]))
        assert same(value, m["value"])
        msg = split_kit_message(value)
        assert msg is not None
        header, body = msg
        assert header.v == RECORD["protocol"]
        assert header.kind == ("delta" if m["name"] == "removal" else m["name"])
        assert not {"v", "epoch", "seq", "kind"} & body.keys()
    assert split_kit_message({"cells": [1]}) is None


def test_refusal_codes_read_back() -> None:
    for e in RECORD["errors"]:
        parsed = parse_kit_refusal(e["message"])
        assert parsed == (None if e["code"] is None else (e["code"], e["text"]))
    err = KitError.from_error(CrowdyExecError("AppError", "kit:cooldown: attacking too fast"))
    assert isinstance(err, KitError)
    assert kit_error_code(err) == "cooldown"
    assert kit_error_code(CrowdyExecError("Denied", "kit:cooldown: x")) is None


def test_shard_keys_are_the_hubs_own() -> None:
    for k in RECORD["shards"]["keys"]:
        assert shard_key(k["x"], k["z"], k["size"]) == k["key"]
    for a in RECORD["shards"]["around"]:
        assert shard_keys_around(a["x"], a["z"], a["size"], a["radius"]) == a["keys"]
    assert parse_shard_key("12,-4") == (12, -4)
    assert parse_shard_key("x") is None


def test_realtime_events_parse_as_the_registry_writes_them() -> None:
    parsers = {
        77: parse_contact_damage, 90: parse_weather_event, 91: parse_turn_event, 92: parse_score_event,
        93: parse_proposal_event, 94: parse_ability_event, 95: parse_movement_violation,
        96: parse_control_point_event, 97: parse_race_timing_event, 98: parse_zone_change_event,
        99: parse_encounter_event, 100: parse_possession_event, 101: parse_emote_event,
    }  # fmt: skip
    assert len(RECORD["events"]) == len(parsers)
    for e in RECORD["events"]:
        data = bytes.fromhex(e["bytes"])
        assert parse_engine_event(data) == (e["event_type"], e["body"])
        assert parsers[e["event_type"]](data) is not None, e["event_type"]
        assert parse_kit_server_event(e["event_type"], data[2:]) == (e["event_type"], e["body"])
    emote = next(e for e in RECORD["events"] if e["event_type"] == EVENT_EMOTE)
    parsed = parse_emote_event(bytes.fromhex(emote["bytes"]))
    assert parsed is not None
    assert parsed.emote == emote["body"]["emote"]


def test_server_driven_actors_decode_beside_players() -> None:
    for a in RECORD["actor_states"]:
        state = decode_kit_actor_state(bytes.fromhex(a["msgpack"]))
        assert state is not None
        assert (state.kind, state.id, state.x, state.z, state.hp) == (
            a["value"]["kind"], a["value"]["id"], a["value"]["x"], a["value"]["z"], a["value"].get("hp")
        )  # fmt: skip
    mob = decode_kit_actor_state(bytes.fromhex(RECORD["actor_states"][0]["msgpack"]))
    lanes = kit_actor_lanes()
    assert [name for name, f in lanes.items() if f(mob)] == ["mobs"]
    assert lanes["players"]("pose:1,2")
    assert not lanes["others"]("pose:1,2")
    assert decode_kit_actor_state(b"not msgpack") is None


# ---- the starters' scripts, replayed ----


@pytest.mark.parametrize("replay", ["world-tick"], indirect=True)
async def test_world(replay: Replay) -> None:
    p1, p2, dev = (
        await kit_for(replay, "player", 1),
        await kit_for(replay, "player", 2),
        await kit_for(replay, "developer", 9),
    )
    await p1.world.conditions.start()
    await p1.world.nodes.start()
    assert len(p1.world.nodes.list("Node")) == 2
    assert len((await p1.world.state())["nodes"]) == 2
    assert await p1.world.harvest(0) == 1
    await until(
        "the depletion delta",
        lambda: (m := p1.world.nodes.member(0)) is not None and m.props["amount"] == 1,
    )
    # The script's resend of the same action: answered from the hub's receipt.
    assert await p1.runtime.call("world", "", "harvest", {"node": 0}, action="resent") == 1
    assert await code(p1.world.harvest(0)) == "cooldown"
    assert await code(p2.world.harvest(7)) == "not_found"
    await replay.server_step()
    await until("the conditions snapshot", lambda: p1.world.conditions.position[1] == 1)
    assert isinstance(p1.world.conditions.get()["night"], bool)
    await p1.world.conditions.resync()
    await p1.world.nodes.resync()
    args = next(s for s in replay.script["steps"] if s.get("call") == "schedule")["args"]
    event = {"name": "blood-moon", "start_ms": args["start_ms"], "end_ms": args["end_ms"]}
    assert await code(p1.world.schedule(event)) == "forbidden"
    assert await dev.world.schedule({**event, "modifiers": {"aggro": 2}}) is True
    await until(
        "the event in the conditions",
        lambda: p1.world.conditions.get()["modifiers"].get("aggro") == 2,
    )
    replay.done()
    for k in (p1, p2, dev):
        await k.runtime.close()


@pytest.mark.parametrize("replay", ["matchmaker"], indirect=True)
async def test_matchmaking(replay: Replay) -> None:
    kits = {p: await kit_for(replay, "player", p) for p in range(1, 6)}

    def q(p: int) -> Any:
        return kits[p].matchmaking.queue("ranked")

    await q(1).counts.start()
    assert q(1).counts.get() == {"waiting": 0, "proposals": 0, "forming": 0}
    assert (await q(1).join(1000))["state"] == "queued"
    assert (await q(2).join(1020))["state"] == "queued"
    assert (await q(1).status())["state"] == "queued"
    await replay.server_step()
    await until(
        "the counts", lambda: q(1).counts.get()["waiting"] == 0 and q(1).counts.position[1] == 1
    )
    assert (await q(1).status())["state"] == "proposed"
    assert (await q(1).accept())["state"] == "proposed"
    matched: list[str] = []
    q(2).on_matched(matched.append)
    assert await q(2).accept() == {"state": "matched", "session": "ranked-1-1"}
    assert matched == ["ranked-1-1"]
    assert (await q(2).status())["state"] == "matched"
    assert await code(q(3).accept()) == "not_found"
    forming = await q(3).join(900, party=[4])
    assert (forming["state"], forming["members"]) == ("party", [3, 4])
    assert (await q(4).status())["state"] == "party"
    assert await code(q(5).join(900, leader=3)) == "forbidden"
    assert (await q(4).join(950, leader="3"))["state"] == "queued"
    assert q(4).place.get()["state"] == "queued"
    assert (await q(4).status())["state"] == "queued"
    assert (await q(1).counts.resync())["waiting"] == 2
    replay.done()
    for k in kits.values():
        await k.runtime.close()


@pytest.mark.parametrize("replay", ["session"], indirect=True)
async def test_sessions(replay: Replay) -> None:
    k1, k2, k3 = [await kit_for(replay, "player", p) for p in (1, 2, 3)]

    def s(k: GameKitClient) -> Any:
        return k.sessions.open("ranked-1-1")

    assert (await s(k1).watch())["phase"] == "lobby"
    await replay.server_step()  # the matchmaker reserves it
    await until("the reservation", lambda: len(s(k1).state.get()["reserved"]) == 2)
    assert await code(s(k3).join()) == "reserved"
    assert await s(k1).join() is True
    assert await s(k2).join() is True
    assert await s(k1).ready() is True
    assert await code(s(k2).start_game()) == "forbidden"
    assert await s(k2).ready(True) is True
    assert await s(k1).start_game() is True
    await until("playing", lambda: s(k1).state.get()["phase"] == "playing")
    assert s(k1).current_turn() == 1
    assert await s(k1).is_my_turn() is True
    assert await code(s(k2).act({"points": 1})) == "not_your_turn"
    assert await s(k1).act({"points": 3}) is True
    # The same move sent again (the reply was lost): the hub answers from its receipt.
    assert (
        await k1.runtime.call("session", "ranked-1-1", "act", {"points": 3}, action="resent")
        is True
    )
    assert await code(s(k2).act({"points": 9})) == "invalid_move"
    await replay.server_step()  # player 2's turn times out
    last = [p for st in replay.script["steps"] for p in st["published"]][-1]["value"]
    await until("the timeout", lambda: s(k1).state.position[1] == last["seq"])
    assert same(s(k1).state.get(), body_of(last))
    assert session_turn(s(k1).state.get()) == 1
    assert same(await s(k1).state.resync(), body_of(last))
    assert (await k1.runtime.call("session", "ranked-1-1", "state"))["phase"] == "playing"
    replay.done()
    for k in (k1, k2, k3):
        await k.runtime.close()


@pytest.mark.parametrize("replay", ["npc-mobs"], indirect=True)
async def test_npcs(replay: Replay) -> None:
    k1, k2 = await kit_for(replay, "player", 1), await kit_for(replay, "player", 2)
    assert k1.npcs.key_at(1, 2) == "4,4"
    area = k1.npcs.area("0,0")
    await area.watch()
    assert len(area.mobs()) == 6
    assert len(area.npcs()) == 2
    await replay.server_step()
    await until("the first deltas", lambda: area.members.position[1] == 8)
    await area.members.resync()
    assert await code(area.attack(1)) == "out_of_reach"
    assert (await area.talk(7))["name"] == "Warden"
    pet = await area.adopt("cat", "Mog")
    assert pet == 9
    assert await area.summon(pet) is True
    assert await code(k2.npcs.area("0,0").summon(pet)) == "forbidden"
    mine = await area.my_pets()
    assert (mine[0]["name"], mine[0]["bond"], mine[0]["mode"]) == ("Mog", 1, "follow")
    await replay.server_step()
    await until("the pet in the mirror", lambda: len(area.pets()) == 1)
    assert "bond" not in area.pets()[0].props  # the bond is never published
    replay.done()
    for k in (k1, k2):
        await k.runtime.close()


@pytest.mark.parametrize("replay", ["world-tick"], indirect=True)
async def test_the_blocking_kit_plays_the_same_script(replay: Replay) -> None:
    """The generated blocking client's kit, over ExecConnection's threads, against the same
    gateway: its push handlers run on the reader thread, so what they start runs beside it."""
    from crowdypy._sync.kit import GameKitClient as BlockingKit

    def play() -> dict[str, Any]:
        conn = ExecConnection.open(replay.url, "player-1", call_timeout=3.0)
        kit = BlockingKit("77", None, connection=conn, player_id=1)  # type: ignore[arg-type]
        kit.world.conditions.start()
        kit.world.nodes.start()
        out: dict[str, Any] = {
            "nodes": len(kit.world.nodes.list("Node")),
            "view": len(kit.world.state()["nodes"]),
            "harvest": kit.world.harvest(0),
        }
        end = time.monotonic() + 3
        while time.monotonic() < end:
            member = kit.world.nodes.member(0)
            if member is not None and member.props["amount"] == 1:
                break
            time.sleep(0.005)
        member = kit.world.nodes.member(0)
        out["mirrored"] = member.props["amount"] if member else None
        kit.runtime.close()
        conn.close()
        return out

    assert await asyncio.to_thread(play) == {"nodes": 2, "view": 2, "harvest": 1, "mirrored": 1}
    assert replay.refused == []
    assert replay.at == 4  # the two snapshots, state, the harvest


# ---- the runtime ----


class Scripted:
    """A gateway a test scripts call by call: ``answer(name, args)`` gives (status, payload)."""

    def __init__(self, answer: Callable[[str, Any], tuple[int, bytes]]) -> None:
        self.answer = answer
        self.calls: list[tuple[str, Any]] = []
        self.peers: list[ServerConnection] = []
        self.subs: set[tuple[str, str, str]] = set()

    async def handle(self, ws: ServerConnection) -> None:
        self.peers.append(ws)
        async for message in ws:
            assert isinstance(message, bytes)
            f = read_frame(message)
            if f["kind"] == "ping":
                continue
            if f["kind"] in ("subscribe", "unsubscribe"):
                (self.subs.add if f["kind"] == "subscribe" else self.subs.discard)(
                    (f["node_type"], f["key"], f["name"])
                )
                await ws.send(reply(f["rid"], 0))
                continue
            args = msgspec.msgpack.decode(f["payload"]) if f["payload"] else None
            self.calls.append((f["name"], args))
            status, payload = self.answer(f["name"], args)
            await ws.send(reply(f["rid"], status, payload))

    async def push(self, node_type: str, key: str, topic: str, value: Any) -> None:
        for ws in self.peers:
            await ws.send(push(node_type, key, topic, msgspec.msgpack.encode(value)))


@pytest.fixture
async def scripted() -> AsyncIterator[Callable[[Callable[[str, Any], tuple[int, bytes]]], Any]]:
    servers: list[Any] = []

    async def make(answer: Callable[[str, Any], tuple[int, bytes]]) -> tuple[Scripted, KitRuntime]:
        gw = Scripted(answer)
        server = await serve(gw.handle, "127.0.0.1", 0).__aenter__()
        servers.append(server)
        port = next(iter(server.sockets)).getsockname()[1]
        conn = await AsyncExecConnection.open(
            f"ws://127.0.0.1:{port}", "player-1", call_timeout=3.0
        )
        return gw, KitRuntime("77", connection=conn, player_id=1, call_budget=1000)

    yield make
    for server in servers:
        server.close()
        await server.wait_closed()


async def test_resends_only_what_is_safe(scripted: Any) -> None:
    seen: dict[str, int] = {}

    def answer(name: str, args: Any) -> tuple[int, bytes]:
        seen[name] = seen.get(name, 0) + 1
        if name == "busy" and seen[name] == 1:
            return 2, b"rate limited: a player may make 120 calls per 10000 ms; retry in 30 ms"
        if name == "lost" and seen[name] <= 2:
            return 8, b"host went away"
        if name == "plain":
            return 8, b"host went away"
        return 0, msgspec.msgpack.encode(7)

    gw, rt = await scripted(answer)
    start = time.monotonic()
    assert await rt.call("arena", "m1", "busy") == 7
    assert time.monotonic() - start >= 0.025  # it held as told
    assert await rt.call("arena", "m1", "lost", {"x": 1}, action=True) == 7
    ids = [args["action_id"] for name, args in gw.calls if name == "lost"]
    assert len(ids) == 3
    assert len(set(ids)) == 1
    with pytest.raises(CrowdyExecError) as caught:
        await rt.call("arena", "m1", "plain")
    assert caught.value.status == "Unavailable"
    assert seen["plain"] == 1
    await rt.close()


async def test_pacing_and_the_subscription_cap(scripted: Any) -> None:
    _gw, rt = await scripted(lambda name, args: (0, msgspec.msgpack.encode(True)))
    paced = KitRuntime(
        "77", connection=await rt.connection(), player_id=1, call_budget=5, budget_window_ms=200
    )
    start = time.monotonic()
    await asyncio.gather(*(paced.call("arena", "m1", "ping") for _ in range(10)))
    assert time.monotonic() - start >= 0.15  # five at once, five as the budget refilled
    stops = [
        await rt.subscribe("t", str(i), "x", lambda p: None) for i in range(KIT_SUBSCRIPTIONS_MAX)
    ]
    assert rt.subscriptions == KIT_SUBSCRIPTIONS_MAX
    with pytest.raises(KitError) as caught:
        await rt.subscribe("t", "one-more", "x", lambda p: None)
    assert caught.value.code == "limit"
    again = await rt.subscribe("t", "0", "x", lambda p: None)
    assert rt.subscriptions == KIT_SUBSCRIPTIONS_MAX, (
        "a second handler on a held topic is not another"
    )
    await stops[0]()
    await again()
    assert rt.subscriptions == KIT_SUBSCRIPTIONS_MAX - 1
    await rt.close()


def kit_message(epoch: int, seq: int, kind: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"v": 1, "epoch": epoch, "seq": seq, "kind": kind, **body}


async def test_a_mirror_resyncs_on_a_gap_and_keeps_order(scripted: Any) -> None:
    snapshots = 0

    def answer(name: str, args: Any) -> tuple[int, bytes]:
        nonlocal snapshots
        if name == "snapshot":
            snapshots += 1
            hp = 5 if snapshots == 1 else 2
            body = {"containers": [{"id": 1, "type_name": "Mob", "props": {"hp": hp}}]}
            return 0, msgspec.msgpack.encode(
                kit_message(
                    1 if snapshots == 1 else 2, 3 if snapshots == 1 else 10, "snapshot", body
                )
            )
        return 0, b""

    gw, rt = await scripted(answer)
    area = CollectionMirror(rt, "npcs", "0,0", "area")
    await area.start()
    assert area.member(1).props["hp"] == 5  # type: ignore[union-attr]
    await gw.push(
        "npcs",
        "0,0",
        "area",
        kit_message(1, 4, "delta", {"id": 2, "type_name": "Pet", "props": {"name": "Mog"}}),
    )
    await until("the delta", lambda: len(area.list("Pet")) == 1)
    await gw.push("npcs", "0,0", "area", kit_message(1, 9, "delta", {"id": 2, "removed": True}))
    await until("the resync", lambda: snapshots == 2 and area.position == (2, 10))
    assert area.member(1).props["hp"] == 2  # type: ignore[union-attr]
    assert area.member(2) is None
    seen: list[str] = []
    area.on_event(lambda body, header: seen.append(body["type"]))
    await gw.push("npcs", "0,0", "area", kit_message(2, 11, "event", {"type": "roar"}))
    await until("the event", lambda: seen == ["roar"])
    await area.stop()
    await rt.close()


async def test_private_state_reads_again_only_for_its_player(scripted: Any) -> None:
    reads = 0

    def answer(name: str, args: Any) -> tuple[int, bytes]:
        nonlocal reads
        if name == "hand":
            reads += 1
            return 0, msgspec.msgpack.encode(["7H"] if reads == 1 else ["7H", "8S"])
        return 0, b""

    gw, rt = await scripted(answer)

    async def read() -> Any:
        return await rt.call("deck", "t1", "hand")

    hand = PrivateState(rt, node_type="deck", key="t1", topic="table", what="hand", read=read)
    assert await hand.start() == ["7H"]
    await gw.push("deck", "t1", "table", kit_message(1, 1, "changed", {"who": [2], "what": "hand"}))
    await gw.push("deck", "t1", "table", kit_message(1, 2, "changed", {"who": [1], "what": "pile"}))
    await asyncio.sleep(0.05)
    assert reads == 1
    await gw.push(
        "deck", "t1", "table", kit_message(1, 3, "changed", {"who": [1, 2], "what": "hand"})
    )
    await until("the second read", lambda: hand.get() == ["7H", "8S"])
    await hand.stop()
    await rt.close()


def test_manifest_problems_are_the_managers() -> None:
    ok = {
        "root": "world",
        "types": {
            "world": {"kind": "hub"},
            "session": {"kind": "hub", "parent": "world", "calls": ["world"]},
        },
    }
    assert kit_manifest_problems(ok) == []
    assert kit_manifest_problems({"root": "world", "types": {}})
    assert kit_manifest_problems({"root": "world", "types": {"world": {"kind": "spoke"}}})
    many = {
        "root": "world",
        "types": {
            "world": {"kind": "hub"},
            "a b": {"kind": "hub"},
            "x": {
                "kind": "hub",
                "parent": "nope",
                "calls": ["ghost"],
                "replicas": 2,
                "scopes": ["root.everything"],
            },
        },
    }
    assert len(kit_manifest_problems(many)) == 6


async def test_emotes_are_the_registrys_bodies() -> None:
    sent: list[tuple[Any, ...]] = []

    class Udp:
        async def send_client_event(
            self, chunk: Any, uuid: str, event_type: int, state: bytes, *, distance: int
        ) -> int:
            sent.append((tuple(chunk), uuid, event_type, state, distance))
            return 1

    kit = GameKitClient("77", None, udp=Udp())  # type: ignore[arg-type]
    assert await kit.emotes.send("u" * 32, (1, 0, 2), "wave", target_uuid="t" * 32) is True
    chunk, _uuid, event_type, state, distance = sent[0]
    assert (chunk, event_type, distance) == ((1, 0, 2), EVENT_EMOTE, 2)
    assert json.loads(state) == {"emote": "wave", "targetUuid": "t" * 32}
    parsed = kit.emotes.parse(event_type, state)
    assert parsed is not None
    assert parsed.emote == "wave"
    assert kit.emotes.parse(90, state) is None
