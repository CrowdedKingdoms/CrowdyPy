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


def starter(name: str) -> dict[str, Any]:
    """``crate``'s script, or ``crate:node_type``'s when the crate serves two types."""
    crate, _, node_type = name.partition(":")
    return next(
        s
        for s in RECORD["starters"]
        if s["crate"] == crate and (not node_type or s["node_type"] == node_type)
    )


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


# ---- the genre starters ----


async def kits(replay: Replay, *callers: tuple[str, int]) -> list[GameKitClient]:
    return [await kit_for(replay, kind, ident) for kind, ident in callers]


async def close_all(*ks: GameKitClient) -> None:
    for k in ks:
        await k.runtime.close()


def step_calling(script: dict[str, Any], method: str) -> dict[str, Any]:
    return next(s for s in script["steps"] if s.get("call") == method)


@pytest.mark.parametrize("replay", ["leaderboard"], indirect=True)
async def test_leaderboards(replay: Replay) -> None:
    p1, p2, dev = await kits(replay, ("player", 1), ("player", 2), ("developer", 9))
    board = p1.leaderboards.board("weekly")
    assert (await board.watch())["season"] == 1
    assert await code(board.submit(1, 9)) == "forbidden"
    for _ in range(4):  # the hubs that decided the scores submit them; the board publishes
        await replay.server_step()
    await until("the top", lambda: len(board.view.get()["top"]) == 2)
    assert [r["player"] for r in await board.top(limit=10)] == [2, 1]
    assert await board.me() == {"player": 1, "rank": 2, "score": 9}
    assert len(await p2.leaderboards.board("weekly").around(radius=1)) == 2
    assert (await board.season())["entries"] == 2
    assert await code(board.roll()) == "forbidden"
    assert await dev.leaderboards.board("weekly").roll() == 2
    assert (await board.view.resync())["season"] == 2
    replay.done()
    await close_all(p1, p2, dev)


@pytest.mark.parametrize("replay", ["deck"], indirect=True)
async def test_tables(replay: Replay) -> None:
    p1, p2 = await kits(replay, ("player", 1), ("player", 2))
    t1, t2 = p1.tables.table("t1"), p2.tables.table("t1")
    assert (await t1.table.start())["phase"] == "lobby"
    assert await t1.join() is True
    assert await t2.join() is True
    assert await code(t2.start()) == "forbidden"
    assert await t1.start() is True
    view = await p1.runtime.call("deck", "t1", "state")
    assert view["phase"] == "playing"
    turn = view["turn"]
    mover, waiter = (p1, p2) if turn == 1 else (p2, p1)
    moving, waiting = (t1, t2) if turn == 1 else (t2, t1)
    assert len((await mover.runtime.call("deck", "t1", "hand"))["cards"]) == 5
    assert len((await waiter.runtime.call("deck", "t1", "hand"))["cards"]) == 5
    assert await code(waiting.draw()) == "not_your_turn"
    play = next(
        s
        for s in replay.script["steps"]
        if s.get("call") == "play" or (s.get("call") == "draw" and s["ok"])
    )
    if play["call"] == "play":
        args = without_action_id(play["args"])
        assert await moving.play(args["card"], args.get("suit")) is True
        assert await mover.runtime.call("deck", "t1", "play", args, action="resent") is True
    else:
        assert await moving.draw() is True
        assert await moving.pass_() is True
    assert "hand_counts" in await t1.table.resync()
    replay.done()
    await close_all(p1, p2)


