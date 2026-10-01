"""New Studio projects: a SERVER target starts from ck-exec's mod starter, a CLIENT target
from a crowdy-client-sdk half (CrowdyJS's ``createCrowdyStudioStarterProject``)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from crowdypy.domains.crowdy_studio import (
    CrowdyStudioProjectFile,
    CrowdyStudioProjectKind,
    CrowdyStudioProjectMetadata,
)
from crowdypy.studio.models import project_targets

__all__ = ["CrowdyStudioStarterProject", "create_crowdy_studio_starter_project"]

CLIENT_SDK_VERSION = "0.1.0"
_MOD_BASE_MAX = 48 - len("-server")

_CLIENT_CARGO = """[package]
name = "{name}"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]

# How often visitors' browsers call tick (clamped 16–1000 ms).
# 1000 = HUD/text. 50 = physics minigames (pool). 16 = shooters, if a tick stays cheap.
[package.metadata.crowdy]
tick_interval_ms = 1000

[dependencies]
crowdy-client-sdk = "{sdk}"
serde_json = "1"
"""

_CLIENT_LIB = 'use crowdy_client_sdk as crowdy;\n\nfn init() {\n    // Runs once in each visitor\'s browser, after they consent to this CLIENT half\n    // (or trust you) and while they stand in this grid.\n    crowdy::log(1, "ready");\n}\n\nfn tick(_dt_ms: u32) {\n    // Every tick_interval_ms (Cargo.toml). The state blob lasts as long as the page\'s worker.\n    let ticks = u32::from_le_bytes(crowdy::state_get().try_into().unwrap_or([0; 4])).wrapping_add(1);\n    crowdy::state_set(&ticks.to_le_bytes());\n    // Host calls cross the page\'s broker as the visiting player, inside this grid: the HUD and\n    // overlay, chunk and actor reads, voxel writes, spatial and channel sends, grid events.\n    // Type "crowdy::api::" for the rest. The page\'s DOM and tokens are never reachable.\n    let _ = crowdy::api::hud_set(serde_json::json!({ "text": format!("Ticks here: {ticks}") }));\n    //\n    // Mouse (holodeck canvas only; Studio chrome is omitted). Drain every tick:\n    //   let data = crowdy::api::pointer_clicks().unwrap_or(serde_json::json!({}));\n    // data["clicks"] = [{ "t": "down"|"up", "button": 0, "atMs", "heldMs", "nx", "ny" }]\n}\n\nfn invoke(payload: &[u8]) -> Vec<u8> {\n    payload.to_vec()\n}\n\nfn event(_payload: &[u8]) {\n    // Grid events from the other CLIENT halves on this page (crowdy::api::emit_event).\n}\n\ncrowdy::register_module!(init: init, tick: tick, invoke: invoke, event: event);\n'


@dataclass(frozen=True, slots=True)
class CrowdyStudioStarterProject:
    """What :meth:`CrowdyStudioAPI.create_project` takes for a new project."""

    app_id: str
    grid_id: str
    kind: CrowdyStudioProjectKind
    metadata: CrowdyStudioProjectMetadata
    files: list[CrowdyStudioProjectFile]


def create_crowdy_studio_starter_project(
    *,
    app_id: str,
    grid_id: str,
    name: str,
    kind: CrowdyStudioProjectKind,
    description: str | None = None,
    mod_starter: Any = None,
) -> CrowdyStudioStarterProject:
    """A new project's metadata and files. ``mod_starter`` (``client.exec.mod_starter()``,
    or anything with ``files``) is required for a SERVER target; module names derive from
    ``name`` (``<base>-server``, ``<base>-client``)."""
    targets = project_targets(kind)
    if "SERVER" in targets and mod_starter is None:
        raise ValueError("A SERVER target starts from the mod starter (client.exec.mod_starter)")
    base = _mod_module_base(name)
    metadata = CrowdyStudioProjectMetadata(
        name=name.strip() or "Untitled mod",
        description=description.strip() if description and description.strip() else None,
        server_module_name=f"{base}-server" if "SERVER" in targets else None,
        client_module_name=f"{base}-client" if "CLIENT" in targets else None,
        pairing_preference="NONE",
    )
    files: list[CrowdyStudioProjectFile] = []
    for target in targets:
        if target == "SERVER":
            files.extend(_mod_starter_files(mod_starter, f"{base}-server"))
        else:
            files.extend(_client_half_files(f"{base}-client"))
    return CrowdyStudioStarterProject(
        app_id=app_id, grid_id=grid_id, kind=kind, metadata=metadata, files=files
    )


def _client_half_files(name: str) -> list[CrowdyStudioProjectFile]:
    return [
        CrowdyStudioProjectFile(
            target="CLIENT",
            path="Cargo.toml",
            content=_CLIENT_CARGO.format(name=name, sdk=CLIENT_SDK_VERSION),
        ),
        CrowdyStudioProjectFile(target="CLIENT", path="src/lib.rs", content=_CLIENT_LIB),
    ]


def _starter_files(starter: Any) -> Sequence[Any]:
    files = (
        starter.get("files") if isinstance(starter, Mapping) else getattr(starter, "files", None)
    )
    return files or []


def _field(file: Any, name: str) -> str:
    return str(file[name] if isinstance(file, Mapping) else getattr(file, name))


def _mod_starter_files(starter: Any, name: str) -> list[CrowdyStudioProjectFile]:
    files = _starter_files(starter)
    paths = {_field(file, "path") for file in files}
    if "Cargo.toml" not in paths or "src/lib.rs" not in paths:
        raise ValueError("The mod starter has no Cargo.toml or src/lib.rs")
    return [
        CrowdyStudioProjectFile(
            target="SERVER",
            path=_field(file, "path"),
            content=(
                _rename_package(_field(file, "content"), name)
                if _field(file, "path") == "Cargo.toml"
                else _field(file, "content")
            ),
        )
        for file in files
    ]


_SECTION = re.compile(r"\s*\[([^\]]+)\]\s*")
_NAME = re.compile(r"\s*name\s*=")


def _rename_package(manifest: str, name: str) -> str:
    section = ""
    renamed = False
    lines = []
    for line in manifest.split("\n"):
        header = _SECTION.fullmatch(line)
        if header:
            section = header[1].strip()
        elif section == "package" and not renamed and _NAME.match(line):
            renamed = True
            line = f'name = "{name}"'
        lines.append(line)
    return "\n".join(lines)


def _mod_module_base(value: str) -> str:
    slug = _slug_module_name(value)
    if not slug:
        return "player-mod"
    cut = (slug if "a" <= slug[0] <= "z" else f"mod-{slug}")[:_MOD_BASE_MAX]
    return cut[:-1] if cut.endswith("-") else cut


def _slug_module_name(value: str) -> str:
    dashed = []
    pending_dash = False
    started = False
    for ch in value.strip().lower():
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            if pending_dash and started:
                dashed.append("-")
            dashed.append(ch)
            started = True
            pending_dash = False
        else:
            pending_dash = True
    return "".join(dashed)
