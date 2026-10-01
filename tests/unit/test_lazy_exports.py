"""The package root resolves its names lazily; type checkers read a separate block.

``_LAZY`` is what runs, the ``if TYPE_CHECKING:`` block is what mypy and IDEs see. A name in
one and not the other is either missing at runtime or typed as ``Any`` (module
``__getattr__``), and an import there without ``X as X`` is not an explicit re-export, so a
strict user's ``from crowdypy import AsyncCrowdyClient`` would be refused.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import crowdypy

INIT = Path(crowdypy.__file__)


def type_checking_imports() -> dict[str, str]:
    tree = ast.parse(INIT.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and getattr(node.test, "id", None) == "TYPE_CHECKING":
            for stmt in node.body:
                assert isinstance(stmt, ast.ImportFrom), ast.unparse(stmt)
                for alias in stmt.names:
                    assert alias.asname == alias.name, f"{alias.name} is not re-exported as itself"
                    assert stmt.module is not None
                    found[alias.name] = stmt.module
    return found


def test_type_checking_block_mirrors_the_lazy_map() -> None:
    assert type_checking_imports() == crowdypy._LAZY


def test_every_lazy_name_resolves_from_the_module_it_names() -> None:
    for name, module in crowdypy._LAZY.items():
        assert getattr(crowdypy, name) is getattr(importlib.import_module(module), name), name


def test_all_lists_the_lazy_names_and_the_eager_ones() -> None:
    eager = {
        "CROWDY_DEFAULT_HOST",
        "CROWDY_DEFAULT_HTTP_ORIGIN",
        "CROWDY_DEFAULT_TIER",
        "CROWDY_DEFAULT_WS_ORIGIN",
        "__version__",
    }
    assert set(crowdypy.__all__) == eager | set(crowdypy._LAZY)
    assert len(crowdypy.__all__) == len(set(crowdypy.__all__))


def test_submodules_resolve() -> None:
    for name in crowdypy._SUBMODULES:
        assert getattr(crowdypy, name) is importlib.import_module(f"crowdypy.{name}")
