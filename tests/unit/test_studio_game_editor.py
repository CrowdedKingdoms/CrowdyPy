"""The game editor (CrowdyJS 18.23.0): the repository path rules, the GitHub transport's batched
reads and multi-file commits, and the controller's draft of a bound Construct game against an
in-memory draft store."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from types import SimpleNamespace
from typing import Any, Literal

import msgspec
import pytest

from conftest import MockApi
from crowdypy.domains.crowdy_studio import (
    CrowdyStudioProject,
    CrowdyStudioProjectFile,
    CrowdyStudioProjectGitHub,
    CrowdyStudioProjectMetadata,
    CrowdyStudioProjectRevision,
    CrowdyStudioProjectSummary,
)
from crowdypy.domains.crowdy_studio_github import (
    CrowdyStudioGitHubCommit,
    CrowdyStudioGitHubFiles,
    CrowdyStudioGitHubLayout,
    CrowdyStudioGitHubTransport,
)
from crowdypy.graphql import AsyncGraphQLClient
from crowdypy.studio import (
    CrowdyStudioController,
    CrowdyStudioError,
    CrowdyStudioFileRef,
    CrowdyStudioRepositoryDocument,
    DraftChange,
    DraftChangeEvent,
    DraftCommitResult,
    DraftConflict,
    DraftEntry,
    DraftError,
    DraftFile,
    DraftPrecondition,
    DraftReadResult,
    DraftSnapshot,
    DraftWriteResult,
    is_likely_binary_path,
    is_repo_dir,
    is_repo_path,
    normalize_repo_path,
)


def make_project(source: str = "GITHUB", sha: str | None = "c1") -> CrowdyStudioProject:
    return CrowdyStudioProject(
        project_id="p-1",
        app_id="42",
        grid_id="7",
        kind="SERVER",
        metadata=CrowdyStudioProjectMetadata(
            name="Game", pairing_preference="OPTIONAL", server_module_name="game-server"
        ),
        files=[CrowdyStudioProjectFile(target="SERVER", path="src/lib.rs", content="fn x() {}")],
        sdk_version="1",
        abi_version=1,
        revision=CrowdyStudioProjectRevision(id="4", saved_at="2026-10-29T00:00:00Z"),
        source=source,  # type: ignore[arg-type]
        github=(
            CrowdyStudioProjectGitHub(owner="me", repo="game", branch="main", sha=sha)
            if source == "GITHUB"
            else None
        ),
        created_at="2026-10-29T00:00:00Z",
        updated_at="2026-10-29T00:00:00Z",
    )


class Provider:
    def __init__(self, project: CrowdyStudioProject) -> None:
        self.project = project
        self.projects = [project]
        self.reads = 0

    async def list_projects(self, app_id: Any) -> list[CrowdyStudioProjectSummary]:
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
            for p in self.projects
        ]

    async def get_project(self, app_id: Any, grid_id: Any, project_id: str) -> CrowdyStudioProject:
        self.reads += 1
        return self.project

    async def list_personal_library_files(self, app_id: Any) -> list[Any]:
        return []

    async def list_common_files(self, app_id: Any) -> list[Any]:
        return []


class Mods:
    async def mod_logs(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []


# ---------------------------------------------------------------------- paths


def test_repo_paths_are_crowdyjs_s() -> None:
    for ok in ("package.json", "src/main.ts", "a/b-c_d.e/f", "README"):
        assert is_repo_path(ok), ok
    for refused in (
        "",
        "/abs",
        "a/../b",
        "dir/",
        ".github/workflows/ci.yml",
        ".env",
        "config/.env.local",
        "x/.git/config",
        "web/node_modules/a.js",
        ".hidden",
        "a//b",
        "spaced name",
        42,
    ):
        assert not is_repo_path(refused), refused
    assert is_repo_dir("")
    assert is_repo_dir("src")
    assert not is_repo_dir("src/../x")
    assert normalize_repo_path("./src//main.ts") == "src/main.ts"
    assert normalize_repo_path("src\\scenes\\pool.mjs") == "src/scenes/pool.mjs"
    assert normalize_repo_path("../escape") is None
    assert normalize_repo_path(None) is None
    assert is_likely_binary_path("art/tiles.PNG")
    assert is_likely_binary_path("models/ship.gltf.bin")
    assert not is_likely_binary_path("src/main.ts")
    assert not is_likely_binary_path("Makefile")


# ---------------------------------------------------------------------- the transport


async def test_get_files_and_commit_files_send_crowdyjs_s_documents(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    github = CrowdyStudioGitHubTransport(graphql)
    api.reply_with_root(
        {
            "commitSha": "c1",
            "missing": ["gone.txt"],
            "files": [
                {"path": "a.ts", "sha": "b1", "size": 3, "binary": False, "content": "x=1"},
                {"path": "i.png", "sha": "b2", "size": 9, "binary": True, "content": None},
            ],
        }
    )
    files = await github.get_files(
        {"appId": "42", "projectId": "p-1", "paths": ["a.ts", "i.png", "gone.txt"]}
    )
    assert isinstance(files, CrowdyStudioGitHubFiles)
    assert files.missing == ["gone.txt"]
    assert files.files[1].binary
    assert files.files[1].content is None
    assert api.last.operation_name == "CrowdyStudioGitHubFiles"
    assert "files { path sha size binary content }" in api.last.query
    assert api.last.variables == {
        "input": {"appId": "42", "projectId": "p-1", "paths": ["a.ts", "i.png", "gone.txt"]}
    }

    api.reply_with_root({"commitSha": "c2", "parentSha": "c1", "written": ["a.ts"], "deleted": []})
    commit = await github.commit_files(
        {
            "appId": "42",
            "projectId": "p-1",
            "message": "edit",
            "expectedCommitSha": "c1",
            "changes": [{"path": "a.ts", "content": "x=2"}, {"path": "b.ts", "delete": True}],
        }
    )
    assert commit == CrowdyStudioGitHubCommit(
        commit_sha="c2", parent_sha="c1", written=["a.ts"], deleted=[]
    )
    assert api.last.operation_name == "CrowdyStudioGitHubCommitFiles"
    assert api.last.variables["input"]["changes"][1] == {"path": "b.ts", "delete": True}


async def test_layout_reads_the_kind_and_an_older_server_reads_as_studio_crates(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    github = CrowdyStudioGitHubTransport(graphql)
    layout = {
        "commitSha": "c1",
        "server": ".",
        "client": None,
        "assets": "assets",
        "fromFile": True,
    }
    api.reply_with_root({**layout, "kind": "CONSTRUCT_GAME"})
    assert (await github.layout({"appId": "42", "projectId": "p-1"})).kind == "CONSTRUCT_GAME"
    assert "fromFile kind }" in api.last.query
    assert msgspec.convert(layout, CrowdyStudioGitHubLayout).kind == "STUDIO_CRATES"


# ---------------------------------------------------------------------- the draft store


class MemoryDraft:
    """A draft over a fixed base: enough of a store to drive the controller."""

    kind = "memory"

    def __init__(self, base: dict[str, str], binary: Sequence[str] = ()) -> None:
        self.base = dict(base)
        self.files = dict(base)
        self.binary = set(binary)
        self.revision = 0
        self.writes: list[tuple[str, str, str]] = []
        self.commits: list[tuple[str, tuple[str, ...] | None]] = []
        self.listeners: list[Callable[[DraftChangeEvent], None]] = []
        self.pending_conflicts: list[DraftConflict] = []
        self.fail_commit: DraftError | None = None
        self.disposed = False
        self.loads = 0

    def snapshot(self) -> DraftSnapshot:
        return DraftSnapshot(
            base_commit_sha="c1",
            revision=self.revision,
            entries=tuple(
                DraftEntry(
                    path=path,
                    hash=f"h-{content}",
                    size=len(content),
                    binary=path in self.binary,
                    dirty=self.base.get(path) != content,
                )
                for path, content in sorted(self.files.items())
            ),
            deleted=tuple(sorted(set(self.base) - set(self.files))),
        )

    async def load(self) -> DraftSnapshot:
        self.loads += 1
        return self.snapshot()

    async def read(self, paths: Sequence[str]) -> DraftReadResult:
        return DraftReadResult(
            files=tuple(
                DraftFile(path, f"h-{self.files[path]}", self.files[path])
                for path in paths
                if path in self.files and path not in self.binary
            ),
            missing=tuple(path for path in paths if path not in self.files),
            binary=tuple(path for path in paths if path in self.binary),
        )

    async def write(
        self,
        path: str,
        content: str,
        *,
        source: Literal["player", "agent"],
        precondition: DraftPrecondition | None = None,
    ) -> DraftWriteResult:
        previous = self.files.get(path)
        self.files[path] = content
        self.writes.append((path, content, source))
        self.emit(source, [DraftChange(path, f"h-{content}")])
        return DraftWriteResult(
            path=path,
            hash=f"h-{content}",
            previous_hash=None if previous is None else f"h-{previous}",
            revision=self.revision,
        )

    async def remove(self, path: str, **options: Any) -> Any:
        raise NotImplementedError

    async def discard(self, paths: Sequence[str] | None = None) -> None:
        for path in paths if paths is not None else list(self.files):
            if path in self.base:
                self.files[path] = self.base[path]
        self.emit("reload", [])

    async def commit(
        self, message: str, *, paths: Sequence[str] | None = None
    ) -> DraftCommitResult:
        self.commits.append((message, None if paths is None else tuple(paths)))
        if self.fail_commit is not None:
            raise self.fail_commit
        dirty = [path for path, content in self.files.items() if self.base.get(path) != content]
        self.base = dict(self.files)
        self.emit("commit", [DraftChange(path, f"h-{self.files[path]}") for path in dirty])
        return DraftCommitResult(
            commit_sha="c2abcdef99", committed=tuple(dirty), base_commit_sha="c1"
        )

    def conflicts(self) -> Sequence[DraftConflict]:
        return tuple(self.pending_conflicts)

    async def resolve_conflict(self, path: str, keep: Literal["mine", "theirs"]) -> None:
        self.pending_conflicts = [c for c in self.pending_conflicts if c.path != path]
        if keep == "theirs":
            self.files[path] = "theirs"

    def subscribe(self, listener: Callable[[DraftChangeEvent], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def dispose(self) -> None:
        self.disposed = True

    def emit(self, source: Any, changes: list[DraftChange]) -> None:
        self.revision += 1
        event = DraftChangeEvent(self.revision, "c1", tuple(changes), source)
        for listener in list(self.listeners):
            listener(event)


class GitHub:
    def __init__(self, kind: str = "CONSTRUCT_GAME") -> None:
        self.kind = kind
        self.layouts: list[Any] = []

    async def status(self, **kwargs: Any) -> Any:
        return SimpleNamespace(connected=True, github_sha="c1")

    async def layout(self, input: Any) -> Any:
        self.layouts.append(input)
        return SimpleNamespace(kind=self.kind)


async def game_editor(
    store: MemoryDraft | None, kind: str = "CONSTRUCT_GAME", project: Any = None
) -> tuple[CrowdyStudioController, Provider, GitHub, list[dict[str, str]]]:
    provider = Provider(project or make_project())
    github = GitHub(kind)
    asked: list[dict[str, str]] = []

    def factory(*, app_id: str, project_id: str, github_sha: str) -> MemoryDraft | None:
        asked.append({"app_id": app_id, "project_id": project_id, "github_sha": github_sha})
        return store

    controller = CrowdyStudioController(
        project_provider=provider,
        mods=Mods(),
        app_id="42",
        grid_id="7",
        github=github,
        draft_store=factory,
        autosave_ms=10_000,
    )
    await controller.initialize()
    await controller.when_editor_known(timeout_ms=1_000)
    return controller, provider, github, asked


async def test_a_construct_game_opens_its_draft_and_drops_the_mod_files() -> None:
    store = MemoryDraft({"package.json": "{}", "src/main.ts": "main", "src/scenes/a.ts": "a"})
    controller, _, github, asked = await game_editor(store)
    state = controller.get_state()
    assert controller.draft_store is store
    assert asked == [{"app_id": "42", "project_id": "p-1", "github_sha": "c1"}]
    assert github.layouts == [{"appId": "42", "projectId": "p-1"}]
    assert state.repository_mode is True
    assert state.repository_store == "memory"
    assert state.repository_files == ("package.json", "src/main.ts", "src/scenes/a.ts")
    assert state.repository_directories == ("src", "src/scenes")
    assert state.repository_base_commit_sha == "c1"
    assert state.repository_busy is False
    assert state.open_files == ()
    assert state.active_file is None

    # The kind is asked once per mirror commit, and the same game keeps its store.
    await controller.refresh_repository_mode()
    assert len(github.layouts) == 1
    assert len(asked) == 1


async def test_draft_files_open_edit_and_commit_as_one() -> None:
    store = MemoryDraft({"src/main.ts": "main", "art/t.png": "?"}, binary=["art/t.png"])
    controller, provider, _, _ = await game_editor(store)
    await controller.open_repository_file("./src//main.ts")
    ref = CrowdyStudioFileRef(source="REPOSITORY", path="src/main.ts")
    assert controller.get_state().active_file == ref
    assert controller.file_content(ref) == "main"
    assert controller.get_state().repository_documents == (
        CrowdyStudioRepositoryDocument("src/main.ts", "main", "h-main"),
    )

    await controller.open_repository_file("art/t.png")
    assert controller.get_state().repository_message == (
        "art/t.png is a binary file; the editor opens text only."
    )
    with pytest.raises(CrowdyStudioError, match="is not in the draft"):
        await controller.open_repository_file("src/missing.ts")
    with pytest.raises(CrowdyStudioError, match="not a file the game editor opens"):
        await controller.open_repository_file(".env")

    controller.update_repository_file("src/main.ts", "main v2")
    assert controller.file_content(ref) == "main v2"
    assert store.writes == [], "a keystroke reaches the draft after the debounce"
    result = await controller.commit_repository("Tune the main loop")
    assert store.writes == [("src/main.ts", "main v2", "player")], "the commit flushes first"
    assert store.commits == [("Tune the main loop", None)]
    assert result.committed == ("src/main.ts",)
    state = controller.get_state()
    assert state.repository_message == "Committed 1 file as c2abcde."
    assert state.repository_busy is False
    assert state.repository_dirty == ()
    reads = provider.reads
    await asyncio.sleep(0.01)
    assert provider.reads == reads + 1, "the project is read again for its new github sha"
    assert controller.get_state().open_files == (ref,), "the draft tab survives the reload"


async def test_a_debounced_write_lands_on_its_own() -> None:
    store = MemoryDraft({"a.ts": "1"})
    controller, _, _, _ = await game_editor(store)
    await controller.open_repository_file("a.ts")
    controller.update_repository_file("a.ts", "2")
    controller.update_repository_file("a.ts", "3")
    await asyncio.sleep(0.4)
    assert store.writes == [("a.ts", "3", "player")]
    assert controller.get_state().repository_documents == (
        CrowdyStudioRepositoryDocument("a.ts", "3", "h-3"),
    )
    assert controller.get_state().repository_dirty == ("a.ts",)


async def test_agent_changes_refresh_open_documents_and_player_changes_do_not() -> None:
    store = MemoryDraft({"a.ts": "1", "b.ts": "1"})
    controller, _, _, _ = await game_editor(store)
    await controller.open_repository_file("a.ts")
    await store.write("a.ts", "agent wrote", source="agent")
    await asyncio.sleep(0)
    assert controller.file_content(CrowdyStudioFileRef("REPOSITORY", "a.ts")) == "agent wrote"
    store.files["a.ts"] = "behind the editor's back"
    store.emit("player", [DraftChange("a.ts", "h-x")])
    await asyncio.sleep(0)
    assert controller.file_content(CrowdyStudioFileRef("REPOSITORY", "a.ts")) == "agent wrote"
    del store.files["a.ts"]
    store.emit("reload", [])
    await asyncio.sleep(0)
    state = controller.get_state()
    assert state.repository_documents == ()
    assert state.active_file is None


async def test_a_refused_commit_lists_conflicts_and_each_is_settled() -> None:
    store = MemoryDraft({"a.ts": "1"})
    controller, _, _, _ = await game_editor(store)
    await controller.open_repository_file("a.ts")
    conflict = DraftConflict(path="a.ts", base="h-1", theirs="h-t", ours="h-2")
    store.pending_conflicts = [conflict]
    store.fail_commit = DraftError("DRAFT_CONFLICT", "The branch moved", [conflict])
    with pytest.raises(DraftError) as raised:
        await controller.commit_repository("edit", paths=["a.ts"])
    assert raised.value.code == "DRAFT_CONFLICT"
    assert store.commits == [("edit", ("a.ts",))]
    state = controller.get_state()
    assert state.repository_conflicts == (conflict,)
    assert state.repository_message == "The branch moved"
    await controller.resolve_repository_conflict("a.ts", "theirs")
    assert controller.get_state().repository_conflicts == ()
    assert controller.file_content(CrowdyStudioFileRef("REPOSITORY", "a.ts")) == "theirs"


async def test_discard_drops_pending_writes() -> None:
    store = MemoryDraft({"a.ts": "1"})
    controller, _, _, _ = await game_editor(store)
    await controller.open_repository_file("a.ts")
    controller.update_repository_file("a.ts", "2")
    await controller.discard_repository_files(["a.ts"])
    await asyncio.sleep(0.4)
    assert store.writes == []


async def test_a_mod_keeps_the_mod_editor() -> None:
    store = MemoryDraft({"a.ts": "1"})
    controller, _, _, asked = await game_editor(store, kind="STUDIO_CRATES")
    assert controller.draft_store is None
    assert asked == []
    assert controller.get_state().repository_mode is None
    with pytest.raises(DraftError) as raised:
        await controller.commit_repository("x")
    assert raised.value.code == "DRAFT_UNAVAILABLE"
    # A STUDIO project never asks for the layout at all.
    controller, _, github, _ = await game_editor(store, project=make_project(source="STUDIO"))
    assert github.layouts == []
    assert controller.draft_store is None


async def test_destroy_disposes_the_draft_and_the_wait_is_bounded() -> None:
    store = MemoryDraft({"a.ts": "1"})
    controller, _, _, _ = await game_editor(store)
    controller.destroy()
    assert store.disposed
    assert controller.get_state().repository_mode is False

    provider = Provider(make_project())
    provider.projects = []
    empty = CrowdyStudioController(project_provider=provider, mods=Mods(), app_id="42", grid_id="7")
    await empty.initialize()
    await asyncio.wait_for(empty.when_editor_known(), 1)

    never = CrowdyStudioController(project_provider=provider, mods=Mods(), app_id="42", grid_id="7")
    await asyncio.wait_for(never.when_editor_known(timeout_ms=20), 1)
