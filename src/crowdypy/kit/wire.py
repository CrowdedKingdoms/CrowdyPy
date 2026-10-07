"""The engine wire formats the Game Kit shares with CrowdyJS: a 48-byte actor pose and the
JSON engine events (``[u16 eventType LE][UTF-8 JSON]``)."""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "EVENT_ABILITY",
    "EVENT_CONTACT_DAMAGE",
    "EVENT_CONTROL_POINT",
    "EVENT_MOVEMENT_VIOLATION",
    "EVENT_PROPOSAL",
    "EVENT_RACE_TIMING",
    "EVENT_SCORE",
    "EVENT_TURN",
    "EVENT_WEATHER",
    "EVENT_ZONE_CHANGE",
    "FLAG_GROUNDED",
    "FLAG_MOB",
    "FLAG_NPC",
    "FLAG_RESERVED3",
    "POSE_BYTES",
    "AbilityEvent",
    "ContactDamageEvent",
    "ControlPointEvent",
    "EnginePose",
    "MovementViolationEvent",
    "ProposalEvent",
    "RaceTimingEvent",
    "ScoreEvent",
    "TurnEvent",
    "WeatherEvent",
    "ZoneChangeEvent",
    "decode_engine_pose",
    "encode_engine_pose",
    "engine_lanes",
    "engine_pose_codec",
    "parse_ability_event",
    "parse_contact_damage",
    "parse_control_point_event",
    "parse_engine_event",
    "parse_movement_violation",
    "parse_proposal_event",
    "parse_race_timing_event",
    "parse_score_event",
    "parse_turn_event",
    "parse_weather_event",
    "parse_zone_change_event",
    "pose_suffix",
]

POSE_BYTES = 48
FLAG_GROUNDED = 0b0001
FLAG_MOB = 0b0010
FLAG_NPC = 0b0100
FLAG_RESERVED3 = 0b1000

#: x y z yaw pitch velX velY velZ (f32), flags held (u8), 2 pad, updatedAtMs (f64), 4 pad.
_POSE = struct.Struct("<8fBB2xd4x")


@dataclass
class EnginePose:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    vel_x: float = 0.0
    vel_y: float = 0.0
    vel_z: float = 0.0
    flags: int = 0
    held: int = 0
    updated_at_ms: float = 0.0
    #: UTF-8 text after the 48-byte pose (a container id), if any.
    suffix: str | None = None


def encode_engine_pose(pose: EnginePose) -> bytes:
    body = _POSE.pack(
        pose.x, pose.y, pose.z, pose.yaw, pose.pitch, pose.vel_x, pose.vel_y, pose.vel_z,
        pose.flags, pose.held, pose.updated_at_ms,
    )  # fmt: skip
    return body + pose.suffix.encode("utf-8") if pose.suffix is not None else body


def decode_engine_pose(data: bytes) -> EnginePose | None:
    """``None`` when the payload is too short or its position is not finite."""
    if len(data) < POSE_BYTES:
        return None
    x, y, z, yaw, pitch, vel_x, vel_y, vel_z, flags, held, updated = _POSE.unpack_from(data)
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
        return None
    return EnginePose(
        x, y, z, yaw, pitch, vel_x, vel_y, vel_z, flags, held, updated, pose_suffix(data)
    )


def pose_suffix(data: bytes) -> str | None:
    if len(data) <= POSE_BYTES:
        return None
    text = bytes(data[POSE_BYTES:]).decode("utf-8", errors="replace").strip()
    return text or None


class _EnginePoseCodec:
    def encode(self, value: EnginePose) -> bytes:
        return encode_engine_pose(value)

    def decode(self, data: bytes) -> EnginePose:
        return decode_engine_pose(data) or EnginePose()


#: A state codec for the World Stores (``create_world_session(..., actor_codec=...)``).
engine_pose_codec = _EnginePoseCodec()


def engine_lanes() -> dict[str, dict[str, int]]:
    """The engine's remote-actor lanes, as options for ``session.actors.lane(name, **spec)``:
    native filters on the pose's flags byte."""
    return {
        "mobs": {"tag_offset": 32, "tag_mask": FLAG_MOB, "tag_value": FLAG_MOB},
        "npcs": {"tag_offset": 32, "tag_mask": FLAG_NPC, "tag_value": FLAG_NPC},
        "players": {"tag_offset": 32, "tag_mask": FLAG_MOB | FLAG_NPC, "tag_value": 0},
    }


EVENT_CONTACT_DAMAGE = 77
EVENT_WEATHER = 90
EVENT_TURN = 91
EVENT_SCORE = 92
EVENT_PROPOSAL = 93
EVENT_ABILITY = 94
EVENT_MOVEMENT_VIOLATION = 95
EVENT_CONTROL_POINT = 96
EVENT_RACE_TIMING = 97
EVENT_ZONE_CHANGE = 98


