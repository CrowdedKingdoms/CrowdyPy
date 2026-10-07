#!/usr/bin/env python3
"""Open each built wheel and check what a user would actually receive.

    python scripts/ci/verify_artifacts.py <tier>|--from-ref dist/*.whl

- the ``_default_origin.py`` INSIDE the wheel names the expected tier (the branch's, or the
  release tag's); a wheel is immutable on PyPI, so this is the last place to catch it;
- the wheel carries no shared libcrypto: the native core links its own statically, and a
  repair tool that had to vendor one means the static link silently failed.
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from assert_default_origin import parse, problems_for, resolve_want

LIBCRYPTO = re.compile(r"(^|/)(lib)?(crypto|ssl)[^/]*\.(so(\.\d+)*|dylib|dll)$", re.I)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    want, source = resolve_want(argv[0])
    failed = False
    for wheel in argv[1:]:
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
            origin = next((n for n in names if n.endswith("crowdypy/_default_origin.py")), None)
            if origin is None:
                print(f"::error::{wheel} has no crowdypy/_default_origin.py", file=sys.stderr)
                failed = True
                continue
            declared = parse(zf.read(origin).decode())
            if want is not None:
                for problem in problems_for(declared, want):
                    print(f"::error::{Path(wheel).name}: {problem}", file=sys.stderr)
                    failed = True
            vendored = [n for n in names if LIBCRYPTO.search(n)]
            if vendored:
                print(
                    f"::error::{Path(wheel).name} bundles a shared libcrypto: {vendored}",
                    file=sys.stderr,
                )
                failed = True
        print(f"{Path(wheel).name}: tier={declared['tier']} host={declared['host']}")
    if want is None:
        print(f"tier comparison skipped ({source} carries no tier)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
