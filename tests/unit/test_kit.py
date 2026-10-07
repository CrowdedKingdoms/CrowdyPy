"""The Game Kit: engine wire formats, optimistic actions and the social helpers."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from crowdypy import kit
from crowdypy.kit import (
    EVENT_SCORE,
    EVENT_WEATHER,
    FLAG_GROUNDED,
    FLAG_MOB,
    POSE_BYTES,
    EnginePose,
    SocialKit,
    decode_engine_pose,
    encode_engine_pose,
    engine_lanes,
    parse_contact_damage,
    parse_engine_event,
    parse_score_event,
    parse_weather_event,
    run_optimistic_action,
)


def test_the_engine_pose_is_48_bytes_and_round_trips() -> None:
    pose = EnginePose(
        1.5, -2.0, 3.25, 0.5, -0.25, 0.1, 0.0, -9.8, FLAG_GROUNDED | FLAG_MOB, 7, 1.7e12
    )
    data = encode_engine_pose(pose)
    assert len(data) == POSE_BYTES
    assert data[32] == FLAG_GROUNDED | FLAG_MOB
    back = decode_engine_pose(data)
    assert back is not None
    assert (back.x, back.z, back.flags, back.held, back.updated_at_ms) == (1.5, 3.25, 3, 7, 1.7e12)
    assert math.isclose(back.vel_z, -9.8, rel_tol=1e-6)
    assert decode_engine_pose(encode_engine_pose(EnginePose(suffix="crate-7"))).suffix == "crate-7"  # type: ignore[union-attr]
    assert decode_engine_pose(data[:40]) is None
    assert decode_engine_pose(encode_engine_pose(EnginePose(x=float("nan")))) is None
    assert kit.engine_pose_codec.decode(b"short") == EnginePose()


def test_engine_lanes_are_native_filters_on_the_flags_byte() -> None:
    lanes = engine_lanes()
    mob = encode_engine_pose(EnginePose(flags=FLAG_MOB))
    player = encode_engine_pose(EnginePose(flags=FLAG_GROUNDED))

    def accepts(spec: dict[str, int], state: bytes) -> bool:
        return (state[spec["tag_offset"]] & spec["tag_mask"]) == spec["tag_value"]

    assert accepts(lanes["mobs"], mob)
    assert not accepts(lanes["mobs"], player)
    assert accepts(lanes["players"], player)
    assert not accepts(lanes["players"], mob)


def event(kind: int, body: dict[str, Any]) -> bytes:
    return bytes([kind & 0xFF, kind >> 8]) + json.dumps(body).encode()


def test_engine_events_parse_by_type() -> None:
    weather = parse_weather_event(
        event(EVENT_WEATHER, {"weather": "rain", "sinceMs": 5, "isNight": True})
    )
    assert weather is not None
    assert (weather.weather, weather.since_ms, weather.body["isNight"]) == ("rain", 5.0, True)
    score = parse_score_event(
        event(
            EVENT_SCORE, {"standings": [{"actorId": "a", "score": 3, "rank": 1}], "winnerId": "a"}
        )
    )
    assert score is not None
    assert (score.standings, score.winner_id) == ([("a", 3.0, 1.0)], "a")
    assert parse_contact_damage(event(EVENT_WEATHER, {})) is None  # another event type
    assert parse_engine_event(b"\x5a\x00not json") is None
    assert parse_engine_event(b"\x5a\x00") == (90, {})


async def test_optimistic_actions_roll_back_a_refusal_and_an_error() -> None:
    state = {"gold": 10}

    def spend() -> Any:
        state["gold"] -= 3
        return lambda: state.__setitem__("gold", state["gold"] + 3)

    async def accept(action_id: str) -> dict[str, Any]:
        return {"success": True, "actionId": action_id}

    async def refuse(action_id: str) -> dict[str, Any]:
        return {"success": False, "reason": "not enough gold"}

    async def fail(action_id: str) -> dict[str, Any]:
        raise RuntimeError("network down")

    ok = await run_optimistic_action(spend, accept, action_id="a1")
    assert (ok.ok, ok.action_id, state["gold"]) == (True, "a1", 7)
    refused = await run_optimistic_action(spend, refuse)
    assert (refused.ok, refused.error_message, state["gold"]) == (False, "not enough gold", 7)
    failed = await run_optimistic_action(spend, fail)
    assert (failed.ok, failed.error_message, state["gold"]) == (False, "network down", 7)


class Domain:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.groups: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        async def call(*args: Any) -> Any:
            self.calls.append((name, args))
            if name == "create":
                group = {"groupId": str(len(self.groups) + 1), "name": args[0]["name"]}
                self.groups.append(group)
                return group
            if name == "list":
                return self.groups
            return {"ok": True}

        return call


async def test_a_party_is_a_team_and_a_channel_of_one_name() -> None:
    teams, channels, game_apps = Domain(), Domain(), Domain()
    social = SocialKit("7", teams, channels, None, game_apps, actor_uuid="a" * 32)  # type: ignore[arg-type]
    party = await social.party.create("raid")
    assert party.name == "party:raid"
    assert teams.calls[0][1][0]["membershipPolicy"] == "invite"
    assert await social.party.find("raid") == party
    await social.party.invite(party, 42)
    assert ("add_member", (party.team_id, 42)) in teams.calls
    assert ("add_member", (party.channel_id, 42)) in channels.calls
    guild = await social.guild.create("knights")
    await social.guild.claim_territory(guild, 9)
    grant = game_apps.calls[0][1][0]
    assert (grant["groupId"], grant["permissionKeys"]) == (
        guild.team_id,
        ["access", "update_voxel_data"],
    )
    with pytest.raises(Exception, match="udp"):
        await social.chat.send("1", "hi")
