"""The headless Studio's model: file references, agent patches and checkpoints, the provider
protocols the controller drives, and the canonical-JSON digests CrowdyJS binds approvals to.

Projects, files and reference files are :mod:`crowdypy.domains.crowdy_studio`'s.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import msgspec

from crowdypy.domains.crowdy_studio import (
    CrowdyStudioProject,
    CrowdyStudioProjectFile,
    CrowdyStudioProjectKind,
    CrowdyStudioProjectMetadata,
    CrowdyStudioProjectSummary,
    CrowdyStudioReferenceFile,
    CrowdyStudioTarget,
    normalize_crowdy_studio_path,
)
from crowdypy.player_host.json_schema import canonical_json, digest_canonical_json, sha256_digest

__all__ = [
    "CrowdyStudioAtomicFileChange",
    "CrowdyStudioAtomicPatchInput",
    "CrowdyStudioAtomicPatchResult",
    "CrowdyStudioCheckpointFile",
    "CrowdyStudioCheckpointMetadata",
    "CrowdyStudioCheckpointRestoreResult",
    "CrowdyStudioFileRef",
    "CrowdyStudioProjectProvider",
    "CrowdyStudioProjectSynchronization",
    "CrowdyStudioSaveState",
    "CrowdyStudioSynchronizationProvider",
    "canonical_json",
    "crowdy_studio_file_key",
    "crowdy_studio_file_uri",
    "digest_canonical_json",
    "project_targets",
    "sha256_digest",
]

CrowdyStudioSaveState = Literal["SAVING", "SAVED", "CONFLICT", "OFFLINE"]
CrowdyStudioFileSource = Literal["PROJECT", "PERSONAL_LIBRARY", "COMMON"]


@dataclass(frozen=True, slots=True)
class CrowdyStudioFileRef:
    """An open editor tab: a project file (``target`` and ``path``) or a read-only reference
    file (``reference_id``, or ``target`` and ``path``)."""

    source: CrowdyStudioFileSource
    path: str
    target: CrowdyStudioTarget | None = None
    reference_id: str | None = None


class CrowdyStudioCheckpointFile(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    target: CrowdyStudioTarget
    path: str
    content_hash: str
    byte_length: int


class CrowdyStudioCheckpointMetadata(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    checkpoint_id: str
    project_revision_id: str
    content_hash: str
    reason: Literal["AGENT_WRITE", "RESTORE_PREIMAGE", "MANUAL"]
    files: list[CrowdyStudioCheckpointFile]
    created_at: str
    restored_at: str | None = None


class CrowdyStudioAtomicFileChange(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """One file an agent patch creates or replaces. ``expected_content_hash`` is the
    current content's ``sha256:`` digest, or ``"ABSENT"`` for a file being created."""

    target: CrowdyStudioTarget
    path: str
    operation: Literal["CREATE", "REPLACE"]
    content: str
    expected_content_hash: str


class CrowdyStudioAtomicPatchInput(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    expected_revision_id: str
    changes: list[CrowdyStudioAtomicFileChange]


class CrowdyStudioAtomicPatchResult(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    project: CrowdyStudioProject
    checkpoint: CrowdyStudioCheckpointMetadata
    changed_files: list[CrowdyStudioCheckpointFile]


class CrowdyStudioCheckpointRestoreResult(
    msgspec.Struct, rename="camel", frozen=True, kw_only=True
):
    project: CrowdyStudioProject
    pre_restore_checkpoint: CrowdyStudioCheckpointMetadata


@dataclass(frozen=True, slots=True)
class CrowdyStudioProjectSynchronization:
    source: Literal["AGENT", "REMOTE"]
    expected_previous_revision_id: str | None = None
    checkpoint: CrowdyStudioCheckpointMetadata | None = None


class CrowdyStudioProjectProvider(Protocol):
    """Where projects live: :class:`crowdypy.domains.crowdy_studio.CrowdyStudioAPI`."""

    async def list_projects(self, app_id: str | int) -> list[CrowdyStudioProjectSummary]: ...

    async def get_project(
        self, app_id: str | int, grid_id: str | int, project_id: str
    ) -> CrowdyStudioProject: ...

    async def create_project(
        self,
        app_id: str | int,
        grid_id: str | int,
        kind: CrowdyStudioProjectKind,
        metadata: CrowdyStudioProjectMetadata | Mapping[str, Any],
        files: Sequence[CrowdyStudioProjectFile | Mapping[str, Any]],
    ) -> CrowdyStudioProject: ...

    async def save_project(
        self,
        app_id: str | int,
        grid_id: str | int,
        project_id: str,
        expected_revision_id: str | int,
        metadata: CrowdyStudioProjectMetadata | Mapping[str, Any],
        files: Sequence[CrowdyStudioProjectFile | Mapping[str, Any]],
    ) -> CrowdyStudioProject: ...

    async def list_personal_library_files(
        self, app_id: str | int
    ) -> list[CrowdyStudioReferenceFile]: ...

    async def list_common_files(self, app_id: str | int) -> list[CrowdyStudioReferenceFile]: ...

    async def import_reference_file(
        self,
        app_id: str | int,
        grid_id: str | int,
        project_id: str,
        expected_revision_id: str | int,
        source: Literal["PERSONAL_LIBRARY", "COMMON"],
        reference_id: str,
        destination_path: str | None = None,
    ) -> CrowdyStudioProject: ...

    async def save_personal_library_file(
        self,
        app_id: str | int,
        title: str,
        target: CrowdyStudioTarget,
        path: str,
        content: str,
        tags: Sequence[str] | None = None,
    ) -> CrowdyStudioReferenceFile: ...


class CrowdyStudioSynchronizationProvider(Protocol):
    """Durable agent writes: atomic multi-file patches with checkpoints, and restores."""

    async def apply_atomic_patch(
        self,
        *,
        app_id: str,
        grid_id: str,
        project_id: str,
        expected_revision_id: str,
        changes: Sequence[CrowdyStudioAtomicFileChange],
    ) -> CrowdyStudioAtomicPatchResult: ...

    async def list_checkpoints(
        self, *, app_id: str, grid_id: str, project_id: str
    ) -> Sequence[CrowdyStudioCheckpointMetadata]: ...

    async def restore_checkpoint(
        self,
        *,
        app_id: str,
        grid_id: str,
        project_id: str,
        checkpoint_id: str,
        expected_revision_id: str,
        approval_grant: str,
    ) -> CrowdyStudioCheckpointRestoreResult: ...


def crowdy_studio_file_key(target: CrowdyStudioTarget, path: str) -> str:
    return f"{target}:{normalize_crowdy_studio_path(path)}"


def crowdy_studio_file_uri(workspace_uri: str, target: CrowdyStudioTarget, path: str) -> str:
    """The editor URI of a project file: ``<workspace>/<server|client>/<encoded path>``."""
    from urllib.parse import quote

    root = workspace_uri.rstrip("/")
    encoded = "/".join(
        quote(part, safe="-_.!~*'()") for part in normalize_crowdy_studio_path(path).split("/")
    )
    return f"{root}/{target.lower()}/{encoded}"


def project_targets(kind: CrowdyStudioProjectKind) -> list[CrowdyStudioTarget]:
    if kind == "SERVER":
        return ["SERVER"]
    if kind == "CLIENT":
        return ["CLIENT"]
    return ["SERVER", "CLIENT"]
