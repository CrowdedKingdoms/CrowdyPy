"""The game editor's draft (CrowdyJS's ``crowdy-studio/draft``): what the controller asks of a
draft store, which repository paths the game editor may touch, git blob ids, and where a draft
is kept between sessions.

A draft is the working copy of a bound Construct game's repository before anything is
committed: edits over the bound branch, versioned by git blob ids, published as one commit.
:class:`~crowdypy.studio.GitHubDraftStore` is the hosted backend; a game's dev server may
provide another for a local checkout. The controller drives any object with
:class:`DraftStore`'s methods, which its ``draft_store`` factory returns.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from crowdypy.errors import CrowdyError

__all__ = [
    "DRAFT_ERROR_CODES",
    "DraftChange",
    "DraftChangeEvent",
    "DraftCommitResult",
    "DraftConflict",
    "DraftDeleteResult",
    "DraftEntry",
    "DraftError",
    "DraftErrorCode",
    "DraftFile",
    "DraftOverlayStorage",
    "DraftPrecondition",
    "DraftReadResult",
    "DraftSnapshot",
    "DraftSource",
    "DraftStore",
    "DraftWriteResult",
    "MemoryDraftOverlayStorage",
    "StoredOverlay",
    "StoredOverlayEntry",
    "git_blob_sha",
    "git_blob_sha_of_text",
    "is_likely_binary_path",
    "is_repo_dir",
    "is_repo_path",
    "normalize_repo_path",
    "utf8_length",
]

#: Who changed the draft. ``player`` is the editor; ``agent`` the harness.
DraftSource = Literal["player", "agent", "commit", "rebase", "reload"]
DraftErrorCode = Literal[
    "DRAFT_STALE",
    "DRAFT_NOT_FOUND",
    "DRAFT_PATH_REFUSED",
    "DRAFT_TOO_LARGE",
    "DRAFT_CONFLICT",
    "DRAFT_DECLINED",
    "DRAFT_UNAVAILABLE",
]

#: The stable codes a :class:`DraftError` carries, by CrowdyJS's keys.
DRAFT_ERROR_CODES: dict[str, DraftErrorCode] = {
    "stale": "DRAFT_STALE",
    "not_found": "DRAFT_NOT_FOUND",
    "refused": "DRAFT_PATH_REFUSED",
    "too_large": "DRAFT_TOO_LARGE",
    "conflict": "DRAFT_CONFLICT",
    "declined": "DRAFT_DECLINED",
    "unavailable": "DRAFT_UNAVAILABLE",
}


@dataclass(frozen=True, slots=True)
class DraftEntry:
    path: str
    #: Git blob id of the draft content.
    hash: str
    size: int
    #: Not UTF-8 text (or over the text cap): listed, never readable as text.
    binary: bool
    #: Differs from the base commit.
    dirty: bool


@dataclass(frozen=True, slots=True)
class DraftSnapshot:
    #: Commit the draft builds on; ``None`` when the store has no remote base.
    base_commit_sha: str | None
    #: Bumps on every change.
    revision: int
    entries: tuple[DraftEntry, ...] = ()
    #: Paths the draft deletes that exist at the base.
    deleted: tuple[str, ...] = ()
    #: The tree was larger than the store serves; entries were dropped.
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class DraftChange:
    path: str
    #: New hash, or ``None`` for a deletion.
    hash: str | None


@dataclass(frozen=True, slots=True)
class DraftChangeEvent:
    revision: int
    base_commit_sha: str | None
    changes: tuple[DraftChange, ...]
    source: DraftSource


@dataclass(frozen=True, slots=True)
class DraftPrecondition:
    """``createIfAbsent``, or ``ifHash`` with the blob id the writer read."""

    kind: Literal["createIfAbsent", "ifHash"]
    hash: str | None = None


@dataclass(frozen=True, slots=True)
class DraftFile:
    path: str
    hash: str
    content: str


@dataclass(frozen=True, slots=True)
class DraftReadResult:
    files: tuple[DraftFile, ...] = ()
    missing: tuple[str, ...] = ()
    binary: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DraftWriteResult:
    path: str
    hash: str
    #: Hash before the write; ``None`` when the write created the file.
    previous_hash: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class DraftDeleteResult:
    path: str
    previous_hash: str
    revision: int


@dataclass(frozen=True, slots=True)
class DraftCommitResult:
    commit_sha: str
    #: Paths written or deleted by the commit.
    committed: tuple[str, ...]
    base_commit_sha: str


@dataclass(frozen=True, slots=True)
class DraftConflict:
    """A path the branch and the draft both changed since the draft's base."""

    path: str
    #: Blob the draft edit started from (``None``: the draft created the path).
    base: str | None
    #: Blob on the branch now (``None``: deleted there).
    theirs: str | None
    #: The draft's content hash (``None``: deleted in the draft).
    ours: str | None


