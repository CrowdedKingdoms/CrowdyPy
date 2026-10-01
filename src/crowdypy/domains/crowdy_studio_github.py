"""The GitHub repository loop for Crowdy Studio projects (``client.crowdy_studio_github``).

It rides the client's one GraphQL transport and session. The Game API resolves the
repository from the project's bind, so no read or write here names an ``owner`` or ``repo``,
and no GitHub token ever reaches the client. GitHub is a filesystem for the project, not a
login, and it is never required: a project starts in Crowdy Studio and may be bound later.

Which token may call what is decided server-side: ``connect_url``, ``repos``, ``bind`` and
``unbind`` need the identity session; ``status``, ``layout``, ``tree``, ``get_file``,
``put_file``, ``delete_file`` and ``refresh`` also work under a game's app-scoped token, for
projects that token's user owns, so a game needs no identity session to author against
GitHub.

Every field here is served only in the app's own datacenter: call it on the client that
adopted the app's endpoint (the one that plays), never on one pointed at the shared origin.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import msgspec

from crowdypy._generated import inputs
from crowdypy._operation import inline_operation
from crowdypy.domains._base import Domain, omit_none
from crowdypy.utils import bigint

__all__ = [
    "CrowdyStudioGitHubConnectStart",
    "CrowdyStudioGitHubFile",
    "CrowdyStudioGitHubLayout",
    "CrowdyStudioGitHubRepo",
    "CrowdyStudioGitHubStatus",
    "CrowdyStudioGitHubTransport",
    "CrowdyStudioGitHubTree",
    "CrowdyStudioGitHubTreeEntry",
]


class CrowdyStudioGitHubStatus(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """The caller's GitHub connection and the repository a project is bound to."""

    configured: bool
    connected: bool
    account_login: str | None = None
    account_type: str | None = None
    owner: str | None = None
    repo: str | None = None
    branch: str | None = None
    #: Commit the project mirror is at; ``None`` when the project is not bound.
    github_sha: str | None = None
    #: ``all`` or ``selected``: which repositories the installation covers. A repository
    #: created on GitHub afterwards must be added to a ``selected`` installation (at
    #: ``install_url``) before it can be bound. ``None`` when not connected.
    repository_selection: str | None = None
    install_url: str | None = None


