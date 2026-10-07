#!/usr/bin/env python3
"""Both pins name commits promoted to THIS branch's tier of their repositories.

    python scripts/ci/check_pin_tiers.py --crowdyjs <checkout> --crowdycpp <checkout> --tier dev
    python scripts/ci/check_pin_tiers.py --crowdyjs <checkout> --crowdycpp <checkout> --from-ref

The org rule: a branch of one repo references artifacts from the SAME tier of another.
CrowdyPy's ``dev`` may pin only CrowdyJS and CrowdyCPP commits reachable from their
``dev``; ``test`` from their ``test``; ``prod`` from their ``prod``. Reachability, not the
branch head: a promotion merges forward, so one commit satisfies every tier once it has
travelled the ladder, and pinning a head would make an unrelated landing turn this red.
The checkouts need full history (``fetch-depth: 0``).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TIERS = ("dev", "test", "prod")


def _tier_of(ref: str) -> str | None:
    head = ref.removeprefix("refs/heads/").removeprefix("refs/tags/").split("/", 1)[0]
    return head if head in TIERS else None


def _from_ref() -> str | None:
    for ref in (os.environ.get("GITHUB_BASE_REF", ""), os.environ.get("GITHUB_REF_NAME", "")):
        if ref:
            return _tier_of(ref)
    branch = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return _tier_of(branch) if branch and branch != "HEAD" else None


def reachable(repo: Path, commit: str, tier: str) -> tuple[bool, str]:
    fetched = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/{tier}:refs/remotes/origin/{tier}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if fetched.returncode != 0:
        return False, f"cannot fetch origin/{tier} in {repo}: {fetched.stderr.strip()[:200]}"
    present = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"], check=False
    )
    if present.returncode != 0:
        return False, f"{repo} does not hold commit {commit} (is it pushed?)"
    ancestor = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "merge-base",
            "--is-ancestor",
            commit,
            f"refs/remotes/origin/{tier}",
        ],
        check=False,
    )
    if ancestor.returncode != 0:
        return False, f"{commit[:12]} is NOT reachable from origin/{tier}"
    return True, f"{commit[:12]} is reachable from origin/{tier}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--crowdyjs", type=Path, required=True)
    parser.add_argument("--crowdycpp", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tier", choices=TIERS)
    group.add_argument("--from-ref", action="store_true")
    args = parser.parse_args(argv)
    tier = args.tier or _from_ref()
    if tier is None:
        print(
            "check_pin_tiers: this ref carries no tier, so the comparison was SKIPPED (a PR into dev/test/prod is judged by that base)"
        )
        return 0
    pins = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["crowdypy"]
    failed = False
    for name, repo, pin in (
        ("CrowdyJS", args.crowdyjs, pins["crowdyjs"]),
        ("CrowdyCPP", args.crowdycpp, pins["crowdycpp"]),
    ):
        ok, detail = reachable(repo, pin["commit"], tier)
        failed |= not ok
        print(
            f"check_pin_tiers: {name} {pin['version']}: {detail}",
            file=sys.stdout if ok else sys.stderr,
        )
    if failed:
        print(
            f"check_pin_tiers: a CrowdyPy {tier} branch may pin only commits promoted to {tier}. "
            "Promote the dependency first (CrowdyJS, then CrowdyCPP, then CrowdyPy).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