class DraftError(CrowdyError):
    """A draft operation that failed for a reason the caller can act on, by ``code``."""

    def __init__(
        self, code: DraftErrorCode, message: str, conflicts: Sequence[DraftConflict] = ()
    ) -> None:
        super().__init__(message)
        self.code: DraftErrorCode = code
        self.conflicts: tuple[DraftConflict, ...] = tuple(conflicts)


class DraftStore(Protocol):
    """One open project's draft. ``kind`` names the backend for the status line (``github``,
    ``dev-server``)."""

    @property
    def kind(self) -> str: ...

    async def load(self) -> DraftSnapshot:
        """Load (or reload) the base and the stored draft."""
        ...

    def snapshot(self) -> DraftSnapshot:
        """The current snapshot; call after :meth:`load`."""
        ...

    async def read(self, paths: Sequence[str]) -> DraftReadResult: ...

    async def write(
        self,
        path: str,
        content: str,
        *,
        source: Literal["player", "agent"],
        precondition: DraftPrecondition | None = None,
    ) -> DraftWriteResult: ...

    async def remove(
        self,
        path: str,
        *,
        source: Literal["player", "agent"],
        precondition: DraftPrecondition | None = None,
    ) -> DraftDeleteResult: ...

    async def discard(self, paths: Sequence[str] | None = None) -> None:
        """Drop draft changes (all of them when ``paths`` is ``None``)."""
        ...

    async def commit(
        self, message: str, *, paths: Sequence[str] | None = None
    ) -> DraftCommitResult:
        """Publish the dirty paths (or the named ones) as one commit."""
        ...

    def conflicts(self) -> Sequence[DraftConflict]: ...

    async def resolve_conflict(self, path: str, keep: Literal["mine", "theirs"]) -> None:
        """``mine`` keeps the draft and commits over the branch; ``theirs`` drops the draft
        edit."""
        ...

    def subscribe(self, listener: Callable[[DraftChangeEvent], None]) -> Callable[[], None]: ...

    def dispose(self) -> None: ...


# ---------------------------------------------------------------------- paths
#
# Listed and writable: relative paths whose segments do not name `.git`, `node_modules` or a
# dotenv file, starting with a letter or digit (so dotfiles at the root, `.github/` included,
# are out). Workflow files are refused even where a dotfile rule would allow them: the Studio
# GitHub App has no `workflows` permission. The server applies its own checks on commit.

_SAFE_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_WORKFLOWS = re.compile(r"\.github/workflows(/|$)", re.IGNORECASE)


def _denied_segment(segment: str) -> bool:
    return segment in (".git", "node_modules", ".env") or segment.startswith(".env.")


def is_repo_path(path: object) -> bool:
    """A relative path in the bound repository the game editor may list and write."""
    if (
        not isinstance(path, str)
        or not _SAFE_PATH.fullmatch(path)
        or ".." in path
        or path.endswith("/")
    ):
        return False
    if any(segment == "" or _denied_segment(segment) for segment in path.split("/")):
        return False
    return _WORKFLOWS.match(path) is None


def is_repo_dir(path: str) -> bool:
    """A repository directory, including the root (``""``)."""
    return path == "" or is_repo_path(path)


