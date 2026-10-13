"""The shared fixtures are still byte-for-byte the files they were copied from.

Runs where a CrowdyJS checkout is available (CI checks out the pinned commit; locally set
CROWDYJS_PATH or keep a sibling ../CrowdyJS); skipped otherwise.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PIN = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["crowdypy"]


def _crowdyjs() -> Path | None:
    for candidate in (os.environ.get("CROWDYJS_PATH"), ROOT.parent / "CrowdyJS", ROOT / "CrowdyJS"):
        if candidate and (Path(candidate) / ".git").exists():
            return Path(candidate)
    return None


def _show(repo: Path, commit: str, path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{path}"], capture_output=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        (
            "tests/fixtures/binary-wire-fixtures.json",
            "test/unit/fixtures/binary-wire-fixtures.json",
        ),
        ("tests/fixtures/exec-client-frames.json", "test/unit/fixtures/exec-client-frames.json"),
        ("tests/fixtures/voice-frames.json", "test/unit/fixtures/voice-frames.json"),
    ],
)
def test_crowdyjs_fixture_unchanged(ours: str, theirs: str) -> None:
    repo = _crowdyjs()
    if repo is None:
        pytest.skip("no CrowdyJS checkout (set CROWDYJS_PATH)")
    source = _show(repo, PIN["crowdyjs"]["commit"], theirs)
    if source is None:
        pytest.skip(
            f"the CrowdyJS checkout lacks the pinned commit {PIN['crowdyjs']['commit'][:12]}"
        )
    assert (ROOT / ours).read_bytes() == source


@pytest.mark.parametrize("name", ["exec-client-frames.json", "voice-frames.json"])
def test_fixture_matches_the_vendored_crowdycpp_copy(name: str) -> None:
    ours = (ROOT / "tests/fixtures" / name).read_bytes()
    vendored = (ROOT / "vendor/CrowdyCPP/tools/parity/fixtures" / name).read_bytes()
    assert ours == vendored
