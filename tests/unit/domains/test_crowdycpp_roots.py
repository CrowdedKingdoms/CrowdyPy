"""The roots CrowdyCPP sends and CrowdyJS 18.0.4 does not: each sends its inline document
with the variables CrowdyCPP sends."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import MockApi
from crowdypy.auth_state import AuthState
from crowdypy.domains import crowdy_studio as studio_module
from crowdypy.domains import crowdy_studio_agent as agent_module
from crowdypy.domains.auth import CONFIRM_EMAIL, RESEND_CONFIRMATION_EMAIL, AuthAPI
from crowdypy.domains.crowdy_studio import UNCHANGED, CrowdyStudioAPI, CrowdyStudioProjectFile
from crowdypy.domains.crowdy_studio_agent import CrowdyStudioAgentAPI
from crowdypy.graphql import AsyncGraphQLClient

PROJECT: dict[str, Any] = {
    "projectId": "p-1",
    "appId": "42",
    "ownerUserId": "7",
    "gridId": None,
    "name": "Quest",
    "description": None,
    "serverModuleName": "quest",
    "clientModuleName": None,
    "pairingPreference": "SERVER_ONLY",
    "sdkVersion": "1",
    "abiVersion": 1,
    "revision": "4",
    "archived": False,
    "archivedAt": None,
    "fileCount": 1,
    "totalBytes": 9,
    "source": "PRIVATE",
    "githubOwner": None,
    "githubRepo": None,
    "githubBranch": None,
    "githubSha": None,
    "createdAt": "2026-09-01T00:00:00Z",
    "updatedAt": "2026-09-02T00:00:00Z",
    "files": [{"target": "SERVER", "path": "src/lib.rs", "content": "fn x() {}"}],
}
LIBRARY_FILE: dict[str, Any] = {
    "libraryFileId": "l-1",
    "appId": "42",
    "ownerUserId": "7",
    "title": "Helpers",
    "pathHint": "src/helpers.rs",
    "target": "SERVER",
    "tags": [],
    "content": "",
    "revision": "3",
    "archived": True,
    "archivedAt": "2026-09-03T00:00:00Z",
    "createdAt": "2026-09-01T00:00:00Z",
    "updatedAt": "2026-09-03T00:00:00Z",
}
COMMON_FILE: dict[str, Any] = {
    "commonFileId": "c-1",
    "appId": "42",
    "slug": "math",
    "title": "Math",
    "description": None,
    "path": "src/math.rs",
    "target": "SERVER",
    "tags": ["util"],
    "status": "PUBLISHED",
    "versionId": "v-2",
    "versionNo": 2,
    "content": "pub fn add() {}",
    "contentSha256": "ab",
    "publishedByUserId": "7",
    "publishedAt": "2026-09-03T00:00:00Z",
    "createdAt": "2026-09-01T00:00:00Z",
    "updatedAt": "2026-09-03T00:00:00Z",
}


async def test_email_confirmation(
    api: MockApi, graphql: AsyncGraphQLClient, session: AuthState
) -> None:
    auth = AuthAPI(graphql, session)
    api.reply_with_root(True)
    assert await auth.confirm_email("tok") is True
    assert (api.last.operation_name, api.last.query, api.last.variables) == (
        "ConfirmEmail",
        CONFIRM_EMAIL.document,
        {"token": "tok"},
    )
    api.reply_with_root(True)
    assert await auth.resend_confirmation_email("a@b.c") is True
    assert (api.last.query, api.last.variables) == (
        RESEND_CONFIRMATION_EMAIL.document,
        {"email": "a@b.c"},
    )


AGENT_CASES: list[tuple[str, tuple[Any, ...], dict[str, Any], str, dict[str, Any]]] = [
    ("provider_consent", (42,), {}, "CrowdyStudioProviderConsent", {"appId": "42"}),
    (
        "set_provider_consent",
        ("42", True),
        {},
        "CrowdyStudioSetProviderConsent",
        {"input": {"appId": "42", "consented": True}},
    ),
    ("model_usage", (42,), {}, "CrowdyStudioModelUsage", {"appId": "42"}),
    ("model_usage", (42, 5), {}, "CrowdyStudioModelUsage", {"appId": "42", "limit": 5}),
    ("policy", (42,), {}, "CrowdyStudioAgentPolicy", {"appId": "42"}),
    ("effective_policy", (42,), {}, "CrowdyStudioAgentEffectivePolicy", {"appId": "42"}),
    (
        "usage",
        (42,),
        {"since": "2026-09-01T00:00:00Z", "limit": 10},
        "CrowdyStudioAgentUsage",
        {"appId": "42", "since": "2026-09-01T00:00:00Z", "limit": 10},
    ),
    (
        "set_policy",
        ({"appId": "42", "enabled": True},),
        {},
        "CrowdyStudioAgentSetPolicy",
        {"input": {"appId": "42", "enabled": True}},
    ),
]


def test_the_agent_api_is_crowdycpp_s() -> None:
    public = {
        n for n, v in vars(CrowdyStudioAgentAPI).items() if callable(v) and not n.startswith("_")
    }
    assert public == {case[0] for case in AGENT_CASES}


@pytest.mark.parametrize("case", AGENT_CASES, ids=lambda c: f"{c[0]}-{len(c[4])}")
async def test_agent_methods_send_their_documents(
    api: MockApi, graphql: AsyncGraphQLClient, case: tuple[Any, ...]
) -> None:
    method, args, kwargs, operation, variables = case
    documents = {op.name: op.document for op in agent_module.INLINE_OPERATIONS}
    api.reply_with_root({"appId": "42"})
    assert await getattr(CrowdyStudioAgentAPI(graphql), method)(*args, **kwargs) == {"appId": "42"}
    assert (api.last.operation_name, api.last.query, api.last.variables) == (
        operation,
        documents[operation],
        variables,
    )


async def test_project_metadata_patches_only_what_is_given(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    studio = CrowdyStudioAPI(graphql)
    api.reply_with_root(PROJECT)
    project = await studio.save_project_metadata(
        42, "p-1", 3, name="Quest", description=None, pairing_preference="SERVER_ONLY"
    )
    assert api.last.query == studio_module.PROJECT_SAVE_METADATA.document
    assert api.last.variables == {
        "input": {
            "appId": "42",
            "projectId": "p-1",
            "expectedRevision": "3",
            "name": "Quest",
            "description": None,
            "pairingPreference": "SERVER_ONLY",
        }
    }
    assert (project.project_id, project.revision.id) == ("p-1", "4")
    assert studio._baselines["p-1"].revision.id == "4"
    assert UNCHANGED is studio_module.UNCHANGED

    api.reply_with_root(PROJECT)
    await studio.save_project_metadata(42, "p-1", 4, grid_id=9, idempotency_key="k")
    assert api.last.variables["input"] == {
        "appId": "42",
        "projectId": "p-1",
        "expectedRevision": "4",
        "gridId": "9",
        "idempotencyKey": "k",
    }


async def test_project_files_archive_and_reference_files(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    studio = CrowdyStudioAPI(graphql)
    api.reply_with_root(PROJECT)
    await studio.save_project_files(
        "42",
        "p-1",
        "4",
        [CrowdyStudioProjectFile(target="SERVER", path="./src/lib.rs", content="fn y() {}")],
        [("CLIENT", "./src/old.rs")],
    )
    assert api.last.query == studio_module.PROJECT_SAVE_FILES.document
    assert api.last.variables == {
        "input": {
            "appId": "42",
            "projectId": "p-1",
            "expectedRevision": "4",
            "upserts": [{"target": "SERVER", "path": "src/lib.rs", "content": "fn y() {}"}],
            "deletes": [{"target": "CLIENT", "path": "src/old.rs"}],
        }
    }

    api.reply_with_root({**PROJECT, "archived": True})
    archived = await studio.set_project_archived(42, "p-1", 4)
    assert api.last.variables == {
        "input": {"appId": "42", "projectId": "p-1", "expectedRevision": "4", "archived": True}
    }
    assert archived.project_id == "p-1"

    api.reply_with_root(LIBRARY_FILE)
    library = await studio.set_personal_library_file_archived(42, "l-1", 2)
    assert api.last.query == studio_module.LIBRARY_SET_ARCHIVED.document
    assert api.last.variables == {
        "input": {"appId": "42", "libraryFileId": "l-1", "expectedRevision": "2", "archived": True}
    }
    assert library.id == "l-1"

    api.reply_with_root(COMMON_FILE)
    common = await studio.publish_common_file(
        42, "math", "Math", "SERVER", "./src/math.rs", "pub fn add() {}", tags=["util"]
    )
    assert api.last.query == studio_module.COMMON_PUBLISH.document
    assert api.last.variables == {
        "input": {
            "appId": "42",
            "slug": "math",
            "title": "Math",
            "target": "SERVER",
            "path": "src/math.rs",
            "content": "pub fn add() {}",
            "tags": ["util"],
        }
    }
    assert common.id == "v-2"
