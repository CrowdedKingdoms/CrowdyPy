#!/usr/bin/env python3
"""CrowdyPy's parity gate against CrowdyJS at the commit pyproject.toml pins.

    python tools/parity/parity.py --crowdyjs ../CrowdyJS --write docs/parity-matrix.md
    python tools/parity/parity.py --crowdyjs ../CrowdyJS --check docs/parity-matrix.md
    python tools/parity/parity.py --crowdyjs ../CrowdyJS --check docs/parity-matrix.md --strict

CrowdyJS is read from git objects at the pinned commit, so the checkout may sit on any
branch as long as it holds that commit. Four surfaces are compared:

1. the schema: CrowdyPy's ``schema.gql`` must be CrowdyJS's, byte for byte;
2. GraphQL root fields: a field is covered when CrowdyPy code SENDS an operation selecting
   it (a generated operation it references, or an inline document), not merely because a
   document for it was copied;
3. classes and methods: CrowdyJS's public classes in the directories CrowdyCPP's gate
   reads, against CrowdyPy's classes (read with ``ast``), names compared case- and
   underscore-insensitively (``mintAppToken`` == ``mint_app_token``);
4. exported functions and values of CrowdyJS's package entry point.

Every difference must carry a reviewed classification in ``classifications.toml``:
``portable-gap`` (missing work, shown as such), ``native-equivalent`` (the same contract
through CrowdyPy's native architecture) or ``browser-exclusion`` (browser/UI/worker-only).
An unclassified difference or a stale classification fails the gate; ``--strict``
additionally fails on any remaining portable gap, which is the release gate once the
gaps reach zero. The matrix records the gate mode, so a matrix written without
``--strict`` never satisfies a ``--strict`` check (and vice versa).
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "src" / "crowdypy"
CLASSIFICATIONS = Path(__file__).resolve().parent / "classifications.toml"
KINDS = {
    "portable-gap": "portable gap",
    "native-equivalent": "native equivalent",
    "browser-exclusion": "browser exclusion",
}

# The CrowdyJS sources whose public classes form the compared surface: the set
# CrowdyCPP's tools/parity/parity.mjs reads, plus the client entry points and the grid
# scope (crowdy-client.ts, client.ts, grid-scope.ts), which CrowdyCPP's gate does not compare.
JS_CLASS_DIRS = ("src/domains/", "src/kit/", "src/stores/")
JS_CLASS_FILES = (
    "src/crowdy-client.ts",
    "src/client.ts",
    "src/grid-scope.ts",
    "src/world.ts",
    "src/crowdy-studio/controller.ts",
    "src/crowdy-studio/dom-shell.ts",
    "src/crowdy-studio/layout.ts",
    "src/crowdy-studio/github/transport.ts",
    "src/live-coding/vfs.ts",
    "src/live-coding/worker-transport.ts",
    "src/crowdy-studio/embed/dock.ts",
    "src/crowdy-studio/embed/hud-layer.ts",
    "src/crowdy-studio/embed/panel.ts",
)
SKIP_PY = ("_sync/", "_generated/")
TS_NOT_METHODS = {"constructor", "if", "for", "return", "switch", "while", "catch"}


def norm(name: str) -> str:
    return name.replace("_", "").lower()


@dataclass
class Report:
    rows: list[tuple[str, str, str]] = field(default_factory=list)  # (section, key, status)
    unclassified: list[str] = field(default_factory=list)
    used_keys: set[str] = field(default_factory=set)
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))


# ----------------------------------------------------------------------- CrowdyJS side


def git_files(repo: Path, commit: str, keep: Iterable[str]) -> dict[str, str]:
    wanted = list(keep)
    listing = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    out: dict[str, str] = {}
    for path in listing:
        if any(
            path == w
            or (w.endswith("/") and path.startswith(w) and path.count("/") == w.count("/"))
            for w in wanted
        ):
            out[path] = subprocess.run(
                ["git", "-C", str(repo), "show", f"{commit}:{path}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
    return out


def balanced_block(text: str, open_index: int) -> str:
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i]
    return text[open_index + 1 :]


def ts_classes(sources: dict[str, str]) -> dict[str, set[str]]:
    classes: dict[str, set[str]] = {}
    method_re = re.compile(
        r"(?:^|\n)  (?!private\b|protected\b)(?:(?:public|static|async|readonly|get|set)\s+)*"
        r"([A-Za-z_]\w*)\s*(?:<[^;\n{]+>)?\s*[(=<]"
    )
    for text in sources.values():
        for match in re.finditer(r"export\s+class\s+(\w+)[^{]*\{", text):
            body = balanced_block(text, match.end() - 1)
            methods = {m for m in method_re.findall(body) if m not in TS_NOT_METHODS}
            for group in re.finditer(r"readonly\s+(\w+)\s*=\s*\{([\s\S]*?)\n  \};", body):
                methods.discard(group.group(1))
                for member in re.findall(
                    r"(?:^|\n)    ([A-Za-z_]\w*)\s*:\s*(?:async\s*)?\(", group.group(2)
                ):
                    methods.add(f"{group.group(1)}.{member}")
            if methods:
                classes.setdefault(match.group(1), set()).update(methods)
    return classes


def ts_exports(index: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r"export\s+(?!type\b)\{([^}]*)\}\s+from\s+'([^']+)'", index):
        if "generated" in match.group(2):
            continue
        for part in match.group(1).split(","):
            part = part.strip()
            if not part or part.startswith("type "):
                continue
            names.add(part.split(" as ")[-1].strip())
    for match in re.finditer(r"export\s+(?:async\s+)?(?:function|const|class)\s+(\w+)", index):
        names.add(match.group(1))
    return names


# ----------------------------------------------------------------------- CrowdyPy side


def py_modules() -> dict[str, ast.Module]:
    out: dict[str, ast.Module] = {}
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).as_posix()
        if rel.startswith(SKIP_PY):
            continue
        out[rel] = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    return out


def _public_methods(node: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not item.name.startswith(
            "_"
        ):
            names.add(item.name)
    return names


def py_classes(modules: dict[str, ast.Module]) -> dict[str, set[str]]:
    defined: dict[str, ast.ClassDef] = {}
    for module in modules.values():
        for node in ast.walk(module):
            if isinstance(node, ast.ClassDef):
                defined.setdefault(node.name, node)
    classes: dict[str, set[str]] = {}
    for name, node in defined.items():
        methods = _public_methods(node)
        # Method groups (CrowdyJS `readonly party = { create: ... }`): an attribute set in
        # __init__ to an instance of another class contributes "attribute.method".
        for item in node.body:
            if (
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == "__init__"
            ):
                for stmt in ast.walk(item):
                    if (
                        isinstance(stmt, ast.Assign)
                        and len(stmt.targets) == 1
                        and isinstance(stmt.targets[0], ast.Attribute)
                        and isinstance(stmt.targets[0].value, ast.Name)
                        and stmt.targets[0].value.id == "self"
                        and isinstance(stmt.value, ast.Call)
                        and isinstance(stmt.value.func, ast.Name)
                        and stmt.value.func.id in defined
                    ):
                        attr = stmt.targets[0].attr
                        if attr.startswith("_"):
                            continue
                        for member in _public_methods(defined[stmt.value.func.id]):
                            methods.add(f"{attr}.{member}")
        classes[name] = methods
    return classes


def py_public_names(modules: dict[str, ast.Module]) -> set[str]:
    names: set[str] = set()
    for module in modules.values():
        for node in module.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return {n for n in names if not n.startswith("_")}


def generated_roots() -> dict[str, tuple[str, tuple[str, ...]]]:
    """``CONST -> (kind, root fields)`` from the generated operations module."""
    tree = ast.parse((PKG / "_generated" / "operations.py").read_text(encoding="utf-8"))
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            kwargs = {k.arg: k.value for k in node.value.keywords}
            if "root_fields" in kwargs and isinstance(node.targets[0], ast.Name):
                kind = ast.literal_eval(kwargs["kind"])
                out[node.targets[0].id] = (kind, tuple(ast.literal_eval(kwargs["root_fields"])))
    return out


def used_root_fields(modules: dict[str, ast.Module]) -> set[tuple[str, str]]:
    roots = generated_roots()
    used: set[tuple[str, str]] = set()
    for module in modules.values():
        for node in ast.walk(module):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ops"
                and node.attr in roots
            ):
                kind, fields = roots[node.attr]
                used.update((kind, f) for f in fields)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "inline_operation"
                and len(node.args) >= 3
            ):
                try:
                    kind = ast.literal_eval(node.args[1])
                    root = ast.literal_eval(node.args[2])
                except ValueError:
                    continue
                used.add((kind, root))
    return used


def schema_roots() -> dict[tuple[str, str], bool]:
    from graphql import build_schema

    schema = build_schema((ROOT / "schema.gql").read_text(encoding="utf-8"))
    out: dict[tuple[str, str], bool] = {}
    for kind, gql_type in (
        ("query", schema.query_type),
        ("mutation", schema.mutation_type),
        ("subscription", schema.subscription_type),
    ):
        if gql_type is None:
            continue
        for name, definition in gql_type.fields.items():
            out[(kind, name)] = definition.deprecation_reason is not None
    return out


# ------------------------------------------------------------------------- the gate


def classify(report: Report, table: dict[str, dict[str, str]], section: str, key: str) -> str:
    entry = table.get(key)
    if entry is None:
        report.unclassified.append(f"{section}: {key}")
        return "UNCLASSIFIED"
    kind = entry.get("kind", "")
    if kind not in KINDS:
        report.unclassified.append(f"{section}: {key} (unknown kind {kind!r})")
        return "UNCLASSIFIED"
    report.used_keys.add(f"{section}:{key}")
    report.counts[kind] += 1
    return f"{KINDS[kind]} — {entry.get('reason', '').strip()}"


def run(crowdyjs: Path) -> tuple[str, Report, list[str]]:
    tool = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["crowdypy"]
    pin = tool["crowdyjs"]
    classifications = tomllib.loads(CLASSIFICATIONS.read_text(encoding="utf-8"))
    report = Report()
    problems: list[str] = []

    keep = [*JS_CLASS_DIRS, *JS_CLASS_FILES, "src/index.ts", "schema.gql", "package.json"]
    js = git_files(crowdyjs, pin["commit"], keep)
    js_version = re.search(r'"version"\s*:\s*"([^"]+)"', js.get("package.json", "")).group(1)  # type: ignore[union-attr]
    if js_version != pin["version"]:
        problems.append(
            f"CrowdyJS {pin['commit'][:12]} is {js_version}; the pin says {pin['version']}"
        )

    schema_same = js.get("schema.gql", "") == (ROOT / "schema.gql").read_text(encoding="utf-8")
    if not schema_same:
        problems.append(
            "schema.gql differs from CrowdyJS's at the pinned commit (scripts/schema_sync.py)"
        )

    modules = py_modules()
    used = used_root_fields(modules)
    roots = schema_roots()
    root_table = classifications.get("roots", {})
    for (kind, name), deprecated in sorted(roots.items()):
        key = f"{kind}.{name}"
        if (kind, name) in used:
            status = "covered"
        elif deprecated:
            status = "deprecated"
        else:
            status = classify(report, root_table, "roots", key)
        report.rows.append(("roots", key, status))

    ts_sources = {p: t for p, t in js.items() if p.endswith(".ts") and p != "src/index.ts"}
    js_classes = ts_classes(ts_sources)
    py = py_classes(modules)
    class_map = classifications.get("class-map", {})
    class_table = classifications.get("classes", {})
    method_table = classifications.get("methods", {})
    for js_class in sorted(js_classes):
        py_class = class_map.get(js_class, js_class)
        if py_class not in py:
            status = classify(report, class_table, "classes", js_class)
            report.rows.append(("classes", f"{js_class} -> (none)", status))
            continue
        report.rows.append(("classes", f"{js_class} -> {py_class}", "covered"))
        available = {norm(m) for m in py[py_class]}
        for method in sorted(js_classes[js_class]):
            key = f"{js_class}.{method}"
            if norm(method) in available:
                status = "covered"
            else:
                status = classify(report, method_table, "methods", key)
            report.rows.append(("methods", key, status))

    exports = ts_exports(js.get("src/index.ts", ""))
    python_names = {norm(n) for n in py_public_names(modules)}
    export_table = classifications.get("exports", {})
    for name in sorted(exports):
        if name in js_classes:
            continue  # judged as a class above
        status = (
            "covered"
            if norm(name) in python_names
            else classify(report, export_table, "exports", name)
        )
        report.rows.append(("exports", name, status))

    problems.extend(
        f"stale classification: [{section}] {key} no longer describes a difference"
        for section in ("roots", "classes", "methods", "exports")
        for key in sorted(classifications.get(section, {}))
        if f"{section}:{key}" not in report.used_keys
    )

    header = (
        "# CrowdyPy parity matrix\n\n"
        "Generated by `tools/parity/parity.py`; do not edit by hand. Classifications live in "
        "`tools/parity/classifications.toml`.\n\n"
        "## Target\n\n"
        f"- CrowdyJS: `{pin['version']}` at `{pin['commit']}`\n"
        f"- CrowdyCPP (native core): `{tool['crowdycpp']['version']}` at `{tool['crowdycpp']['commit']}`\n"
        f"- schema.gql identical to CrowdyJS's: {'yes' if schema_same else 'NO'}\n"
    )
    return header, report, problems


def render(header: str, report: Report, strict: bool) -> str:
    mode = (
        "strict portable parity (every portable gap is release-blocking)"
        if strict
        else "baseline (portable gaps are listed as missing work; new differences and stale classifications fail)"
    )
    out = [header.rstrip("\n"), f"- Gate mode: {mode}", "", "## Summary", ""]
    covered = sum(1 for _, _, s in report.rows if s == "covered")
    out.append(f"- Covered: {covered}")
    for kind, label in KINDS.items():
        out.append(f"- {label.capitalize()}: {report.counts[kind]}")
    out.append(f"- Deprecated roots: {sum(1 for _, _, s in report.rows if s == 'deprecated')}")
    out += [f"- Unclassified: {len(report.unclassified)}", ""]
    titles = {
        "roots": ("GraphQL root fields", "Field"),
        "classes": ("Classes", "CrowdyJS -> CrowdyPy"),
        "methods": ("Methods", "CrowdyJS method"),
        "exports": ("Package exports", "CrowdyJS export"),
    }
    for section, (title, column) in titles.items():
        rows = [(k, s) for sec, k, s in report.rows if sec == section]
        out += [f"## {title}", "", f"| {column} | Status |", "|---|---|"]
        out.extend(f"| `{k}` | {s} |" for k, s in rows)
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--crowdyjs",
        type=Path,
        required=True,
        help="a CrowdyJS git checkout holding the pinned commit",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", type=Path)
    group.add_argument("--check", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)

    header, report, problems = run(args.crowdyjs)
    text = render(header, report, args.strict)
    if args.write:
        args.write.write_text(text, encoding="utf-8")
        print(f"parity: wrote {args.write}")
    else:
        current = args.check.read_text(encoding="utf-8") if args.check.exists() else None
        if current != text:
            problems.append(
                f"{args.check} is not what this run generates (stale, hand-edited, or written in "
                "the other gate mode); regenerate with --write and the same flags"
            )
    for item in report.unclassified:
        problems.append(f"unclassified difference: {item}")
    if args.strict and report.counts["portable-gap"]:
        problems.append(f"--strict: {report.counts['portable-gap']} portable gap(s) remain")
    if problems:
        for problem in problems:
            print(f"parity: {problem}", file=sys.stderr)
        return 1
    print(
        "parity: OK -- "
        + ", ".join(f"{report.counts[k]} {v}" for k, v in KINDS.items())
        + f", {sum(1 for _, _, s in report.rows if s == 'covered')} covered"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
