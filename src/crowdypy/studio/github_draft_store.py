"""The hosted draft (CrowdyJS's ``GitHubDraftStore``): an overlay over the bound branch.

The base is the project's mirror commit (``github_sha``): its tree comes from
``crowdyStudioGitHubTree``, file text from ``crowdyStudioGitHubFiles`` (100 paths per call,
cached by blob id). The overlay holds every path the draft changed, created or deleted, with
the blob it started from. Committing is one ``crowdyStudioGitHubCommitFiles`` on top of the
base; when the branch has moved, the store refreshes the base, carries every edit that does not
collide onto it, retries once, and otherwise reports the colliding paths as conflicts for the
player to settle.

Pass a factory that builds one to the controller (``draft_store=``)::

    def drafts(*, app_id: str, project_id: str, github_sha: str) -> GitHubDraftStore:
        return GitHubDraftStore(
            transport=game.crowdy_studio_github,
            app_id=app_id,
            project_id=project_id,
            base_commit_sha=github_sha,
        )
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from crowdypy.domains.crowdy_studio_github import (
    CrowdyStudioGitHubCommit,
    CrowdyStudioGitHubFiles,
    CrowdyStudioGitHubStatus,
    CrowdyStudioGitHubTree,
)
from crowdypy.errors import CrowdyGraphQLError
from crowdypy.studio.draft import (
    DraftChange,
    DraftChangeEvent,
    DraftCommitResult,
    DraftConflict,
    DraftDeleteResult,
    DraftEntry,
    DraftError,
    DraftFile,
    DraftOverlayStorage,
    DraftPrecondition,
    DraftReadResult,
    DraftSnapshot,
    DraftSource,
    DraftWriteResult,
    MemoryDraftOverlayStorage,
    StoredOverlay,
    StoredOverlayEntry,
    git_blob_sha_of_text,
    is_likely_binary_path,
    normalize_repo_path,
    utf8_length,
)

__all__ = ["GitHubDraftStore", "GitHubDraftTransport"]

_READ_BATCH = 100
_COMMIT_BATCH = 100
_PERSIST_DEBOUNCE_S = 0.25

T = TypeVar("T")


class GitHubDraftTransport(Protocol):
    """The GitHub calls the hosted draft needs; ``client.crowdy_studio_github`` (a
    :class:`~crowdypy.domains.crowdy_studio_github.CrowdyStudioGitHubTransport`) is one."""

    async def tree(self, input: Mapping[str, Any]) -> CrowdyStudioGitHubTree: ...

    async def get_files(self, input: Mapping[str, Any]) -> CrowdyStudioGitHubFiles: ...

    async def commit_files(self, input: Mapping[str, Any]) -> CrowdyStudioGitHubCommit: ...

    async def refresh(self, input: Mapping[str, Any]) -> CrowdyStudioGitHubStatus: ...


@dataclass(frozen=True, slots=True)
class _BaseFile:
    sha: str
    size: int


def _is_stale(error: BaseException) -> bool:
    return isinstance(error, CrowdyGraphQLError) and error.code == "GITHUB_STALE_SHA"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class GitHubDraftStore:
    """A :class:`~crowdypy.studio.DraftStore` over a bound GitHub branch, kept in
    ``storage`` (process memory by default)."""

    kind = "github"

    def __init__(
        self,
        *,
        transport: GitHubDraftTransport,
        app_id: str | int,
        project_id: str,
        base_commit_sha: str,
        storage: DraftOverlayStorage | None = None,
        max_text_bytes: int = 512 * 1024,
        max_entries: int = 5000,
    ) -> None:
        """``base_commit_sha`` is the project's mirror commit when the store is created;
        ``max_text_bytes`` is the largest text file the draft serves (the server's cap) and
        ``max_entries`` the most entries it lists."""
        self._transport = transport
        self._app_id = str(app_id)
        self._project_id = project_id
        self._storage: DraftOverlayStorage = storage or MemoryDraftOverlayStorage()
        self._max_text_bytes = max_text_bytes
        self._max_entries = max_entries
        self._key = f"{self._app_id}/{project_id}"
        self._base_commit_sha = base_commit_sha
        self._base: dict[str, _BaseFile] = {}
        self._truncated = False
        self._overlay: dict[str, StoredOverlayEntry] = {}
        #: Blob id -> text, or None when binary or over the cap.
        self._text: dict[str, str | None] = {}
        self._conflicts: tuple[DraftConflict, ...] = ()
        self._revision = 0
        self._loaded = False
        self._disposed = False
        self._listeners: dict[Callable[[DraftChangeEvent], None], None] = {}
        self._lock = asyncio.Lock()
        self._persist_timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Future[Any]] = set()

    # ------------------------------------------------------------------ reads

    async def load(self) -> DraftSnapshot:
        async def work() -> DraftSnapshot:
            await self._load_base(self._base_commit_sha)
            stored = await self._storage.load_overlay(self._key)
            self._overlay.clear()
            if stored is not None:
                for entry in stored.entries:
                    if normalize_repo_path(entry.path) == entry.path:
                        self._overlay[entry.path] = entry
                self._revision = max(self._revision, stored.revision)
                if stored.base_commit_sha != self._base_commit_sha:
                    self._rebase_overlay()
            self._loaded = True
            self._revision += 1
            self._emit((), "reload")
            return self.snapshot()

        return await self._serial(work)

    def snapshot(self) -> DraftSnapshot:
        entries: list[DraftEntry] = []
        deleted: list[str] = []
        for path in sorted({*self._base, *self._overlay}):
            edit = self._overlay.get(path)
            base_file = self._base.get(path)
            if edit is not None and edit.content is None:
                if base_file is not None:
                    deleted.append(path)
                continue
            if edit is not None and edit.hash:
                entries.append(
                    DraftEntry(
                        path=path,
                        hash=edit.hash,
                        size=utf8_length(edit.content or ""),
                        binary=False,
                        dirty=edit.hash != (base_file.sha if base_file else None),
                    )
                )
                continue
            if base_file is None:
                continue
            entries.append(
                DraftEntry(
                    path=path,
                    hash=base_file.sha,
                    size=base_file.size,
                    binary=self._is_binary_base(path, base_file),
                    dirty=False,
                )
            )
        return DraftSnapshot(
            base_commit_sha=self._base_commit_sha,
            revision=self._revision,
            entries=tuple(entries),
            deleted=tuple(deleted),
            truncated=self._truncated,
        )

    async def read(self, paths: Sequence[str]) -> DraftReadResult:
        self._assert_loaded()
        files: list[DraftFile] = []
        missing: list[str] = []
        binary: list[str] = []
        fetch: list[str] = []
        for raw in paths:
            path = normalize_repo_path(raw)
            if not path:
                missing.append(raw)
                continue
            edit = self._overlay.get(path)
            if edit is not None:
                if edit.content is None or not edit.hash:
                    missing.append(path)
                else:
                    files.append(DraftFile(path, edit.hash, edit.content))
                continue
            base_file = self._base.get(path)
            if base_file is None:
                missing.append(path)
                continue
            if base_file.size > self._max_text_bytes:
                binary.append(path)
                continue
            if base_file.sha in self._text:
                cached, text = True, self._text[base_file.sha]
            else:
                cached, text = await self._storage.load_blob_text(base_file.sha)
            if not cached:
                fetch.append(path)
                continue
            self._text[base_file.sha] = text
            if text is None:
                binary.append(path)
            else:
                files.append(DraftFile(path, base_file.sha, text))
        for start in range(0, len(fetch), _READ_BATCH):
            got = await self._transport.get_files(
                {
                    "appId": self._app_id,
                    "projectId": self._project_id,
                    "commitSha": self._base_commit_sha,
                    "paths": fetch[start : start + _READ_BATCH],
                }
            )
            for file in got.files:
                text = None if file.binary or file.content is None else file.content
                self._text[file.sha] = text
                with contextlib.suppress(Exception):
                    await self._storage.save_blob_text(file.sha, text)
                if text is None:
                    binary.append(file.path)
                else:
                    files.append(DraftFile(file.path, file.sha, text))
            missing.extend(got.missing)
        return DraftReadResult(files=tuple(files), missing=tuple(missing), binary=tuple(binary))

    def conflicts(self) -> Sequence[DraftConflict]:
        return self._conflicts

    # ------------------------------------------------------------------ writes

    async def write(
        self,
        path: str,
        content: str,
        *,
        source: Literal["player", "agent"],
        precondition: DraftPrecondition | None = None,
    ) -> DraftWriteResult:
        async def work() -> DraftWriteResult:
            self._assert_loaded()
            target = self._writable_path(path)
            if utf8_length(content) > self._max_text_bytes:
                raise DraftError(
                    "DRAFT_TOO_LARGE", f"{target} is larger than {self._max_text_bytes} bytes."
                )
            current = self._current_hash(target)
            self._check_precondition(target, current, precondition)
            blob = git_blob_sha_of_text(content)
            base_file = self._base.get(target)
            existing = self._overlay.get(target)
            if base_file is not None and base_file.sha == blob:
                self._overlay.pop(target, None)
            else:
                self._overlay[target] = StoredOverlayEntry(
                    path=target,
                    content=content,
                    hash=blob,
                    base_sha=existing.base_sha
                    if existing is not None
                    else (base_file.sha if base_file else None),
                    updated_at=_now_ms(),
                )
            self._text[blob] = content
            self._revision += 1
            self._persist()
            self._emit((DraftChange(target, blob),), source)
            return DraftWriteResult(
                path=target, hash=blob, previous_hash=current, revision=self._revision
            )

        return await self._serial(work)

    async def remove(
        self,
        path: str,
        *,
        source: Literal["player", "agent"],
        precondition: DraftPrecondition | None = None,
    ) -> DraftDeleteResult:
        async def work() -> DraftDeleteResult:
            self._assert_loaded()
            target = self._writable_path(path)
            current = self._current_hash(target)
            if current is None:
                raise DraftError("DRAFT_NOT_FOUND", f"{target} is not in the draft.")
            self._check_precondition(target, current, precondition)
            base_file = self._base.get(target)
            existing = self._overlay.get(target)
            if base_file is not None:
                self._overlay[target] = StoredOverlayEntry(
                    path=target,
                    content=None,
                    hash=None,
                    base_sha=existing.base_sha if existing is not None else base_file.sha,
                    updated_at=_now_ms(),
                )
            else:
                self._overlay.pop(target, None)
            self._revision += 1
            self._persist()
            self._emit((DraftChange(target, None),), source)
            return DraftDeleteResult(path=target, previous_hash=current, revision=self._revision)

        return await self._serial(work)

    async def discard(self, paths: Sequence[str] | None = None) -> None:
        async def work() -> None:
            self._assert_loaded()
            targets = (
                [p for p in (normalize_repo_path(raw) for raw in paths) if p]
                if paths is not None
                else list(self._overlay)
            )
            changes: list[DraftChange] = []
            for target in targets:
                if self._overlay.pop(target, None) is None:
                    continue
                base_file = self._base.get(target)
                changes.append(DraftChange(target, base_file.sha if base_file else None))
            self._conflicts = tuple(c for c in self._conflicts if c.path not in targets)
            if not changes:
                return
            self._revision += 1
            self._persist()
            self._emit(tuple(changes), "player")

        await self._serial(work)

    async def resolve_conflict(self, path: str, keep: Literal["mine", "theirs"]) -> None:
        async def work() -> None:
            target = normalize_repo_path(path)
            if not target or not any(c.path == target for c in self._conflicts):
                return
            self._conflicts = tuple(c for c in self._conflicts if c.path != target)
            base_file = self._base.get(target)
            if keep == "theirs":
                self._overlay.pop(target, None)
                self._revision += 1
                self._persist()
                self._emit((DraftChange(target, base_file.sha if base_file else None),), "player")
                return
            edit = self._overlay.get(target)
            if edit is not None:
                self._overlay[target] = replace(edit, base_sha=base_file.sha if base_file else None)
            self._persist()

        await self._serial(work)

    async def commit(
        self, message: str, *, paths: Sequence[str] | None = None
    ) -> DraftCommitResult:
        async def work() -> DraftCommitResult:
            self._assert_loaded()
            try:
                return await self._commit_once(message, paths)
            except CrowdyGraphQLError as error:
                if not _is_stale(error):
                    raise
            # The branch moved: bring the base forward, carry what does not collide, retry once.
            refreshed = await self._transport.refresh(
                {"appId": self._app_id, "projectId": self._project_id}
            )
            if not refreshed.github_sha:
                raise DraftError("DRAFT_UNAVAILABLE", "The project is no longer bound to GitHub.")
            await self._load_base(refreshed.github_sha)
            self._rebase_overlay()
            self._revision += 1
            self._persist()
            self._emit((), "rebase")
            blocking = tuple(c for c in self._conflicts if paths is None or c.path in paths)
            if blocking:
                listed = ", ".join(c.path for c in blocking)
                raise DraftError(
                    "DRAFT_CONFLICT",
                    f"The branch moved and changed {listed} too. Keep the draft or take the "
                    "branch's version, then commit again.",
                    blocking,
                )
            return await self._commit_once(message, paths)

        return await self._serial(work)

    def subscribe(self, listener: Callable[[DraftChangeEvent], None]) -> Callable[[], None]:
        self._listeners[listener] = None

        def unsubscribe() -> None:
            self._listeners.pop(listener, None)

        return unsubscribe

    def dispose(self) -> None:
        if self._persist_timer is not None:
            self._persist_timer.cancel()
            self._persist_timer = None
            self._spawn(self._flush())
        self._disposed = True
        self._listeners.clear()

    # ------------------------------------------------------------------ internals

    async def _commit_once(self, message: str, paths: Sequence[str] | None) -> DraftCommitResult:
        wanted = None if paths is None else {p for p in map(normalize_repo_path, paths) if p}
        dirty = [
            edit
            for edit in self._overlay.values()
            if (wanted is None or edit.path in wanted)
            and (
                edit.path in self._base
                if edit.content is None
                else edit.hash != self._base_sha(edit.path)
            )
        ]
        if not dirty:
            raise DraftError("DRAFT_NOT_FOUND", "The draft has nothing to commit.")
        dirty_paths = {edit.path for edit in dirty}
        conflicted = tuple(c for c in self._conflicts if c.path in dirty_paths)
        if conflicted:
            listed = ", ".join(c.path for c in conflicted)
            raise DraftError("DRAFT_CONFLICT", f"Settle {listed} before committing.", conflicted)
        title = message.strip()[:200] or f"Update {len(dirty)} file{'' if len(dirty) == 1 else 's'}"
        commit_sha = self._base_commit_sha
        committed: list[str] = []
        # One commit when the draft fits a call; larger drafts stack commits on each other.
        for start in range(0, len(dirty), _COMMIT_BATCH):
            batch = dirty[start : start + _COMMIT_BATCH]
            result = await self._transport.commit_files(
                {
                    "appId": self._app_id,
                    "projectId": self._project_id,
                    "message": (
                        f"{title} ({start // _COMMIT_BATCH + 1})"
                        if len(dirty) > _COMMIT_BATCH
                        else title
                    ),
                    "expectedCommitSha": commit_sha,
                    "changes": [
                        {"path": edit.path, "delete": True}
                        if edit.content is None
                        else {"path": edit.path, "content": edit.content}
                        for edit in batch
                    ],
                }
            )
            commit_sha = result.commit_sha
            for edit in batch:
                committed.append(edit.path)
                if edit.content is None:
                    self._base.pop(edit.path, None)
                else:
                    self._base[edit.path] = _BaseFile(edit.hash or "", utf8_length(edit.content))
                now = self._overlay.get(edit.path)
                if now is not None and now.updated_at == edit.updated_at:
                    del self._overlay[edit.path]
                elif now is not None:
                    self._overlay[edit.path] = replace(now, base_sha=self._base_sha(edit.path))
            self._base_commit_sha = commit_sha
        self._revision += 1
        self._persist()
        self._emit(tuple(DraftChange(path, self._base_sha(path)) for path in committed), "commit")
        return DraftCommitResult(
            commit_sha=commit_sha, committed=tuple(committed), base_commit_sha=commit_sha
        )

    async def _load_base(self, commit_sha: str) -> None:
        tree = await self._transport.tree(
            {"appId": self._app_id, "projectId": self._project_id, "commitSha": commit_sha}
        )
        base: dict[str, _BaseFile] = {}
        dropped = False
        for entry in tree.entries:
            if entry.type != "blob" or not entry.sha:
                continue
            if normalize_repo_path(entry.path) != entry.path:
                continue
            if len(base) >= self._max_entries:
                dropped = True
                continue
            base[entry.path] = _BaseFile(entry.sha, entry.size or 0)
        self._base = base
        self._truncated = dropped
        self._base_commit_sha = tree.commit_sha

    def _rebase_overlay(self) -> None:
        """After the base moved: an edit whose starting blob is still the base blob carries over
        unchanged; one where the branch also changed the path becomes a conflict, unless both
        sides ended at the same content."""
        conflicts: list[DraftConflict] = []
        for path, edit in list(self._overlay.items()):
            theirs = self._base_sha(path)
            if theirs == edit.base_sha:
                continue
            if theirs == edit.hash:
                del self._overlay[path]
                continue
            conflicts.append(
                DraftConflict(path=path, base=edit.base_sha, theirs=theirs, ours=edit.hash)
            )
        self._conflicts = tuple(conflicts)

    def _base_sha(self, path: str) -> str | None:
        base_file = self._base.get(path)
        return base_file.sha if base_file is not None else None

    def _current_hash(self, path: str) -> str | None:
        edit = self._overlay.get(path)
        if edit is not None:
            return None if edit.content is None else edit.hash
        return self._base_sha(path)

    @staticmethod
    def _check_precondition(
        path: str, current: str | None, precondition: DraftPrecondition | None
    ) -> None:
        if precondition is None:
            return
        if precondition.kind == "createIfAbsent" and current is not None:
            raise DraftError(
                "DRAFT_STALE", f"{path} already exists in the draft; read it before replacing it."
            )
        if precondition.kind == "ifHash" and current != precondition.hash:
            raise DraftError(
                "DRAFT_STALE",
                f"{path} is no longer in the draft."
                if current is None
                else f"{path} changed in the draft since it was read; read it again.",
            )

    @staticmethod
    def _writable_path(raw: str) -> str:
        path = normalize_repo_path(raw)
        if not path:
            raise DraftError(
                "DRAFT_PATH_REFUSED", f"{raw} is not a file the game editor may write."
            )
        return path

    def _is_binary_base(self, path: str, file: _BaseFile) -> bool:
        if file.size > self._max_text_bytes:
            return True
        if file.sha in self._text:
            return self._text[file.sha] is None
        return is_likely_binary_path(path)

    def _assert_loaded(self) -> None:
        if self._disposed:
            raise DraftError("DRAFT_UNAVAILABLE", "The draft was closed.")
        if not self._loaded:
            raise DraftError("DRAFT_UNAVAILABLE", "The draft is still loading.")

    def _emit(self, changes: tuple[DraftChange, ...], source: DraftSource) -> None:
        event = DraftChangeEvent(self._revision, self._base_commit_sha, changes, source)
        for listener in list(self._listeners):
            # A listener's failure is its own.
            with contextlib.suppress(Exception):
                listener(event)

    def _persist(self) -> None:
        if self._persist_timer is not None:
            self._persist_timer.cancel()
            self._persist_timer = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def fire() -> None:
            self._persist_timer = None
            self._spawn(self._flush())

        self._persist_timer = loop.call_later(_PERSIST_DEBOUNCE_S, fire)

    async def _flush(self) -> None:
        # The draft stays in memory when this fails; the next change tries again.
        with contextlib.suppress(Exception):
            await self._storage.save_overlay(
                self._key,
                StoredOverlay(
                    base_commit_sha=self._base_commit_sha,
                    revision=self._revision,
                    entries=tuple(self._overlay.values()),
                ),
            )

    async def _serial(self, work: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            return await work()

    def _spawn(self, awaitable: Awaitable[Any]) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            return
        task = asyncio.ensure_future(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


if TYPE_CHECKING:
    from crowdypy.studio.draft import DraftStore

    def _is_a_draft_store(store: GitHubDraftStore) -> DraftStore:
        return store
