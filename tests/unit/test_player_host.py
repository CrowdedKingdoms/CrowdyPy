"""Player-host observation against verdicts CrowdyJS computed on the same values and
schemas (tests/fixtures/player-host.v1.json, from tools/fixtures/player_host.mjs)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from crowdypy.player_host import (
    CROWDY_AGENT_ERROR_CODES,
    GAME_COMMAND_SCHEMAS_V1,
    GAME_OBSERVATION_SCHEMA_V1,
    CrowdyAgentError,
    CrowdyAgentOutcomeUnknownError,
    assert_bounded_json_schema,
    canonical_json,
    digest_canonical_json,
    is_decimal_string,
    to_agent_error,
    validate_json_schema_value,
)
from crowdypy.player_host import schemas as schema_module

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "tests/fixtures/player-host.v1.json").read_text(encoding="utf-8"))
PIN = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["crowdypy"][
    "crowdyjs"
]


def schema_for(ref: Any) -> Any:
    if isinstance(ref, str):
        return getattr(schema_module, ref)
    if "$ref" in ref:
        base, key = ref["$ref"].split(".")
        return getattr(schema_module, base)[key]
    return ref


def verdict(run: Any) -> dict[str, Any]:
    try:
        run()
    except CrowdyAgentError as error:
        return {"ok": False, "error": dict(error.to_json())}
    return {"ok": True}


def test_generated_from_the_pinned_crowdyjs() -> None:
    assert FIXTURE["crowdyJs"] == {"version": PIN["version"], "commit": PIN["commit"]}
    assert FIXTURE["crowdyJs"] == schema_module.CROWDYJS_SOURCE
    assert list(CROWDY_AGENT_ERROR_CODES) == FIXTURE["errorCodes"]


@pytest.mark.parametrize("case", FIXTURE["values"], ids=lambda c: c["name"])
def test_values_validate_as_in_crowdyjs(case: dict[str, Any]) -> None:
    names = {
        "maxNodes": "max_nodes",
        "maxDepth": "max_depth",
        "maxBytes": "max_bytes",
        "direction": "direction",
    }
    options = {names[k]: v for k, v in case.get("options", {}).items()}
    got = verdict(
        lambda: validate_json_schema_value(schema_for(case["schema"]), case["value"], **options)
    )
    assert got == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["schemas"], ids=lambda c: c["name"])
def test_schemas_are_judged_as_in_crowdyjs(case: dict[str, Any]) -> None:
    reject = case.get("options", {}).get("rejectAuthorityFields", True)
    got = verdict(
        lambda: assert_bounded_json_schema(
            schema_for(case["schema"]), reject_authority_fields=reject
        )
    )
    assert got == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["errors"], ids=lambda c: c["message"][:24])
def test_agent_errors_sanitize_as_in_crowdyjs(case: dict[str, Any]) -> None:
    options = case.get("options", {})
    error = CrowdyAgentError(
        case["code"],
        case["message"],
        retryable=options.get("retryable", False),
        remediation=options.get("remediation"),
        field=options.get("field"),
        required_scope=options.get("requiredScope"),
    )
    assert error.to_json() == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["canonical"], ids=lambda c: c["name"])
def test_canonical_json_is_crowdyjs_s(case: dict[str, Any]) -> None:
    assert canonical_json(case["value"]) == case["canonical"]
    assert digest_canonical_json(case["value"]) == case["digest"]


def test_the_schemas_are_read_only_and_the_helpers_behave() -> None:
    with pytest.raises(TypeError):
        GAME_OBSERVATION_SCHEMA_V1["type"] = "array"  # type: ignore[index]
    assert set(GAME_COMMAND_SCHEMAS_V1) >= {"MOVE", "STOP", "CHAT_SEND"}
    assert is_decimal_string("-12")
    assert not is_decimal_string("-0")
    assert not is_decimal_string("012")
    assert to_agent_error(ValueError("secret")) == {
        "code": "AGENT_TOOL_FAILED",
        "message": "Agent operation failed",
        "retryable": False,
    }
    assert to_agent_error(CrowdyAgentOutcomeUnknownError())["code"] == "AGENT_TOOL_OUTCOME_UNKNOWN"
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    with pytest.raises(CrowdyAgentError, match="Cyclic"):
        canonical_json(cyclic)