@pytest.mark.parametrize("replay", ["tactics"], indirect=True)
async def test_tactics(replay: Replay) -> None:
    p1, p2 = await kits(replay, ("player", 1), ("player", 2))
    b1, b2 = p1.tactics.battle("b1"), p2.tactics.battle("b1")
    assert (await b1.watch())["phase"] == "lobby"
    assert await b1.join() is True
    assert await b2.join() is True
    assert await code(b2.start()) == "forbidden"
    assert await b1.start() is True
    await until("the players' phase", lambda: b1.battle.get()["phase"] == "players")
    assert len((await p1.runtime.call("tactics", "b1", "state"))["units"]) > 0
    [mine, *_] = await b1.my_units()
    move = step_calling(replay.script, "move")["args"]
    assert (move["x"], move["z"]) == (mine["x"] + 1, mine["z"])
    assert await b1.move(move["x"], move["z"]) is True
    again = {"x": move["x"], "z": move["z"]}
    assert await p1.runtime.call("tactics", "b1", "move", again, action="resent") is True
    target = step_calling(replay.script, "attack")["args"]["target"]
    assert await code(b1.attack(target)) == "out_of_reach"
    assert await b1.end_turn() is True
    assert await b2.end_turn() is True
    assert len((await b1.battle.resync())["log"]) > 0
    replay.done()
    await close_all(p1, p2)


@pytest.mark.parametrize("replay", ["director"], indirect=True)
async def test_encounters(replay: Replay) -> None:
    p1, p2 = await kits(replay, ("player", 1), ("player", 2))
    e = p1.encounters.encounter("gate")
    events: list[str] = []
    e.view.on_event(lambda body, _h: events.append(body["type"]))
    assert (await e.watch())["phase"] == "lobby"
    assert await e.join() is True
    assert await code(p2.encounters.encounter("gate").start()) == "forbidden"
    assert await e.start() is True
    assert await e.build(5, 3) == 20
    assert (
        await p1.runtime.call("director", "gate", "build", {"x": 5, "z": 3}, action="resent") == 20
    )
    assert await code(e.build(99, 0)) == "bad_request"
    await replay.server_step()  # the first wave
    await until("the wave", lambda: "wave_started" in events)
    enemy = (await p1.runtime.call("director", "gate", "state"))["enemies"][0]["id"]
    assert await e.hit(enemy) == 15
    assert await code(e.hit(enemy)) == "cooldown"
    assert (await e.view.resync())["enemies"][0]["hp"] == 15
    replay.done()
    await close_all(p1, p2)


@pytest.mark.parametrize("replay", ["minigame"], indirect=True)
async def test_minigames(replay: Replay) -> None:
    p1, p2, p3 = await kits(replay, ("player", 1), ("player", 2), ("player", 3))
    d1, d2 = p1.minigames.duel("t1"), p2.minigames.duel("t1")
    assert (await d1.watch())["phase"] == "waiting"
    assert await d1.join() is False
    assert await d2.join() is True  # the second seat starts the first round
    assert await code(p3.minigames.duel("t1").join()) == "full"
    assert await d1.throw("rock") is False
    assert await code(d1.throw("paper")) == "conflict"
    assert await code(d2.throw("lizard")) == "bad_request"
    assert await d2.throw("scissors") is True
    await until("the reveal", lambda: (d1.view.get()["last"] or {}).get("round") == 1)
    assert d1.view.get()["last"]["hands"] == ["rock", "scissors"]
    assert await d1.throw("paper") is False
    assert (
        await p1.runtime.call("minigame", "t1", "throw", {"hand": "paper"}, action="resent")
        is False
    )
    assert await d2.throw("rock") is True
    await until("the winner", lambda: d1.view.get()["winner"] == 1)
    assert await d2.rematch() is True
    assert (await d1.view.resync())["phase"] == "playing"
    replay.done()
    await close_all(p1, p2, p3)


@pytest.mark.parametrize("replay", ["instance"], indirect=True)
async def test_instances(replay: Replay) -> None:
    p1, p2, p3 = await kits(replay, ("player", 1), ("player", 2), ("player", 3))
    r1, r2 = p1.instances.run("run-a"), p2.instances.run("run-a")
    assert (await r1.watch())["phase"] == "unopened"
    assert await r1.open(seed=7) == 7
    assert await code(r2.join()) == "forbidden"
    assert await r1.invite(2) is True
    assert await r2.join() is True
    assert await code(p3.instances.run("run-a").enter()) == "not_member"
    first = await r2.enter()
    assert "won" in first
    assert await p2.runtime.call("instance", "run-a", "enter", None, action="resent") == first
    assert "the boss: won" in await r1.enter()
    assert await code(r1.enter()) == "finished"
    assert (await r1.view.resync())["phase"] == "cleared"
    replay.done()
    await close_all(p1, p2, p3)


