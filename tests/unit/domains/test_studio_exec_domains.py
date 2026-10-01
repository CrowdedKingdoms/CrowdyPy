"""Crowdy Studio, its GitHub transport and ck-exec, against the mock transport.

Every ported method is called through its class and checked for the operation it sends,
its variables (BigInt arguments as decimal strings, omitted optionals) and what comes back.
Beyond that: the Studio save path (the file delta, revision conflicts, the GitHub-bound
commit loop, offline errors), CrowdyExecError's rate-limit parsing, the build polling loop,
deploy's digests and base64, and the refusals of mod_client_artifact_bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import httpx
import msgspec
import pytest

from conftest import MockApi, Sent
from crowdypy._generated import inputs
from crowdypy._generated.enums import CrowdyStudioGitHubBindInitial
from crowdypy.domains import exec as exec_module
from crowdypy.domains.crowdy_studio import (
    CrowdyStudioAPI,
    CrowdyStudioOfflineError,
    CrowdyStudioProjectFile,
    CrowdyStudioProjectMetadata,
    CrowdyStudioProjectSummary,
    CrowdyStudioReferenceFile,
    CrowdyStudioRevisionConflictError,
    normalize_crowdy_studio_path,
)
from crowdypy.domains.crowdy_studio_github import (
    INLINE_OPERATIONS,
    CrowdyStudioGitHubConnectStart,
    CrowdyStudioGitHubFile,
    CrowdyStudioGitHubLayout,
    CrowdyStudioGitHubRepo,
    CrowdyStudioGitHubTransport,
    CrowdyStudioGitHubTree,
    CrowdyStudioGitHubTreeEntry,
)
from crowdypy.domains.exec import (
    EXEC_CLIENT_ABI_IMPORTS,
    EXEC_CLIENT_ABI_VERSION,
    EXEC_STATUSES,
    CrowdyExecError,
    ExecAPI,
    ExecCrate,
    ExecDeployResult,
    ExecEndpoint,
    ExecModScope,
    ExecSourceFile,
    ExecStarter,
    exec_mod_type,
    exec_status,
    is_name_list,
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


class GraphQLErrors:
    """An answer that refuses the operation with these GraphQL error entries."""

    def __init__(self, *errors: dict[str, Any]) -> None:
        self.errors = list(errors)


def route(api: MockApi, answers: Mapping[str, Any]) -> None:
    """Answer each operation by name: a root-field value, a callable of the variables
    returning one, or :class:`GraphQLErrors`.
    """

    def responder(sent: Sent) -> Any:
        answer = answers[sent.operation_name or ""]
        if callable(answer):
            answer = answer(sent.variables)
        if isinstance(answer, GraphQLErrors):
            return {"errors": answer.errors}
        root = re.match(r"\s*(\w+)", sent.query[sent.query.index("{") + 1 :])
        assert root is not None
        return {"data": {root.group(1): answer}}

    api.responder = responder


def sent(api: MockApi) -> list[tuple[str | None, dict[str, Any]]]:
    return [(request.operation_name, request.variables) for request in api.sent]


def refusal(code: str, message: str | None = None) -> GraphQLErrors:
    return GraphQLErrors({"message": message or code, "extensions": {"code": code}})


# ---- the method sets ----

PORTED: dict[type, set[str]] = {
    CrowdyStudioAPI: {
        "list_projects",
        "get_project",
        "create_project",
        "save_project",
        "list_personal_library_files",
        "save_personal_library_file",
        "list_common_files",
        "import_reference_file",
    },
    CrowdyStudioGitHubTransport: {
        "status",
        "connect_url",
        "repos",
        "bind",
        "unbind",
        "refresh",
        "layout",
        "tree",
        "get_file",
        "put_file",
        "delete_file",
    },
    # CrowdyJS's ExecAPI; connect and connect_as_developer open an ExecConnection.
    ExecAPI: {
        "connect",
        "connect_as_developer",
        "developer_endpoint",
        "logs",
        "instances",
        "versions",
        "endpoint_stats",
        "status",
        "activate_version",
        "set_enabled",
        "starters",
        "build",
        "build_status",
        "wait_for_build",
        "mod_starter",
        "mod_build",
        "mod_build_status",
        "wait_for_mod_build",
        "mod_deploy",
        "mod_set_enabled",
        "mod_delete",
        "mods",
        "my_mods",
        "mod_logs",
        "mod_publish",
        "mod_listings",
        "mod_unpublish",
        "mod_install",
        "app_mods",
        "mod_switches",
        "mod_set_switch",
        "mod_client_build",
        "mod_client_deploy",
        "mod_client_delete",
        "grid_client_mods",
        "consent_client_mod",
        "trust_author",
        "revoke_client_mod_consent",
        "revoke_author_trust",
        "mod_client_artifact",
        "mod_client_artifact_bytes",
        "endpoint",
        "deploy",
    },
}


@pytest.mark.parametrize("domain", list(PORTED), ids=lambda domain: domain.__name__)
def test_each_class_has_the_crowdyjs_method_set(domain: type) -> None:
    public = {
        name for name, value in vars(domain).items() if callable(value) and not name.startswith("_")
    }
    assert public == PORTED[domain]


# ---- Crowdy Studio: fixtures ----

APP = "80000000000001"
GRID = "17"
SHA_A, SHA_B, SHA_C, SHA_D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)


def file_dto(target: str, path: str, content: str) -> dict[str, Any]:
    return {
        "target": target,
        "path": path,
        "content": content,
        "revision": "1",
        "provenance": "AUTHORED",
        "provenanceLibraryFileId": None,
        "provenanceLibraryRevision": None,
        "provenanceCommonVersionId": None,
        "createdAt": "2026-09-01T00:00:00.000Z",
        "updatedAt": "2026-09-01T00:00:00.000Z",
    }


def project_dto(revision: int = 3, **overrides: Any) -> dict[str, Any]:
    dto: dict[str, Any] = {
        "projectId": "p-1",
        "appId": APP,
        "ownerUserId": "42",
        "gridId": GRID,
        "name": "A project",
        "description": None,
        "serverModuleName": None,
        "clientModuleName": None,
        "pairingPreference": "SERVER_ONLY",
        "sdkVersion": "0.1.8",
        "abiVersion": 0,
        "revision": str(revision),
        "archived": False,
        "archivedAt": None,
        "fileCount": 1,
        "totalBytes": "12",
        "source": "STUDIO",
        "githubOwner": None,
        "githubRepo": None,
        "githubBranch": None,
        "githubSha": None,
        "createdAt": "2026-09-01T00:00:00.000Z",
        "updatedAt": "2026-09-02T00:00:00.000Z",
        "files": [file_dto("SERVER", "src/lib.rs", "fn main() {}")],
    }
    dto.update(overrides)
    return dto


BOUND_FILES = [file_dto("SERVER", "src/lib.rs", "a"), file_dto("CLIENT", "src/lib.rs", "c")]


def bound_dto(revision: int = 3, **overrides: Any) -> dict[str, Any]:
    fields = {
        "source": "GITHUB",
        "githubOwner": "modder",
        "githubRepo": "my-mod",
        "githubBranch": "main",
        "githubSha": SHA_A,
        "pairingPreference": "PAIRED",
        "files": BOUND_FILES,
    }
    return project_dto(revision, **{**fields, **overrides})


#: Metadata and files a bound save leaves exactly as BOUND_FILES has them.
BOUND_META = {"name": "A project", "pairingPreference": "REQUIRED"}
BOUND_SAME = [
    {"target": "SERVER", "path": "src/lib.rs", "content": "a"},
    {"target": "CLIENT", "path": "src/lib.rs", "content": "c"},
]
LAYOUT = {
    "commitSha": SHA_A,
    "server": "server",
    "client": "client/",
    "assets": "assets",
    "fromFile": True,
}
GITHUB_STATUS = {
    "configured": True,
    "connected": True,
    "accountLogin": "modder",
    "accountType": "User",
    "owner": "modder",
    "repo": "my-mod",
    "branch": "main",
    "githubSha": SHA_A,
    "repositorySelection": "selected",
    "installUrl": "https://example.test/installations/1",
}
CONFLICT = {
    "message": "Project p-1 has moved on: expected revision 3 but the current revision is 4.",
    "extensions": {"code": "CROWDY_STUDIO_REVISION_CONFLICT"},
}


# ---- Crowdy Studio: reads ----


async def test_list_projects_maps_summaries(graphql: AsyncGraphQLClient, api: MockApi) -> None:
    summary = {
        "projectId": "p-1",
        "gridId": GRID,
        "name": "A project",
        "serverModuleName": None,
        "clientModuleName": None,
        "pairingPreference": "SERVER_ONLY",
        "revision": "3",
        "archived": False,
        "source": "STUDIO",
        "githubOwner": None,
        "githubRepo": None,
        "githubBranch": None,
        "githubSha": None,
        "updatedAt": "2026-09-02T00:00:00.000Z",
    }
    bound = {
        **summary,
        "projectId": "p-2",
        "pairingPreference": "PAIRED",
        "serverModuleName": "srv",
        "source": "GITHUB",
        "githubOwner": "modder",
        "githubRepo": "my-mod",
        "githubBranch": "main",
        "githubSha": SHA_A,
    }
    half_bound = {
        **bound,
        "projectId": "p-3",
        "pairingPreference": "CLIENT_ONLY",
        "githubBranch": None,
    }
    api.reply_with_root([summary, bound, half_bound])

    projects = await CrowdyStudioAPI(graphql).list_projects(int(APP))

    assert api.last.operation_name == "CrowdyStudioProjects"
    assert api.last.variables == {"appId": APP, "includeArchived": False, "limit": 50, "offset": 0}
    assert projects == [
        CrowdyStudioProjectSummary(
            project_id="p-1",
            name="A project",
            kind="SERVER",
            revision_id="3",
            source="STUDIO",
            github_sha=None,
            updated_at="2026-09-02T00:00:00.000Z",
        ),
        CrowdyStudioProjectSummary(
            project_id="p-2",
            name="A project",
            kind="FULL_STACK",
            revision_id="3",
            server_module_name="srv",
            source="GITHUB",
            github="modder/my-mod@main",
            github_sha=SHA_A,
            updated_at="2026-09-02T00:00:00.000Z",
        ),
        CrowdyStudioProjectSummary(
            project_id="p-3",
            name="A project",
            kind="CLIENT",
            revision_id="3",
            server_module_name="srv",
            source="STUDIO",
            github_sha=None,
            updated_at="2026-09-02T00:00:00.000Z",
        ),
    ]


async def test_get_project_maps_the_project(graphql: AsyncGraphQLClient, api: MockApi) -> None:
    api.reply_with_root(
        bound_dto(
            gridId=None,
            description="d",
            clientModuleName="",
            files=[file_dto("SERVER", "./src\\lib.rs", "a"), file_dto("CLIENT", "src/hud.rs", "h")],
        )
    )

    project = await CrowdyStudioAPI(graphql).get_project(APP, 99, "p-1")

    assert api.last.operation_name == "CrowdyStudioProject"
    assert api.last.variables == {"appId": APP, "projectId": "p-1"}
    assert msgspec.to_builtins(project) == {
        "projectId": "p-1",
        "appId": APP,
        "gridId": "99",
        "kind": "FULL_STACK",
        "metadata": {
            "name": "A project",
            "description": "d",
            "serverModuleName": None,
            "clientModuleName": None,
            "pairingPreference": "REQUIRED",
        },
        "files": [
            {"target": "SERVER", "path": "src/lib.rs", "content": "a"},
            {"target": "CLIENT", "path": "src/hud.rs", "content": "h"},
        ],
        "sdkVersion": "0.1.8",
        "abiVersion": 0,
        "revision": {"id": "3", "savedAt": "2026-09-02T00:00:00.000Z"},
        "source": "GITHUB",
        "github": {"owner": "modder", "repo": "my-mod", "branch": "main", "sha": SHA_A},
        "createdAt": "2026-09-01T00:00:00.000Z",
        "updatedAt": "2026-09-02T00:00:00.000Z",
    }


async def test_a_project_with_a_grid_keeps_it(graphql: AsyncGraphQLClient, api: MockApi) -> None:
    api.reply_with_root(project_dto(pairingPreference="INDEPENDENT"))
    project = await CrowdyStudioAPI(graphql).get_project(APP, 99, "p-1")
    assert project.grid_id == GRID
    assert project.kind == "FULL_STACK"
    assert project.metadata.pairing_preference == "OPTIONAL"
    assert project.source == "STUDIO"
    assert project.github is None


# ---- Crowdy Studio: create and save ----


@pytest.mark.parametrize(
    ("kind", "pairing", "api_pairing"),
    [
        ("SERVER", "NONE", "SERVER_ONLY"),
        ("CLIENT", "REQUIRED", "CLIENT_ONLY"),
        ("FULL_STACK", "REQUIRED", "PAIRED"),
        ("FULL_STACK", "OPTIONAL", "INDEPENDENT"),
        ("FULL_STACK", "NONE", "INDEPENDENT"),
    ],
)
async def test_create_project_sends_the_api_shape(
    graphql: AsyncGraphQLClient, api: MockApi, kind: Any, pairing: str, api_pairing: str
) -> None:
    api.reply_with_root(project_dto(gridId=None))

    project = await CrowdyStudioAPI(graphql).create_project(
        APP,
        int(GRID),
        kind,
        {"name": "A project", "serverModuleName": "srv", "pairingPreference": pairing},
        [{"target": "SERVER", "path": "./src/lib.rs", "content": "x"}],
    )

    assert api.last.operation_name == "CrowdyStudioProjectCreate"
    assert api.last.variables == {
        "input": {
            "appId": APP,
            "gridId": GRID,
            "name": "A project",
            "description": None,
            "serverModuleName": "srv",
            "clientModuleName": None,
            "pairingPreference": api_pairing,
            "sdkVersion": "0.1.8",
            "abiVersion": 0,
            "initialFiles": [{"target": "SERVER", "path": "src/lib.rs", "content": "x"}],
        }
    }
    assert project.grid_id == GRID


async def test_save_project_reads_a_baseline_then_sends_only_the_delta(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    baseline = project_dto(
        pairingPreference="PAIRED",
        files=[
            file_dto("SERVER", "src/lib.rs", "a"),
            file_dto("SERVER", "src/util.rs", "u"),
            file_dto("CLIENT", "src/lib.rs", "c"),
        ],
    )
    saved = project_dto(revision=4, name="Renamed")
    route(api, {"CrowdyStudioProject": baseline, "CrowdyStudioProjectSave": saved})
    studio = CrowdyStudioAPI(graphql)
    files = [
        CrowdyStudioProjectFile(target="SERVER", path="src/lib.rs", content="a"),
        {"target": "SERVER", "path": "./src/util.rs", "content": "u2"},
        {"target": "SERVER", "path": "src/new.rs", "content": "n"},
    ]

    project = await studio.save_project(
        APP,
        GRID,
        "p-1",
        3,
        CrowdyStudioProjectMetadata(name="Renamed", pairing_preference="NONE"),
        files,
    )

    assert project.revision.id == "4"
    assert sent(api) == [
        ("CrowdyStudioProject", {"appId": APP, "projectId": "p-1"}),
        (
            "CrowdyStudioProjectSave",
            {
                "input": {
                    "appId": APP,
                    "projectId": "p-1",
                    "expectedRevision": "3",
                    "gridId": GRID,
                    "name": "Renamed",
                    "description": None,
                    "serverModuleName": None,
                    "clientModuleName": None,
                    "pairingPreference": "SERVER_ONLY",
                    "upserts": [
                        {"target": "SERVER", "path": "src/util.rs", "content": "u2"},
                        {"target": "SERVER", "path": "src/new.rs", "content": "n"},
                    ],
                    "deletes": [{"target": "CLIENT", "path": "src/lib.rs"}],
                }
            },
        ),
    ]

    # The saved project is the next baseline: no read, and an unchanged file is not sent.
    await studio.save_project(
        APP, GRID, "p-1", "4", {"name": "Renamed", "pairingPreference": "NONE"}, project.files
    )
    assert [operation for operation, _ in sent(api)][2:] == ["CrowdyStudioProjectSave"]
    assert api.last.variables["input"]["upserts"] == []
    assert api.last.variables["input"]["deletes"] == []


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(CONFLICT, id="by-code"),
        pytest.param(
            {
                "message": "CROWDY_STUDIO_REVISION_CONFLICT: expected 3, found 4",
                "extensions": {"code": "CONFLICT"},
            },
            id="by-message",
        ),
    ],
)
async def test_a_revision_conflict_carries_the_remote_project(
    graphql: AsyncGraphQLClient, api: MockApi, error: dict[str, Any]
) -> None:
    route(
        api,
        {
            "CrowdyStudioProject": project_dto(revision=4),
            "CrowdyStudioProjectSave": GraphQLErrors(error),
        },
    )

    with pytest.raises(CrowdyStudioRevisionConflictError) as raised:
        await CrowdyStudioAPI(graphql).save_project(
            APP, GRID, "p-1", "3", {"name": "A project", "pairingPreference": "NONE"}, []
        )

    assert raised.value.code == "PROJECT_REVISION_CONFLICT"
    assert raised.value.message == error["message"]
    assert raised.value.remote_project is not None
    assert raised.value.remote_project.revision.id == "4"
    assert isinstance(raised.value.__cause__, CrowdyGraphQLError)
    assert [operation for operation, _ in sent(api)] == [
        "CrowdyStudioProject",
        "CrowdyStudioProjectSave",
        "CrowdyStudioProject",
    ]


async def test_an_unrelated_conflict_is_raised_untouched(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    route(
        api,
        {
            "CrowdyStudioProject": project_dto(),
            "CrowdyStudioProjectSave": refusal("CONFLICT", "Idempotency key reused"),
        },
    )
    with pytest.raises(CrowdyGraphQLError) as raised:
        await CrowdyStudioAPI(graphql).save_project(
            APP, GRID, "p-1", "3", {"name": "A project", "pairingPreference": "NONE"}, []
        )
    assert not isinstance(raised.value, CrowdyStudioRevisionConflictError)
    assert raised.value.code == "CONFLICT"


async def test_a_conflict_survives_a_failed_follow_up_read(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    reads: list[Any] = [project_dto(), refusal("INTERNAL")]
    route(
        api,
        {
            "CrowdyStudioProject": lambda _: reads.pop(0),
            "CrowdyStudioProjectSave": GraphQLErrors(CONFLICT),
        },
    )
    with pytest.raises(CrowdyStudioRevisionConflictError) as raised:
        await CrowdyStudioAPI(graphql).save_project(
            APP, GRID, "p-1", "3", {"name": "A project", "pairingPreference": "NONE"}, []
        )
    assert raised.value.remote_project is None


# ---- Crowdy Studio: saving a GitHub-bound project ----


async def test_a_bound_save_commits_each_changed_file_on_the_last_commit(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    baseline = bound_dto(files=[*BOUND_FILES, file_dto("SERVER", "src/old.rs", "o")])
    projects = [baseline, bound_dto(revision=4, githubSha=SHA_D)]
    commits = iter([SHA_B, SHA_C])
    route(
        api,
        {
            "CrowdyStudioProject": lambda _: projects.pop(0),
            "CrowdyStudioGitHubLayout": LAYOUT,
            "CrowdyStudioGitHubPutFile": lambda variables: {
                "path": variables["input"]["path"],
                "content": variables["input"]["content"],
                "sha": "blob",
                "commitSha": next(commits),
            },
            "CrowdyStudioGitHubDeleteFile": {**GITHUB_STATUS, "githubSha": SHA_D},
        },
    )

    project = await CrowdyStudioAPI(graphql).save_project(
        APP,
        GRID,
        "p-1",
        "3",
        BOUND_META,
        [
            {"target": "SERVER", "path": "src/lib.rs", "content": "a2"},
            {"target": "CLIENT", "path": "src/lib.rs", "content": "c"},
            {"target": "CLIENT", "path": "./src/hud.rs", "content": "h"},
        ],
    )

    assert project.revision.id == "4"
    assert project.github is not None
    assert project.github.sha == SHA_D
    scope = {"appId": APP, "projectId": "p-1"}
    assert sent(api) == [
        ("CrowdyStudioProject", {"appId": APP, "projectId": "p-1"}),
        ("CrowdyStudioGitHubLayout", {"input": {**scope, "commitSha": SHA_A}}),
        (
            "CrowdyStudioGitHubPutFile",
            {
                "input": {
                    **scope,
                    "path": "server/src/lib.rs",
                    "content": "a2",
                    "message": "studio: update server/src/lib.rs",
                    "expectedCommitSha": SHA_A,
                }
            },
        ),
        (
            "CrowdyStudioGitHubPutFile",
            {
                "input": {
                    **scope,
                    "path": "client/src/hud.rs",
                    "content": "h",
                    "message": "studio: update client/src/hud.rs",
                    "expectedCommitSha": SHA_B,
                }
            },
        ),
        (
            "CrowdyStudioGitHubDeleteFile",
            {
                "input": {
                    **scope,
                    "path": "server/src/old.rs",
                    "message": "studio: delete server/src/old.rs",
                    "expectedCommitSha": SHA_C,
                }
            },
        ),
        ("CrowdyStudioProject", {"appId": APP, "projectId": "p-1"}),
    ]


async def test_a_bound_save_saves_changed_metadata_without_file_bodies(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    renamed = bound_dto(revision=4, name="Renamed")
    projects = [bound_dto(), renamed]
    route(
        api, {"CrowdyStudioProject": lambda _: projects.pop(0), "CrowdyStudioProjectSave": renamed}
    )

    await CrowdyStudioAPI(graphql).save_project(
        APP, GRID, "p-1", "3", {**BOUND_META, "name": "Renamed"}, BOUND_SAME
    )

    assert [operation for operation, _ in sent(api)] == [
        "CrowdyStudioProject",
        "CrowdyStudioProjectSave",
        "CrowdyStudioProject",
    ]
    assert api.sent[1].variables["input"] == {
        "appId": APP,
        "projectId": "p-1",
        "expectedRevision": "3",
        "gridId": GRID,
        "name": "Renamed",
        "description": None,
        "serverModuleName": None,
        "clientModuleName": None,
        "pairingPreference": "PAIRED",
        "upserts": [],
        "deletes": [],
    }


async def test_a_bound_save_refuses_a_stale_revision_before_writing(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    route(api, {"CrowdyStudioProject": bound_dto()})

    with pytest.raises(
        CrowdyStudioRevisionConflictError,
        match=r"^CROWDY_STUDIO_REVISION_CONFLICT: expected project revision 2; current revision is 3\.$",
    ) as raised:
        await CrowdyStudioAPI(graphql).save_project(APP, GRID, "p-1", 2, BOUND_META, BOUND_SAME)

    assert raised.value.remote_project is not None
    assert [operation for operation, _ in sent(api)] == ["CrowdyStudioProject"] * 2


async def test_a_stale_commit_is_a_revision_conflict(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    projects = [bound_dto(), bound_dto(revision=4, githubSha=SHA_B)]
    route(
        api,
        {
            "CrowdyStudioProject": lambda _: projects.pop(0),
            "CrowdyStudioGitHubLayout": LAYOUT,
            "CrowdyStudioGitHubPutFile": refusal("GITHUB_STALE_SHA", "the branch moved"),
        },
    )

    with pytest.raises(CrowdyStudioRevisionConflictError, match="the branch moved") as raised:
        await CrowdyStudioAPI(graphql).save_project(
            APP, GRID, "p-1", "3", BOUND_META, [{**BOUND_SAME[0], "content": "a2"}, BOUND_SAME[1]]
        )

    remote = raised.value.remote_project
    assert remote is not None
    assert remote.github is not None
    assert remote.github.sha == SHA_B


async def test_a_bound_save_refuses_a_file_its_layout_has_no_root_for(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    route(
        api,
        {
            "CrowdyStudioProject": bound_dto(),
            "CrowdyStudioGitHubLayout": {**LAYOUT, "client": None},
        },
    )
    with pytest.raises(
        CrowdyError, match=re.escape("has no client directory, so src/hud.rs has nowhere to go.")
    ) as raised:
        await CrowdyStudioAPI(graphql).save_project(
            APP,
            GRID,
            "p-1",
            "3",
            BOUND_META,
            [*BOUND_SAME, {"target": "CLIENT", "path": "src/hud.rs", "content": "h"}],
        )
    assert not isinstance(raised.value, CrowdyStudioRevisionConflictError)
    assert "CrowdyStudioGitHubPutFile" not in [operation for operation, _ in sent(api)]


# ---- Crowdy Studio: offline ----


async def test_a_server_error_is_offline(graphql: AsyncGraphQLClient, api: MockApi) -> None:
    api.status = 503
    with pytest.raises(CrowdyStudioOfflineError) as raised:
        await CrowdyStudioAPI(graphql).list_projects(APP)
    assert raised.value.code == "PROJECT_OFFLINE"
    assert isinstance(raised.value.cause, CrowdyHttpError)


async def test_a_client_error_is_not_offline(graphql: AsyncGraphQLClient, api: MockApi) -> None:
    api.status = 400
    with pytest.raises(CrowdyHttpError):
        await CrowdyStudioAPI(graphql).list_common_files(APP)


@pytest.mark.parametrize(
    ("failure", "cause"),
    [
        (httpx.ConnectError("refused"), CrowdyNetworkError),
        (httpx.ReadTimeout("slow"), CrowdyTimeoutError),
    ],
)
async def test_network_failures_are_offline(
    graphql: AsyncGraphQLClient, api: MockApi, failure: Exception, cause: type
) -> None:
    def fail(_: Sent) -> Any:
        raise failure

    api.responder = fail
    with pytest.raises(CrowdyStudioOfflineError) as raised:
        await CrowdyStudioAPI(graphql).get_project(APP, GRID, "p-1")
    assert isinstance(raised.value.cause, cause)


# ---- Crowdy Studio: the personal library and common files ----

LIBRARY_ROW = {
    "libraryFileId": "lib-1",
    "appId": APP,
    "ownerUserId": "42",
    "title": "Util",
    "pathHint": "./src/util.rs",
    "target": "SERVER",
    "tags": ["math"],
    "content": "pub fn x() {}",
    "revision": "1",
    "archived": False,
    "archivedAt": None,
    "createdAt": "t0",
    "updatedAt": "t1",
}
COMMON_ROW = {
    "commonFileId": "cf-1",
    "appId": APP,
    "slug": "vec",
    "title": "Vectors",
    "description": None,
    "path": "src/vec.rs",
    "target": "CLIENT",
    "tags": [],
    "status": "PUBLISHED",
    "versionId": "v-1",
    "versionNo": "2",
    "content": "pub struct V;",
    "contentSha256": "e" * 64,
    "publishedByUserId": "1",
    "publishedAt": "t0",
    "createdAt": "t0",
    "updatedAt": "t2",
}
LIBRARY_FILE = CrowdyStudioReferenceFile(
    id="lib-1",
    source="PERSONAL_LIBRARY",
    title="Util",
    target="SERVER",
    path="src/util.rs",
    content="pub fn x() {}",
    tags=["math"],
    updated_at="t1",
)


async def test_list_personal_library_files(graphql: AsyncGraphQLClient, api: MockApi) -> None:
    api.reply_with_root([LIBRARY_ROW])
    assert await CrowdyStudioAPI(graphql).list_personal_library_files(APP) == [LIBRARY_FILE]
    assert api.last.operation_name == "CrowdyStudioLibraryFiles"
    assert api.last.variables == {"appId": APP, "includeArchived": False, "limit": 100, "offset": 0}


@pytest.mark.parametrize(("tags", "sent_tags"), [(None, []), (("math", "util"), ["math", "util"])])
async def test_save_personal_library_file(
    graphql: AsyncGraphQLClient, api: MockApi, tags: Any, sent_tags: list[str]
) -> None:
    api.reply_with_root(LIBRARY_ROW)
    saved = await CrowdyStudioAPI(graphql).save_personal_library_file(
        int(APP), "Util", "SERVER", ".\\src\\util.rs", "pub fn x() {}", tags
    )
    assert saved == LIBRARY_FILE
    assert api.last.operation_name == "CrowdyStudioLibrarySave"
    assert api.last.variables == {
        "input": {
            "appId": APP,
            "title": "Util",
            "pathHint": "src/util.rs",
            "target": "SERVER",
            "tags": sent_tags,
            "content": "pub fn x() {}",
        }
    }


async def test_list_common_files(graphql: AsyncGraphQLClient, api: MockApi) -> None:
    api.reply_with_root([COMMON_ROW])
    assert await CrowdyStudioAPI(graphql).list_common_files(APP) == [
        CrowdyStudioReferenceFile(
            id="v-1",
            source="COMMON",
            title="Vectors",
            target="CLIENT",
            path="src/vec.rs",
            content="pub struct V;",
            tags=[],
            updated_at="t2",
        )
    ]
    assert api.last.operation_name == "CrowdyStudioCommonFiles"
    assert api.last.variables == {"appId": APP, "limit": 100, "offset": 0}


@pytest.mark.parametrize(
    ("source", "destination", "sent_source"),
    [
        (
            "PERSONAL_LIBRARY",
            "./src/x.rs",
            {"source": "LIBRARY", "libraryFileId": "ref-1", "destinationPath": "src/x.rs"},
        ),
        ("COMMON", None, {"source": "COMMON", "commonVersionId": "ref-1"}),
    ],
)
async def test_import_reference_file(
    graphql: AsyncGraphQLClient,
    api: MockApi,
    source: Any,
    destination: str | None,
    sent_source: dict[str, Any],
) -> None:
    api.reply_with_root(project_dto(revision=4, gridId=None))
    project = await CrowdyStudioAPI(graphql).import_reference_file(
        APP, 99, "p-1", 3, source, "ref-1", destination
    )
    assert project.grid_id == "99"
    assert api.last.operation_name == "CrowdyStudioProjectImportFile"
    assert api.last.variables == {
        "input": {"appId": APP, "projectId": "p-1", "expectedProjectRevision": "3", **sent_source}
    }


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("src/lib.rs", "src/lib.rs"),
        ("./src\\lib.rs", "src/lib.rs"),
        (".///src/lib.rs", "src/lib.rs"),
        ("  src/lib.rs\n", "src/lib.rs"),
        ("\ufeffsrc/lib.rs\u3000", "src/lib.rs"),
        ("é" * 240, "é" * 240),
        ("😀" * 120, "😀" * 120),
    ],
)
def test_project_paths_normalize_as_crowdyjs_does(raw: str, normalized: str) -> None:
    assert normalize_crowdy_studio_path(raw) == normalized


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "/src/lib.rs",
        "src/",
        "src//lib.rs",
        "src/./lib.rs",
        "../lib.rs",
        "./../lib.rs",
        "src/\x00.rs",
        "\x1fsrc/lib.rs",
        "src/lib.rs\x7f",
        "a" * 241,
        "😀" * 121,
    ],
)
def test_invalid_project_paths_are_refused(raw: str) -> None:
    with pytest.raises(CrowdyProtocolError, match="Invalid project file path"):
        normalize_crowdy_studio_path(raw)


# ---- the GitHub transport ----


async def test_the_github_transport_sends_named_operations_with_the_project_scope(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    unbound = {**GITHUB_STATUS, "owner": None, "repo": None, "branch": None, "githubSha": None}
    file = {"path": "server/src/lib.rs", "content": "fn a(){}", "sha": "abc", "commitSha": SHA_A}
    route(
        api,
        {
            "CrowdyStudioGitHubStatus": GITHUB_STATUS,
            "CrowdyStudioGitHubConnectUrl": {"connectUrl": "https://example.test/install?state=x"},
            "CrowdyStudioGitHubRepos": [
                {
                    "owner": "modder",
                    "name": "my-mod",
                    "fullName": "modder/my-mod",
                    "private": True,
                    "defaultBranch": "main",
                }
            ],
            "CrowdyStudioGitHubBind": GITHUB_STATUS,
            "CrowdyStudioGitHubUnbind": unbound,
            "CrowdyStudioGitHubRefresh": {**GITHUB_STATUS, "githubSha": SHA_B},
            "CrowdyStudioGitHubLayout": LAYOUT,
            "CrowdyStudioGitHubTree": {
                "commitSha": SHA_A,
                "entries": [{"path": "server", "type": "tree", "sha": None, "size": None}],
            },
            "CrowdyStudioGitHubFile": file,
            "CrowdyStudioGitHubPutFile": {**file, "content": "fn b(){}", "commitSha": SHA_B},
            "CrowdyStudioGitHubDeleteFile": {**GITHUB_STATUS, "githubSha": SHA_C},
        },
    )
    github = CrowdyStudioGitHubTransport(graphql)
    scope = {"appId": "89", "projectId": "p1"}

    status = await github.status(app_id=89, project_id="p1")
    assert msgspec.to_builtins(status) == GITHUB_STATUS
    assert await github.connect_url() == CrowdyStudioGitHubConnectStart(
        connect_url="https://example.test/install?state=x"
    )
    assert await github.repos() == [
        CrowdyStudioGitHubRepo(
            owner="modder",
            name="my-mod",
            full_name="modder/my-mod",
            private=True,
            default_branch="main",
        )
    ]
    bind = inputs.BindCrowdyStudioGitHubInput(
        app_id="89",
        project_id="p1",
        owner="modder",
        repo="my-mod",
        initial=CrowdyStudioGitHubBindInitial.PUSH_PROJECT,
    )
    assert (await github.bind(bind)).branch == "main"
    assert (await github.unbind(scope)).owner is None
    assert (await github.refresh(scope)).github_sha == SHA_B
    assert await github.layout({**scope, "commitSha": SHA_A}) == CrowdyStudioGitHubLayout(
        commit_sha=SHA_A, server="server", client="client/", assets="assets", from_file=True
    )
    assert await github.tree(scope) == CrowdyStudioGitHubTree(
        commit_sha=SHA_A, entries=[CrowdyStudioGitHubTreeEntry(path="server", type="tree")]
    )
    assert await github.get_file({**scope, "path": "server/src/lib.rs"}) == CrowdyStudioGitHubFile(
        path="server/src/lib.rs", content="fn a(){}", sha="abc", commit_sha=SHA_A
    )
    put = {
        **scope,
        "path": "server/src/lib.rs",
        "content": "fn b(){}",
        "message": "m",
        "expectedCommitSha": SHA_A,
    }
    assert (await github.put_file(put)).commit_sha == SHA_B
    delete = {**scope, "path": "server/src/old.rs", "message": "rm", "expectedCommitSha": SHA_B}
    assert (await github.delete_file(delete)).github_sha == SHA_C

    assert sent(api) == [
        ("CrowdyStudioGitHubStatus", {"appId": "89", "projectId": "p1"}),
        ("CrowdyStudioGitHubConnectUrl", {}),
        ("CrowdyStudioGitHubRepos", {}),
        (
            "CrowdyStudioGitHubBind",
            {"input": {**scope, "owner": "modder", "repo": "my-mod", "initial": "PUSH_PROJECT"}},
        ),
        ("CrowdyStudioGitHubUnbind", {"input": scope}),
        ("CrowdyStudioGitHubRefresh", {"input": scope}),
        ("CrowdyStudioGitHubLayout", {"input": {**scope, "commitSha": SHA_A}}),
        ("CrowdyStudioGitHubTree", {"input": scope}),
        ("CrowdyStudioGitHubFile", {"input": {**scope, "path": "server/src/lib.rs"}}),
        ("CrowdyStudioGitHubPutFile", {"input": put}),
        ("CrowdyStudioGitHubDeleteFile", {"input": delete}),
    ]
    # Each request carries its inline document, which never selects a token.
    assert [request.query for request in api.sent] == [op.document for op in INLINE_OPERATIONS]
    assert not re.search(r"token|secret", api.sent[0].query, re.I)


async def test_github_status_without_a_project_and_repos_without_any(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    route(
        api,
        {
            "CrowdyStudioGitHubStatus": {"configured": True, "connected": False},
            "CrowdyStudioGitHubRepos": None,
        },
    )
    github = CrowdyStudioGitHubTransport(graphql)
    assert (await github.status()).install_url is None
    assert api.last.variables == {}
    assert await github.repos() == []


# ---- ck-exec: errors, statuses and constants ----


@pytest.mark.parametrize(
    ("status", "message", "rate_limited", "retry_after_ms", "retryable"),
    [
        ("Busy", "rate limited: 120 calls per 10 s; retry in 1500 ms", True, 1500, True),
        ("RateLimited", "slow down, retry in 20 ms", True, 20, True),
        ("RateLimited", "slow down", True, None, True),
        ("Busy", "the hub is busy, retry in 5 ms", False, None, True),
        ("Busy", "rate limited, retry soon", True, None, True),
        ("AppError", "rate limited by the game, retry in 9 ms", False, None, False),
        ("Moved", "elsewhere", False, None, True),
        ("Denied", "no", False, None, False),
    ],
)
def test_crowdy_exec_error_reads_rate_limits(
    status: Any, message: str, rate_limited: bool, retry_after_ms: int | None, retryable: bool
) -> None:
    cause = RuntimeError("socket")
    error = CrowdyExecError(status, message, cause)
    assert isinstance(error, CrowdyError)
    assert str(error) == f"{status}: {message}"
    assert error.status == status
    assert error.rate_limited is rate_limited
    assert error.retry_after_ms == retry_after_ms
    assert error.retryable is retryable
    assert error.cause is cause


def test_statuses_and_helpers() -> None:
    assert len(EXEC_STATUSES) == 12
    assert [exec_status(0), exec_status(2), exec_status(11)] == ["Ok", "Busy", "BadRequest"]
    assert [exec_status(12), exec_status(-1)] == ["Unknown", "Unknown"]
    assert exec_mod_type("turret") == "mod:turret"
    assert is_name_list(["hud_set", "log"])
    assert is_name_list([])
    assert not is_name_list(["hud_set", {"fn": "voxel_set"}])
    assert not is_name_list("hud_set")
    assert EXEC_CLIENT_ABI_VERSION == 0
    assert dict(EXEC_CLIENT_ABI_IMPORTS) == {
        "ck": ("log", "now_ms", "state_get", "state_set", "host_call"),
        "wasi_snapshot_preview1": ("random_get",),
    }


# ---- ck-exec: fixtures ----

WASM = bytes([0, 97, 115, 109, 1, 0, 0, 0])
DIGEST = hashlib.sha256(WASM).hexdigest()
SUMMARY = {
    "version": 1,
    "target": "client",
    "imports": ["ck.host_call", "ck.log"],
    "hostFunctions": ["hud_set"],
    "capabilityGroups": ["present"],
    "presentationHooks": ["hud_set"],
    "exportedFunctions": ["ck_alloc", "ck_free", "handle_invoke", "init", "tick"],
}
SUMMARY_JSON = json.dumps(SUMMARY)
HASH = "c" * 64
FLOW = "0123456789abcdef0123456789abcdef"
LINE = {
    "id": "9",
    "nodeType": "arena",
    "key": "m1",
    "level": 2,
    "host": "h",
    "at": "2026-09-25T00:00:00.000Z",
    "text": "hi",
    "flow": FLOW,
}
APP_STATUS = {
    "activeVersion": 2,
    "disabled": False,
    "disabledTypes": ["bare"],
    "budgetPaused": False,
}
BUILD = {
    "buildId": "b1",
    "status": "queued",
    "kind": "exec",
    "log": None,
    "createdAt": "t",
    "startedAt": None,
    "finishedAt": None,
    "artifacts": [],
}
MOD = {
    "modId": "900",
    "gridId": "5",
    "name": "turret",
    "ownerId": "42",
    "version": 1,
    "digest": "ab" * 32,
    "enabled": False,
    "listingId": None,
    "blocked": None,
    "running": False,
    "updatedAt": "t",
}
LISTING = {
    "listingId": "555",
    "title": "Shop",
    "description": None,
    "publisherId": "42",
    "sourceModId": "900",
    "sourceVersion": 1,
    "digest": "ab" * 32,
    "installs": 0,
    "clientDigest": None,
    "clientCapabilitySummaryJson": None,
    "clientCapabilityHash": None,
    "clientTickIntervalMs": None,
    "createdAt": "t",
    "delistedAt": None,
}
LISTING_WITH_CLIENT = {
    **LISTING,
    "listingId": "556",
    "clientDigest": DIGEST,
    "clientCapabilitySummaryJson": SUMMARY_JSON,
    "clientCapabilityHash": HASH,
    "clientTickIntervalMs": 250,
}
SWITCH = {"scope": "GRID", "target": "5", "reason": None, "createdBy": "user:1", "createdAt": "t"}
STARTER = {
    "crate": "grid-mod",
    "nodeType": "mod",
    "description": "d",
    "files": [{"path": "Cargo.toml", "content": "c"}],
}


def artifact_answer(**overrides: Any) -> dict[str, Any]:
    return {
        "modId": "900",
        "name": "hud",
        "gridId": "5",
        "clientVersion": 2,
        "digest": DIGEST,
        "wasmBase64": base64.b64encode(WASM).decode("ascii"),
        "sizeBytes": len(WASM),
        "capabilitySummaryJson": SUMMARY_JSON,
        "capabilityHash": HASH,
        "tickIntervalMs": 250,
        "fuelPerDispatch": "100000000",
        "abiVersion": 0,
        **overrides,
    }


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Each interval the build polls slept, in seconds, without waiting it out."""
    intervals: list[float] = []

    async def record(seconds: float) -> None:
        intervals.append(seconds)

    monkeypatch.setattr(exec_module, "sleep", record)
    return intervals


