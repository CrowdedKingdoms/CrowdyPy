"""Crowdy Studio cloud projects, personal library and common files (``client.crowdy_studio``).

This is the Game API's project storage, not the browser editor. Every call needs the
app-scoped token of the app it names, and reaches only the signed-in player's own projects
and library; the common catalog is readable by every player in the app.

A project's ``source`` decides how :meth:`CrowdyStudioAPI.save_project` persists files:
``STUDIO`` projects go through ``crowdyStudioProjectSave`` under the project revision;
``GITHUB`` projects commit each changed file through the GitHub transport under
``github.sha`` (``expectedCommitSha``), because their files are a server-maintained mirror of
the repository and the Studio file mutations refuse them. Either way a lost race is a
:class:`CrowdyStudioRevisionConflictError` carrying the project as it is now, and an
unreachable service is a :class:`CrowdyStudioOfflineError`.

Project models carry CrowdyJS's names: a project's ``kind`` is ``SERVER``, ``CLIENT`` or
``FULL_STACK`` and its ``pairing_preference`` is ``NONE``, ``OPTIONAL`` or ``REQUIRED``; the
API's own pairing values (``PAIRED``, ``SERVER_ONLY``, ...) never reach the caller. A mapping
given for a model uses its camelCase keys, as CrowdyJS objects do.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import msgspec

from crowdypy._generated import operations as ops
from crowdypy._generated.enums import CrowdyStudioImportSource as _ImportSource
from crowdypy._generated.enums import CrowdyStudioPairingPreference as _ApiPairing
from crowdypy._generated.enums import CrowdyStudioProjectSource as _ApiSource
from crowdypy._operation import Operation, inline_operation
from crowdypy.domains._base import Domain
from crowdypy.domains.crowdy_studio_github import (
    CrowdyStudioGitHubLayout,
    CrowdyStudioGitHubTransport,
)
from crowdypy.errors import (
    CrowdyError,
    CrowdyGraphQLError,
    CrowdyHttpError,
    CrowdyNetworkError,
    CrowdyProtocolError,
    CrowdyTimeoutError,
)
from crowdypy.graphql import AsyncGraphQLClient
from crowdypy.utils import bigint

__all__ = [
    "UNCHANGED",
    "CrowdyStudioAPI",
    "CrowdyStudioOfflineError",
    "CrowdyStudioPairingPreference",
    "CrowdyStudioProject",
    "CrowdyStudioProjectFile",
    "CrowdyStudioProjectGitHub",
    "CrowdyStudioProjectKind",
    "CrowdyStudioProjectMetadata",
    "CrowdyStudioProjectRevision",
    "CrowdyStudioProjectSource",
    "CrowdyStudioProjectSummary",
    "CrowdyStudioReferenceFile",
    "CrowdyStudioRevisionConflictError",
    "CrowdyStudioTarget",
    "normalize_crowdy_studio_path",
]

#: Compile targets of a player-authored project.
CrowdyStudioTarget = Literal["SERVER", "CLIENT"]
#: The target layout chosen when a project is created.
CrowdyStudioProjectKind = Literal["SERVER", "CLIENT", "FULL_STACK"]
#: The pairing a project records. A ck-exec mod has no pairing (its CLIENT half rides it),
#: so new projects use ``NONE``; ``OPTIONAL`` or ``REQUIRED`` on an older one changes nothing.
CrowdyStudioPairingPreference = Literal["NONE", "OPTIONAL", "REQUIRED"]
#: Where a project's files are authored: every project starts as ``STUDIO``; its owner may
#: bind a GitHub repository later and unbind it again.
CrowdyStudioProjectSource = Literal["STUDIO", "GITHUB"]


class CrowdyStudioProjectFile(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A source file in one project target; ``path`` is relative to that target."""

    target: CrowdyStudioTarget
    path: str
    content: str


