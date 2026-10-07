"""The version lives in six places, and they must agree.

``pyproject.toml`` is the source; ``_version.py`` is what ``crowdypy.__version__`` reports;
``uv.lock`` records the project; the compatibility page, the README's release line and the
migration notes are what a reader trusts. A release workflow rewrites only the PUBLISHED
version (``X.Y.Z.devN`` / ``X.Y.ZrcN``) in the build workspace; the branch always carries
the bare ``X.Y.Z``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not (ROOT / "src" / "crowdypy").is_dir(),
    reason="a source-tree gate: wheel tests run without src/ and on a stamped pyproject.toml",
)


def _project_version() -> str:
    return str(
        tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    )


def _sites() -> dict[str, str | None]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = next((p["version"] for p in lock.get("package", []) if p["name"] == "crowdypy"), None)
    version_py = re.search(
        r'__version__ = "([^"]+)"', (ROOT / "src/crowdypy/_version.py").read_text(encoding="utf-8")
    )
    compat = re.search(
        r"CrowdyPy `(\d+\.\d+\.\d+)`", (ROOT / "docs/compatibility.md").read_text(encoding="utf-8")
    )
    readme = re.search(
        r"^\*\*v(\d+\.\d+\.\d+)\b", (ROOT / "README.md").read_text(encoding="utf-8"), re.M
    )
    migration = re.search(
        r"^## (\d+\.\d+\.\d+)\b", (ROOT / "MIGRATION.md").read_text(encoding="utf-8"), re.M
    )
    return {
        "src/crowdypy/_version.py": version_py.group(1) if version_py else None,
        "uv.lock": locked,
        "docs/compatibility.md": compat.group(1) if compat else None,
        "README.md (first **vX.Y.Z line)": readme.group(1) if readme else None,
        "MIGRATION.md (first ## X.Y.Z heading)": migration.group(1) if migration else None,
    }


def test_the_project_version_is_plain_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", _project_version()), "the branch carries a bare X.Y.Z"


def test_every_version_site_agrees() -> None:
    want = _project_version()
    wrong = {site: got for site, got in _sites().items() if got != want}
    assert wrong == {}, f"pyproject.toml says {want}; these disagree: {wrong}"
