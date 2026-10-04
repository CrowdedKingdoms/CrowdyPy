#!/usr/bin/env python3
"""Public content policy: nothing CrowdyPy publishes may name a private repository or
internal infrastructure.

    python scripts/ci/check_content_policy.py                     # the repository tree
    python scripts/ci/check_content_policy.py dist/*.whl dist/*.tar.gz   # built artifacts

Two corpora, because the artifact is what a user receives. CrowdyJS learned this the hard
way: a private name reached dozens of published versions inside a generated file that no
git-backed search could see, because the published directory was gitignored. So the tree
is walked from disk (never through an ignore file), and every wheel and sdist is opened
and every member read. The run prints how much it read, so a clean corpus and an unopened
one never print the same line. The vendored CrowdyCPP tree is in the corpus too: it ships.
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The same terms CrowdyJS's and CrowdyCPP's gates refuse.
DENYLIST = (
    "cks-udp-api",
    "cks-michael-root",
    "cks-project-root",
    "MessageType.hpp",
    "wire-protocol-reference",
    "P2P_SECRET",
    "P2P_TOKEN",
    "CHANNEL_MUTATION",
    "peer port",
    "port 9081",
    ":9081",
    "buddydev",
    "BUDDY_BUILDER",
    "dev-run-buddy",
)
SKIP_DIRS = {
    ".git": "object store; holds every historical revision by construction",
    ".venv": "the developer's virtualenv; third-party code",
    "build": "CMake build trees, regenerated per build",
    "node_modules": "third-party dependencies",
    ".mypy_cache": "tool cache",
    ".ruff_cache": "tool cache",
    ".pytest_cache": "tool cache",
    ".hypothesis": "tool cache",
    "__pycache__": "bytecode",
    "dist": "built artifacts; pass them as arguments to scan them",
    "wheelhouse": "built artifacts; pass them as arguments to scan them",
}
SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".so",
    ".pyd",
    ".dylib",
    ".a",
    ".o",
}
SELF = "scripts/ci/check_content_policy.py"


def tree() -> Iterator[tuple[str, bytes]]:
    stack = [ROOT]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir()):
            rel = entry.relative_to(ROOT).as_posix()
            if entry.is_dir():
                if entry.name not in SKIP_DIRS:
                    stack.append(entry)
            elif entry.suffix not in SKIP_SUFFIXES and rel != SELF:
                yield rel, entry.read_bytes()


def artifact(path: Path) -> Iterator[tuple[str, bytes]]:
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.endswith("/"):
                    yield f"{path.name}:{name}", zf.read(name)
    elif path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile():
                    handle = tf.extractfile(member)
                    if handle is not None:
                        yield f"{path.name}:{member.name}", handle.read()
    else:
        raise SystemExit(f"check_content_policy: {path} is neither a wheel nor an sdist")


def scan(corpus: Iterator[tuple[str, bytes]]) -> tuple[int, list[str]]:
    count = 0
    hits: list[str] = []
    needles = [(term, term.encode()) for term in DENYLIST]
    for where, data in corpus:
        count += 1
        if where.endswith(SELF):
            continue
        for term, needle in needles:
            if needle in data:
                line = data[: data.index(needle)].count(b"\n") + 1
                hits.append(f"{where}:{line}: {term}")
    return count, hits


def main(argv: list[str]) -> int:
    corpora = [(a, artifact(Path(a))) for a in argv] if argv else [("tree", tree())]
    failed = False
    for label, corpus in corpora:
        count, hits = scan(corpus)
        if count == 0:
            print(
                f"check_content_policy: {label}: read 0 files -- a broken corpus, not a clean one",
                file=sys.stderr,
            )
            failed = True
            continue
        for hit in hits:
            print(f"DENYLISTED {hit}", file=sys.stderr)
        failed |= bool(hits)
        print(
            f"check_content_policy: {label}: {count} file(s) read, {len(hits)} denylisted term(s)"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