@pytest.mark.parametrize("replay", ["arena"], indirect=True)
async def test_arenas(replay: Replay) -> None:
    p1, p2, p3 = await kits(replay, ("player", 1), ("player", 2), ("player", 3))
    a1, a2 = p1.arenas.arena("pit"), p2.arenas.arena("pit")
    events: list[str] = []
    a1.view.on_event(lambda body, _h: events.append(body["type"]))
    assert (await a1.watch())["phase"] == "waiting"
    assert await a1.join("warrior") is True
    assert await a2.join("mage") is True
    assert await code(p3.arenas.arena("pit").join("bard")) == "bad_request"
    assert await code(a2.start()) == "forbidden"
    assert await a1.start() is True
    assert await a1.cast("strike", 11, 10) is True
    again = {"ability": "strike", "x": 11, "z": 10}
    assert await p1.runtime.call("arena", "pit", "cast", again, action="resent") is True
    assert await code(a1.cast("strike", 11, 10)) == "cooldown"
    assert await a2.cast("bolt", 10, 10) is False  # a projectile lands later
    await replay.server_step()
    await until("the impact", lambda: "impact" in events)
    assert await a2.move(30, 10) == [11, 10]  # a jump is refused
    assert len((await a1.view.resync())["fighters"]) == 2
    replay.done()
    await close_all(p1, p2, p3)


@pytest.mark.parametrize("replay", ["racing:race"], indirect=True)
async def test_race(replay: Replay) -> None:
    p1, p2 = await kits(replay, ("player", 1), ("player", 2))
    race = p1.racing.race("r1")
    events: list[str] = []
    race.view.on_event(
        lambda body, _h: events.append(body["stage"] if body["type"] == "timing" else body["type"])
    )
    assert (await race.watch())["phase"] == "lobby"
    assert await race.join() is True
    assert await p2.racing.race("r1").join() is True
    assert await code(p2.racing.race("r1").start()) == "forbidden"
    assert await race.start() is True
    await replay.server_step()  # the countdown ends
    await until("the start", lambda: "started" in events)
    for x in (5, 10, 5, 0):
        await replay.server_step()
        assert await race.pos(x, 0) == [x, 0]
    await until("the finish", lambda: "finished" in events)
    ghost = await p2.racing.race("r1").ghost()
    assert ghost is not None
    assert ghost["player"] == 1
    assert len(ghost["frames"]) >= 4
    await replay.server_step()  # the rest's time runs out
    done = await race.view.resync()
    assert done["phase"] == "finished"
    assert next(r for r in done["racers"] if r["player"] == 2)["did_not_finish"] is True
    replay.done()
    await close_all(p1, p2)


@pytest.mark.parametrize("replay", ["racing:ball"], indirect=True)
async def test_ball(replay: Replay) -> None:
    p1, p2 = await kits(replay, ("player", 1), ("player", 2))
    m1, m2 = p1.racing.ball("m1"), p2.racing.ball("m1")
    events: list[str] = []
    m1.view.on_event(lambda body, _h: events.append(body["type"]))
    assert (await m1.watch())["phase"] == "lobby"
    assert await m1.join("red") is True
    assert await m2.join("blue") is True
    assert await m1.start() is True
    for x in (22, 29.5):
        await replay.server_step()
        assert await m1.pos(x, 20) == [x, 20]
    assert await code(m2.claim()) == "out_of_reach"
    assert await m1.claim() is True
    assert await m1.shoot(60, 20) is True
    await replay.server_step()
    await until("the goal", lambda: "goal" in events)
    assert (await m1.view.resync())["score"] == [1, 0]
    replay.done()
    await close_all(p1, p2)


def point(view: dict[str, Any], ident: str) -> dict[str, Any]:
    return next(p for p in view["points"] if p["id"] == ident)