def parse_engine_event(data: bytes) -> tuple[int, dict[str, Any]] | None:
    """``(event_type, body)``, or ``None`` when the bytes are not an engine event."""
    if len(data) < 2:
        return None
    event_type = data[0] | (data[1] << 8)
    body: dict[str, Any] = {}
    if len(data) > 2:
        try:
            parsed = json.loads(bytes(data[2:]).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        body = parsed
    return event_type, body


def _body(data: bytes, event_type: int) -> dict[str, Any] | None:
    parsed = parse_engine_event(data)
    return parsed[1] if parsed is not None and parsed[0] == event_type else None


def _text(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    return "" if value is None else str(value)


def _number(body: dict[str, Any], key: str) -> float:
    value = body.get(key)
    return float(value) if value is not None else 0.0


def _optional_text(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    return None if value is None else str(value)


@dataclass
class ContactDamageEvent:
    target_uuid: str
    damage: float
    mob_id: str
    mob_name: str


def parse_contact_damage(data: bytes) -> ContactDamageEvent | None:
    body = _body(data, EVENT_CONTACT_DAMAGE)
    if body is None:
        return None
    return ContactDamageEvent(
        _text(body, "targetUuid"),
        _number(body, "damage"),
        _text(body, "mobId"),
        _text(body, "mobName"),
    )


@dataclass
class WeatherEvent:
    weather: str
    since_ms: float
    until_ms: float
    body: dict[str, Any] = field(default_factory=dict)


def parse_weather_event(data: bytes) -> WeatherEvent | None:
    body = _body(data, EVENT_WEATHER)
    if body is None:
        return None
    return WeatherEvent(
        _text(body, "weather"), _number(body, "sinceMs"), _number(body, "untilMs"), body
    )


@dataclass
class TurnEvent:
    actor_id: str
    round: float
    turn_in_round: float
    body: dict[str, Any] = field(default_factory=dict)


def parse_turn_event(data: bytes) -> TurnEvent | None:
    body = _body(data, EVENT_TURN)
    if body is None:
        return None
    return TurnEvent(
        _text(body, "actorId"), _number(body, "round"), _number(body, "turnInRound"), body
    )


@dataclass
class ScoreEvent:
    #: ``(actor_id, score, rank)`` per actor, when present.
    standings: list[tuple[str, float, float]]
    winner_id: str | None
    body: dict[str, Any] = field(default_factory=dict)


def parse_score_event(data: bytes) -> ScoreEvent | None:
    body = _body(data, EVENT_SCORE)
    if body is None:
        return None
    rows = body.get("standings")
    standings = (
        [
            (_text(s, "actorId"), _number(s, "score"), _number(s, "rank"))
            for s in rows
            if isinstance(s, dict)
        ]
        if isinstance(rows, list)
        else []
    )
    return ScoreEvent(standings, _optional_text(body, "winnerId"), body)


@dataclass
class ProposalEvent:
    proposal_id: str
    mode: str
    players: list[str]
    body: dict[str, Any] = field(default_factory=dict)


def parse_proposal_event(data: bytes) -> ProposalEvent | None:
    body = _body(data, EVENT_PROPOSAL)
    if body is None:
        return None
    players = body.get("players")
    return ProposalEvent(
        _text(body, "proposalId"),
        _text(body, "mode"),
        [str(p) for p in players] if isinstance(players, list) else [],
        body,
    )


@dataclass
class AbilityEvent:
    #: ``cast`` or ``impact``.
    kind: str
    ability_id: str
    caster_id: str
    victim_id: str | None
    damage: float
    body: dict[str, Any] = field(default_factory=dict)


def parse_ability_event(data: bytes) -> AbilityEvent | None:
    body = _body(data, EVENT_ABILITY)
    if body is None:
        return None
    return AbilityEvent(
        _text(body, "kind"), _text(body, "abilityId"), _text(body, "casterId"),
        _optional_text(body, "victimId"), _number(body, "damage"), body,
    )  # fmt: skip


@dataclass
class MovementViolationEvent:
    #: ``speed``, ``teleport`` or ``bounds``.
    kind: str
    user_id: str
    detail: str
    body: dict[str, Any] = field(default_factory=dict)


def parse_movement_violation(data: bytes) -> MovementViolationEvent | None:
    body = _body(data, EVENT_MOVEMENT_VIOLATION)
    if body is None:
        return None
    return MovementViolationEvent(
        _text(body, "kind"), _text(body, "userId"), _text(body, "detail"), body
    )


@dataclass
class ControlPointEvent:
    point_id: str
    owner: str
    previous_owner: str
    body: dict[str, Any] = field(default_factory=dict)


def parse_control_point_event(data: bytes) -> ControlPointEvent | None:
    body = _body(data, EVENT_CONTROL_POINT)
    if body is None:
        return None
    return ControlPointEvent(
        _text(body, "pointId"), _text(body, "owner"), _text(body, "previousOwner"), body
    )


@dataclass
class RaceTimingEvent:
    #: ``started``, ``checkpoint``, ``lap`` or ``finished``.
    kind: str
    course_id: str
    user_id: str
    body: dict[str, Any] = field(default_factory=dict)


def parse_race_timing_event(data: bytes) -> RaceTimingEvent | None:
    body = _body(data, EVENT_RACE_TIMING)
    if body is None:
        return None
    return RaceTimingEvent(
        _text(body, "kind"), _text(body, "courseId"), _text(body, "userId"), body
    )


@dataclass
class ZoneChangeEvent:
    kind: str
    phase: float | None
    radius_now: float
    center_x: float
    center_z: float
    body: dict[str, Any] = field(default_factory=dict)


def parse_zone_change_event(data: bytes) -> ZoneChangeEvent | None:
    body = _body(data, EVENT_ZONE_CHANGE)
    if body is None:
        return None
    phase = body.get("phase")
    return ZoneChangeEvent(
        _text(body, "kind"),
        float(phase) if phase is not None else None,
        _number(body, "radiusNow"),
        _number(body, "centerX"),
        _number(body, "centerZ"),
        body,
    )