class CrowdyStudioGitHubConnectStart(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    connect_url: str


class CrowdyStudioGitHubRepo(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    owner: str
    name: str
    full_name: str
    private: bool
    default_branch: str | None = None


class CrowdyStudioGitHubTreeEntry(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    path: str
    #: ``blob`` or ``tree``.
    type: str
    sha: str | None = None
    size: int | None = None


class CrowdyStudioGitHubTree(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    commit_sha: str
    entries: list[CrowdyStudioGitHubTreeEntry]


class CrowdyStudioGitHubFile(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    path: str
    content: str
    sha: str
    #: Commit the file was read at, or the commit a write created (the new ``github_sha``).
    commit_sha: str | None = None


class CrowdyStudioGitHubLayout(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """Where the SERVER and CLIENT crates live in the bound repository at one commit.

    Resolved server-side from ``crowdy.json`` (or inferred): the only layout grammar, so
    never parse ``crowdy.json`` yourself.
    """

    commit_sha: str
    #: Directory of the SERVER ``Cargo.toml``; ``.`` is the repository root.
    server: str
    #: Directory of the CLIENT ``Cargo.toml``, or ``None`` when server-only.
    client: str | None = None
    assets: str
    from_file: bool


_STATUS_FIELDS = (
    "\n  configured connected accountLogin accountType owner repo branch githubSha"
    " repositorySelection installUrl\n"
)

CROWDY_STUDIO_GITHUB_STATUS = inline_operation(
    "CrowdyStudioGitHubStatus",
    "query",
    "crowdyStudioGitHubStatus",
    "\n  query CrowdyStudioGitHubStatus($appId: BigInt, $projectId: String) {\n"
    "    crowdyStudioGitHubStatus(appId: $appId, projectId: $projectId) { "
    + _STATUS_FIELDS
    + " }\n  }\n",
)
CROWDY_STUDIO_GITHUB_CONNECT_URL = inline_operation(
    "CrowdyStudioGitHubConnectUrl",
    "mutation",
    "crowdyStudioGitHubConnectUrl",
    "\n  mutation CrowdyStudioGitHubConnectUrl { crowdyStudioGitHubConnectUrl { connectUrl } }\n",
)
CROWDY_STUDIO_GITHUB_REPOS = inline_operation(
    "CrowdyStudioGitHubRepos",
    "query",
    "crowdyStudioGitHubRepos",
    "\n  query CrowdyStudioGitHubRepos { crowdyStudioGitHubRepos"
    " { owner name fullName private defaultBranch } }\n",
)
CROWDY_STUDIO_GITHUB_BIND = inline_operation(
    "CrowdyStudioGitHubBind",
    "mutation",
    "crowdyStudioGitHubBind",
    "\n  mutation CrowdyStudioGitHubBind($input: BindCrowdyStudioGitHubInput!) {\n"
    "    crowdyStudioGitHubBind(input: $input) { " + _STATUS_FIELDS + " }\n  }\n",
)
CROWDY_STUDIO_GITHUB_UNBIND = inline_operation(
    "CrowdyStudioGitHubUnbind",
    "mutation",
    "crowdyStudioGitHubUnbind",
    "\n  mutation CrowdyStudioGitHubUnbind($input: CrowdyStudioGitHubProjectInput!) {\n"
    "    crowdyStudioGitHubUnbind(input: $input) { " + _STATUS_FIELDS + " }\n  }\n",
)
CROWDY_STUDIO_GITHUB_REFRESH = inline_operation(
    "CrowdyStudioGitHubRefresh",
    "mutation",
    "crowdyStudioGitHubRefresh",
    "\n  mutation CrowdyStudioGitHubRefresh($input: CrowdyStudioGitHubProjectInput!) {\n"
    "    crowdyStudioGitHubRefresh(input: $input) { " + _STATUS_FIELDS + " }\n  }\n",
)
CROWDY_STUDIO_GITHUB_LAYOUT = inline_operation(
    "CrowdyStudioGitHubLayout",
    "query",
    "crowdyStudioGitHubLayout",
    "\n  query CrowdyStudioGitHubLayout($input: CrowdyStudioGitHubAtCommitInput!) {\n"
    "    crowdyStudioGitHubLayout(input: $input) { commitSha server client assets fromFile }\n"
    "  }\n",
)
CROWDY_STUDIO_GITHUB_TREE = inline_operation(
    "CrowdyStudioGitHubTree",
    "query",
    "crowdyStudioGitHubTree",
    "\n  query CrowdyStudioGitHubTree($input: CrowdyStudioGitHubAtCommitInput!) {\n"
    "    crowdyStudioGitHubTree(input: $input) { commitSha entries { path type sha size } }\n"
    "  }\n",
)
CROWDY_STUDIO_GITHUB_FILE = inline_operation(
    "CrowdyStudioGitHubFile",
    "query",
    "crowdyStudioGitHubFile",
    "\n  query CrowdyStudioGitHubFile($input: CrowdyStudioGitHubFileInput!) {\n"
    "    crowdyStudioGitHubFile(input: $input) { path content sha commitSha }\n"
    "  }\n",
)
CROWDY_STUDIO_GITHUB_PUT_FILE = inline_operation(
    "CrowdyStudioGitHubPutFile",
    "mutation",
    "crowdyStudioGitHubPutFile",
    "\n  mutation CrowdyStudioGitHubPutFile($input: CrowdyStudioGitHubPutFileInput!) {\n"
    "    crowdyStudioGitHubPutFile(input: $input) { path content sha commitSha }\n"
    "  }\n",
)
CROWDY_STUDIO_GITHUB_DELETE_FILE = inline_operation(
    "CrowdyStudioGitHubDeleteFile",
    "mutation",
    "crowdyStudioGitHubDeleteFile",
    "\n  mutation CrowdyStudioGitHubDeleteFile($input: CrowdyStudioGitHubDeleteFileInput!) {\n"
    "    crowdyStudioGitHubDeleteFile(input: $input) { " + _STATUS_FIELDS + " }\n  }\n",
)

#: Documents this module declares inline (CrowdyJS declares the same ones inline).
INLINE_OPERATIONS = (
    CROWDY_STUDIO_GITHUB_STATUS,
    CROWDY_STUDIO_GITHUB_CONNECT_URL,
    CROWDY_STUDIO_GITHUB_REPOS,
    CROWDY_STUDIO_GITHUB_BIND,
    CROWDY_STUDIO_GITHUB_UNBIND,
    CROWDY_STUDIO_GITHUB_REFRESH,
    CROWDY_STUDIO_GITHUB_LAYOUT,
    CROWDY_STUDIO_GITHUB_TREE,
    CROWDY_STUDIO_GITHUB_FILE,
    CROWDY_STUDIO_GITHUB_PUT_FILE,
    CROWDY_STUDIO_GITHUB_DELETE_FILE,
)


class CrowdyStudioGitHubTransport(Domain):
    """GraphQL transport for GitHub-backed Crowdy Studio projects."""

    async def status(
        self, *, app_id: str | int | None = None, project_id: str | None = None
    ) -> CrowdyStudioGitHubStatus:
        """The caller's GitHub connection; with ``app_id`` and ``project_id``, also the
        repository bound to that project and the commit its mirror is at (``github_sha``).
        """
        payload = await self._request(
            CROWDY_STUDIO_GITHUB_STATUS,
            omit_none(
                {
                    "appId": None if app_id is None else bigint(app_id),
                    "projectId": project_id,
                }
            ),
        )
        return msgspec.convert(payload, CrowdyStudioGitHubStatus)

    async def connect_url(self) -> CrowdyStudioGitHubConnectStart:
        """The install URL of this tier's Crowdy Studio GitHub App, with a signed state.

        Open it in a browser. Identity session only.
        """
        payload = await self._request(CROWDY_STUDIO_GITHUB_CONNECT_URL)
        return msgspec.convert(payload, CrowdyStudioGitHubConnectStart)

    async def repos(self) -> list[CrowdyStudioGitHubRepo]:
        """Repositories the caller granted to their Crowdy Studio installation.

        Identity session only.
        """
        payload = await self._request(CROWDY_STUDIO_GITHUB_REPOS)
        return msgspec.convert([] if payload is None else payload, list[CrowdyStudioGitHubRepo])

    async def bind(
        self, input: inputs.BindCrowdyStudioGitHubInput | Mapping[str, Any]
    ) -> CrowdyStudioGitHubStatus:
        """Bind a project you own and make the two sides agree in one commit.

        ``PUSH_PROJECT`` commits the project files to the branch (refused with
        ``GITHUB_REPO_HAS_FILES`` when the branch already has Rust under the layout roots);
        ``TAKE_REPOSITORY`` replaces the project files with the branch's (refused with
        ``GITHUB_REPO_EMPTY`` when it has none). Identity session only.
        """
        payload = await self._request(CROWDY_STUDIO_GITHUB_BIND, {"input": input})
        return msgspec.convert(payload, CrowdyStudioGitHubStatus)

    async def unbind(
        self, input: inputs.CrowdyStudioGitHubProjectInput | Mapping[str, Any]
    ) -> CrowdyStudioGitHubStatus:
        """Clear the bind. The project keeps its files and is a STUDIO project again.

        Nothing on GitHub changes. Identity session only.
        """
        payload = await self._request(CROWDY_STUDIO_GITHUB_UNBIND, {"input": input})
        return msgspec.convert(payload, CrowdyStudioGitHubStatus)

    async def refresh(
        self, input: inputs.CrowdyStudioGitHubProjectInput | Mapping[str, Any]
    ) -> CrowdyStudioGitHubStatus:
        """Bring the project mirror forward to the branch head after a push made elsewhere.

        A no-op when it is already there.
        """
        payload = await self._request(CROWDY_STUDIO_GITHUB_REFRESH, {"input": input})
        return msgspec.convert(payload, CrowdyStudioGitHubStatus)

    async def layout(
        self, input: inputs.CrowdyStudioGitHubAtCommitInput | Mapping[str, Any]
    ) -> CrowdyStudioGitHubLayout:
        """Where the SERVER and CLIENT crates live at ``commitSha`` (default: the mirror commit)."""
        payload = await self._request(CROWDY_STUDIO_GITHUB_LAYOUT, {"input": input})
        return msgspec.convert(payload, CrowdyStudioGitHubLayout)

    async def tree(
        self, input: inputs.CrowdyStudioGitHubAtCommitInput | Mapping[str, Any]
    ) -> CrowdyStudioGitHubTree:
        """The bound repository's recursive file list at ``commitSha`` (default: the mirror commit)."""
        payload = await self._request(CROWDY_STUDIO_GITHUB_TREE, {"input": input})
        return msgspec.convert(payload, CrowdyStudioGitHubTree)

    async def get_file(
        self, input: inputs.CrowdyStudioGitHubFileInput | Mapping[str, Any]
    ) -> CrowdyStudioGitHubFile:
        """One UTF-8 file of the bound repository with its blob SHA, at ``commitSha``
        (default: the mirror commit).
        """
        payload = await self._request(CROWDY_STUDIO_GITHUB_FILE, {"input": input})
        return msgspec.convert(payload, CrowdyStudioGitHubFile)

    async def put_file(
        self, input: inputs.CrowdyStudioGitHubPutFileInput | Mapping[str, Any]
    ) -> CrowdyStudioGitHubFile:
        """Commit one file to the bound branch.

        ``expectedCommitSha`` is the project ``github.sha`` the writer read; a stale one is
        refused with ``GITHUB_STALE_SHA`` and nothing moves. The blob ``sha`` is optional: the
        server resolves it from the tree at that commit. The returned ``commit_sha`` is the
        new ``github.sha``.
        """
        payload = await self._request(CROWDY_STUDIO_GITHUB_PUT_FILE, {"input": input})
        return msgspec.convert(payload, CrowdyStudioGitHubFile)

    async def delete_file(
        self, input: inputs.CrowdyStudioGitHubDeleteFileInput | Mapping[str, Any]
    ) -> CrowdyStudioGitHubStatus:
        """Delete one file from the bound branch under the same guards as :meth:`put_file`.

        ``crowdy.json`` cannot be deleted this way.
        """
        payload = await self._request(CROWDY_STUDIO_GITHUB_DELETE_FILE, {"input": input})
        return msgspec.convert(payload, CrowdyStudioGitHubStatus)
