#!/usr/bin/env python3
"""Owns schema.gql and operations/: CrowdyJS's, at the commit pyproject.toml pins.

    python scripts/schema_sync.py --crowdyjs ../CrowdyJS --ref <commit-or-tag>
        re-pin [tool.crowdypy.crowdyjs] to that commit and copy its schema and operations
    python scripts/schema_sync.py --check
        offline: the committed files hash to [tool.crowdypy.graphql]
    python scripts/schema_sync.py --check --crowdyjs <checkout>
        also re-derive both from the pinned commit and compare byte for byte

The operations are the flagship's, unedited, so a CrowdyPy method sends the same
document CrowdyJS does. Two directories are not copied, each for a reason the parity
matrix also records: `hosting` publishes a browser bundle to Crowdy Games (a browser
exclusion) and `udp` is the GraphQL UDP proxy, which CrowdyPy replaces with native UDP
through CrowdyCPP (a native equivalent).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from _pins import (
    ROOT,
    load_tool_table,
    read_dir,
    read_tree,
    resolve_commit,
    tree_sha256,
    write_tool_table,
)

SCHEMA = ROOT / "schema.gql"
OPERATIONS = ROOT / "operations"
JS_OPERATIONS = "src/operations/"

EXCLUDED = {
    "hosting": "browser exclusion: publishes a browser bundle to Crowdy Games",
    "udp": "native equivalent: the GraphQL UDP proxy; CrowdyPy sends native UDP through CrowdyCPP",
}


def _keep(path: str) -> bool:
    if path == "schema.gql" or path == "package.json":
        return True
    if not path.startswith(JS_OPERATIONS) or not path.endswith(".graphql"):
        return False
    domain = path[len(JS_OPERATIONS) :].split("/", 1)[0]
    return domain not in EXCLUDED


def derive(repo: Path, commit: str) -> tuple[bytes, dict[str, bytes], str]:
    files = read_tree(repo, commit, _keep)
    if "schema.gql" not in files:
        raise SystemExit(f"schema_sync: CrowdyJS has no schema.gql at {commit}")
    version = json.loads(files.pop("package.json"))["version"]
    schema = files.pop("schema.gql")
    operations = {path[len(JS_OPERATIONS) :]: data for path, data in files.items()}
    return schema, operations, version


def committed() -> tuple[bytes | None, dict[str, bytes]]:
    schema = SCHEMA.read_bytes() if SCHEMA.exists() else None
    return schema, read_dir(OPERATIONS)


def sync(repo: Path, ref: str) -> int:
    commit = resolve_commit(repo, ref)
    schema, operations, version = derive(repo, commit)
    SCHEMA.write_bytes(schema)
    if OPERATIONS.exists():
        shutil.rmtree(OPERATIONS)
    for path, data in operations.items():
        dest = OPERATIONS / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    write_tool_table("tool.crowdypy.crowdyjs", {"version": version, "commit": commit})
    write_tool_table(
        "tool.crowdypy.graphql",
        {
            "schema-sha256": _sha256(schema),
            "operations-sha256": tree_sha256(operations),
        },
    )
    print(
        f"schema_sync: CrowdyJS {version} ({commit[:12]}): schema.gql and "
        f"{len(operations)} operation file(s); run scripts/codegen.py next"
    )
    return 0


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check(repo: Path | None) -> int:
    tool = load_tool_table()
    pin, recorded = tool["crowdyjs"], tool["graphql"]
    problems: list[str] = []
    schema, operations = committed()
    if schema is None:
        problems.append("schema.gql is missing")
    elif _sha256(schema) != recorded["schema-sha256"]:
        problems.append(
            "schema.gql does not hash to [tool.crowdypy.graphql].schema-sha256; it is "
            "CrowdyJS's file and is never edited here (scripts/schema_sync.py)"
        )
    if not operations:
        problems.append("operations/ is missing or empty")
    elif tree_sha256(operations) != recorded["operations-sha256"]:
        problems.append(
            "operations/ does not hash to [tool.crowdypy.graphql].operations-sha256; the "
            "documents are CrowdyJS's and are never edited here"
        )
    stray = sorted(p for p in operations if p.split("/", 1)[0] in EXCLUDED)
    if stray:
        problems.append(f"operations/ carries excluded domains: {', '.join(stray)}")
    if repo is not None:
        js_schema, js_operations, version = derive(repo, pin["commit"])
        if version != pin["version"]:
            problems.append(
                f"CrowdyJS {pin['commit'][:12]} is {version}; the pin says {pin['version']}"
            )
        if js_schema != schema:
            problems.append(f"schema.gql differs from CrowdyJS at {pin['commit'][:12]}")
        if js_operations != operations:
            changed = sorted(
                p
                for p in set(js_operations) | set(operations)
                if js_operations.get(p) != operations.get(p)
            )
            problems.append(
                f"operations/ differs from CrowdyJS at {pin['commit'][:12]} in "
                f"{len(changed)} file(s): {', '.join(changed[:8])}"
            )
        if not problems:
            print(f"re-derived schema.gql and {len(js_operations)} operation file(s): identical")
    if problems:
        for problem in problems:
            print(f"schema_sync: {problem}", file=sys.stderr)
        return 1
    print(
        f"schema_sync: OK -- CrowdyJS {pin['version']} ({pin['commit'][:12]}), "
        f"{len(operations)} operation file(s)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--crowdyjs", type=Path, help="a CrowdyJS git checkout")
    parser.add_argument("--ref", help="the commit or tag to pin (with --crowdyjs)")
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args(argv)
    if args.check:
        return check(args.crowdyjs)
    if not args.crowdyjs or not args.ref:
        parser.error("syncing needs --crowdyjs and --ref")
    return sync(args.crowdyjs, args.ref)


if __name__ == "__main__":
    raise SystemExit(main())