# ---- ck-exec: endpoints and operating an app ----


async def test_endpoints_carry_a_redacted_connect_token(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    endpoint = {
        "gatewayUrl": "wss://gateway.example.test",
        "token": "connect-secret",
        "host": "host-1",
        "expiresAt": "2026-09-30T00:01:00.000Z",
    }
    api.reply_with_root(endpoint)
    exec_api = ExecAPI(graphql)

    player = await exec_api.endpoint(77, node_type="arena", key="m1")
    assert sent(api)[-1] == ("ExecConnect", {"appId": "77", "nodeType": "arena", "key": "m1"})
    assert player == ExecEndpoint(
        gateway_url="wss://gateway.example.test",
        token="connect-secret",
        host="host-1",
        expires_at="2026-09-30T00:01:00.000Z",
    )
    assert "connect-secret" not in repr(player)

    await exec_api.endpoint("77")
    assert sent(api)[-1] == ("ExecConnect", {"appId": "77"})
    developer = await exec_api.developer_endpoint("77", node_type="bare")
    assert sent(api)[-1] == ("ExecConnectAsDeveloper", {"appId": "77", "nodeType": "bare"})
    assert developer.token == "connect-secret"


async def test_operating_an_app_passes_arguments_and_maps_results(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    manifest = {
        "root": "lobby",
        "types": {
            "lobby": {"kind": "hub", "client": True, "seed_bytes": 4},
            "arena": {"kind": "hub", "parent": "lobby", "calls": ["lobby"]},
        },
    }
    instance = {
        "instanceId": "1",
        "nodeType": "lobby",
        "key": "",
        "kind": "hub",
        "phase": "running",
        "host": "h",
        "epoch": 3,
        "sinceMs": 5,
        "heldBack": None,
    }
    versions = [
        {
            "version": 2,
            "createdBy": "user:1",
            "createdAt": "t2",
            "types": 2,
            "active": True,
            "manifestJson": json.dumps(manifest),
        },
        {
            "version": 1,
            "createdBy": None,
            "createdAt": "t1",
            "types": 1,
            "active": False,
            "manifestJson": None,
        },
    ]
    stat = {
        "nodeType": "arena",
        "method": "hit",
        "calls": 40,
        "appErrors": 1,
        "busy": 3,
        "denied": 0,
        "deadlineExceeded": 0,
        "otherErrors": 0,
        "timedCalls": 37,
        "latencyMsAvg": 1.5,
        "latencyMsMax": 9,
        "firstMinute": "2026-09-26T10:00:00.000Z",
        "lastMinute": "2026-09-26T10:59:00.000Z",
    }
    route(
        api,
        {
            "ExecLogs": [LINE],
            "ExecInstances": [instance],
            "ExecVersions": versions,
            "ExecEndpointStats": [stat],
            "ExecAppStatus": APP_STATUS,
            "ExecActivateVersion": APP_STATUS,
            "ExecSetEnabled": APP_STATUS,
        },
    )
    exec_api = ExecAPI(graphql)

    [line] = await exec_api.logs(77, node_type="arena", max_level=1, limit=10, flow=FLOW)
    assert msgspec.to_builtins(line) == LINE
    await exec_api.logs("77", key="m1", before="8")
    assert msgspec.to_builtins(await exec_api.instances("77")) == [instance]
    active, gone = await exec_api.versions("77")
    assert active.manifest_json == json.dumps(manifest)
    assert active.manifest == manifest
    assert gone.manifest is None
    assert msgspec.to_builtins(
        await exec_api.endpoint_stats("77", node_type="arena", since_minutes=30)
    ) == [stat]
    status = await exec_api.status("77")
    assert msgspec.to_builtins(status) == APP_STATUS
    assert await exec_api.activate_version("77", 1) == status
    assert await exec_api.set_enabled("77", False, "bare") == status
    await exec_api.set_enabled("77", True)

    assert sent(api) == [
        (
            "ExecLogs",
            {"appId": "77", "nodeType": "arena", "maxLevel": 1, "limit": 10, "flow": FLOW},
        ),
        ("ExecLogs", {"appId": "77", "key": "m1", "before": "8"}),
        ("ExecInstances", {"appId": "77"}),
        ("ExecVersions", {"appId": "77"}),
        ("ExecEndpointStats", {"appId": "77", "nodeType": "arena", "sinceMinutes": 30}),
        ("ExecAppStatus", {"appId": "77"}),
        ("ExecActivateVersion", {"appId": "77", "version": 1}),
        ("ExecSetEnabled", {"appId": "77", "enabled": False, "nodeType": "bare"}),
        ("ExecSetEnabled", {"appId": "77", "enabled": True}),
    ]


# ---- ck-exec: starters, builds and the build poll ----


async def test_starters_build_and_the_build_poll(
    graphql: AsyncGraphQLClient, api: MockApi, slept: list[float]
) -> None:
    artifact = {
        "crate": "world-tick",
        "digest": "ab" * 32,
        "sizeBytes": 9,
        "capabilitySummaryJson": None,
        "capabilityHash": None,
        "tickIntervalMs": None,
    }
    statuses = ["queued", "building", "succeeded"]
    route(
        api,
        {
            "ExecStarters": {
                "manifestJson": '{"root":"world","types":{"world":{"kind":"hub","crate":"world-tick","client":true}}}',
                "starters": [{**STARTER, "crate": "world-tick", "nodeType": "world"}],
            },
            "ExecBuild": BUILD,
            "ExecBuildStatus": lambda _: {
                **BUILD,
                "status": statuses.pop(0),
                "artifacts": [artifact],
            },
        },
    )
    exec_api = ExecAPI(graphql)

    pack = await exec_api.starters(77)
    assert pack.manifest == {
        "root": "world",
        "types": {"world": {"kind": "hub", "crate": "world-tick", "client": True}},
    }
    assert pack.starters == [
        ExecStarter(
            crate="world-tick",
            node_type="world",
            description="d",
            files=[ExecSourceFile(path="Cargo.toml", content="c")],
        )
    ]
    queued = await exec_api.build(
        "77",
        [
            ExecCrate(name=pack.starters[0].crate, files=pack.starters[0].files),
            {"name": "mine", "files": {"Cargo.toml": "m", "src/lib.rs": "l"}},
        ],
    )
    assert msgspec.to_builtins(queued) == BUILD
    done = await exec_api.wait_for_build("77", "b1", interval_ms=250)
    assert done.status == "succeeded"
    assert msgspec.to_builtins(done.artifacts) == [{**artifact, "capabilitySummary": None}]
    assert slept == [0.25, 0.25]

    assert sent(api) == [
        ("ExecStarters", {"appId": "77"}),
        (
            "ExecBuild",
            {
                "input": {
                    "appId": "77",
                    "crates": [
                        {"name": "world-tick", "files": [{"path": "Cargo.toml", "content": "c"}]},
                        {
                            "name": "mine",
                            "files": [
                                {"path": "Cargo.toml", "content": "m"},
                                {"path": "src/lib.rs", "content": "l"},
                            ],
                        },
                    ],
                }
            },
        ),
        *[("ExecBuildStatus", {"appId": "77", "buildId": "b1"})] * 3,
    ]


async def test_a_failed_build_is_returned_and_a_missing_one_refused(
    graphql: AsyncGraphQLClient, api: MockApi, slept: list[float]
) -> None:
    exec_api = ExecAPI(graphql)
    api.reply_with_root({**BUILD, "status": "failed", "log": "error[E0425]"})
    failed = await exec_api.wait_for_build("77", "b1")
    assert failed.log == "error[E0425]"

    api.reply_with_root(None)
    assert await exec_api.build_status("77", "b9") is None
    assert sent(api)[-1] == ("ExecBuildStatus", {"appId": "77", "buildId": "b9"})
    with pytest.raises(CrowdyError, match="no build b9 in app 77"):
        await exec_api.wait_for_build(77, "b9")
    assert slept == []


@pytest.mark.parametrize(
    ("method", "operation"),
    [("wait_for_build", "ExecBuildStatus"), ("wait_for_mod_build", "ExecModBuildStatus")],
)
async def test_the_build_poll_gives_up_after_its_timeout(
    graphql: AsyncGraphQLClient,
    api: MockApi,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    operation: str,
) -> None:
    now = [1000.0]

    async def advance(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(exec_module, "sleep", advance)
    monkeypatch.setattr(exec_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    api.reply_with_root({**BUILD, "status": "building"})

    with pytest.raises(CrowdyError, match="build b1 is still building"):
        await getattr(ExecAPI(graphql), method)("77", "b1", interval_ms=2000, timeout_ms=5000)

    assert [operation_name for operation_name, _ in sent(api)] == [operation] * 4
    assert now[0] == 1006.0


# ---- ck-exec: mods ----


async def test_mods_pass_their_arguments_and_map_results(
    graphql: AsyncGraphQLClient, api: MockApi, slept: list[float]
) -> None:
    statuses = ["building", "succeeded"]
    route(
        api,
        {
            "ExecModStarter": STARTER,
            "ExecModBuild": BUILD,
            "ExecModBuildStatus": lambda _: {**BUILD, "status": statuses.pop(0)},
            "ExecModDeploy": MOD,
            "ExecModSetEnabled": {**MOD, "enabled": True},
            "ExecModDelete": True,
            "ExecMods": [MOD],
            "ExecMyMods": [MOD],
            "ExecModLogs": [LINE],
            "ExecModPublish": LISTING,
            "ExecModListings": [LISTING_WITH_CLIENT, LISTING],
            "ExecModUnpublish": True,
            "ExecModInstall": {**MOD, "name": "shop", "listingId": "555"},
            "ExecAppMods": [MOD],
            "ExecModSwitches": [SWITCH],
            "ExecModSetSwitch": [SWITCH],
        },
    )
    exec_api = ExecAPI(graphql)

    starter = await exec_api.mod_starter(77)
    assert msgspec.to_builtins(starter) == STARTER
    crate = ExecCrate(name="turret", files={"Cargo.toml": "c", "src/lib.rs": "l"})
    assert (await exec_api.mod_build("77", crate)).status == "queued"
    assert (await exec_api.wait_for_mod_build("77", "b1")).status == "succeeded"
    assert slept == [2.0]
    assert msgspec.to_builtins(await exec_api.mod_deploy("77", 5, "turret", "b1")) == MOD
    assert (await exec_api.mod_set_enabled("77", "5", "turret", True)).enabled is True
    assert await exec_api.mod_delete("77", "5", "turret") is True
    assert msgspec.to_builtins(await exec_api.mods("77", "5")) == [MOD]
    assert msgspec.to_builtins(await exec_api.my_mods("77")) == [MOD]
    assert msgspec.to_builtins(
        await exec_api.mod_logs("77", "5", "turret", max_level=0, limit=5)
    ) == [LINE]
    published = await exec_api.mod_publish("77", "5", "turret", "Shop", "Sells things")
    assert msgspec.to_builtins(published) == {**LISTING, "clientCapabilitySummary": None}
    await exec_api.mod_publish("77", "5", "turret", "Shop")
    with_client, without = await exec_api.mod_listings("77")
    assert with_client.client_capability_summary == SUMMARY
    assert with_client.client_tick_interval_ms == 250
    assert without.client_capability_summary is None
    assert await exec_api.mod_unpublish("77", 555) is True
    assert (await exec_api.mod_install("77", "5", "shop", "555")).listing_id == "555"
    await exec_api.app_mods("77", grid_id=5)
    assert msgspec.to_builtins(await exec_api.app_mods("77", owner_id="42")) == [MOD]
    assert msgspec.to_builtins(await exec_api.mod_switches("77")) == [SWITCH]
    off = await exec_api.mod_set_switch("77", ExecModScope.GRID, True, target="5")
    assert msgspec.to_builtins(off) == [SWITCH]
    await exec_api.mod_set_switch(77, "ALL", False, reason="maintenance")

    turret = {"appId": "77", "gridId": "5", "name": "turret"}
    assert sent(api) == [
        ("ExecModStarter", {"appId": "77"}),
        (
            "ExecModBuild",
            {
                "appId": "77",
                "crate": {
                    "name": "turret",
                    "files": [
                        {"path": "Cargo.toml", "content": "c"},
                        {"path": "src/lib.rs", "content": "l"},
                    ],
                },
            },
        ),
        ("ExecModBuildStatus", {"appId": "77", "buildId": "b1"}),
        ("ExecModBuildStatus", {"appId": "77", "buildId": "b1"}),
        ("ExecModDeploy", {**turret, "buildId": "b1"}),
        ("ExecModSetEnabled", {**turret, "enabled": True}),
        ("ExecModDelete", turret),
        ("ExecMods", {"appId": "77", "gridId": "5"}),
        ("ExecMyMods", {"appId": "77"}),
        ("ExecModLogs", {**turret, "maxLevel": 0, "limit": 5}),
        ("ExecModPublish", {**turret, "title": "Shop", "description": "Sells things"}),
        ("ExecModPublish", {**turret, "title": "Shop"}),
        ("ExecModListings", {"appId": "77"}),
        ("ExecModUnpublish", {"appId": "77", "listingId": "555"}),
        ("ExecModInstall", {"appId": "77", "gridId": "5", "name": "shop", "listingId": "555"}),
        ("ExecAppMods", {"appId": "77", "gridId": "5"}),
        ("ExecAppMods", {"appId": "77", "ownerId": "42"}),
        ("ExecModSwitches", {"appId": "77"}),
        ("ExecModSetSwitch", {"appId": "77", "scope": "GRID", "off": True, "target": "5"}),
        (
            "ExecModSetSwitch",
            {"appId": "77", "scope": "ALL", "off": False, "reason": "maintenance"},
        ),
    ]


# ---- ck-exec: CLIENT halves ----


async def test_client_halves_pass_their_arguments_and_parse_their_summaries(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    client_artifact = {
        "crate": "hud",
        "digest": DIGEST,
        "sizeBytes": 8,
        "capabilitySummaryJson": SUMMARY_JSON,
        "capabilityHash": HASH,
        "tickIntervalMs": 250,
    }
    attached = {
        "modId": "900",
        "gridId": "5",
        "name": "hud",
        "ownerId": "42",
        "clientVersion": 3,
        "digest": DIGEST,
        "sizeBytes": 8,
        "capabilitySummaryJson": SUMMARY_JSON,
        "capabilityHash": HASH,
        "tickIntervalMs": 250,
        "updatedAt": "t",
    }
    served = {
        "modId": "900",
        "name": "hud",
        "gridId": "5",
        "authorId": "42",
        "listingId": None,
        "clientVersion": 2,
        "digest": DIGEST,
        "capabilitySummaryJson": SUMMARY_JSON,
        "capabilityHash": HASH,
        "tickIntervalMs": 250,
        "callerConsented": False,
        "authorCapabilitySummaryJson": "{not json",
        "authorCapabilityHash": "d" * 64,
        "callerTrustsAuthor": False,
        "updatedAt": "t",
    }
    route(
        api,
        {
            "ExecModClientBuild": {**BUILD, "kind": "client", "artifacts": [client_artifact]},
            "ExecModClientDeploy": attached,
            "ExecModClientDelete": True,
            "ExecGridClientMods": [served],
            "ExecConsentClientMod": True,
            "ExecTrustAuthor": True,
            "ExecRevokeClientModConsent": True,
            "ExecRevokeAuthorTrust": False,
            "ExecModClientArtifact": artifact_answer(),
        },
    )
    exec_api = ExecAPI(graphql)

    queued = await exec_api.mod_client_build(
        "77",
        {
            "name": "hud",
            "files": [
                ExecSourceFile(path="Cargo.toml", content="c"),
                {"path": "src/lib.rs", "content": "l"},
            ],
        },
    )
    assert queued.kind == "client"
    assert queued.artifacts[0].capability_summary == SUMMARY
    deployed = await exec_api.mod_client_deploy("77", "5", "hud", "b7")
    assert msgspec.to_builtins(deployed) == {**attached, "capabilitySummary": SUMMARY}
    assert await exec_api.mod_client_delete("77", "5", "hud") is True
    [mod] = await exec_api.grid_client_mods("77", 5)
    assert mod.capability_summary_json == SUMMARY_JSON
    assert mod.capability_summary == SUMMARY
    assert mod.author_capability_summary_json == "{not json"
    assert mod.author_capability_summary is None
    assert await exec_api.consent_client_mod("77", "900", HASH) is True
    assert await exec_api.trust_author("77", "5", 42, "d" * 64) is True
    assert await exec_api.revoke_client_mod_consent("77", "900") is True
    assert await exec_api.revoke_author_trust("77", "5", "42") is False
    raw = await exec_api.mod_client_artifact(77, "900")
    assert raw.fuel_per_dispatch == "100000000"
    assert raw.wasm_base64 == base64.b64encode(WASM).decode("ascii")
    assert raw.capability_summary == SUMMARY
    assert raw.wasm_base64 not in repr(raw)

    assert sent(api) == [
        (
            "ExecModClientBuild",
            {
                "appId": "77",
                "crate": {
                    "name": "hud",
                    "files": [
                        {"path": "Cargo.toml", "content": "c"},
                        {"path": "src/lib.rs", "content": "l"},
                    ],
                },
            },
        ),
        ("ExecModClientDeploy", {"appId": "77", "gridId": "5", "name": "hud", "buildId": "b7"}),
        ("ExecModClientDelete", {"appId": "77", "gridId": "5", "name": "hud"}),
        ("ExecGridClientMods", {"appId": "77", "gridId": "5"}),
        ("ExecConsentClientMod", {"appId": "77", "modId": "900", "capabilityHash": HASH}),
        (
            "ExecTrustAuthor",
            {"appId": "77", "gridId": "5", "authorId": "42", "capabilityHash": "d" * 64},
        ),
        ("ExecRevokeClientModConsent", {"appId": "77", "modId": "900"}),
        ("ExecRevokeAuthorTrust", {"appId": "77", "gridId": "5", "authorId": "42"}),
        ("ExecModClientArtifact", {"appId": "77", "modId": "900"}),
    ]


async def test_mod_client_artifact_bytes_decodes_and_checks_the_module(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    api.reply_with_root(artifact_answer(digest=DIGEST.upper()))

    module = await ExecAPI(graphql).mod_client_artifact_bytes(77, "900")

    assert api.last.operation_name == "ExecModClientArtifact"
    assert api.last.variables == {"appId": "77", "modId": "900"}
    assert module.bytes == WASM
    assert module.digest == DIGEST
    assert module.fuel_per_dispatch == 100_000_000
    assert module.capability_summary == SUMMARY
    assert module.capability_summary_json == SUMMARY_JSON
    assert (
        module.mod_id,
        module.name,
        module.grid_id,
        module.client_version,
        module.capability_hash,
        module.abi_version,
        module.size_bytes,
        module.tick_interval_ms,
    ) == ("900", "hud", "5", 2, HASH, EXEC_CLIENT_ABI_VERSION, len(WASM), 250)
    assert "bytes=<8 bytes>" in repr(module)


@pytest.mark.parametrize(
    ("overrides", "refusal_message"),
    [
        pytest.param({"digest": "e" * 64}, f"SHA-256 is {DIGEST}, not the digest e", id="digest"),
        pytest.param(
            {"wasmBase64": base64.b64encode(b"tampered").decode()}, "not the digest", id="bytes"
        ),
        pytest.param({"abiVersion": 1}, "built for CLIENT ABI 1; this SDK runs ABI 0", id="abi"),
        pytest.param(
            {"capabilitySummaryJson": '{"version":1'}, "does not parse", id="summary-json"
        ),
        pytest.param({"capabilitySummaryJson": "[]"}, "does not parse", id="summary-array"),
        pytest.param({"capabilitySummaryJson": "{}"}, "does not parse", id="no-host-functions"),
        pytest.param(
            {"capabilitySummaryJson": '{"hostFunctions":["hud_set",{"fn":"voxel_set"}]}'},
            "does not parse",
            id="host-function-not-a-name",
        ),
        pytest.param(
            {"capabilitySummaryJson": '{"hostFunctions":NaN}'}, "does not parse", id="nan"
        ),
        pytest.param({"wasmBase64": "AGFzb"}, "its module is not base64", id="base64"),
        pytest.param(
            {"fuelPerDispatch": "lots"}, "fuelPerDispatch 'lots' is not an integer", id="fuel"
        ),
    ],
)
async def test_mod_client_artifact_bytes_refuses_what_does_not_check_out(
    graphql: AsyncGraphQLClient, api: MockApi, overrides: dict[str, Any], refusal_message: str
) -> None:
    api.reply_with_root(artifact_answer(**overrides))
    with pytest.raises(CrowdyProtocolError, match=re.escape(refusal_message)) as raised:
        await ExecAPI(graphql).mod_client_artifact_bytes("77", "900")
    assert raised.value.message.startswith("CLIENT half of mod 900")


@pytest.mark.parametrize("code", ["NOT_FOUND", "RATE_LIMITED"])
async def test_mod_client_artifact_bytes_passes_refusals_through(
    graphql: AsyncGraphQLClient, api: MockApi, code: str
) -> None:
    route(api, {"ExecModClientArtifact": refusal(code)})
    with pytest.raises(CrowdyGraphQLError) as raised:
        await ExecAPI(graphql).mod_client_artifact_bytes("77", "900")
    assert raised.value.code == code


# ---- ck-exec: deploy ----


async def test_deploy_sends_the_manifest_with_digests_and_each_module_once(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    api.reply_with_root({"version": 4})

    result = await ExecAPI(graphql).deploy(
        77,
        "lobby",
        {
            "lobby": {"kind": "hub", "wasm": WASM, "client": True},
            "arena": {
                "kind": "hub",
                "parent": "lobby",
                "wasm": bytearray(WASM),
                "persist_every_ms": 5000,
            },
        },
    )

    assert result == ExecDeployResult(version=4)
    assert DIGEST == "93a44bbb96c751218e4c00d479e4c14358122a389acca16205b1e4d0dc5f9476"
    assert api.last.operation_name == "ExecDeploy"
    assert api.last.variables == {
        "input": {
            "appId": "77",
            "manifestJson": (
                '{"root":"lobby","types":{'
                f'"lobby":{{"kind":"hub","client":true,"digest":"{DIGEST}"}},'
                f'"arena":{{"kind":"hub","parent":"lobby","persist_every_ms":5000,"digest":"{DIGEST}"}}'
                "}}"
            ),
            "artifacts": [{"digest": DIGEST, "wasmBase64": "AGFzbQEAAAA="}],
        }
    }
    assert base64.b64decode(api.last.variables["input"]["artifacts"][0]["wasmBase64"]) == WASM


async def test_deploy_with_a_build_names_crates_and_uploads_only_given_modules(
    graphql: AsyncGraphQLClient, api: MockApi
) -> None:
    api.reply_with_root({"version": 5})
    exec_api = ExecAPI(graphql)

    await exec_api.deploy(
        "77",
        "world",
        {
            "world": {"kind": "hub", "crate": "world-tick", "client": True, "wasm": None},
            "extra": {"kind": "spoke", "parent": "world", "wasm": WASM},
            "empty": {"kind": "spoke", "parent": "world", "wasm": b""},
        },
        "b1",
    )

    deployed = api.last.variables["input"]
    assert deployed["buildId"] == "b1"
    manifest = json.loads(deployed["manifestJson"])
    assert manifest["types"]["world"] == {"kind": "hub", "crate": "world-tick", "client": True}
    assert manifest["types"]["extra"]["digest"] == DIGEST
    assert manifest["types"]["empty"]["digest"] == hashlib.sha256(b"").hexdigest()
    assert deployed["artifacts"] == [
        {"digest": DIGEST, "wasmBase64": "AGFzbQEAAAA="},
        {"digest": hashlib.sha256(b"").hexdigest(), "wasmBase64": ""},
    ]

    await exec_api.deploy("77", "world", {"world": {"kind": "hub", "crate": "world-tick"}}, "b1")
    assert api.last.variables["input"]["artifacts"] == []


@pytest.mark.parametrize(
    ("types", "build_id"),
    [
        ({"world": {"kind": "hub", "crate": "world-tick"}}, None),
        ({"world": {"kind": "hub", "crate": ""}}, "b1"),
        ({"world": {"kind": "hub"}}, "b1"),
    ],
)
async def test_deploy_refuses_a_type_without_a_module_before_any_request(
    graphql: AsyncGraphQLClient, api: MockApi, types: dict[str, Any], build_id: str | None
) -> None:
    with pytest.raises(
        CrowdyError, match="type 'world' needs its wasm, or a crate of the deploy's buildId"
    ):
        await ExecAPI(graphql).deploy("77", "world", types, build_id)
    assert api.sent == []
