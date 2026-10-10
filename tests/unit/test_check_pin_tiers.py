"""scripts/ci/check_pin_tiers.py: a pin must be a published release, not just a promoted commit."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_pin_tiers", ROOT / "scripts" / "ci" / "check_pin_tiers.py"
)
assert _spec
assert _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

RELEASED = "7662d0b0" + "a" * 32
UNRELEASED = "ca9fbb50" + "b" * 32
TAG_OBJECT = "f00dfeed" + "c" * 32


def test_parses_lightweight_and_annotated_tags_the_peeled_commit_winning() -> None:
    tags = check.parse_tag_refs(
        "\n".join(
            [
                f"{RELEASED}\trefs/tags/dev/v18.6.0",
                f"{TAG_OBJECT}\trefs/tags/test/v18.6.0",
                f"{RELEASED}\trefs/tags/test/v18.6.0^{{}}",
            ]
        )
    )
    assert tags == {"dev/v18.6.0": RELEASED, "test/v18.6.0": RELEASED}


def test_a_pin_on_a_tagged_release_passes_whichever_tier_tagged_it() -> None:
    ok, detail = check.tag_verdict(RELEASED, "18.6.0", {"dev/v18.6.0": RELEASED})
    assert ok
    assert "dev/v18.6.0" in detail


def test_refuses_0_7_0s_pin_reachable_but_not_the_released_commit() -> None:
    ok, detail = check.tag_verdict(UNRELEASED, "18.6.0", {"dev/v18.6.0": RELEASED})
    assert not ok
    assert "not a published 18.6.0: dev/v18.6.0 is 7662d0b0aaaa" in detail


def test_refuses_a_version_no_tier_has_tagged() -> None:
    ok, detail = check.tag_verdict(RELEASED, "18.9.0", {})
    assert not ok
    assert "no dev/v18.9.0 / test/v18.9.0 / prod/v18.9.0 tag exists" in detail


def test_another_versions_tag_at_the_same_commit_does_not_count() -> None:
    ok, _ = check.tag_verdict(RELEASED, "18.6.0", {"dev/v18.6.1": RELEASED})
    assert not ok
