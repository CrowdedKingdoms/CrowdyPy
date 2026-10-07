#!/usr/bin/env python3
"""Refuse a generated default origin that names the wrong tier.

    python scripts/ci/assert_default_origin.py dev|test|prod   # judge as that tier (the release guard)
    python scripts/ci/assert_default_origin.py --from-ref       # the PR base, else the pushed branch
    python scripts/ci/assert_default_origin.py --self-test

``src/crowdypy/_default_origin.py`` is generated per branch by the operator tooling and
ships in every wheel. A promotion can rewrite it in either direction with NO conflict, and
a published PyPI version can never be corrected in place, so the tier it names must be
the tier of the branch (or tag) carrying it. A pull request is judged by its BASE, so a
dev -> test promotion is refused before it merges. This is the hermetic half; the operator
gate ``check-sdk-default-origin.mjs`` compares the host against the tier table.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILE = ROOT / "src" / "crowdypy" / "_default_origin.py"
TIERS = ("dev", "test", "prod")


def parse(text: str) -> dict[str, str | None]:
    def one(name: str) -> str | None:
        match = re.search(rf'^{name}(?::\s*Final)?\s*=\s*"([^"]*)"\s*$', text, re.M)
        return match.group(1) if match else None

    return {
        "tier": one("CROWDY_DEFAULT_TIER"),
        "host": one("CROWDY_DEFAULT_HOST"),
        "http": one("CROWDY_DEFAULT_HTTP_ORIGIN"),
        "ws": one("CROWDY_DEFAULT_WS_ORIGIN"),
    }


def tier_of_ref(ref: str) -> str | None:
    bare = ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
    head = bare.split("/", 1)[0]
    return head if head in TIERS else None


def problems_for(declared: dict[str, str | None], want: str) -> list[str]:
    tier, host = declared["tier"], declared["host"]
    if not tier:
        return ["no CROWDY_DEFAULT_TIER line; refusing rather than passing on an unreadable tier"]
    if not host:
        return ["no CROWDY_DEFAULT_HOST line; refusing rather than passing on an unreadable host"]
    out: list[str] = []
    if declared["http"] != f"https://{host}":
        out.append(f"CROWDY_DEFAULT_HTTP_ORIGIN is {declared['http']!r}; it must be https://{host}")
    if declared["ws"] != f"wss://{host}":
        out.append(f"CROWDY_DEFAULT_WS_ORIGIN is {declared['ws']!r}; it must be wss://{host}")
    if f".{tier}." not in f".{host}.":
        out.append(
            f"tier {tier!r} and host {host!r} disagree (the host carries no {tier!r} label); "
            "one was edited without the other -- regenerate the file"
        )
    if tier != want:
        out.append(
            f"the file declares tier {tier!r} and this is {want!r}. Each branch publishes one tier's "
            "wheel and PyPI never replaces a version: regenerate for the destination tier "
            f"(sync-client-origins.mjs --write --tier {want} --only crowdypy) rather than hand-editing. "
            "A promotion can rewrite this file with NO conflict."
        )
    return out


def resolve_want(arg: str) -> tuple[str | None, str]:
    if arg != "--from-ref":
        return arg, "argument"
    for source, ref in (
        ("GITHUB_BASE_REF (the pull request base)", os.environ.get("GITHUB_BASE_REF", "")),
        ("GITHUB_REF_NAME", os.environ.get("GITHUB_REF_NAME", "")),
    ):
        if ref:
            return tier_of_ref(ref), f"{source} = {ref}"
    branch = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if branch and branch != "HEAD":
        return tier_of_ref(branch), f"the checked-out branch {branch}"
    if os.environ.get("GITHUB_ACTIONS"):
        raise SystemExit("::error::no ref to judge against; in CI that is a misconfiguration")
    return None, "no ref"


def self_test() -> int:
    good = {
        "tier": "test",
        "host": "ck.test.example.test",
        "http": "https://ck.test.example.test",
        "ws": "wss://ck.test.example.test",
    }
    cases = [
        ("a correct test file on test", good, "test", False),
        ("dev's file planted on test", {**good, "tier": "dev"}, "test", True),
        ("host and tier disagree", {**good, "tier": "prod"}, "prod", True),
        ("a mangled ws scheme", {**good, "ws": "ws://ck.test.example.test"}, "test", True),
        ("an unreadable tier", {**good, "tier": None}, "test", True),
    ]
    bad = 0
    for label, declared, want, should_fail in cases:
        failed = bool(problems_for(declared, want))
        ok = failed == should_fail
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {label}")
    for ref, tier in (("test", "test"), ("refs/tags/prod/v1.2.3", "prod"), ("michael/x", None)):
        ok = tier_of_ref(ref) == tier
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} tier of {ref!r} is {tier!r}")
    sample = (
        '"""doc"""\n\nfrom typing import Final\n\nCROWDY_DEFAULT_TIER: Final = "dev"\n'
        'CROWDY_DEFAULT_HOST: Final = "h"\n'
    )
    parsed_ok = parse(sample)["tier"] == "dev" and parse(sample)["host"] == "h"
    bad += not parsed_ok
    print(f"{'ok  ' if parsed_ok else 'FAIL'} parses the generated shape")
    print("assert_default_origin --self-test:", "all as expected" if not bad else f"{bad} WRONG")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "usage: assert_default_origin.py dev|test|prod|--from-ref|--self-test", file=sys.stderr
        )
        return 2
    if argv[0] == "--self-test":
        return self_test()
    want, source = resolve_want(argv[0])
    if not FILE.exists():
        print(f"::error::{FILE} is missing; it is generated and load-bearing", file=sys.stderr)
        return 1
    declared = parse(FILE.read_text(encoding="utf-8"))
    if want is None:
        print(
            f"tier=skipped declares={declared['tier']} ({source} carries no tier; a PR into dev/test/prod is judged by that base)"
        )
        return 0
    if want not in TIERS:
        print(f"::error::{want!r} is not one of dev, test, prod", file=sys.stderr)
        return 2
    problems = problems_for(declared, want)
    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"tier={declared['tier']} host={declared['host']} (judged as {want} from {source})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
