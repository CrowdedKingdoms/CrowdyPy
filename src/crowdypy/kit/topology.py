"""Composing and deploying an app's tree of starter hubs. CrowdyJS ``src/kit/topology.ts``."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from crowdypy.errors import CrowdyError

if TYPE_CHECKING:
    from crowdypy.domains.exec import ExecAPI, ExecBuild, ExecStarterPack

__all__ = [
    "KIT_BUILD_MAX_CRATES",
    "KIT_NODE_API_SCOPES",
    "KitDeployment",
    "KitTopology",
    "KitTopologyPlan",
    "kit_manifest_problems",
]

#: The crates one build takes at most (the game API's ``execBuild``).
KIT_BUILD_MAX_CRATES = 16

#: The node API scopes a manifest may declare (ck-exec ``bounds::NODE_API_SCOPES``).
KIT_NODE_API_SCOPES: tuple[str, ...] = (
    "players.read",
    "players.write",
    "world.read",
    "world.write",
    "grids.read",
    "permissions.write",
    "grids.write",
    "access.read",
    "access.write",
)

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def kit_manifest_problems(manifest: Mapping[str, Any]) -> list[str]:
    """What the execution manager would refuse in ``manifest`` (``{root, types}``), in its words
    (ck-exec ``Manifest::problems``, less the digests, which the build fills in): the root is a
    hub without a parent, every other type has a defined parent, ``calls`` name defined types or
    ``*``, replicas are for spokes, scopes are known. Empty when it deploys."""
    out: list[str] = []
    types: Mapping[str, Mapping[str, Any]] = manifest.get("types") or {}
    root_name = manifest.get("root", "")
    root = types.get(root_name)
    if root is None:
        out.append(f"root type `{root_name}` is not defined")
    elif root.get("kind") != "hub":
        out.append("the root type must be a hub")
    elif root.get("parent"):
        out.append("the root type has no parent")
    for name, t in types.items():
        if not _NAME.match(name):
            out.append(f"type name `{name}` must be 1-64 letters, digits, `-` or `_`")
        if name != root_name:
            parent = t.get("parent")
            if not parent:
                out.append(f"`{name}` needs a parent")
            elif parent not in types:
                out.append(f"`{name}`: parent `{parent}` is not defined")
        out.extend(
            f"`{name}` calls `{c}`, which is not defined"
            for c in t.get("calls") or []
            if c != "*" and c not in types
        )
        if t.get("kind") == "hub" and any(
            k in t for k in ("replicas", "max_replicas", "concurrency")
        ):
            out.append(f"`{name}`: replicas, max_replicas and concurrency are for spokes")
        out.extend(
            f"`{name}`: unknown node API scope `{s}` (allowed: {', '.join(KIT_NODE_API_SCOPES)})"
            for s in t.get("scopes") or []
            if s not in KIT_NODE_API_SCOPES
        )
    return out


@dataclass(frozen=True, slots=True)
class KitTopologyPlan:
    """What :meth:`KitTopology.plan` composes: a manifest, and the starter crates it builds."""

    root: str
    types: dict[str, dict[str, Any]]
    #: The starter crates the types name, each built once.
    crates: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class KitDeployment:
    version: int
    build_id: str


class KitTopology:
    """An app's tree of kit hubs, composed from the starter packs (``exec.starters``): pick the
    node types a game uses, and :meth:`plan` adds the hubs they hang under and the ones they
    call and checks the tree; :meth:`deploy` builds the crates on the platform and deploys them.
    For the app's developers (the org's ``manage_compute``), from an identity session."""

    def __init__(self, app_id: str, exec: ExecAPI | None) -> None:
        self.app_id = app_id
        self._exec = exec
        self._pack: ExecStarterPack | None = None

    def _api(self) -> ExecAPI:
        if self._exec is None:
            raise CrowdyError("the kit has no exec client")
        return self._exec

    async def pack(self) -> ExecStarterPack:
        """The starter packs this tier's toolchain carries (fetched once)."""
        if self._pack is None:
            self._pack = await self._api().starters(self.app_id)
        return self._pack

    async def types(self) -> list[str]:
        """The node types the packs offer."""
        return list((await self.pack()).manifest.get("types", {}))

    async def plan(
        self, select: Sequence[str], *, overrides: Mapping[str, Mapping[str, Any]] | None = None
    ) -> KitTopologyPlan:
        """A manifest with the types in ``select``, the types they hang under up to the root, and
        the types they call (by name, not ``*``), with ``overrides`` merged into each named type
        (a seed, limits, scopes, ``client``, ...). Raises :class:`~crowdypy.errors.CrowdyError`
        for a type the packs do not have, a tree the manager would refuse, or more crates than
        one build takes."""
        pack = await self.pack()
        everything: dict[str, dict[str, Any]] = pack.manifest.get("types", {})
        root: str = pack.manifest.get("root", "")
        chosen: list[str] = []

        def add(name: str) -> None:
            if name in chosen:
                return
            t = everything.get(name)
            if t is None:
                raise CrowdyError(
                    f"the starter packs have no node type '{name}' (they have {', '.join(everything)})"
                )
            chosen.append(name)
            if t.get("parent"):
                add(t["parent"])
            for c in t.get("calls") or []:
                if c != "*":
                    add(c)

        add(root)
        for name in select:
            add(name)
        types: dict[str, dict[str, Any]] = {}
        for name in chosen:
            merged = {**everything[name], **dict((overrides or {}).get(name, {}))}
            # A type calls only what the plan holds.
            if "calls" in merged:
                merged["calls"] = [c for c in merged["calls"] if c == "*" or c in chosen]
            types[name] = merged
        problems = kit_manifest_problems({"root": root, "types": types})
        if problems:
            raise CrowdyError(f"the composed manifest would be refused: {'; '.join(problems)}")
        crates: list[str] = []
        for t in types.values():
            crate = t.get("crate")
            if isinstance(crate, str) and crate not in crates:
                crates.append(crate)
        if len(crates) > KIT_BUILD_MAX_CRATES:
            raise CrowdyError(
                f"{len(crates)} crates are more than one build takes ({KIT_BUILD_MAX_CRATES})"
            )
        return KitTopologyPlan(root, types, crates)

    async def deploy(self, plan: KitTopologyPlan, *, timeout_ms: float = 600_000) -> KitDeployment:
        """Builds the plan's crates on the platform, waits, and deploys the manifest with the
        build. Raises :class:`~crowdypy.errors.CrowdyError` when the build fails (its log in the
        message)."""
        pack = await self.pack()
        crates: list[dict[str, Any]] = []
        for name in plan.crates:
            starter = next((s for s in pack.starters if s.crate == name), None)
            if starter is None:
                raise CrowdyError(f"the starter packs have no crate '{name}'")
            crates.append({"name": name, "files": {f.path: f.content for f in starter.files}})
        api = self._api()
        queued = await api.build(self.app_id, crates)
        done: ExecBuild = await api.wait_for_build(
            self.app_id, queued.build_id, timeout_ms=timeout_ms
        )
        if done.status != "succeeded":
            raise CrowdyError(f"build {done.build_id} {done.status}: {(done.log or '')[-2000:]}")
        deployed = await api.deploy(self.app_id, plan.root, plan.types, done.build_id)
        return KitDeployment(deployed.version, done.build_id)
