#!/usr/bin/env python3
"""Stamp the published version into a release build's workspace (never committed).

    python scripts/ci/stamp_version.py 0.1.0.dev3

The branch carries the bare ``X.Y.Z``; a tier tag publishes ``X.Y.Z.devN`` / ``X.Y.ZrcN`` /
``X.Y.Z``. Only ``[project].version`` in pyproject.toml is rewritten, so the wheel's
metadata carries the tier version while ``crowdypy.__version__`` stays the bare release
(as CrowdyJS's ``VERSION`` does), and the base must match it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    version = argv[0]
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(\.dev\d+|rc\d+)?", version)
    if not match:
        print(f"stamp_version: {version!r} is not X.Y.Z, X.Y.Z.devN or X.Y.ZrcN", file=sys.stderr)
        return 2
    text = PYPROJECT.read_text(encoding="utf-8")
    current = re.search(r'^version = "([^"]+)"$', text, re.M)
    if not current or current.group(1) != match.group(1):
        found = current.group(1) if current else None
        print(
            f"stamp_version: pyproject.toml is {found}, not the base {match.group(1)}",
            file=sys.stderr,
        )
        return 1
    PYPROJECT.write_text(
        text.replace(current.group(0), f'version = "{version}"', 1), encoding="utf-8"
    )
    print(f"stamped {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
