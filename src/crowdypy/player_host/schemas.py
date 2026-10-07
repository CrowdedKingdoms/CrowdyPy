"""The player-host contract's JSON Schemas, CrowdyJS's objects verbatim.

``schemas.v1.json`` is generated from CrowdyJS at the pinned commit by
``tools/fixtures/player_host.mjs``; never edit it. Each schema here is read-only.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from crowdypy.player_host.json_schema import JsonSchema, deep_freeze

__all__ = [
    "CROWDYJS_SOURCE",
    "GAME_COMMAND_RESULT_SCHEMA_V1",
    "GAME_COMMAND_SCHEMAS_V1",
    "GAME_COMMAND_SCHEMA_V1",
    "GAME_OBSERVATION_SCHEMA_V1",
    "OBSERVE_REQUEST_SCHEMA_V1",
    "PLAYER_HOST_CAPABILITIES_SCHEMA_V1",
]

_DATA: dict[str, Any] = json.loads(
    resources.files("crowdypy.player_host").joinpath("schemas.v1.json").read_text(encoding="utf-8")
)
#: The CrowdyJS version and commit the schemas were generated from.
CROWDYJS_SOURCE: dict[str, str] = dict(_DATA["crowdyJs"])
_SCHEMAS: dict[str, Any] = _DATA["schemas"]

GAME_COMMAND_RESULT_SCHEMA_V1: JsonSchema = deep_freeze(_SCHEMAS["GAME_COMMAND_RESULT_SCHEMA_V1"])
GAME_COMMAND_SCHEMAS_V1: dict[str, JsonSchema] = deep_freeze(_SCHEMAS["GAME_COMMAND_SCHEMAS_V1"])
GAME_COMMAND_SCHEMA_V1: JsonSchema = deep_freeze(_SCHEMAS["GAME_COMMAND_SCHEMA_V1"])
GAME_OBSERVATION_SCHEMA_V1: JsonSchema = deep_freeze(_SCHEMAS["GAME_OBSERVATION_SCHEMA_V1"])
OBSERVE_REQUEST_SCHEMA_V1: JsonSchema = deep_freeze(_SCHEMAS["OBSERVE_REQUEST_SCHEMA_V1"])
PLAYER_HOST_CAPABILITIES_SCHEMA_V1: JsonSchema = deep_freeze(
    _SCHEMAS["PLAYER_HOST_CAPABILITIES_SCHEMA_V1"]
)
