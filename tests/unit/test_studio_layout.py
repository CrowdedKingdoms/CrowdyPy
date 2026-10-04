"""The headless Studio layout against CrowdyCPP's shared CrowdyJS fixture."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from crowdypy.studio import (
    STUDIO_LAYOUT_STORAGE_KEY,
    STUDIO_PANE_IDS,
    StudioLayoutController,
    clamp_studio_pane_size,
    studio_pane_size_range,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(
    (ROOT / "vendor/CrowdyCPP/tools/parity/fixtures/crowdyjs-studio-layout.v1.json").read_text(
        encoding="utf-8"
    )
)
NUMBERS = {"NaN": math.nan, "-Infinity": -math.inf, "Infinity": math.inf}


class Storage:
    def __init__(self, value: str | None = None) -> None:
        self.values: dict[str, str] = {} if value is None else {STUDIO_LAYOUT_STORAGE_KEY: value}

    def get_item(self, key: str) -> str | None:
        return self.values.get(key)

    def set_item(self, key: str, value: str) -> None:
        self.values[key] = value


def test_the_constants_are_crowdyjs_s() -> None:
    assert FIXTURE["storageKey"] == STUDIO_LAYOUT_STORAGE_KEY
    assert list(STUDIO_PANE_IDS) == FIXTURE["paneIds"]
    assert StudioLayoutController().get_state() == FIXTURE["defaults"]
    for pane, bounds in FIXTURE["ranges"].items():
        assert studio_pane_size_range(pane) == (bounds["min"], bounds["max"])


@pytest.mark.parametrize("case", FIXTURE["clamping"], ids=lambda c: f"{c['pane']}-{c['input']}")
def test_clamping_rounds_as_javascript_does(case: dict[str, Any]) -> None:
    value = NUMBERS.get(case["input"], case["input"])
    assert clamp_studio_pane_size(case["pane"], value) == case["output"]


def test_the_persisted_json_is_byte_identical() -> None:
    storage = Storage()
    controller = StudioLayoutController(storage=storage)
    controller.set_size("explorer", controller.pane_size("explorer"))
    assert storage.values[STUDIO_LAYOUT_STORAGE_KEY] == FIXTURE["defaultPersistedJson"]

    storage = Storage()
    controller = StudioLayoutController(storage=storage)
    seen: list[Any] = []
    controller.subscribe(seen.append)
    controller.set_visible("settings", True)
    controller.toggle("explorer")
    controller.set_size("agent", 999.6)
    controller.set_size("bottom", 111.5)
    assert controller.get_state() == FIXTURE["scenario"]["state"]
    assert storage.values[STUDIO_LAYOUT_STORAGE_KEY] == FIXTURE["scenario"]["persistedJson"]
    assert len(seen) == 4

    reloaded = StudioLayoutController(storage=Storage(FIXTURE["scenario"]["persistedJson"]))
    assert reloaded.get_state() == FIXTURE["scenario"]["state"]


def test_a_corrupt_layout_keeps_the_defaults() -> None:
    for raw in (
        "not json",
        "[]",
        json.dumps({"visible": {"explorer": "yes"}, "sizes": {"agent": "big"}}),
    ):
        assert StudioLayoutController(storage=Storage(raw)).get_state() == FIXTURE["defaults"]
