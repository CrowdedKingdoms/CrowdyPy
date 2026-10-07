"""rustc diagnostics parsing against CrowdyCPP's shared CrowdyJS fixture."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import msgspec
import pytest

from crowdypy.studio.diagnostics import parse_rustc_diagnostics

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(
    (ROOT / "vendor/CrowdyCPP/tools/parity/fixtures/crowdy-studio-diagnostics.v1.json").read_text(
        encoding="utf-8"
    )
)


def test_the_fixture_is_the_pinned_contract() -> None:
    assert FIXTURE["contractVersion"] == "crowdy.studio-diagnostics/1"
    pin = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["crowdypy"][
        "crowdyjs"
    ]
    assert FIXTURE["crowdyJs"] == {"version": pin["version"], "commit": pin["commit"]}


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
def test_parses_as_crowdyjs_does(case: dict[str, Any]) -> None:
    parsed = parse_rustc_diagnostics(case["output"], case["defaultTarget"])
    assert msgspec.to_builtins(parsed) == case["expected"]


def test_nothing_to_parse() -> None:
    assert parse_rustc_diagnostics(None, "SERVER") == []
    assert parse_rustc_diagnostics("", "SERVER") == []
    assert (
        parse_rustc_diagnostics(
            '{"message": "x", "spans": [{"is_primary": true, "file_name": "src/a.rs", "line_start": NaN, "column_start": 1}]}',
            "SERVER",
        )
        == []
    )
