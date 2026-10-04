#!/usr/bin/env python3
"""Compare the committed schema.gql with the SDL the docs site publishes.

    python scripts/schema_drift.py            # report
    python scripts/schema_drift.py --issue    # report, and open or update a tracking issue

Exit 0: no drift. Exit 1: drift (reported; with --issue, filed). Exit 2: could not compare.

schema.gql is CrowdyJS's at the pinned commit, so drift has two possible owners: the
platform published fields CrowdyJS has not picked up yet (wait for CrowdyJS, then re-pin
with scripts/schema_sync.py), or CrowdyJS moved and this repo has not re-pinned. The
report names the fields that differ in each direction so a reader can tell which.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
import urllib.request
from pathlib import Path

from graphql import GraphQLInputObjectType, GraphQLInterfaceType, GraphQLObjectType, build_schema

ROOT = Path(__file__).resolve().parents[1]
TITLE = "schema drift: the published SDL differs from schema.gql"


def fields(sdl: str) -> set[str]:
    schema = build_schema(sdl)
    out: set[str] = set()
    for name, gql_type in schema.type_map.items():
        if name.startswith("__"):
            continue
        out.add(name)
        if isinstance(gql_type, (GraphQLObjectType, GraphQLInterfaceType, GraphQLInputObjectType)):
            out.update(f"{name}.{field}" for field in gql_type.fields)
    return out


def report(url: str, committed: set[str], published: set[str]) -> str:
    added = sorted(published - committed)
    removed = sorted(committed - published)
    lines = [
        f"The SDL published at {url} differs from CrowdyPy's committed `schema.gql`.",
        "",
        f"- Published but not in schema.gql ({len(added)}): "
        + (", ".join(f"`{a}`" for a in added[:60]) or "none"),
        f"- In schema.gql but not published ({len(removed)}): "
        + (", ".join(f"`{r}`" for r in removed[:60]) or "none"),
        "",
        "schema.gql is CrowdyJS's at the pinned commit. When CrowdyJS carries the change on this "
        "tier, re-pin: `python scripts/schema_sync.py --crowdyjs <checkout> --ref <commit>`, then "
        "`python scripts/codegen.py` and `python tools/parity/parity.py ... --write docs/parity-matrix.md --strict`.",
    ]
    return "\n".join(lines)


def file_issue(body: str) -> None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "CrowdedKingdoms/CrowdyPy")
    if not token:
        print("schema_drift: no GH_TOKEN; not filing", file=sys.stderr)
        return
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    def call(method: str, path: str, payload: dict[str, object] | None = None) -> object:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}{path}",
            method=method,
            headers=headers,
            data=json.dumps(payload).encode() if payload is not None else None,
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    issues = call("GET", "/issues?state=open&per_page=100")
    existing = next((i for i in issues if isinstance(i, dict) and i.get("title") == TITLE), None)  # type: ignore[union-attr]
    if existing:
        call("PATCH", f"/issues/{existing['number']}", {"body": body})
        print(f"schema_drift: updated issue #{existing['number']}")
    else:
        created = call("POST", "/issues", {"title": TITLE, "body": body})
        print(f"schema_drift: opened issue #{created['number']}")  # type: ignore[index]


def tier_sdl_url() -> str:
    """The SDL this branch's tier publishes.

    The configured URL is prod's docs site. Each tier has its own (``docs.<tier>.`` for dev
    and test), and comparing a dev snapshot against prod's schema reports every field the
    tiers legitimately differ by, so the tier comes from this branch's generated default
    origin and is spliced into the host.
    """
    url = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["crowdypy"]["published-sdl"][
        "url"
    ]
    match = re.search(
        r'^CROWDY_DEFAULT_TIER(?::\s*Final)?\s*=\s*"([a-z]+)"',
        (ROOT / "src/crowdypy/_default_origin.py").read_text(),
        re.M,
    )
    tier = match.group(1) if match else "prod"
    if tier == "prod":
        return url
    return re.sub(r"^(https://docs\.)", rf"\g<1>{tier}.", url)


def main(argv: list[str]) -> int:
    url = tier_sdl_url()
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            published = fields(response.read().decode())
        committed = fields((ROOT / "schema.gql").read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"schema_drift: could not compare: {exc}", file=sys.stderr)
        return 2
    if published == committed:
        print(f"schema_drift: no drift against {url}")
        return 0
    body = report(url, committed, published)
    print(body)
    if "--issue" in argv:
        file_issue(body)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
