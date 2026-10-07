#!/usr/bin/env python3
"""The version a tier tag publishes to PyPI.

    python scripts/ci/resolve_pypi_version.py <tier> <X.Y.Z> [--package crowdypy]
    python scripts/ci/resolve_pypi_version.py --self-test

``dev/vX.Y.Z`` publishes ``X.Y.Z.devN``, ``test/vX.Y.Z`` publishes ``X.Y.ZrcN`` and
``prod/vX.Y.Z`` publishes ``X.Y.Z``. N is the next free number on PyPI for that base, never
a CI run number: PyPI accepts a version exactly once, so a re-run after a failed upload must
move on rather than collide. PEP 440 orders the three forms dev < rc < final, which is the
tier ladder, and pip ignores pre-releases unless a consumer pins one exactly, which is how
each tier's consumers are expected to pin.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable

SUFFIX = {"dev": ".dev", "test": "rc", "prod": ""}


def published_versions(package: str) -> list[str]:
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{package}/json", timeout=20
        ) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    return list(data.get("releases", {}).keys())


def resolve(tier: str, base: str, existing: Iterable[str]) -> str:
    if tier not in SUFFIX:
        raise SystemExit(f"resolve_pypi_version: {tier!r} is not one of dev, test, prod")
    if not re.fullmatch(r"\d+\.\d+\.\d+", base):
        raise SystemExit(f"resolve_pypi_version: {base!r} is not a bare X.Y.Z")
    versions = set(existing)
    if tier == "prod":
        if base in versions:
            raise SystemExit(
                f"resolve_pypi_version: {base} is already on PyPI; a version is published once"
            )
        return base
    pattern = re.compile(rf"^{re.escape(base)}{re.escape(SUFFIX[tier])}(\d+)$")
    used = [int(m.group(1)) for v in versions if (m := pattern.match(v))]
    return f"{base}{SUFFIX[tier]}{max(used, default=0) + 1}"


def self_test() -> int:
    cases = [
        (("dev", "0.1.0", []), "0.1.0.dev1"),
        (("dev", "0.1.0", ["0.1.0.dev1", "0.1.0.dev2", "0.1.0rc1"]), "0.1.0.dev3"),
        (("test", "0.1.0", ["0.1.0.dev4"]), "0.1.0rc1"),
        (("test", "0.2.0", ["0.1.0rc3", "0.2.0rc1"]), "0.2.0rc2"),
        (("prod", "0.1.0", ["0.1.0rc2"]), "0.1.0"),
    ]
    bad = 0
    for (tier, base, existing), want in cases:
        got = resolve(tier, base, existing)
        ok = got == want
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {tier} {base} over {existing} -> {got}")
    try:
        resolve("prod", "0.1.0", ["0.1.0"])
        bad += 1
        print("FAIL a prod version already published must be refused")
    except SystemExit:
        print("ok   a prod version already published is refused")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    tier, base = argv[0], argv[1].removeprefix("v")
    package = argv[argv.index("--package") + 1] if "--package" in argv else "crowdypy"
    version = resolve(tier, base, published_versions(package))
    print(f"version={version}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"version={version}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
