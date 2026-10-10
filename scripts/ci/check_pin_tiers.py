#!/usr/bin/env python3
"""Both pins name published releases, promoted to THIS branch's tier of their repositories.

    python scripts/ci/check_pin_tiers.py --crowdyjs <checkout> --crowdycpp <checkout> --tier dev
    python scripts/ci/check_pin_tiers.py --crowdyjs <checkout> --crowdycpp <checkout> --from-ref

The org rule: a branch of one repo references artifacts from the SAME tier of another.
CrowdyPy's ``dev`` may pin only CrowdyJS and CrowdyCPP commits reachable from their
``dev``; ``test`` from their ``test``; ``prod`` from their ``prod``. Reachability, not the
branch head: a promotion merges forward, so one commit satisfies every tier once it has
travelled the ladder, and pinning a head would make an unrelated landing turn this red.
The checkouts need full history (``fetch-depth: 0``).

And released: each pinned commit must be the target of a ``<tier>/v<version>`` tag of its
repository for the pinned version (any tier's, for the same ladder reason). Reachability
alone let 0.7.0 pin CrowdyJS ``ca9fbb5`` as 18.6.0 while ``dev/v18.6.0`` is ``7662d0b0``.
That half needs no tier, so it runs on every ref.
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


def parse_tag_refs(text: str) -> dict[str, str]:
    """``git ls-remote --tags`` output: tag name -> the commit it points at (an annotated tag's
    peeled ``^{}`` line wins over the tag object's own sha)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) != 2 or len(parts[0]) != 40 or not parts[1].startswith("refs/tags/"):
            continue
        sha, ref = parts
        name = ref.removeprefix("refs/tags/")
        if name.endswith("^{}"):
            out[name.removesuffix("^{}")] = sha
        else:
            out.setdefault(name, sha)
    return out


def tag_verdict(commit: str, version: str, tags: dict[str, str]) -> tuple[bool, str]:
    """Whether ``commit`` is a published ``version``: the target of a ``<tier>/v<version>`` tag."""
    named = [f"{tier}/v{version}" for tier in TIERS]
    matching = [name for name in named if tags.get(name) == commit]
    if matching:
        return True, f"{commit[:12]} is {', '.join(matching)}"
    elsewhere = [f"{name} is {tags[name][:12]}" for name in named if name in tags]
    if elsewhere:
        return False, f"{commit[:12]} is not a published {version}: {', '.join(elsewhere)}"
    return False, f"{commit[:12]} is not a published {version}: no {' / '.join(named)} tag exists"


def tagged(repo: Path, commit: str, version: str) -> tuple[bool, str]:
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "ls-remote",
            "--tags",
            "origin",
            *[f"refs/tags/{tier}/v{version}*" for tier in TIERS],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        return False, f"cannot list the tags of {repo}: {listed.stderr.strip()[:200]}"
    return tag_verdict(commit, version, parse_tag_refs(listed.stdout))


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
    pins = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["crowdypy"]
    failed = False
    unreleased = False
    for name, repo, pin in (
        ("CrowdyJS", args.crowdyjs, pins["crowdyjs"]),
        ("CrowdyCPP", args.crowdycpp, pins["crowdycpp"]),
    ):
        ok, detail = tagged(repo, pin["commit"], pin["version"])
        unreleased |= not ok
        print(
            f"check_pin_tiers: {name} {pin['version']}: {detail}",
            file=sys.stdout if ok else sys.stderr,
        )
        if tier is None:
            continue
        ok, detail = reachable(repo, pin["commit"], tier)
        failed |= not ok
        print(
            f"check_pin_tiers: {name} {pin['version']}: {detail}",
            file=sys.stdout if ok else sys.stderr,
        )
    if unreleased:
        print(
            "check_pin_tiers: pin the commit a release tag points at (CrowdyJS's, then CrowdyCPP's).",
            file=sys.stderr,
        )
        return 1
    if tier is None:
        print(
            "check_pin_tiers: this ref carries no tier, so the reachability comparison was SKIPPED (a PR into dev/test/prod is judged by that base)"
        )
        return 0
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