class CrowdyStudioProjectMetadata(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    name: str
    description: str | None = None
    server_module_name: str | None = None
    client_module_name: str | None = None
    pairing_preference: CrowdyStudioPairingPreference


class CrowdyStudioProjectRevision(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """The immutable revision identity of a saved project."""

    id: str
    saved_at: str


class CrowdyStudioProjectGitHub(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """The repository a GITHUB project is bound to, and the commit its files mirror."""

    owner: str
    repo: str
    branch: str
    #: Every bound write presents it as ``expectedCommitSha``; a stale one is refused with
    #: ``GITHUB_STALE_SHA`` and surfaces as a revision conflict.
    sha: str | None


class CrowdyStudioProject(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """One atomic project snapshot: SERVER and CLIENT files share one revision.

    For a GITHUB project ``files`` is the server-maintained mirror of the repository at
    ``github.sha``, read exactly like a STUDIO project's files.
    """

    project_id: str
    app_id: str
    grid_id: str
    kind: CrowdyStudioProjectKind
    metadata: CrowdyStudioProjectMetadata
    files: list[CrowdyStudioProjectFile]
    sdk_version: str
    abi_version: int
    revision: CrowdyStudioProjectRevision
    source: CrowdyStudioProjectSource
    github: CrowdyStudioProjectGitHub | None
    created_at: str
    updated_at: str


class CrowdyStudioProjectSummary(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    project_id: str
    name: str
    kind: CrowdyStudioProjectKind
    revision_id: str
    server_module_name: str | None = None
    client_module_name: str | None = None
    source: CrowdyStudioProjectSource
    #: ``owner/repo@branch`` for a GITHUB project.
    github: str | None = None
    github_sha: str | None
    updated_at: str


class CrowdyStudioReferenceFile(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A read-only source file from the personal library or the common catalog."""

    id: str
    source: Literal["PERSONAL_LIBRARY", "COMMON"]
    title: str
    #: A target fence; a file without one is useful from either target.
    target: CrowdyStudioTarget | None = None
    path: str
    content: str
    tags: list[str] | None = None
    updated_at: str | None = None


class CrowdyStudioRevisionConflictError(CrowdyError):
    """The expected revision lost a save race: the project changed in another session.

    ``remote_project`` is the project as it is now, when it could be read back, so the
    caller can show what moved and offer to keep its own version.
    """

    code = "PROJECT_REVISION_CONFLICT"

    def __init__(
        self,
        message: str = "The project changed in another session",
        remote_project: CrowdyStudioProject | None = None,
    ) -> None:
        super().__init__(message)
        self.remote_project = remote_project


class CrowdyStudioOfflineError(CrowdyError):
    """The project service could not be reached (network, timeout or HTTP 5xx); retry later."""

    code = "PROJECT_OFFLINE"

    def __init__(
        self,
        message: str = "The project service is offline",
        cause: BaseException | object | None = None,
    ) -> None:
        super().__init__(message, cause=cause)


# What JavaScript's String.prototype.trim removes: Python's str.strip() would also take
# \x1c-\x1f and \x85 and leave \ufeff, which would change which paths are valid.
_JS_WHITESPACE = (
    "\t\n\v\f\r \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
    "\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_LEADING_DOT_SLASH = re.compile(r"^\./+")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


def normalize_crowdy_studio_path(path: str) -> str:
    """Validate and normalize a target-relative project path.

    Backslashes become slashes and one leading ``./`` is dropped. An empty or absolute path,
    a trailing slash, an empty, ``.`` or ``..`` segment, control characters, or more than 240
    UTF-16 code units raise :class:`~crowdypy.errors.CrowdyProtocolError`.
    """
    normalized = _LEADING_DOT_SLASH.sub("", path.strip(_JS_WHITESPACE).replace("\\", "/"))
    if (
        not normalized
        or len(normalized.encode("utf-16-le", "surrogatepass")) // 2 > 240
        or normalized.startswith("/")
        or normalized.endswith("/")
        or any(part in ("", ".", "..") for part in normalized.split("/"))
        or _CONTROL_CHARACTER.search(normalized)
    ):
        raise CrowdyProtocolError(f"Invalid project file path: {path}")
    return normalized


def _kind_from_api(pairing: str) -> CrowdyStudioProjectKind:
    if pairing == _ApiPairing.SERVER_ONLY:
        return "SERVER"
    if pairing == _ApiPairing.CLIENT_ONLY:
        return "CLIENT"
    return "FULL_STACK"


def _from_api_pairing(pairing: str) -> CrowdyStudioPairingPreference:
    if pairing == _ApiPairing.PAIRED:
        return "REQUIRED"
    if pairing == _ApiPairing.INDEPENDENT:
        return "OPTIONAL"
    return "NONE"


def _to_api_pairing(kind: str, pairing: str) -> _ApiPairing:
    if kind == "SERVER":
        return _ApiPairing.SERVER_ONLY
    if kind == "CLIENT":
        return _ApiPairing.CLIENT_ONLY
    return _ApiPairing.PAIRED if pairing == "REQUIRED" else _ApiPairing.INDEPENDENT


def _metadata(
    value: CrowdyStudioProjectMetadata | Mapping[str, Any],
) -> CrowdyStudioProjectMetadata:
    if isinstance(value, CrowdyStudioProjectMetadata):
        return value
    return msgspec.convert(dict(value), CrowdyStudioProjectMetadata)


def _project_files(
    files: Sequence[CrowdyStudioProjectFile | Mapping[str, Any]],
) -> list[CrowdyStudioProjectFile]:
    return [
        file
        if isinstance(file, CrowdyStudioProjectFile)
        else msgspec.convert(dict(file), CrowdyStudioProjectFile)
        for file in files
    ]


def _to_api_file(file: CrowdyStudioProjectFile) -> dict[str, Any]:
    return {
        "target": file.target,
        "path": normalize_crowdy_studio_path(file.path),
        "content": file.content,
    }


def _project_kind(files: Sequence[CrowdyStudioProjectFile]) -> CrowdyStudioProjectKind:
    has_server = any(file.target == "SERVER" for file in files)
    has_client = any(file.target == "CLIENT" for file in files)
    if has_server and has_client:
        return "FULL_STACK"
    if has_server:
        return "SERVER"
    return "CLIENT"


def _file_key(file: CrowdyStudioProjectFile) -> str:
    return f"{file.target}:{normalize_crowdy_studio_path(file.path)}"


def _is_bound(dto: Mapping[str, Any]) -> bool:
    return bool(
        dto.get("source") == _ApiSource.GITHUB
        and dto.get("githubOwner")
        and dto.get("githubRepo")
        and dto.get("githubBranch")
    )


def _from_summary_dto(dto: Mapping[str, Any]) -> CrowdyStudioProjectSummary:
    bound = _is_bound(dto)
    return CrowdyStudioProjectSummary(
        project_id=dto["projectId"],
        name=dto["name"],
        kind=_kind_from_api(dto["pairingPreference"]),
        revision_id=str(dto["revision"]),
        source="GITHUB" if bound else "STUDIO",
        github=f"{dto['githubOwner']}/{dto['githubRepo']}@{dto['githubBranch']}" if bound else None,
        github_sha=dto.get("githubSha") if bound else None,
        server_module_name=dto.get("serverModuleName") or None,
        client_module_name=dto.get("clientModuleName") or None,
        updated_at=dto["updatedAt"],
    )


def _from_project_dto(dto: Mapping[str, Any], fallback_grid_id: str) -> CrowdyStudioProject:
    files = [
        CrowdyStudioProjectFile(
            target=file["target"],
            path=normalize_crowdy_studio_path(file["path"]),
            content=file["content"],
        )
        for file in dto["files"]
    ]
    return CrowdyStudioProject(
        project_id=dto["projectId"],
        app_id=str(dto["appId"]),
        grid_id=fallback_grid_id if dto.get("gridId") is None else str(dto["gridId"]),
        kind=_kind_from_api(dto["pairingPreference"]),
        metadata=CrowdyStudioProjectMetadata(
            name=dto["name"],
            description=dto.get("description") or None,
            server_module_name=dto.get("serverModuleName") or None,
            client_module_name=dto.get("clientModuleName") or None,
            pairing_preference=_from_api_pairing(dto["pairingPreference"]),
        ),
        files=files,
        sdk_version=dto["sdkVersion"],
        abi_version=dto["abiVersion"],
        revision=CrowdyStudioProjectRevision(id=str(dto["revision"]), saved_at=dto["updatedAt"]),
        source="GITHUB" if dto.get("source") == _ApiSource.GITHUB else "STUDIO",
        github=CrowdyStudioProjectGitHub(
            owner=dto["githubOwner"],
            repo=dto["githubRepo"],
            branch=dto["githubBranch"],
            sha=dto.get("githubSha"),
        )
        if _is_bound(dto)
        else None,
        created_at=dto["createdAt"],
        updated_at=dto["updatedAt"],
    )


def _from_library_dto(dto: Mapping[str, Any]) -> CrowdyStudioReferenceFile:
    return CrowdyStudioReferenceFile(
        id=dto["libraryFileId"],
        source="PERSONAL_LIBRARY",
        title=dto["title"],
        target=dto["target"],
        path=normalize_crowdy_studio_path(dto["pathHint"]),
        content=dto["content"],
        tags=list(dto["tags"]),
        updated_at=dto["updatedAt"],
    )


def _from_common_dto(dto: Mapping[str, Any]) -> CrowdyStudioReferenceFile:
    return CrowdyStudioReferenceFile(
        id=dto["versionId"],
        source="COMMON",
        title=dto["title"],
        target=dto["target"],
        path=normalize_crowdy_studio_path(dto["path"]),
        content=dto["content"],
        tags=list(dto["tags"]),
        updated_at=dto["updatedAt"],
    )


@dataclass(frozen=True, slots=True)
class _ProjectSave:
    """A save_project call with its BigInt arguments as decimal strings."""

    app_id: str
    grid_id: str
    project_id: str
    expected_revision_id: str
    metadata: CrowdyStudioProjectMetadata
    files: list[CrowdyStudioProjectFile]


@dataclass(frozen=True, slots=True)
class _FileDelta:
    upserts: list[CrowdyStudioProjectFile]
    #: ``(target, path)`` of each baseline file the save no longer has.
    deletes: list[tuple[CrowdyStudioTarget, str]]


def _project_file_delta(
    baseline: CrowdyStudioProject, files: Sequence[CrowdyStudioProjectFile]
) -> _FileDelta:
    previous = {_file_key(file): file for file in baseline.files}
    current = {_file_key(file): file for file in files}
    upserts = [
        file
        for file in files
        if (before := previous.get(_file_key(file))) is None or before.content != file.content
    ]
    deletes = [
        (file.target, file.path) for file in baseline.files if _file_key(file) not in current
    ]
    return _FileDelta(upserts=upserts, deletes=deletes)


def _metadata_changed(baseline: CrowdyStudioProject, save: _ProjectSave) -> bool:
    a, b = baseline.metadata, save.metadata
    return (
        baseline.grid_id != save.grid_id
        or a.name != b.name
        or (a.description or "") != (b.description or "")
        or (a.server_module_name or "") != (b.server_module_name or "")
        or (a.client_module_name or "") != (b.client_module_name or "")
        or _to_api_pairing(baseline.kind, a.pairing_preference)
        != _to_api_pairing(_project_kind(save.files), b.pairing_preference)
    )


def _save_input(
    save: _ProjectSave,
    upserts: Sequence[CrowdyStudioProjectFile],
    deletes: Sequence[tuple[CrowdyStudioTarget, str]],
) -> dict[str, Any]:
    metadata = save.metadata
    return {
        "appId": save.app_id,
        "projectId": save.project_id,
        "expectedRevision": save.expected_revision_id,
        "gridId": save.grid_id,
        "name": metadata.name,
        "description": metadata.description,
        "serverModuleName": metadata.server_module_name,
        "clientModuleName": metadata.client_module_name,
        "pairingPreference": _to_api_pairing(
            _project_kind(save.files), metadata.pairing_preference
        ),
        "upserts": [_to_api_file(file) for file in upserts],
        "deletes": [{"target": target, "path": path} for target, path in deletes],
    }


def _is_conflict(error: CrowdyGraphQLError, *codes: str) -> bool:
    # The server's code is the long name (CROWDY_STUDIO_REVISION_CONFLICT, not CONFLICT);
    # older tiers put it only in the message, so either one counts.
    return error.code in codes or any(code in error.message for code in codes)


def _studio_file_to_repo_path(
    layout: CrowdyStudioGitHubLayout, target: str, path: str
) -> str | None:
    root = layout.server if target == "SERVER" else layout.client
    if root is None:
        return None
    base, rest = root.strip("/"), path.strip("/")
    if not base or base == ".":
        return rest
    return f"{base}/{rest}" if rest else base


class _Unchanged(enum.Enum):
    UNCHANGED = enum.auto()


#: A metadata patch field left as it is (``None`` clears a nullable field).
UNCHANGED: Final = _Unchanged.UNCHANGED


def _fragment(operation: Operation, name: str) -> str:
    """A fragment CrowdyJS's generated document carries, for a document of our own that
    must decode the same way."""
    return operation.document[operation.document.index(f"fragment {name} ") :]


_PROJECT_FIELDS = _fragment(ops.CROWDY_STUDIO_PROJECT_SAVE, "CrowdyStudioProjectFields")
_LIBRARY_FIELDS = (
    "libraryFileId appId ownerUserId title pathHint target tags content revision archived "
    "archivedAt createdAt updatedAt"
)
_COMMON_FIELDS = (
    "commonFileId appId slug title description path target tags status versionId versionNo "
    "content contentSha256 publishedByUserId publishedAt createdAt updatedAt"
)

# CrowdyCPP's narrow project and reference-file mutations (CrowdyJS saves through
# crowdyStudioProjectSave only).
PROJECT_SAVE_METADATA = inline_operation(
    "CrowdyStudioProjectSaveMetadata",
    "mutation",
    "crowdyStudioProjectSaveMetadata",
    "mutation CrowdyStudioProjectSaveMetadata($input: SaveCrowdyStudioProjectMetadataInput!) "
    "{ crowdyStudioProjectSaveMetadata(input: $input) { ...CrowdyStudioProjectFields } }\n"
    + _PROJECT_FIELDS,
)
PROJECT_SAVE_FILES = inline_operation(
    "CrowdyStudioProjectSaveFiles",
    "mutation",
    "crowdyStudioProjectSaveFiles",
    "mutation CrowdyStudioProjectSaveFiles($input: SaveCrowdyStudioProjectFilesInput!) "
    "{ crowdyStudioProjectSaveFiles(input: $input) { ...CrowdyStudioProjectFields } }\n"
    + _PROJECT_FIELDS,
)
PROJECT_SET_ARCHIVED = inline_operation(
    "CrowdyStudioProjectSetArchived",
    "mutation",
    "crowdyStudioProjectSetArchived",
    "mutation CrowdyStudioProjectSetArchived($input: SetCrowdyStudioProjectArchivedInput!) "
    "{ crowdyStudioProjectSetArchived(input: $input) { ...CrowdyStudioProjectFields } }\n"
    + _PROJECT_FIELDS,
)
LIBRARY_SET_ARCHIVED = inline_operation(
    "CrowdyStudioLibrarySetArchived",
    "mutation",
    "crowdyStudioLibrarySetArchived",
    "mutation CrowdyStudioLibrarySetArchived($input: SetCrowdyStudioLibraryFileArchivedInput!) "
    f"{{ crowdyStudioLibrarySetArchived(input: $input) {{ {_LIBRARY_FIELDS} }} }}",
)
COMMON_PUBLISH = inline_operation(
    "CrowdyStudioCommonPublish",
    "mutation",
    "crowdyStudioCommonPublish",
    "mutation CrowdyStudioCommonPublish($input: PublishCrowdyStudioCommonFileInput!) "
    f"{{ crowdyStudioCommonPublish(input: $input) {{ {_COMMON_FIELDS} }} }}",
)
INLINE_OPERATIONS = (
    PROJECT_SAVE_METADATA,
    PROJECT_SAVE_FILES,
    PROJECT_SET_ARCHIVED,
    LIBRARY_SET_ARCHIVED,
    COMMON_PUBLISH,
)


def _patch(input_: dict[str, Any], key: str, value: object) -> None:
    if value is not UNCHANGED:
        input_[key] = value


class CrowdyStudioAPI(Domain):
    """Private Crowdy Studio projects and reusable files, and how a save reaches them."""

    def __init__(self, graphql: AsyncGraphQLClient) -> None:
        super().__init__(graphql)
        self._github = CrowdyStudioGitHubTransport(graphql)
        #: The project each save diffs against: the last one this client read or wrote.
        self._baselines: dict[str, CrowdyStudioProject] = {}
        #: Layout per ``projectId@commit``; commits are immutable, so it never goes stale.
        self._layouts: dict[str, CrowdyStudioGitHubLayout] = {}

    async def list_projects(self, app_id: str | int) -> list[CrowdyStudioProjectSummary]:
        """The signed-in player's active projects in the app, newest first (at most 50)."""
        rows: list[dict[str, Any]] = await self._request(
            ops.CROWDY_STUDIO_PROJECTS,
            {"appId": bigint(app_id), "includeArchived": False, "limit": 50, "offset": 0},
        )
        return [_from_summary_dto(row) for row in rows]

    async def get_project(
        self, app_id: str | int, grid_id: str | int, project_id: str
    ) -> CrowdyStudioProject:
        """One project with all its files; ``NOT_FOUND`` unless it is the caller's own.

        ``grid_id`` stands in for a project that has no grid of its own.
        """
        payload = await self._request(
            ops.CROWDY_STUDIO_PROJECT, {"appId": bigint(app_id), "projectId": project_id}
        )
        return self._remember(_from_project_dto(payload, str(grid_id)))

    async def create_project(
        self,
        app_id: str | int,
        grid_id: str | int,
        kind: CrowdyStudioProjectKind,
        metadata: CrowdyStudioProjectMetadata | Mapping[str, Any],
        files: Sequence[CrowdyStudioProjectFile | Mapping[str, Any]],
    ) -> CrowdyStudioProject:
        """Create a private project with its first files.

        Paths, per-target caps and the owner's aggregate storage are enforced atomically; the
        grid is an authoring hint that grants no deployment authority.
        """
        meta = _metadata(metadata)
        grid = bigint(grid_id)
        payload = await self._request(
            ops.CROWDY_STUDIO_PROJECT_CREATE,
            {
                "input": {
                    "appId": bigint(app_id),
                    "gridId": grid,
                    "name": meta.name,
                    "description": meta.description,
                    "serverModuleName": meta.server_module_name,
                    "clientModuleName": meta.client_module_name,
                    "pairingPreference": _to_api_pairing(kind, meta.pairing_preference),
                    "sdkVersion": "0.1.8",
                    "abiVersion": 0,
                    "initialFiles": [_to_api_file(file) for file in _project_files(files)],
                }
            },
        )
        return self._remember(_from_project_dto(payload, grid))

    async def save_project(
        self,
        app_id: str | int,
        grid_id: str | int,
        project_id: str,
        expected_revision_id: str | int,
        metadata: CrowdyStudioProjectMetadata | Mapping[str, Any],
        files: Sequence[CrowdyStudioProjectFile | Mapping[str, Any]],
    ) -> CrowdyStudioProject:
        """Save the whole project under ``expected_revision_id``, sending only what changed.

        The change is the difference from the project this client last read or wrote (read
        first when there is none). A STUDIO project saves in one transaction; a GITHUB
        project saves its metadata, then commits each changed file to the bound branch. A
        lost race raises :class:`CrowdyStudioRevisionConflictError` with the project as it is
        now.
        """
        save = _ProjectSave(
            app_id=bigint(app_id),
            grid_id=bigint(grid_id),
            project_id=project_id,
            expected_revision_id=bigint(expected_revision_id),
            metadata=_metadata(metadata),
            files=_project_files(files),
        )
        baseline = self._baselines.get(project_id)
        if baseline is None:
            baseline = await self.get_project(save.app_id, save.grid_id, project_id)
        if baseline.source == "GITHUB" and baseline.github is not None and baseline.github.sha:
            return await self._save_bound_project(baseline, save)
        try:
            delta = _project_file_delta(baseline, save.files)
            payload = await self._request(
                ops.CROWDY_STUDIO_PROJECT_SAVE,
                {"input": _save_input(save, delta.upserts, delta.deletes)},
            )
            return self._remember(_from_project_dto(payload, save.grid_id))
        except CrowdyGraphQLError as error:
            if not _is_conflict(error, "CROWDY_STUDIO_REVISION_CONFLICT"):
                raise
            remote = await self._remote_project(save)
            raise CrowdyStudioRevisionConflictError(error.message, remote) from error

    async def _save_bound_project(
        self, baseline: CrowdyStudioProject, save: _ProjectSave
    ) -> CrowdyStudioProject:
        # Metadata through the project save (a CAS on the revision, no file bodies), then each
        # changed file as its own commit carrying the commit the previous one produced. A
        # stale race part-way leaves the earlier commits on the branch and the project
        # describing them; the conflict recovery re-reads and re-applies the rest.
        start_sha = baseline.github.sha if baseline.github is not None else None
        if not start_sha:
            raise CrowdyError(
                "The bound project has no mirror commit; refresh it from GitHub first."
            )
        # The caller's precondition is the revision it read. One holding an older revision
        # must not ride the baseline's commit onto the branch: the STUDIO path's server-side
        # CAS refuses exactly that lost update.
        if save.expected_revision_id != baseline.revision.id:
            remote = await self._remote_project(save)
            raise CrowdyStudioRevisionConflictError(
                f"CROWDY_STUDIO_REVISION_CONFLICT: expected project revision "
                f"{save.expected_revision_id}; current revision is {baseline.revision.id}.",
                remote,
            )
        try:
            if _metadata_changed(baseline, save):
                await self._request(
                    ops.CROWDY_STUDIO_PROJECT_SAVE, {"input": _save_input(save, [], [])}
                )
            delta = _project_file_delta(baseline, save.files)
            sha = start_sha
            if delta.upserts or delta.deletes:
                layout = await self._layout_at(save.app_id, save.project_id, sha)
                for file in delta.upserts:
                    path = normalize_crowdy_studio_path(file.path)
                    repo_path = _studio_file_to_repo_path(layout, file.target, path)
                    if not repo_path:
                        raise CrowdyError(
                            f"The bound repository's crowdy.json has no {file.target.lower()} "
                            f"directory, so {path} has nowhere to go."
                        )
                    written = await self._github.put_file(
                        {
                            "appId": save.app_id,
                            "projectId": save.project_id,
                            "path": repo_path,
                            "content": file.content,
                            "message": f"studio: update {repo_path}",
                            "expectedCommitSha": sha,
                        }
                    )
                    sha = written.commit_sha if written.commit_sha is not None else sha
                for target, file_path in delta.deletes:
                    repo_path = _studio_file_to_repo_path(
                        layout, target, normalize_crowdy_studio_path(file_path)
                    )
                    if not repo_path:
                        continue
                    status = await self._github.delete_file(
                        {
                            "appId": save.app_id,
                            "projectId": save.project_id,
                            "path": repo_path,
                            "message": f"studio: delete {repo_path}",
                            "expectedCommitSha": sha,
                        }
                    )
                    sha = status.github_sha if status.github_sha is not None else sha
            return await self.get_project(save.app_id, save.grid_id, save.project_id)
        except CrowdyGraphQLError as error:
            if not _is_conflict(error, "GITHUB_STALE_SHA", "CROWDY_STUDIO_REVISION_CONFLICT"):
                raise
            remote = await self._remote_project(save)
            raise CrowdyStudioRevisionConflictError(error.message, remote) from error

    async def _layout_at(
        self, app_id: str, project_id: str, commit_sha: str
    ) -> CrowdyStudioGitHubLayout:
        key = f"{project_id}@{commit_sha}"
        cached = self._layouts.get(key)
        if cached is not None:
            return cached
        layout = await self._github.layout(
            {"appId": app_id, "projectId": project_id, "commitSha": commit_sha}
        )
        if len(self._layouts) > 64:
            del self._layouts[next(iter(self._layouts))]
        self._layouts[key] = layout
        return layout

    async def list_personal_library_files(
        self, app_id: str | int
    ) -> list[CrowdyStudioReferenceFile]:
        """The signed-in player's active personal-library files in the app (at most 100)."""
        rows: list[dict[str, Any]] = await self._request(
            ops.CROWDY_STUDIO_LIBRARY_FILES,
            {"appId": bigint(app_id), "includeArchived": False, "limit": 100, "offset": 0},
        )
        return [_from_library_dto(row) for row in rows]

    async def save_personal_library_file(
        self,
        app_id: str | int,
        title: str,
        target: CrowdyStudioTarget,
        path: str,
        content: str,
        tags: Sequence[str] | None = None,
    ) -> CrowdyStudioReferenceFile:
        """Add a file to the signed-in player's personal library.

        Content is at most 64 KiB, and the library's aggregate storage is bounded.
        """
        payload = await self._request(
            ops.CROWDY_STUDIO_LIBRARY_SAVE,
            {
                "input": {
                    "appId": bigint(app_id),
                    "title": title,
                    "pathHint": normalize_crowdy_studio_path(path),
                    "target": target,
                    "tags": [] if tags is None else list(tags),
                    "content": content,
                }
            },
        )
        return _from_library_dto(payload)

    async def list_common_files(self, app_id: str | int) -> list[CrowdyStudioReferenceFile]:
        """The current published versions of the app's curated common files (at most 100)."""
        rows: list[dict[str, Any]] = await self._request(
            ops.CROWDY_STUDIO_COMMON_FILES, {"appId": bigint(app_id), "limit": 100, "offset": 0}
        )
        return [_from_common_dto(row) for row in rows]

    async def import_reference_file(
        self,
        app_id: str | int,
        grid_id: str | int,
        project_id: str,
        expected_revision_id: str | int,
        source: Literal["PERSONAL_LIBRARY", "COMMON"],
        reference_id: str,
        destination_path: str | None = None,
    ) -> CrowdyStudioProject:
        """Copy a library file or a published common version into a project, by value.

        ``reference_id`` is the reference file's ``id``. The project revision advances
        atomically, under the same caps as a save.
        """
        from_library = source == "PERSONAL_LIBRARY"
        input_: dict[str, Any] = {
            "appId": bigint(app_id),
            "projectId": project_id,
            "expectedProjectRevision": bigint(expected_revision_id),
            "source": _ImportSource.LIBRARY if from_library else _ImportSource.COMMON,
            ("libraryFileId" if from_library else "commonVersionId"): reference_id,
        }
        if destination_path:
            input_["destinationPath"] = normalize_crowdy_studio_path(destination_path)
        payload = await self._request(ops.CROWDY_STUDIO_PROJECT_IMPORT_FILE, {"input": input_})
        return self._remember(_from_project_dto(payload, str(grid_id)))

    async def save_project_metadata(
        self,
        app_id: str | int,
        project_id: str,
        expected_revision_id: str | int,
        *,
        grid_id: str | int | _Unchanged | None = UNCHANGED,
        name: str | _Unchanged = UNCHANGED,
        description: str | _Unchanged | None = UNCHANGED,
        server_module_name: str | _Unchanged | None = UNCHANGED,
        client_module_name: str | _Unchanged | None = UNCHANGED,
        pairing_preference: _ApiPairing | str | _Unchanged = UNCHANGED,
        sdk_version: str | _Unchanged = UNCHANGED,
        abi_version: int | _Unchanged = UNCHANGED,
        idempotency_key: str | None = None,
    ) -> CrowdyStudioProject:
        """Patch a project's metadata under optimistic revision control, leaving its files.

        Fields left ``UNCHANGED`` keep their value; ``None`` clears a nullable one.
        ``pairing_preference`` is the API's (``SERVER_ONLY``, ``CLIENT_ONLY``, ``PAIRED``
        or ``INDEPENDENT``). A stale ``expected_revision_id`` raises the platform's
        conflict error.
        """
        input_: dict[str, Any] = {
            "appId": bigint(app_id),
            "projectId": project_id,
            "expectedRevision": bigint(expected_revision_id),
        }
        _patch(input_, "gridId", bigint(grid_id) if isinstance(grid_id, (str, int)) else grid_id)
        _patch(input_, "name", name)
        _patch(input_, "description", description)
        _patch(input_, "serverModuleName", server_module_name)
        _patch(input_, "clientModuleName", client_module_name)
        _patch(input_, "pairingPreference", pairing_preference)
        _patch(input_, "sdkVersion", sdk_version)
        _patch(input_, "abiVersion", abi_version)
        if idempotency_key:
            input_["idempotencyKey"] = idempotency_key
        payload = await self._request(PROJECT_SAVE_METADATA, {"input": input_})
        return self._remember(_from_project_dto(payload, self._grid_of(project_id, payload)))

    async def save_project_files(
        self,
        app_id: str | int,
        project_id: str,
        expected_revision_id: str | int,
        upserts: Sequence[CrowdyStudioProjectFile | Mapping[str, Any]] = (),
        deletes: Sequence[tuple[CrowdyStudioTarget, str]] = (),
        *,
        idempotency_key: str | None = None,
    ) -> CrowdyStudioProject:
        """Upsert and delete project files in one transaction under one expected revision.

        ``deletes`` are ``(target, path)`` pairs; paths are normalized as a save normalizes
        them.
        """
        input_: dict[str, Any] = {
            "appId": bigint(app_id),
            "projectId": project_id,
            "expectedRevision": bigint(expected_revision_id),
            "upserts": [_to_api_file(file) for file in _project_files(upserts)],
            "deletes": [
                {"target": target, "path": normalize_crowdy_studio_path(path)}
                for target, path in deletes
            ],
        }
        if idempotency_key:
            input_["idempotencyKey"] = idempotency_key
        payload = await self._request(PROJECT_SAVE_FILES, {"input": input_})
        return self._remember(_from_project_dto(payload, self._grid_of(project_id, payload)))

    async def set_project_archived(
        self,
        app_id: str | int,
        project_id: str,
        expected_revision_id: str | int,
        archived: bool = True,
        *,
        idempotency_key: str | None = None,
    ) -> CrowdyStudioProject:
        """Archive or restore a project; nothing is deleted."""
        input_: dict[str, Any] = {
            "appId": bigint(app_id),
            "projectId": project_id,
            "expectedRevision": bigint(expected_revision_id),
            "archived": archived,
        }
        if idempotency_key:
            input_["idempotencyKey"] = idempotency_key
        payload = await self._request(PROJECT_SET_ARCHIVED, {"input": input_})
        return self._remember(_from_project_dto(payload, self._grid_of(project_id, payload)))

    async def set_personal_library_file_archived(
        self,
        app_id: str | int,
        library_file_id: str,
        expected_revision_id: str | int,
        archived: bool = True,
        *,
        idempotency_key: str | None = None,
    ) -> CrowdyStudioReferenceFile:
        """Archive or restore one of the signed-in player's personal-library files."""
        input_: dict[str, Any] = {
            "appId": bigint(app_id),
            "libraryFileId": library_file_id,
            "expectedRevision": bigint(expected_revision_id),
            "archived": archived,
        }
        if idempotency_key:
            input_["idempotencyKey"] = idempotency_key
        return _from_library_dto(await self._request(LIBRARY_SET_ARCHIVED, {"input": input_}))

    async def publish_common_file(
        self,
        app_id: str | int,
        slug: str,
        title: str,
        target: CrowdyStudioTarget,
        path: str,
        content: str,
        *,
        common_file_id: str | None = None,
        description: str | None = None,
        tags: Sequence[str] | None = None,
        idempotency_key: str | None = None,
    ) -> CrowdyStudioReferenceFile:
        """Publish a new immutable version of a curated common file and make it current.

        Needs ``manage_compute`` on the app. Pass ``common_file_id`` to version an existing
        file.
        """
        input_: dict[str, Any] = {
            "appId": bigint(app_id),
            "slug": slug,
            "title": title,
            "target": target,
            "path": normalize_crowdy_studio_path(path),
            "content": content,
            "tags": [] if tags is None else list(tags),
        }
        if common_file_id:
            input_["commonFileId"] = common_file_id
        if description is not None:
            input_["description"] = description
        if idempotency_key:
            input_["idempotencyKey"] = idempotency_key
        return _from_common_dto(await self._request(COMMON_PUBLISH, {"input": input_}))

    def _grid_of(self, project_id: str, payload: Mapping[str, Any]) -> str:
        if payload.get("gridId") is not None:
            return str(payload["gridId"])
        baseline = self._baselines.get(project_id)
        return baseline.grid_id if baseline is not None else ""

    def _remember(self, project: CrowdyStudioProject) -> CrowdyStudioProject:
        self._baselines[project.project_id] = msgspec.structs.replace(
            project, files=list(project.files)
        )
        return project

    async def _remote_project(self, save: _ProjectSave) -> CrowdyStudioProject | None:
        # A conflict stays actionable when the follow-up read fails.
        try:
            return await self.get_project(save.app_id, save.grid_id, save.project_id)
        except Exception:
            return None

    async def _request(
        self, operation: Operation, variables: Mapping[str, Any] | None = None
    ) -> Any:
        try:
            return await super()._request(operation, variables)
        except (CrowdyNetworkError, CrowdyTimeoutError) as error:
            raise CrowdyStudioOfflineError(error.message, error) from error
        except CrowdyHttpError as error:
            if error.status >= 500:
                raise CrowdyStudioOfflineError(error.message, error) from error
            raise