@pytest.mark.parametrize("replay", ["territory:territory"], indirect=True)
async def test_territory(replay: Replay) -> None:
    p1, p2, dev = await kits(replay, ("player", 1), ("player", 2), ("developer", 9))
    m = p1.territory.map("valley")
    events: list[str] = []
    m.view.on_event(lambda body, _h: events.append(body["type"]))
    assert (await m.watch())["phase"] == "open"
    assert await m.join("blue") is True
    assert await code(m.join("red")) == "conflict"
    assert await code(p2.territory.map("valley").join("green")) == "bad_request"
    await replay.server_step()  # two scans
    await until("the capture", lambda: "captured" in events)
    state = await p1.runtime.call("territory", "valley", "state")
    assert point(state, "tower")["owner"] == "blue"
    assert await code(m.reset()) == "forbidden"
    assert await dev.territory.map("valley").reset() is True
    assert point(await m.view.resync(), "tower")["owner"] is None
    replay.done()
    await close_all(p1, p2, dev)


@pytest.mark.parametrize("replay", ["territory:zone"], indirect=True)
async def test_zone(replay: Replay) -> None:
    p1, p2, p3 = await kits(replay, ("player", 1), ("player", 2), ("player", 3))
    z1 = p1.territory.zone("match-1")
    assert (await z1.watch())["phase"] == "lobby"
    assert await z1.join() is True
    assert await p2.territory.zone("match-1").join() is True
    assert await code(p2.territory.zone("match-1").start()) == "forbidden"
    assert await z1.start() is True
    await replay.server_step()  # a scan
    await until("the scan", lambda: any(c["outside"] for c in z1.view.get()["players"]))
    state = await p1.runtime.call("zone", "match-1", "state")
    assert [c["outside"] for c in state["players"]] == [False, True]
    assert await code(p3.territory.zone("match-1").join()) == "started"
    assert (await z1.view.resync())["alive"] == 2
    replay.done()
    await close_all(p1, p2, p3)


# ---- the economy ----


@pytest.mark.parametrize("replay", ["player"], indirect=True)
async def test_player(replay: Replay) -> None:
    p1, p2, dev = await kits(replay, ("player", 1), ("player", 2), ("developer", 9))
    events: list[str] = []
    p1.player.on_event(lambda e: events.append(e["type"]))
    await p1.player.watch("profile")
    assert p1.progression.profile.get()["level"] == 1
    me = await p1.player.me()
    assert me["wallet"]["balances"]["gold"] == 100
    assert next(q for q in me["progress"]["quests"] if q["id"] == "welcome")["state"] == "active"
    assert await code(p2.runtime.call("player", "1", "me")) == "forbidden"
    assert (await p2.progression.profile_of(1))["player"] == 1
    assert any(r["id"] == "plank" for r in await p1.crafting.recipes())
    deposit = {"ref": "seed-wood", "bundle": {"items": {"wood": 4}}}
    assert await dev.runtime.call("player", "1", "deposit", deposit) is True
    crafted = await p1.crafting.craft("plank", 2)
    assert crafted["made"]["items"] == {"plank": 2}
    assert crafted["earned"]["achievements"] == ["first_craft"]
    again = {"recipe": "plank", "times": 2}
    assert same(await p1.runtime.call("player", "1", "craft", again, action="resent"), crafted)
    assert await code(p1.crafting.craft("plank")) == "insufficient"
    assert await code(p1.quests.step("crafted")) == "forbidden"
    assert (await p1.quests.step("tutorial:inventory"))["quests"] == ["welcome"]
    assert (await p1.quests.claim("welcome"))["currency"] == {"gold": 25}
    assert await code(p1.quests.claim("welcome")) == "not_ready"
    assert (await p1.loot.open("chest"))["roll"]["count"] > 0
    await replay.server_step()  # half an hour away
    assert (await p1.production.collect())["items"] == {"ore": 30, "wood": 60}
    for _ in range(4):  # a market holds, settles, releases and sells
        await replay.server_step()
    grant = {"ref": "nope", "bundle": {"currency": {"gold": 1}}}
    assert await code(p1.runtime.call("player", "1", "grant", grant)) == "forbidden"
    await replay.server_step()  # a run's reward: three levels
    await until("the level events", lambda: "level" in events)
    assert (await p1.inventory.take_mail()) == {"items": {}, "currency": {}}
    assert (await p1.wallet.watch())["balances"]["gold"] == 80
    assert p1.wallet.balance("gems") == 10
    await p1.inventory.watch()
    assert p1.inventory.count("plank") == 2
    assert (await p1.progression.watch())["level"] == 4
    assert (
        next(a for a in p1.achievements.list() if a["id"] == "first_craft")["unlocked_at"]
        is not None
    )
    assert next(q for q in p1.quests.list() if q["id"] == "welcome")["state"] == "done"
    await replay.server_step()
    await until(
        "the profile at level 4", lambda: (p1.progression.profile.get() or {}).get("level") == 4
    )
    topic = {"topic": "player"}
    assert (await p1.runtime.call("player", "1", "snapshot", topic))["level"] == 4
    replay.done()
    await close_all(p1, p2, dev)


