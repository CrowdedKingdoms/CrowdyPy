"""Helpers shared by the scripts that own pinned, re-derivable artifacts."""

from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def resolve_commit(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()


def read_tree(repo: Path, commit: str, keep: Callable[[str], bool]) -> dict[str, bytes]:
    """Every blob at ``commit`` whose path ``keep`` accepts, read from git objects."""
    listing = git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    blobs: list[tuple[str, str]] = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, kind, sha = meta.decode().split()
        name = path.decode()
        if kind != "blob" or not keep(name):
            continue
        if mode == "120000":
            raise SystemExit(f"{name} is a symlink at {commit}; refusing to copy it")
        blobs.append((name, sha))
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input=b"".join(f"{sha}\n".encode() for _, sha in blobs),
        check=True,
        capture_output=True,
    )
    out = proc.stdout
    files: dict[str, bytes] = {}
    pos = 0
    for name, sha in blobs:
        header_end = out.index(b"\n", pos)
        got_sha, _, size = out[pos:header_end].decode().split()
        if got_sha != sha:
            raise SystemExit(f"git cat-file returned {got_sha} for {sha}")
        start = header_end + 1
        files[name] = out[start : start + int(size)]
        pos = start + int(size) + 1
    return files


def tree_sha256(files: dict[str, bytes]) -> str:
    """sha256 over ``<sha256(content)>  <path>`` lines sorted by path (sha256sum's format)."""
    lines = "".join(f"{hashlib.sha256(files[p]).hexdigest()}  {p}\n" for p in sorted(files))
    return hashlib.sha256(lines.encode()).hexdigest()


def read_dir(directory: Path, skip: frozenset[str] = frozenset()) -> dict[str, bytes]:
    if not directory.is_dir():
        return {}
    files: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            rel = path.relative_to(directory).as_posix()
            if rel not in skip:
                files[rel] = path.read_bytes()
    return files


def load_tool_table() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["crowdypy"]


def rewrite_table(text: str, table: str, values: dict[str, str]) -> str:
    """Replace the body of ``[table]`` in pyproject.toml; everything else stays byte-identical."""
    body = "".join(f'{key} = "{value}"\n' for key, value in values.items())
    pattern = re.compile(rf"(^\[{re.escape(table)}\]\n)(.*?)(?=^\[|^#|\Z)", re.M | re.S)
    if not pattern.search(text):
        raise SystemExit(f"pyproject.toml has no [{table}] table")
    return pattern.sub(lambda m: m.group(1) + body + "\n", text, count=1)


def write_tool_table(table: str, values: dict[str, str]) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    PYPROJECT.write_text(rewrite_table(text, table, values), encoding="utf-8")
