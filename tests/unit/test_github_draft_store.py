"""The hosted game draft (CrowdyJS 18.23.0's ``GitHubDraftStore``, its tests ported) over an
in-memory bound branch, and the controller editing a Construct game through it."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from crowdypy.domains.crowdy_studio import (
    CrowdyStudioProject,
    CrowdyStudioProjectGitHub,
    CrowdyStudioProjectMetadata,
    CrowdyStudioProjectRevision,
    CrowdyStudioProjectSummary,
)
from crowdypy.domains.crowdy_studio_github import (
    CrowdyStudioGitHubCommit,
    CrowdyStudioGitHubFileContent,
    CrowdyStudioGitHubFiles,
    CrowdyStudioGitHubStatus,
    CrowdyStudioGitHubTree,
    CrowdyStudioGitHubTreeEntry,
)
from crowdypy.errors import CrowdyGraphQLError
from crowdypy.studio import (
    CrowdyStudioController,
    DraftError,
    DraftPrecondition,
    GitHubDraftStore,
    MemoryDraftOverlayStorage,
    git_blob_sha_of_text,
    utf8_length,
)

APP = "89"
PROJECT = "11111111-1111-4111-8111-111111111111"


class FakeRepo:
    """A bound branch in memory: tree, batched reads, one-commit writes, refresh."""

    def __init__(self, initial: Mapping[str, str]) -> None:
        self.files = dict(initial)
        self.head = "1" * 40
        self.counter = 1
        self.calls: list[tuple[Any, ...]] = []

    def next_sha(self) -> str:
        self.counter += 1
        return str(self.counter).zfill(40)

    def push(self, path: str, content: str | None) -> None:
        """Someone pushed to the branch outside Studio."""
        if content is None:
            self.files.pop(path, None)
        else:
            self.files[path] = content
        self.head = self.next_sha()

    async def tree(self, input: Mapping[str, Any]) -> CrowdyStudioGitHubTree:
        self.calls.append(("tree", input.get("commitSha")))
        return CrowdyStudioGitHubTree(
            commit_sha=self.head,
            entries=[
                CrowdyStudioGitHubTreeEntry(
                    path=path, type="blob", sha=git_blob_sha_of_text(text), size=utf8_length(text)
                )
                for path, text in self.files.items()
            ],
        )

    async def get_files(self, input: Mapping[str, Any]) -> CrowdyStudioGitHubFiles:
        self.calls.append(("get_files", len(input["paths"])))
        files, missing = [], []
        for path in input["paths"]:
            if path not in self.files:
                missing.append(path)
                continue
            text = self.files[path]
            binary = "\0" in text
            files.append(
                CrowdyStudioGitHubFileContent(
                    path=path,
                    sha=git_blob_sha_of_text(text),
                    size=utf8_length(text),
                    binary=binary,
                    content=None if binary else text,
                )
            )
        return CrowdyStudioGitHubFiles(commit_sha=self.head, files=files, missing=missing)

    async def commit_files(self, input: Mapping[str, Any]) -> CrowdyStudioGitHubCommit:
        self.calls.append(("commit_files", len(input["changes"]), input["message"]))
        if input["expectedCommitSha"] != self.head:
            raise CrowdyGraphQLError(
                [
                    {
                        "message": "The project moved on since it was read.",
                        "extensions": {"code": "GITHUB_STALE_SHA"},
                    }
                ]
            )
        for change in input["changes"]:
            if change.get("delete"):
                self.files.pop(change["path"], None)
            else:
                self.files[change["path"]] = change["content"]
        parent, self.head = self.head, self.next_sha()
        return CrowdyStudioGitHubCommit(
            commit_sha=self.head,
            parent_sha=parent,
            written=[c["path"] for c in input["changes"] if not c.get("delete")],
            deleted=[c["path"] for c in input["changes"] if c.get("delete")],
        )

    async def refresh(self, input: Mapping[str, Any]) -> CrowdyStudioGitHubStatus:
        self.calls.append(("refresh",))
        return CrowdyStudioGitHubStatus(configured=True, connected=True, github_sha=self.head)


def store_for(repo: FakeRepo, storage: MemoryDraftOverlayStorage | None = None) -> GitHubDraftStore:
    return GitHubDraftStore(
        transport=repo,
        app_id=APP,
        project_id=PROJECT,
        base_commit_sha=repo.head,
        storage=storage or MemoryDraftOverlayStorage(),
    )


def test_versions_are_git_blob_ids() -> None:
    assert git_blob_sha_of_text("hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert git_blob_sha_of_text("") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


async def test_the_draft_lists_the_base_reads_in_batches_and_caches_text_by_blob_id() -> None:
    repo = FakeRepo(
        {
            "src/main.ts": "export {}\n",
            "model/catalog.mjs": "export const PROGRAM_CATALOG = [];\n",
            ".env": "SECRET=1",
            "node_modules/x/i.js": "",
        }
    )
    store = store_for(repo)
    snap = await store.load()
    assert [e.path for e in snap.entries] == ["model/catalog.mjs", "src/main.ts"]
    assert all(not e.dirty for e in snap.entries)
    first = await store.read(["src/main.ts", "model/catalog.mjs", "src/gone.ts", ".env"])
    assert sorted(f.path for f in first.files) == ["model/catalog.mjs", "src/main.ts"]
    assert sorted(first.missing) == [".env", "src/gone.ts"]
    assert len([c for c in repo.calls if c[0] == "get_files"]) == 1
    await store.read(["src/main.ts"])
    assert len([c for c in repo.calls if c[0] == "get_files"]) == 1, "cached by blob id"


async def test_writes_honour_their_preconditions_and_the_base_content_back_is_not_dirty() -> None:
    repo = FakeRepo({"src/main.ts": "export {}\n"})
    store = store_for(repo)
    await store.load()
    base = (await store.read(["src/main.ts"])).files[0]
    with pytest.raises(DraftError) as stale:
        await store.write(
            "src/main.ts", "x", source="agent", precondition=DraftPrecondition("createIfAbsent")
        )
    assert stale.value.code == "DRAFT_STALE"
    with pytest.raises(DraftError) as moved:
        await store.write(
            "src/main.ts", "x", source="agent", precondition=DraftPrecondition("ifHash", "0" * 40)
        )
    assert moved.value.code == "DRAFT_STALE"
    events: list[Any] = []
    store.subscribe(events.append)
    written = await store.write(
        "src/main.ts",
        "export const a = 1;\n",
        source="agent",
        precondition=DraftPrecondition("ifHash", base.hash),
    )
    assert written.previous_hash == base.hash
    assert next(e for e in store.snapshot().entries if e.path == "src/main.ts").dirty
    assert [(c.path, c.hash) for c in events[-1].changes] == [("src/main.ts", written.hash)]
    assert events[-1].source == "agent"
    await store.write("src/main.ts", "export {}\n", source="player")
    assert not next(e for e in store.snapshot().entries if e.path == "src/main.ts").dirty
    for refused in (".github/workflows/ci.yml", "../x.ts"):
        with pytest.raises(DraftError) as raised:
            await store.write(refused, "x", source="agent")
        assert raised.value.code == "DRAFT_PATH_REFUSED"


async def test_commit_publishes_every_change_as_one_commit_and_follows_its_own_base() -> None:
    repo = FakeRepo({"src/main.ts": "export {}\n", "src/old.ts": "old\n"})
    store = store_for(repo)
    await store.load()
    await store.write(
        "src/scenes/pool/PoolScene.ts",
        "export class PoolScene {}\n",
        source="agent",
        precondition=DraftPrecondition("createIfAbsent"),
    )
    await store.write("src/main.ts", "export const x = 1;\n", source="agent")
    await store.remove("src/old.ts", source="agent")
    assert store.snapshot().deleted == ("src/old.ts",)
    result = await store.commit("Add the pool scene")
    assert sorted(result.committed) == ["src/main.ts", "src/old.ts", "src/scenes/pool/PoolScene.ts"]
    assert [c for c in repo.calls if c[0] == "commit_files"] == [
        ("commit_files", 3, "Add the pool scene")
    ]
    assert result.base_commit_sha == repo.head
    after = store.snapshot()
    assert not any(e.dirty for e in after.entries)
    assert after.deleted == ()
    assert repo.files["src/main.ts"] == "export const x = 1;\n"
    with pytest.raises(DraftError) as nothing:
        await store.commit("nothing")
    assert nothing.value.code == "DRAFT_NOT_FOUND"


async def test_a_moved_branch_rebases_the_edits_that_do_not_collide_and_retries_once() -> None:
    repo = FakeRepo({"src/main.ts": "export {}\n", "README.md": "# game\n"})
    store = store_for(repo)
    await store.load()
    await store.write("src/main.ts", "export const mine = 1;\n", source="agent")
    repo.push("README.md", "# game, renamed upstream\n")
    result = await store.commit("mine")
    assert result.committed == ("src/main.ts",)
    assert len([c for c in repo.calls if c[0] == "refresh"]) == 1
    assert repo.files["README.md"] == "# game, renamed upstream\n", "the upstream change is kept"
    assert repo.files["src/main.ts"] == "export const mine = 1;\n"


async def test_a_path_both_sides_changed_is_a_conflict_the_player_settles() -> None:
    repo = FakeRepo({"src/main.ts": "export {}\n"})
    store = store_for(repo)
    await store.load()
    await store.write("src/main.ts", "export const mine = 1;\n", source="agent")
    repo.push("src/main.ts", "export const theirs = 1;\n")
    with pytest.raises(DraftError) as raised:
        await store.commit("mine")
    assert raised.value.code == "DRAFT_CONFLICT"
    assert [c.path for c in raised.value.conflicts] == ["src/main.ts"]
    assert [c.path for c in store.conflicts()] == ["src/main.ts"]
    await store.resolve_conflict("src/main.ts", "mine")
    result = await store.commit("mine, over theirs")
    assert result.committed == ("src/main.ts",)
    assert repo.files["src/main.ts"] == "export const mine = 1;\n"

    await store.write("src/main.ts", "export const again = 1;\n", source="agent")
    repo.push("src/main.ts", "export const upstream = 2;\n")
    with pytest.raises(DraftError) as again:
        await store.commit("again")
    assert again.value.code == "DRAFT_CONFLICT"
    await store.resolve_conflict("src/main.ts", "theirs")
    assert (await store.read(["src/main.ts"])).files[0].content == "export const upstream = 2;\n"
    assert not any(e.dirty for e in store.snapshot().entries)


async def test_the_draft_survives_a_new_store_on_the_same_storage() -> None:
    repo = FakeRepo({"src/main.ts": "export {}\n"})
    storage = MemoryDraftOverlayStorage()
    first = store_for(repo, storage)
    await first.load()
    await first.write("src/main.ts", "export const kept = 1;\n", source="player")
    first.dispose()
    await asyncio.sleep(0.02)
    second = store_for(repo, storage)
    await second.load()
    assert (await second.read(["src/main.ts"])).files[0].content == "export const kept = 1;\n"
    assert second.snapshot().entries[0].dirty
    with pytest.raises(DraftError) as closed:
        await first.read(["src/main.ts"])
    assert closed.value.code == "DRAFT_UNAVAILABLE"


class GameGitHub(FakeRepo):
    """The transport the controller and the store share: the branch plus status and layout."""

    async def status(self, **kwargs: Any) -> CrowdyStudioGitHubStatus:
        return CrowdyStudioGitHubStatus(configured=True, connected=True, github_sha=self.head)

    async def layout(self, input: Mapping[str, Any]) -> Any:
        return SimpleNamespace(kind="CONSTRUCT_GAME")


class Provider:
    def __init__(self, project: CrowdyStudioProject) -> None:
        self.project = project

    async def list_projects(self, app_id: Any) -> list[CrowdyStudioProjectSummary]:
        p = self.project
        return [
            CrowdyStudioProjectSummary(
                project_id=p.project_id,
                name=p.metadata.name,
                kind=p.kind,
                revision_id=p.revision.id,
                source=p.source,
                github_sha=p.github.sha if p.github else None,
                updated_at=p.updated_at,
            )
        ]

    async def get_project(self, app_id: Any, grid_id: Any, project_id: str) -> Any:
        return self.project

    async def list_personal_library_files(self, app_id: Any) -> list[Any]:
        return []

    async def list_common_files(self, app_id: Any) -> list[Any]:
        return []


async def test_the_controller_edits_and_commits_a_game_through_the_hosted_draft() -> None:
    github = GameGitHub({"src/main.ts": "export {}\n", "README.md": "# game\n"})
    project = CrowdyStudioProject(
        project_id=PROJECT,
        app_id=APP,
        grid_id="7",
        kind="SERVER",
        metadata=CrowdyStudioProjectMetadata(name="Game", pairing_preference="OPTIONAL"),
        files=[],
        sdk_version="1",
        abi_version=1,
        revision=CrowdyStudioProjectRevision(id="1", saved_at="2026-10-29T00:00:00Z"),
        source="GITHUB",
        github=CrowdyStudioProjectGitHub(owner="me", repo="game", branch="main", sha=github.head),
        created_at="2026-10-29T00:00:00Z",
        updated_at="2026-10-29T00:00:00Z",
    )

    def drafts(*, app_id: str, project_id: str, github_sha: str) -> GitHubDraftStore:
        return GitHubDraftStore(
            transport=github, app_id=app_id, project_id=project_id, base_commit_sha=github_sha
        )

    controller = CrowdyStudioController(
        project_provider=Provider(project),
        mods=SimpleNamespace(),
        app_id=APP,
        grid_id="7",
        github=github,
        draft_store=drafts,
    )
    await controller.initialize()
    await controller.when_editor_known(timeout_ms=1_000)
    assert controller.get_state().repository_files == ("README.md", "src/main.ts")
    await controller.open_repository_file("src/main.ts")
    controller.update_repository_file("src/main.ts", "export const tuned = 1;\n")
    result = await controller.commit_repository("Tune it")
    assert result.committed == ("src/main.ts",)
    assert github.files["src/main.ts"] == "export const tuned = 1;\n"
    assert controller.get_state().repository_dirty == ()
    controller.destroy()