def normalize_repo_path(path: object) -> str | None:
    """``./a//b`` -> ``a/b``; ``None`` for anything :func:`is_repo_path` refuses."""
    if not isinstance(path, str):
        return None
    cleaned = "/".join(
        segment for segment in path.replace("\\", "/").split("/") if segment not in ("", ".")
    )
    return cleaned if is_repo_path(cleaned) else None


_BINARY_EXTENSIONS = frozenset(
    {
        *("png", "jpg", "jpeg", "gif", "webp", "avif", "ico", "bmp", "tif", "tiff", "psd"),
        *("glb", "gltf.bin", "bin", "fbx", "obj.bin", "ktx", "ktx2", "basis", "hdr", "exr"),
        *("mp3", "ogg", "wav", "flac", "m4a", "aac", "mp4", "webm", "mov"),
        *("woff", "woff2", "ttf", "otf", "eot"),
        *("wasm", "zip", "gz", "tgz", "br", "zst", "7z", "rar", "tar", "pdf", "jar", "class"),
        *("so", "dll", "exe"),
    }
)


def is_likely_binary_path(path: str) -> bool:
    """A path that is binary by extension, before its bytes are known."""
    name = path[path.rfind("/") + 1 :].lower()
    dot = name.find(".")
    if dot == -1:
        return False
    return (
        name[name.rfind(".") + 1 :] in _BINARY_EXTENSIONS or name[dot + 1 :] in _BINARY_EXTENSIONS
    )


# ---------------------------------------------------------------------- git blob ids
#
# A draft file's version is the SHA-1 git computes for it (`blob <bytes>\0<content>`), so an
# unchanged file in the draft carries the same id as its blob on GitHub, and "dirty" is a
# string comparison.


def git_blob_sha(data: bytes) -> str:
    """The git blob SHA-1 of raw bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def git_blob_sha_of_text(text: str) -> str:
    """The git blob SHA-1 of UTF-8 text."""
    return git_blob_sha(text.encode())


def utf8_length(text: str) -> int:
    """The UTF-8 byte length of text."""
    return len(text.encode())


# ---------------------------------------------------------------------- where a draft is kept


@dataclass(frozen=True, slots=True)
class StoredOverlayEntry:
    """One path the draft changed, created or deleted."""

    path: str
    #: Draft content; ``None`` for a deletion.
    content: str | None
    hash: str | None
    #: Base blob the edit started from; ``None`` when the draft created the path.
    base_sha: str | None
    #: Milliseconds since the epoch.
    updated_at: int


@dataclass(frozen=True, slots=True)
class StoredOverlay:
    base_commit_sha: str
    revision: int
    entries: tuple[StoredOverlayEntry, ...] = ()


class DraftOverlayStorage(Protocol):
    """Where a draft lives between sessions: only what the draft changed, and blob text cached
    by blob id, which never changes meaning."""

    async def load_overlay(self, key: str) -> StoredOverlay | None: ...

    async def save_overlay(self, key: str, overlay: StoredOverlay) -> None: ...

    async def load_blob_text(self, sha: str) -> tuple[bool, str | None]:
        """``(True, text)`` for a cached blob, ``text`` ``None`` when it is known binary;
        ``(False, None)`` when it is not cached."""
        ...

    async def save_blob_text(self, sha: str, text: str | None) -> None: ...


class MemoryDraftOverlayStorage:
    """Process memory only: the draft lasts as long as this object."""

    def __init__(self) -> None:
        self._overlays: dict[str, StoredOverlay] = {}
        self._blobs: dict[str, str | None] = {}

    async def load_overlay(self, key: str) -> StoredOverlay | None:
        return self._overlays.get(key)

    async def save_overlay(self, key: str, overlay: StoredOverlay) -> None:
        self._overlays[key] = overlay

    async def load_blob_text(self, sha: str) -> tuple[bool, str | None]:
        return (True, self._blobs[sha]) if sha in self._blobs else (False, None)

    async def save_blob_text(self, sha: str, text: str | None) -> None:
        self._blobs[sha] = text