@pytest.mark.parametrize("replay", ["market"], indirect=True)
async def test_market(replay: Replay) -> None:
    p1, p2, p3 = await kits(replay, ("player", 1), ("player", 2), ("player", 3))
    shop = p1.shops.at("town")
    assert (await shop.watch())["currency"] == "gold"
    assert (await shop.vendors())[0]["id"] == "smith"
    assert await shop.buy("smith", "sword") == {"items": {"sword": 1}, "currency": {}}
    sword = {"vendor": "smith", "item": "sword"}
    assert await p1.runtime.call("market", "town", "buy", sword, action="resent") == {
        "items": {"sword": 1},
        "currency": {},
    }
    assert await code(shop.buy("smith", "sword")) == "limit"
    assert await code(p3.shops.at("town").buy("smith", "potion", 2)) == "insufficient"
    assert await shop.sell("smith", "sword") == {"items": {}, "currency": {"gold": 20}}
    sell = await p2.market.at("town").order(item="ore", side="sell", price=4, count=10)
    assert sell["open"] == 10
    buy = await p1.market.at("town").order(item="ore", side="buy", price=5, count=6)
    assert (buy["fills"], buy["open"]) == ([[4, 6]], 0)
    assert (await p1.market.at("town").book("ore"))["asks"] == [[4, 4]]
    [mine] = await p2.runtime.call("market", "town", "orders")
    assert mine["open"] == 4
    assert await p2.market.at("town").cancel(mine["id"]) is True
    desk = p1.trades.at("town")
    trade = await desk.open(2)
    assert await desk.offer(trade, {"currency": {"gold": 30}}) is True
    assert await p2.trades.at("town").offer(trade, {"items": {"ore": 2}}) is True
    assert await desk.accept(trade) == "open"
    assert await p2.trades.at("town").accept(trade) == "settling"
    assert await p1.runtime.call("market", "town", "trades") == []
    assert await code(p3.trades.at("town").decline(trade)) == "forbidden"
    await replay.server_step()
    assert (await shop.view.resync())["open_trades"] == 0
    replay.done()
    await close_all(p1, p2, p3)


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
        if name == "buy" and seen[name] == 1:
            return 1, b"kit:unavailable: the player's hub did not answer; send it again"
        if name == "buy-plain":
            return 1, b"kit:unavailable: no answer"
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
    # A hub that could not hear from another keeps no receipt: sent again with the action id.
    assert await rt.call("market", "town", "buy", {"item": "potion"}, action=True) == 7
    buys = [args["action_id"] for name, args in gw.calls if name == "buy"]
    assert len(buys) == 2
    assert len(set(buys)) == 1
    assert await code(rt.call("market", "town", "buy-plain")) == "unavailable"
    assert seen["buy-plain"] == 1, "without an action id the caller decides"
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
