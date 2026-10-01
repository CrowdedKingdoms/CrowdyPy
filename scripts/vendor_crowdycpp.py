#!/usr/bin/env python3
"""Owns vendor/CrowdyCPP: CrowdyCPP's sources at the commit pyproject.toml pins.

    python scripts/vendor_crowdycpp.py --crowdycpp ../CrowdyCPP --ref dev/v0.54.0
        re-vendor from that ref and rewrite [tool.crowdypy.crowdycpp]
    python scripts/vendor_crowdycpp.py --check
        offline: the vendored tree hashes to the pin, and CrowdyCPP's own CrowdyJS
        parity pin equals ours
    python scripts/vendor_crowdycpp.py --check --crowdycpp <checkout>
        also re-derive the subset from the pinned commit and compare byte for byte

Files are read from git objects at the pinned commit, never from a working tree, so
an uncommitted edit in the CrowdyCPP checkout cannot leak into the vendored copy.

The tree hash alone cannot prove provenance: an edit to a vendored file plus a matching
edit to `tree-sha256` passes it. Re-derivation from the commit is what CI runs, and it
is the check that can fail for that reason.
"""

from __future__ import annotations

import argparse
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

VENDOR = ROOT / "vendor" / "CrowdyCPP"
PROVENANCE_NOTE = "PROVENANCE.md"

# What the Python wheel compiles, plus the two inputs CrowdyCPP's CMakeLists reads
# unconditionally (package.json for its version record, tools/parity/fixtures for the
# embedded Studio fixtures). Tests, benchmarks, docs and maintainer tooling stay in
# CrowdyCPP.
SUBSET = (
    "CMakeLists.txt",
    "LICENSE",
    "package.json",
    "cmake/",
    "include/",
    "src/",
    "third_party/yyjson/",
    "tools/parity/fixtures/",
)


def _in_subset(path: str) -> bool:
    return any(
        path == entry or (entry.endswith("/") and path.startswith(entry)) for entry in SUBSET
    )


def read_subset(repo: Path, commit: str) -> dict[str, bytes]:
    files = read_tree(repo, commit, _in_subset)
    if not files:
        raise SystemExit(f"vendor_crowdycpp: nothing of the subset exists at {commit}")
    return files


def read_vendored() -> dict[str, bytes]:
    return read_dir(VENDOR, skip=frozenset({PROVENANCE_NOTE}))


def write_vendor(files: dict[str, bytes], tag: str, commit: str) -> None:
    if VENDOR.exists():
        shutil.rmtree(VENDOR)
    for name, data in files.items():
        dest = VENDOR / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    (VENDOR / PROVENANCE_NOTE).write_text(
        "# Vendored CrowdyCPP\n\n"
        "This directory is CrowdyCPP's source subset at the commit that `pyproject.toml`\n"
        "pins under `[tool.crowdypy.crowdycpp]`. It is written by\n"
        "`scripts/vendor_crowdycpp.py` and checked in CI by re-deriving it from that commit.\n"
        "Do not edit it: change CrowdyCPP, release it on the same tier, and re-vendor.\n\n"
        f"Vendored from `{tag}` (`{commit}`).\n",
        encoding="utf-8",
    )


def parity_target(files: dict[str, bytes]) -> dict[str, str]:
    target = json.loads(files["package.json"])["crowdyjsParityTarget"]
    return {"version": target["version"], "commit": target["commit"]}


def vendor(repo: Path, ref: str) -> int:
    commit = resolve_commit(repo, ref)
    files = read_subset(repo, commit)
    version = json.loads(files["package.json"])["version"]
    write_vendor(files, ref, commit)
    write_tool_table(
        "tool.crowdypy.crowdycpp",
        {"version": version, "tag": ref, "commit": commit, "tree-sha256": tree_sha256(files)},
    )
    print(f"vendored CrowdyCPP {version} at {ref} ({commit[:12]}): {len(files)} files")
    target = parity_target(files)
    pin = load_tool_table()["crowdyjs"]
    if pin["commit"] != target["commit"]:
        print(
            f"NOTE: CrowdyCPP pins CrowdyJS {target['version']} ({target['commit'][:12]}) and "
            f"this repo pins {pin['version']} ({pin['commit'][:12]}); the gate refuses until "
            "they agree (scripts/schema_sync.py --crowdyjs ... --ref <CrowdyCPP's commit>)."
        )
    return 0


def check(repo: Path | None) -> int:
    tool = load_tool_table()
    pin = tool["crowdycpp"]
    problems: list[str] = []
    vendored = read_vendored()
    if not vendored:
        problems.append("vendor/CrowdyCPP is missing or empty")
    else:
        got = tree_sha256(vendored)
        if got != pin["tree-sha256"]:
            problems.append(
                f"vendor/CrowdyCPP hashes to {got}, the pin says {pin['tree-sha256']}. "
                "Re-vendor with scripts/vendor_crowdycpp.py; never edit vendored files."
            )
        version = json.loads(vendored["package.json"])["version"]
        if version != pin["version"]:
            problems.append(f"vendored package.json is {version}, the pin says {pin['version']}")
        target = parity_target(vendored)
        ours = {"version": tool["crowdyjs"]["version"], "commit": tool["crowdyjs"]["commit"]}
        if target != ours:
            problems.append(
                f"CrowdyCPP's own CrowdyJS pin is {target['version']} ({target['commit']}); "
                f"[tool.crowdypy.crowdyjs] is {ours['version']} ({ours['commit']}). "
                "The two must name the same CrowdyJS commit."
            )
    if repo is not None:
        derived = read_subset(repo, pin["commit"])
        if derived != vendored:
            changed = sorted(
                p for p in set(derived) | set(vendored) if derived.get(p) != vendored.get(p)
            )
            problems.append(
                f"re-deriving from {pin['commit'][:12]} differs in {len(changed)} file(s): "
                + ", ".join(changed[:8])
                + (" ..." if len(changed) > 8 else "")
            )
        else:
            print(f"re-derived {len(derived)} files from CrowdyCPP {pin['commit'][:12]}: identical")
    if problems:
        for problem in problems:
            print(f"vendor_crowdycpp: {problem}", file=sys.stderr)
        return 1
    print(
        f"vendor_crowdycpp: OK -- CrowdyCPP {pin['version']} at {pin['tag']} "
        f"({pin['commit'][:12]}), {len(vendored)} files, tree {pin['tree-sha256'][:12]}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--crowdycpp", type=Path, help="a CrowdyCPP git checkout")
    parser.add_argument("--ref", help="the tag or commit to vendor (with --crowdycpp)")
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args(argv)
    if args.check:
        return check(args.crowdycpp)
    if not args.crowdycpp or not args.ref:
        parser.error("vendoring needs --crowdycpp and --ref")
    return vendor(args.crowdycpp, args.ref)


if __name__ == "__main__":
    raise SystemExit(main())
