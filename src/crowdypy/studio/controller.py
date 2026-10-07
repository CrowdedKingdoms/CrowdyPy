"""Crowdy Studio, headless: CrowdyJS's ``CrowdyStudioController`` on asyncio.

The controller owns one open project and everything around it: the editor's open files and
edits, autosave with optimistic revisions (and conflict resolution), the GitHub card, agent
patches and checkpoints, and the run pipeline. A SERVER target builds and deploys as a
ck-exec mod; a CLIENT target builds as that mod's CLIENT half and runs in a host you provide
(``broker_factory``; the browser Studio runs it in a Web Worker). State is an immutable
:class:`CrowdyStudioState` snapshot replaced on every change; ``subscribe`` to follow it.

Drive it from a running event loop: autosave, retries and the polled surfaces are timers
on that loop. The checks CrowdyJS makes, and the messages, are CrowdyJS's.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import msgspec

from crowdypy.domains.crowdy_studio import (
    CrowdyStudioOfflineError,
    CrowdyStudioPairingPreference,
    CrowdyStudioProject,
    CrowdyStudioProjectFile,
    CrowdyStudioProjectSummary,
    CrowdyStudioReferenceFile,
    CrowdyStudioRevisionConflictError,
    CrowdyStudioTarget,
    normalize_crowdy_studio_path,
)
from crowdypy.errors import CrowdyError, CrowdyGraphQLError
from crowdypy.studio.diagnostics import CrowdyStudioDiagnostic, parse_rustc_diagnostics
from crowdypy.studio.github_new_repo import github_new_repository_url, github_repository_slug
from crowdypy.studio.models import (
    CrowdyStudioAtomicPatchInput,
    CrowdyStudioAtomicPatchResult,
    CrowdyStudioCheckpointMetadata,
    CrowdyStudioFileRef,
    CrowdyStudioProjectProvider,
    CrowdyStudioProjectSynchronization,
    CrowdyStudioSaveState,
    CrowdyStudioSynchronizationProvider,
    crowdy_studio_file_key,
    digest_canonical_json,
    project_targets,
    sha256_digest,
)
from crowdypy.studio.starter_projects import create_crowdy_studio_starter_project

__all__ = [
    "CrowdyStudioAgentContext",
    "CrowdyStudioAgentWorkContext",
    "CrowdyStudioBroker",
    "CrowdyStudioBrokerOptions",
    "CrowdyStudioController",
    "CrowdyStudioDeployResult",
    "CrowdyStudioDeploymentPlan",
    "CrowdyStudioError",
    "CrowdyStudioInvokeResult",
    "CrowdyStudioLogLine",
    "CrowdyStudioRuntimeStatus",
    "CrowdyStudioRuntimeSync",
    "CrowdyStudioState",
    "CrowdyStudioStopResult",
    "project_content_hash",
]

CrowdyStudioPhase = Literal[
    "IDLE",
    "TESTING_DRAFT",
    "DEPLOYING_LIVE",
    "COMPILING",
    "ENABLING",
    "RUNNING",
    "COMPILE_FAILED",
    "STOPPING",
    "STOPPED",
    "PARTIAL_FAILURE",
    "ERROR",
]
CrowdyStudioPolledSurface = Literal["logs", "usage"]
CrowdyStudioRuntimeSyncState = Literal["NEVER_RUN", "RUNNING_SAVED", "RUNNING_STALE", "STOPPED"]
CrowdyStudioLogLevel = Literal["error", "warn", "info", "debug"]
CrowdyStudioAgentActivity = Literal["IDLE", "PREPARING", "WORKING", "PAUSED"]
Deployment = Literal["DRAFT", "LIVE"]

_MOD_NAME = re.compile(r"[a-z0-9_-]{1,48}")
_CRATE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_LEGACY_CLIENT = re.compile(r"^\s*crowdy-compute-sdk\s*=", re.MULTILINE)
_LOG_LEVELS: tuple[CrowdyStudioLogLevel, ...] = ("error", "warn", "info", "debug")
_PREVIEW_LOG_LINES_KEPT = 100
_LEGACY_CLIENT_CRATE = (
    "This CLIENT crate depends on crowdy-compute-sdk, legacy player compute. On ck-exec a CLIENT "
    "target is a mod\u2019s CLIENT half, one crowdy-client-sdk crate: in Cargo.toml replace the "
    'crowdy-compute-sdk line with crowdy-client-sdk = "0.1.0", and in src/ use crowdy_client_sdk '
    "in place of crowdy_compute_sdk. Its host calls are the same less the Game Model, sessions and "
    "grid state (grid_state_get / grid_state_set): the mod\u2019s server half, a hub keyed by the "
    "grid, holds grid state now."
)


class CrowdyStudioError(CrowdyError):
    """A Studio action refused: nothing open, unsaved edits in the way, a missing target or
    permission, or a provider the action needs."""

    code = "CROWDY_STUDIO"


class _Cancelled(CrowdyStudioError):
    """A newer operation (or ``destroy``) superseded this one."""

    def __init__(self) -> None:
        super().__init__("The operation was superseded by a newer one or the Studio closed")


@dataclass(frozen=True, slots=True)
class CrowdyStudioRuntimeStatus:
    phase: CrowdyStudioPhase
    target: CrowdyStudioTarget | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class CrowdyStudioRuntimeSync:
    """Whether what runs is what is saved."""

    state: CrowdyStudioRuntimeSyncState
    saved_revision_id: str | None = None
    running_revision_id: str | None = None
    deployment: Deployment | None = None
    started_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        """CrowdyJS's object form (the keys it sets), as approvals digest it."""
        pairs = (
            ("state", self.state),
            ("savedRevisionId", self.saved_revision_id),
            ("runningRevisionId", self.running_revision_id),
            ("deployment", self.deployment),
            ("startedAt", self.started_at),
        )
        return {key: value for key, value in pairs if value is not None}


@dataclass(frozen=True, slots=True)
class CrowdyStudioDeployResult:
    deployment: Deployment
    status: Literal["RUNNING", "COMPILE_FAILED", "FAILED"]
    project_revision_id: str
    targets: tuple[CrowdyStudioTarget, ...]
    message: str


@dataclass(frozen=True, slots=True)
class CrowdyStudioDeploymentPlan:
    """An approved deployment: it runs only while the project still matches it. A live
    deployment binds the pairing and the project's content hash too."""

    expected_revision_id: str
    targets: Sequence[CrowdyStudioTarget]
    pairing_preference: CrowdyStudioPairingPreference | None = None
    project_content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class CrowdyStudioAgentWorkContext:
    runtime_sync: CrowdyStudioRuntimeSync
    project_id: str | None = None
    project_revision_id: str | None = None
    save_state: Literal["SAVED"] = "SAVED"


@dataclass(frozen=True, slots=True)
class CrowdyStudioAgentContext:
    app_ref: str
    grid_ref: str
    context_version: str
    project_ref: str | None = None
    project_content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class CrowdyStudioLogLine:
    id: str
    source: Literal["mod", "preview"]
    module_name: str
    level: CrowdyStudioLogLevel
    at: str
    text: str


@dataclass(frozen=True, slots=True)
class CrowdyStudioInvokeResult:
    result_json: str
    duration_us: int


@dataclass(frozen=True, slots=True)
class CrowdyStudioStopResult:
    server_stopped: bool | None
    client_stopped: bool | None
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CrowdyStudioState:
    projects: tuple[CrowdyStudioProjectSummary, ...] = ()
    project: CrowdyStudioProject | None = None
    personal_library_files: tuple[CrowdyStudioReferenceFile, ...] = ()
    common_files: tuple[CrowdyStudioReferenceFile, ...] = ()
    open_files: tuple[CrowdyStudioFileRef, ...] = ()
    active_file: CrowdyStudioFileRef | None = None
    save_state: CrowdyStudioSaveState = "SAVED"
    save_message: str | None = None
    github: Any = None
    github_message: str | None = None
    github_busy: bool = False
    github_pending_repo: str | None = None
    runtime: CrowdyStudioRuntimeStatus = CrowdyStudioRuntimeStatus("IDLE")
    runtime_sync: CrowdyStudioRuntimeSync = CrowdyStudioRuntimeSync("NEVER_RUN")
    agent_activity: CrowdyStudioAgentActivity = "IDLE"
    checkpoints: tuple[CrowdyStudioCheckpointMetadata, ...] = ()
    build_output: str = ""
    authoritative_diagnostics: tuple[CrowdyStudioDiagnostic, ...] = ()
    local_diagnostics: tuple[CrowdyStudioDiagnostic, ...] = ()
    logs: tuple[CrowdyStudioLogLine, ...] = ()
    wallet: Any = None
    invoke_result: CrowdyStudioInvokeResult | None = None


@dataclass(frozen=True, slots=True)
class CrowdyStudioBrokerOptions:
    """What a CLIENT host needs to run the half just attached."""

    engine: str
    module_name: str
    artifact_hash: str
    fuel_per_dispatch: int
    consented_host_calls: tuple[str, ...]
    tick_interval_ms: int
    on_log: Callable[[str, str], None]
    grid: Any = None
    on_host_call: Any = None
    on_presentation: Any = None


class CrowdyStudioBroker(Protocol):
    """Runs a CLIENT half: ``start`` with its WebAssembly bytes, ``stop`` to end it."""

    async def start(self, artifact: bytes) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _Compiled:
    target: CrowdyStudioTarget
    name: str
    version_id: str


class CrowdyStudioController:
    """One Studio session on an app and grid. ``project_provider`` is
    ``client.crowdy_studio`` and ``mods`` is ``client.exec`` (see :meth:`for_client`)."""

    def __init__(
        self,
        *,
        project_provider: CrowdyStudioProjectProvider,
        mods: Any,
        app_id: str,
        grid_id: str,
        player_wallet: Any = None,
        github: Any = None,
        initial_project_id: str | None = None,
        grid: Any = None,
        on_host_call: Any = None,
        on_presentation: Any = None,
        target_permissions: Mapping[str, Mapping[str, bool]] | None = None,
        client_tick_interval_ms: int | None = None,
        autosave_ms: float = 700,
        retry_ms: float = 3_000,
        compile_poll_ms: float = 1_500,
        compile_poll_limit: int = 60,
        monitor_poll_ms: float = 5_000,
        synchronization_provider: CrowdyStudioSynchronizationProvider | None = None,
        on_project_synchronized: (
            Callable[[CrowdyStudioProject, CrowdyStudioProjectSynchronization], Any] | None
        ) = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        broker_factory: Callable[[CrowdyStudioBrokerOptions], CrowdyStudioBroker] | None = None,
        is_online: Callable[[], bool] | None = None,
        on_state_change: Callable[[CrowdyStudioState], Any] | None = None,
    ) -> None:
        if mods is None:
            raise CrowdyStudioError(
                "Crowdy Studio needs mods (client.exec): the SERVER target runs as a ck-exec mod"
            )
        self._provider = project_provider
        self._mods = mods
        self._app_id = str(app_id)
        self._grid_id = str(grid_id)
        self._wallet = player_wallet
        self._github = github
        self._initial_project_id = initial_project_id
        self._grid = grid
        self._on_host_call = on_host_call
        self._on_presentation = on_presentation
        self._permissions = dict(target_permissions or {})
        self._client_tick_interval_ms = client_tick_interval_ms
        self._autosave_ms = autosave_ms
        self._retry_ms = retry_ms
        self._compile_poll_ms = compile_poll_ms
        self._compile_poll_limit = compile_poll_limit
        self._monitor_poll_ms = monitor_poll_ms
        self._sync = synchronization_provider
        self._on_synchronized = on_project_synchronized
        self._sleep_fn = sleep
        self._broker_factory = broker_factory
        self._is_online = is_online

        self._state = CrowdyStudioState()
        self._listeners: dict[Callable[[CrowdyStudioState], Any], None] = {}
        self._human_edit_listeners: dict[Callable[[], Any], None] = {}
        self._autosave_timer: asyncio.TimerHandle | None = None
        self._retry_timer: asyncio.TimerHandle | None = None
        self._save_task: asyncio.Future[bool] | None = None
        self._edit_generation = 0
        self._persisted_generation = 0
        self._conflict_remote: CrowdyStudioProject | None = None
        self._broker: CrowdyStudioBroker | None = None
        self._operation_generation = 0
        self._agent_operation_generation = 0
        self._github_status_generation = 0
        self._visible_surfaces: dict[CrowdyStudioPolledSurface, None] = {}
        self._surface_timers: dict[CrowdyStudioPolledSurface, asyncio.TimerHandle] = {}
        self._page_visible = True
        self._destroyed = False
        self._mod_connection: tuple[str, asyncio.Future[Any]] | None = None
        self._mod_log_lines: tuple[CrowdyStudioLogLine, ...] = ()
        self._preview_log_lines: list[CrowdyStudioLogLine] = []
        self._preview_log_seq = 0
        self._tasks: set[asyncio.Future[Any]] = set()
        if on_state_change is not None:
            self._listeners[on_state_change] = None

    @classmethod
    def for_client(
        cls, client: Any, *, app_id: str | int, grid_id: str | int, **options: Any
    ) -> CrowdyStudioController:
        """A controller over an :class:`~crowdypy.AsyncCrowdyClient`'s Studio, exec, GitHub
        and player-wallet APIs."""
        return cls(
            project_provider=client.crowdy_studio,
            mods=client.exec,
            github=options.pop("github", client.crowdy_studio_github),
            player_wallet=options.pop("player_wallet", client.player_wallet),
            app_id=str(app_id),
            grid_id=str(grid_id),
            **options,
        )

    # ------------------------------------------------------------------ state

    def get_state(self) -> CrowdyStudioState:
        return self._state

    def subscribe(self, listener: Callable[[CrowdyStudioState], Any]) -> Callable[[], None]:
        """Call ``listener`` now and on every change; returns the unsubscribe."""
        self._listeners[listener] = None
        listener(self._state)

        def unsubscribe() -> None:
            self._listeners.pop(listener, None)

        return unsubscribe

    def on_human_edit(self, listener: Callable[[], Any]) -> Callable[[], None]:
        """Call ``listener`` before every human edit (an agent pauses on one)."""
        self._human_edit_listeners[listener] = None

        def unsubscribe() -> None:
            self._human_edit_listeners.pop(listener, None)

        return unsubscribe

    # ------------------------------------------------------------------ agent work

    async def prepare_for_agent_work(self) -> CrowdyStudioAgentWorkContext:
        """Save, then mark the agent working; refuses while a save is unresolved."""
        self._ensure_alive()
        self._update(agent_activity="PREPARING")
        saved = await self.save_now()
        if not saved or self._state.save_state != "SAVED":
            self._update(agent_activity="PAUSED")
            raise CrowdyStudioError("Resolve the project save before starting agent work")
        project = self._state.project
        self._update(agent_activity="WORKING")
        return CrowdyStudioAgentWorkContext(
            runtime_sync=self._state.runtime_sync,
            project_id=project.project_id if project else None,
            project_revision_id=project.revision.id if project else None,
        )

    def finish_agent_work(self, paused: bool = False) -> None:
        self._update(agent_activity="PAUSED" if paused else "IDLE")

    def begin_agent_operation(self) -> int:
        """A token for agent-started runs; :meth:`cancel_agent_operation` voids it."""
        self._agent_operation_generation += 1
        return self._agent_operation_generation

    def cancel_agent_operation(self, message: str = "Agent operation cancelled") -> None:
        self._agent_operation_generation += 1
        self._operation_generation += 1
        self._update(
            agent_activity="PAUSED", runtime=CrowdyStudioRuntimeStatus("IDLE", message=message)
        )

    def can_target(self, target: CrowdyStudioTarget, action: Literal["write", "run"]) -> bool:
        """The host's view of the grid's permissions; the server's authorization is final."""
        permission = self._permissions.get(target)
        if permission is None:
            return True
        return bool(permission.get("canWrite" if action == "write" else "canRun", True))

    def get_agent_context(self) -> CrowdyStudioAgentContext:
        project = self._state.project
        content_hash = project_content_hash(project) if project else None
        payload: dict[str, Any] = {
            "contract": "crowdy.studio-context/1",
            "appRef": self._app_id,
            "gridRef": self._grid_id,
            "saveState": self._state.save_state,
            "runtimeSync": self._state.runtime_sync.to_json(),
        }
        if project is not None:
            payload |= {
                "projectRef": project.project_id,
                "projectRevisionId": project.revision.id,
                "projectContentHash": content_hash,
            }
        return CrowdyStudioAgentContext(
            app_ref=self._app_id,
            grid_ref=self._grid_id,
            context_version=digest_canonical_json(payload),
            project_ref=project.project_id if project else None,
            project_content_hash=content_hash,
        )

    # ------------------------------------------------------------------ projects

    async def initialize(self) -> None:
        """Load the project list and reference files, and open the initial project."""
        self._ensure_alive()
        try:
            projects, library, common = await asyncio.gather(
                self._provider.list_projects(self._app_id),
                self._provider.list_personal_library_files(self._app_id),
                self._provider.list_common_files(self._app_id),
            )
        except Exception as error:
            if isinstance(error, CrowdyStudioOfflineError) or self._offline():
                self._update(save_state="OFFLINE", save_message=_message(error))
                return
            raise
        self._update(
            projects=tuple(projects),
            personal_library_files=tuple(library),
            common_files=tuple(common),
            save_state="SAVED",
            save_message=None,
        )
        first = next(
            (p for p in projects if p.project_id == self._initial_project_id),
            projects[0] if projects else None,
        )
        if first is not None:
            await self._load_project(first.project_id)

    async def create_project(
        self,
        *,
        name: str,
        kind: Literal["SERVER", "CLIENT", "FULL_STACK"],
        description: str | None = None,
    ) -> CrowdyStudioProject:
        """Create and open a project from the starters (saving the open one first)."""
        self._ensure_alive()
        if self._state.project is not None and not await self.save_now():
            raise CrowdyStudioError(
                "Resolve or retry the current project save before creating another"
            )
        mod_starter = (
            await self._mods.mod_starter(self._app_id)
            if "SERVER" in project_targets(kind)
            else None
        )
        starter = create_crowdy_studio_starter_project(
            app_id=self._app_id,
            grid_id=self._grid_id,
            name=name,
            kind=kind,
            description=description,
            mod_starter=mod_starter,
        )
        project = await self._provider.create_project(
            starter.app_id, starter.grid_id, starter.kind, starter.metadata, starter.files
        )
        self._install_project(project)
        self._update(
            projects=_upsert_summary(self._state.projects, _summary_of(project)),
            save_state="SAVED",
            save_message=None,
        )
        return project

    async def switch_project(self, project_id: str) -> None:
        if self._state.project is not None and self._state.project.project_id == project_id:
            return
        if self._state.project is not None and not await self.save_now():
            raise CrowdyStudioError("Resolve or retry the current project save before switching")
        await self._load_project(project_id)

    async def reload_project(self) -> None:
        """Read the open project again (after a GitHub bind, unbind or refresh)."""
        self._ensure_alive()
        current = self._state.project
        if current is None:
            return
        if self._state.save_state != "SAVED":
            raise CrowdyStudioError("Unsaved local edits; the reload waits for the next save")
        project = await self._provider.get_project(self._app_id, self._grid_id, current.project_id)
        open_files, active = self._state.open_files, self._state.active_file
        self._install_project(project)

        def still_there(ref: CrowdyStudioFileRef) -> bool:
            return ref.source != "PROJECT" or any(
                f.target == ref.target and f.path == ref.path for f in project.files
            )

        kept = tuple(ref for ref in open_files if still_there(ref))
        if kept:
            self._update(
                open_files=kept,
                active_file=active if active is not None and still_there(active) else kept[0],
            )

    async def _load_project(self, project_id: str) -> None:
        project = await self._provider.get_project(self._app_id, self._grid_id, project_id)
        self._install_project(project)
        if self._sync is not None:
            await self.refresh_checkpoints()

    def _install_project(self, project: CrowdyStudioProject) -> None:
        self._operation_generation += 1
        self._stop_surface_polling()
        self._stop_broker()
        self._close_mod_connection()
        self._clear_save_timers()
        self._edit_generation = 0
        self._persisted_generation = 0
        self._conflict_remote = None
        files = list(project.files)
        preferred = next((f for f in files if f.path == "src/lib.rs"), files[0] if files else None)
        active = _project_file_ref(preferred) if preferred is not None else None
        self._update(
            project=msgspec.structs.replace(project, files=files),
            open_files=(active,) if active else (),
            active_file=active,
            save_state="SAVED",
            save_message=None,
            runtime=CrowdyStudioRuntimeStatus("IDLE"),
            runtime_sync=CrowdyStudioRuntimeSync(
                "NEVER_RUN", saved_revision_id=project.revision.id
            ),
            agent_activity="IDLE",
            checkpoints=(),
            build_output="",
            authoritative_diagnostics=(),
            local_diagnostics=(),
            logs=self._clear_log_lines(),
            invoke_result=None,
            github=None,
            github_message=None,
            github_busy=False,
            github_pending_repo=None,
        )
        self._restart_visible_surface_polling()
        self._spawn(self.refresh_github_status())

    # ------------------------------------------------------------------ files

    def open_file(self, ref: CrowdyStudioFileRef) -> None:
        self._require_file(ref)
        open_files = self._state.open_files
        self._update(
            open_files=open_files if ref in open_files else (*open_files, ref), active_file=ref
        )

    def close_file(self, ref: CrowdyStudioFileRef) -> None:
        open_files = tuple(entry for entry in self._state.open_files if entry != ref)
        active = self._state.active_file
        if active is not None and active == ref:
            active = open_files[-1] if open_files else None
        self._update(open_files=open_files, active_file=active)

    def file_content(self, ref: CrowdyStudioFileRef) -> str:
        return self._require_file(ref).content

    def add_file(self, target: CrowdyStudioTarget, path: str, content: str = "") -> None:
        project = self._require_project()
        self._assert_project_target(project, target)
        normalized = normalize_crowdy_studio_path(path)
        if any(f.target == target and f.path == normalized for f in project.files):
            raise CrowdyStudioError(f"{target}:{normalized} already exists")
        files = sorted(
            [
                *project.files,
                CrowdyStudioProjectFile(target=target, path=normalized, content=content),
            ],
            key=_file_order,
        )
        self._set_project(msgspec.structs.replace(project, files=files))
        self._mark_edited()
        self.open_file(CrowdyStudioFileRef("PROJECT", normalized, target))

    def rename_file(self, target: CrowdyStudioTarget, path: str, next_path: str) -> None:
        project = self._require_project()
        normalized = normalize_crowdy_studio_path(path)
        renamed = normalize_crowdy_studio_path(next_path)
        if not any(f.target == target and f.path == normalized for f in project.files):
            raise CrowdyStudioError(f"{target}:{normalized} does not exist")
        if any(f.target == target and f.path == renamed for f in project.files):
            raise CrowdyStudioError(f"{target}:{renamed} already exists")
        files = sorted(
            (
                msgspec.structs.replace(f, path=renamed)
                if f.target == target and f.path == normalized
                else f
                for f in project.files
            ),
            key=_file_order,
        )
        self._set_project(msgspec.structs.replace(project, files=files))

        def swap(ref: CrowdyStudioFileRef) -> CrowdyStudioFileRef:
            if ref.source == "PROJECT" and ref.target == target and ref.path == normalized:
                return replace(ref, path=renamed)
            return ref

        active = self._state.active_file
        self._update(
            open_files=tuple(swap(ref) for ref in self._state.open_files),
            active_file=swap(active) if active is not None else None,
        )
        self._mark_edited()

    def delete_file(self, target: CrowdyStudioTarget, path: str) -> None:
        project = self._require_project()
        normalized = normalize_crowdy_studio_path(path)
        if not any(f.target == target and f.path == normalized for f in project.files):
            raise CrowdyStudioError(f"{target}:{normalized} does not exist")
        files = [f for f in project.files if not (f.target == target and f.path == normalized)]
        self._set_project(msgspec.structs.replace(project, files=files))
        self.close_file(CrowdyStudioFileRef("PROJECT", normalized, target))
        self._mark_edited()

    async def import_reference_file(
        self, reference: CrowdyStudioReferenceFile, destination_path: str | None = None
    ) -> None:
        """Copy a library or common file into the project (saving first) and open it."""
        destination = reference.path if destination_path is None else destination_path
        self._require_project()
        if not await self.save_now():
            raise CrowdyStudioError("Resolve the current project save before importing a file")
        project = self._require_project()
        saved = await self._provider.import_reference_file(
            self._app_id,
            self._grid_id,
            project.project_id,
            project.revision.id,
            reference.source,
            reference.id,
            destination,
        )
        self._install_project(saved)
        self._update(projects=_upsert_summary(self._state.projects, _summary_of(saved)))
        wanted = normalize_crowdy_studio_path(destination)
        imported = next(
            (f for f in saved.files if f.target == reference.target and f.path == wanted), None
        )
        if imported is not None:
            self.open_file(_project_file_ref(imported))

    async def save_project_file_to_library(
        self, target: CrowdyStudioTarget, path: str, title: str | None = None
    ) -> CrowdyStudioReferenceFile:
        wanted = normalize_crowdy_studio_path(path)
        file = next(
            (f for f in self._require_project().files if f.target == target and f.path == wanted),
            None,
        )
        if file is None:
            raise CrowdyStudioError(f"{target}:{path} does not exist")
        saved = await self._provider.save_personal_library_file(
            self._app_id,
            (title or "").strip() or file.path.split("/")[-1] or file.path,
            target,
            file.path,
            file.content,
        )
        self._update(
            personal_library_files=_upsert_reference(self._state.personal_library_files, saved)
        )
        return saved

    def update_file(self, target: CrowdyStudioTarget, path: str, content: str) -> None:
        project = self._require_project()
        normalized = normalize_crowdy_studio_path(path)
        file = next((f for f in project.files if f.target == target and f.path == normalized), None)
        if file is None:
            raise CrowdyStudioError(f"{target}:{normalized} does not exist")
        if file.content == content:
            return
        files = [
            msgspec.structs.replace(f, content=content) if f is file else f for f in project.files
        ]
        self._set_project(msgspec.structs.replace(project, files=files))
        self._mark_edited()

    def update_settings(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        server_module_name: str | None = None,
        client_module_name: str | None = None,
    ) -> None:
        """Change the project's name or module names (the pairing stays)."""
        project = self._require_project()
        patch = {
            key: value
            for key, value in (
                ("name", name),
                ("description", description),
                ("server_module_name", server_module_name),
                ("client_module_name", client_module_name),
            )
            if value is not None
        }
        metadata = msgspec.structs.replace(project.metadata, **patch)
        self._set_project(msgspec.structs.replace(project, metadata=metadata))
        self._mark_edited()

    def set_local_diagnostics(self, diagnostics: Sequence[CrowdyStudioDiagnostic]) -> None:
        self._update(local_diagnostics=tuple(diagnostics))

    # ------------------------------------------------------------------ saving

    async def save_now(self) -> bool:
        """Save every edit so far. ``False`` on a conflict or while offline (a retry is
        scheduled); raises on any other failure."""
        self._ensure_alive()
        self._clear_timer("autosave")
        if self._state.project is None:
            return True
        if self._save_task is not None:
            await asyncio.shield(self._save_task)
            if self._persisted_generation == self._edit_generation:
                return True
        task: asyncio.Future[bool] = asyncio.ensure_future(self._perform_save_loop())
        self._save_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._save_task is task:
                self._save_task = None

    async def retry_save(self) -> bool:
        self._clear_timer("retry")
        if self._state.project is None:
            self._update(save_state="SAVING", save_message=None)
            await self.initialize()
            return self._state.save_state != "OFFLINE"
        if self._state.save_state == "CONFLICT":
            return False
        self._update(save_state="SAVING", save_message=None)
        return await self.save_now()

    async def accept_remote_conflict(self) -> None:
        """Drop local edits for the project as the other session saved it."""
        if self._state.save_state != "CONFLICT":
            return
        remote = self._conflict_remote or await self._provider.get_project(
            self._app_id, self._grid_id, self._require_project().project_id
        )
        self._install_project(remote)

    async def overwrite_conflict(self) -> bool:
        """Keep local edits: save them over the other session's revision."""
        if self._state.save_state != "CONFLICT":
            return await self.save_now()
        project = self._require_project()
        remote = self._conflict_remote or await self._provider.get_project(
            self._app_id, self._grid_id, project.project_id
        )
        self._set_project(msgspec.structs.replace(project, revision=remote.revision))
        self._conflict_remote = None
        self._update(save_state="SAVING", save_message=None)
        return await self.save_now()

    async def _perform_save_loop(self) -> bool:
        while self._persisted_generation != self._edit_generation:
            snapshot = self._require_project()
            saving_generation = self._edit_generation
            self._update(save_state="SAVING", save_message=None)
            try:
                saved = await self._provider.save_project(
                    self._app_id,
                    self._grid_id,
                    snapshot.project_id,
                    snapshot.revision.id,
                    snapshot.metadata,
                    snapshot.files,
                )
            except CrowdyStudioRevisionConflictError as error:
                self._conflict_remote = error.remote_project
                self._update(save_state="CONFLICT", save_message=error.message)
                return False
            except Exception as error:
                self._update(save_state="OFFLINE", save_message=_message(error))
                if isinstance(error, CrowdyStudioOfflineError) or self._offline():
                    self._schedule_retry()
                    return False
                raise
            self._persisted_generation = saving_generation
            current = self._state.project
            if current is None or current.project_id != saved.project_id:
                return True
            if self._edit_generation == saving_generation:
                project = msgspec.structs.replace(saved, files=list(saved.files))
            else:
                project = msgspec.structs.replace(
                    current, revision=saved.revision, updated_at=saved.updated_at
                )
            self._update(
                project=project,
                projects=_upsert_summary(self._state.projects, _summary_of(saved)),
                save_state="SAVED"
                if self._persisted_generation == self._edit_generation
                else "SAVING",
                save_message=None,
                runtime_sync=_resync(self._state.runtime_sync, saved.revision.id),
            )
        self._update(save_state="SAVED", save_message=None)
        return True

    def _mark_edited(self) -> None:
        for listener in list(self._human_edit_listeners):
            listener()
        self._edit_generation += 1
        sync = self._state.runtime_sync
        self._update(
            save_state="SAVING",
            save_message=None,
            agent_activity="PAUSED"
            if self._state.agent_activity == "WORKING"
            else self._state.agent_activity,
            runtime_sync=replace(sync, state="RUNNING_STALE")
            if sync.state == "RUNNING_SAVED"
            else sync,
        )
        self._clear_timer("autosave")
        self._autosave_timer = self._later(self._autosave_ms, self._autosave)

    def _autosave(self) -> None:
        self._autosave_timer = None
        self._spawn(self.save_now())

    def _schedule_retry(self) -> None:
        self._clear_timer("retry")
        if self._destroyed:
            return

        def fire() -> None:
            self._retry_timer = None
            self._spawn(self.retry_save())

        self._retry_timer = self._later(self._retry_ms, fire)

    # ------------------------------------------------------------------ GitHub

    async def refresh_github_status(self) -> None:
        if self._github is None:
            return
        self._github_status_generation += 1
        generation = self._github_status_generation
        project = self._state.project
        try:
            github = await self._github.status(
                app_id=self._app_id, project_id=project.project_id if project else None
            )
        except Exception as error:
            if generation == self._github_status_generation:
                self._update(github_message=_message(error))
            return
        if generation == self._github_status_generation:
            self._update(github=github, github_message=None)

    async def connect_github(self) -> str:
        """The GitHub App installation URL; open it, then refresh the status."""
        if self._github is None:
            raise CrowdyStudioError("GitHub is not available in this Studio.")
        start = await self._github.connect_url()
        self._update(github_message="Finish installing on GitHub at that URL, then Refresh.")
        return str(start.connect_url)

    def create_github_repository(self) -> str:
        """GitHub's new-repository page for this project, prefilled; create it there, then
        :meth:`bind_github_repo`."""
        if self._github is None:
            raise CrowdyStudioError("GitHub is not available in this Studio.")
        project = self._require_project()
        github = self._state.github
        if github is None or not github.connected:
            raise CrowdyStudioError(
                "Connect GitHub first; the repository is created under your GitHub account."
            )
        owner = github.account_login or None
        name = github_repository_slug(project.metadata.name)
        url = github_new_repository_url(
            owner=owner,
            name=name,
            description=project.metadata.description
            or f"Crowdy Studio mod: {project.metadata.name}",
        )
        slug = f"{owner}/{name}" if owner else name
        self._update(
            github_pending_repo=slug,
            github_message=(
                f"Create {slug} on GitHub, add it to your Crowdy Studio installation, then bind it (Push this project)."
                if github.repository_selection == "selected"
                else f"Create {slug} on GitHub, then bind it (Push this project)."
            ),
        )
        return url

    async def bind_github_repo(
        self, slug: str, initial: Literal["PUSH_PROJECT", "TAKE_REPOSITORY"] = "PUSH_PROJECT"
    ) -> None:
        """Bind ``owner/repo`` or ``owner/repo@branch``: push this project to it, or take
        its files as the project. Saves then commit to the branch."""
        if self._github is None:
            raise CrowdyStudioError("GitHub is not available in this Studio.")
        scope = self._github_scope()
        if scope is None:
            raise CrowdyStudioError("Open a Studio project before binding a repository.")
        if self._edit_generation != self._persisted_generation:
            raise CrowdyStudioError("Save Studio edits before binding a repository.")
        trimmed = slug.strip()
        at = trimmed.rfind("@")
        repo_path = trimmed[:at] if at > 0 else trimmed
        branch = trimmed[at + 1 :].strip() if at > 0 else ""
        slash = repo_path.find("/")
        if slash <= 0 or slash == len(repo_path) - 1:
            raise CrowdyStudioError("Use owner/repo or owner/repo@branch")
        self._update(github_busy=True)
        try:
            bind: dict[str, Any] = {
                **scope,
                "owner": repo_path[:slash],
                "repo": repo_path[slash + 1 :],
                "initial": initial,
            }
            if branch:
                bind["branch"] = branch
            github = await self._github.bind(bind)
            await self.reload_project()
            where = f"{github.owner}/{github.repo}@{github.branch}"
            self._update(
                github=github,
                github_pending_repo=None,
                github_message=(
                    f"Pushed the project to {where}. It is the working tree now; edits commit as you save."
                    if initial == "PUSH_PROJECT"
                    else f"Took {where} as the project. Edits commit as you save."
                ),
            )
        finally:
            self._update(github_busy=False)

    async def unbind_github(self) -> None:
        if self._github is None:
            return
        scope = self._github_scope()
        if scope is None:
            return
        if self._edit_generation != self._persisted_generation:
            self._update(github_message="Save edits before unbinding.")
            return
        self._update(github_busy=True)
        try:
            github = await self._github.unbind(scope)
            await self.reload_project()
            self._update(
                github=github, github_message="Repository unbound. The files stay in Studio."
            )
        finally:
            self._update(github_busy=False)

    async def refresh_from_github(self) -> bool:
        """Bring the bound project forward to its branch head."""
        if self._github is None:
            return False
        scope = self._github_scope()
        project = self._state.project
        if scope is None or project is None or project.github is None:
            self._update(github_message="Bind a repository first.")
            return False
        if self._edit_generation != self._persisted_generation:
            self._update(github_message="Save Studio edits before refreshing from GitHub.")
            return False
        self._update(github_busy=True)
        try:
            before = project.github.sha
            github = await self._github.refresh(scope)
            await self.reload_project()
            self._update(
                github=github,
                github_message="Already at the branch head."
                if github.github_sha == before
                else f"Refreshed to {github.github_sha[:7] if github.github_sha else 'head'}.",
            )
            return True
        except Exception as error:
            self._update(github_message=f"GitHub refresh failed: {_message(error)}")
            return False
        finally:
            self._update(github_busy=False)

    def _github_scope(self) -> dict[str, str] | None:
        project = self._state.project
        return {"appId": self._app_id, "projectId": project.project_id} if project else None

    # ------------------------------------------------------------------ agent patches

    async def refresh_checkpoints(self) -> tuple[CrowdyStudioCheckpointMetadata, ...]:
        project = self._require_project()
        if self._sync is not None:
            checkpoints = tuple(
                await self._sync.list_checkpoints(
                    app_id=self._app_id, grid_id=self._grid_id, project_id=project.project_id
                )
            )
        else:
            checkpoints = self._state.checkpoints
        self._update(checkpoints=checkpoints)
        return checkpoints

    async def apply_atomic_patch(
        self, input: CrowdyStudioAtomicPatchInput
    ) -> CrowdyStudioAtomicPatchResult:
        """Apply an agent's multi-file patch durably, checkpointing first. Every change is
        validated against the open revision and content hashes before anything is sent."""
        if not await self.save_now():
            raise CrowdyStudioError(
                "Resolve the current project save before applying an agent patch"
            )
        baseline = self._require_project()
        if baseline.revision.id != input.expected_revision_id:
            raise CrowdyStudioRevisionConflictError(
                f"Expected revision {input.expected_revision_id}, found {baseline.revision.id}",
                baseline,
            )
        _apply_validated_patch(baseline, input)
        if self._sync is None:
            raise CrowdyStudioError(
                "Atomic agent patches require a durable synchronization provider"
            )
        result = await self._sync.apply_atomic_patch(
            app_id=self._app_id,
            grid_id=self._grid_id,
            project_id=baseline.project_id,
            expected_revision_id=input.expected_revision_id,
            changes=input.changes,
        )
        if result.project.project_id != baseline.project_id:
            raise CrowdyStudioError("Atomic patch returned a different project")
        if (
            result.project.revision.id == baseline.revision.id
            or result.checkpoint.project_revision_id != baseline.revision.id
        ):
            raise CrowdyStudioError("Atomic patch returned invalid revision/checkpoint metadata")
        for change in input.changes:
            path = normalize_crowdy_studio_path(change.path)
            persisted = next(
                (f for f in result.project.files if f.target == change.target and f.path == path),
                None,
            )
            if persisted is None or persisted.content != change.content:
                raise CrowdyStudioError(
                    f"Atomic patch did not synchronize {change.target}:{change.path}"
                )
        self.synchronize_project(
            result.project,
            CrowdyStudioProjectSynchronization(
                "AGENT",
                expected_previous_revision_id=baseline.revision.id,
                checkpoint=result.checkpoint,
            ),
        )
        return result

    def synchronize_project(
        self, project: CrowdyStudioProject, synchronization: CrowdyStudioProjectSynchronization
    ) -> None:
        """Adopt a revision written elsewhere (an agent, another session). Human edits not
        yet saved win: the incoming revision becomes a conflict instead."""
        current = self._require_project()
        if project.project_id != current.project_id:
            raise CrowdyStudioError(
                "Project synchronization target does not match the open project"
            )
        expected = synchronization.expected_previous_revision_id
        if expected and expected != current.revision.id:
            raise CrowdyStudioRevisionConflictError(
                "Project synchronization started from a stale revision", project
            )
        if self._persisted_generation != self._edit_generation:
            self._conflict_remote = project
            self._update(
                save_state="CONFLICT",
                save_message="Human edits preempted an incoming agent project revision",
                agent_activity="PAUSED",
            )
            raise CrowdyStudioRevisionConflictError(
                "Human edits preempted the agent project synchronization", project
            )
        self._clear_save_timers()
        clone = msgspec.structs.replace(project, files=list(project.files))
        self._edit_generation = 0
        self._persisted_generation = 0
        self._conflict_remote = None
        open_files = tuple(
            ref for ref in self._state.open_files if _file_ref_exists(clone, self._state, ref)
        )
        active = self._state.active_file
        if active is None or active not in open_files:
            active = open_files[-1] if open_files else None
        checkpoint = synchronization.checkpoint
        self._update(
            project=clone,
            projects=_upsert_summary(self._state.projects, _summary_of(clone)),
            open_files=open_files,
            active_file=active,
            save_state="SAVED",
            save_message=None,
            checkpoints=_upsert_checkpoint(self._state.checkpoints, checkpoint)
            if checkpoint is not None
            else self._state.checkpoints,
            runtime_sync=_resync(self._state.runtime_sync, clone.revision.id),
        )
        if self._on_synchronized is not None:
            self._on_synchronized(
                msgspec.structs.replace(clone, files=list(clone.files)), synchronization
            )

    async def restore_checkpoint(
        self, checkpoint_id: str, approval_grant: str, expected_revision_id: str | None = None
    ) -> CrowdyStudioCheckpointMetadata:
        """Restore a checkpoint durably (the provider checkpoints the current state first).
        ``approval_grant`` is the opaque grant for exactly this restore."""
        expected = (
            self._require_project().revision.id
            if expected_revision_id is None
            else expected_revision_id
        )
        if len(approval_grant.strip()) < 8:
            raise CrowdyStudioError("Checkpoint restore requires an opaque exact approval grant")
        if not await self.save_now():
            raise CrowdyStudioError(
                "Resolve the current project save before restoring a checkpoint"
            )
        current = self._require_project()
        if current.revision.id != expected:
            raise CrowdyStudioRevisionConflictError(
                f"Expected revision {expected}, found {current.revision.id}", current
            )
        if self._sync is None:
            raise CrowdyStudioError(
                "Checkpoint restore requires a durable synchronization provider"
            )
        restored = await self._sync.restore_checkpoint(
            app_id=self._app_id,
            grid_id=self._grid_id,
            project_id=current.project_id,
            checkpoint_id=checkpoint_id,
            expected_revision_id=expected,
            approval_grant=approval_grant,
        )
        if (
            restored.project.project_id != current.project_id
            or restored.project.revision.id == current.revision.id
            or restored.pre_restore_checkpoint.project_revision_id != current.revision.id
        ):
            raise CrowdyStudioError("Checkpoint restore returned invalid synchronization metadata")
        self.synchronize_project(
            restored.project,
            CrowdyStudioProjectSynchronization(
                "AGENT",
                expected_previous_revision_id=expected,
                checkpoint=restored.pre_restore_checkpoint,
            ),
        )
        return restored.pre_restore_checkpoint

    # ------------------------------------------------------------------ running

    async def test_draft(self, agent_operation: int | None = None) -> CrowdyStudioDeployResult:
        """Build and run the saved project as a draft."""
        return await self._deploy_project(True, agent_operation)

    async def test_draft_plan(
        self, plan: CrowdyStudioDeploymentPlan, agent_operation: int | None = None
    ) -> CrowdyStudioDeployResult:
        return await self._deploy_project(True, agent_operation, plan)

    async def deploy_live(self, agent_operation: int | None = None) -> CrowdyStudioDeployResult:
        return await self._deploy_project(False, agent_operation)

    async def deploy_live_plan(
        self, plan: CrowdyStudioDeploymentPlan, agent_operation: int | None = None
    ) -> CrowdyStudioDeployResult:
        """Deploy live only as approved: the revision, targets, pairing and content hash
        must all still match ``plan``."""
        return await self._deploy_project(False, agent_operation, plan)

    async def _deploy_project(
        self,
        draft: bool,
        agent_operation: int | None,
        plan: CrowdyStudioDeploymentPlan | None = None,
    ) -> CrowdyStudioDeployResult:
        deployment: Deployment = "DRAFT" if draft else "LIVE"
        self._check_agent_operation(agent_operation)
        self._require_project()
        if not await self.save_now():
            message = "Project must be saved before it can be built"
            self._set_runtime("ERROR", message)
            project = self._require_project()
            return CrowdyStudioDeployResult(
                deployment,
                "FAILED",
                project.revision.id,
                tuple(project_targets(project.kind)),
                message,
            )
        self._check_agent_operation(agent_operation)
        project = self._require_project()
        if plan is not None:
            self._assert_deployment_plan(project, plan, draft)
        targets: tuple[CrowdyStudioTarget, ...] = (
            tuple(plan.targets) if plan is not None else tuple(project_targets(project.kind))
        )
        self._operation_generation += 1
        operation = self._operation_generation
        self._stop_surface_polling()
        self._update(
            runtime=CrowdyStudioRuntimeStatus("TESTING_DRAFT" if draft else "DEPLOYING_LIVE"),
            build_output="",
            authoritative_diagnostics=(),
        )

        def result(
            status: Literal["RUNNING", "COMPILE_FAILED", "FAILED"], message: str
        ) -> CrowdyStudioDeployResult:
            return CrowdyStudioDeployResult(
                deployment, status, project.revision.id, targets, message
            )

        try:
            if len(targets) == 1:
                target = targets[0]
                compiled = await self._compile_target(project, target, operation)
                if compiled is None:
                    return result(
                        "COMPILE_FAILED", self._state.runtime.message or "Compilation failed"
                    )
                if target == "SERVER":
                    await self._enable_server(compiled.name, operation)
                else:
                    await self._run_client(compiled, operation)
            else:
                client = await self._compile_target(project, "CLIENT", operation)
                if client is None:
                    return result(
                        "COMPILE_FAILED", self._state.runtime.message or "Client compilation failed"
                    )
                server = await self._compile_target(project, "SERVER", operation)
                if server is None:
                    return result(
                        "COMPILE_FAILED", self._state.runtime.message or "Server compilation failed"
                    )
                self._check_operation(operation)
                await self._enable_server(server.name, operation)
                await self._run_client(client, operation)
            message = "Draft test is running" if draft else "Project is live"
            self._update(
                runtime=CrowdyStudioRuntimeStatus("RUNNING", message=message),
                runtime_sync=CrowdyStudioRuntimeSync(
                    "RUNNING_SAVED",
                    saved_revision_id=project.revision.id,
                    running_revision_id=project.revision.id,
                    deployment=deployment,
                    started_at=_now_iso(),
                ),
            )
            with contextlib.suppress(Exception):  # the wallet line is advisory
                await self.refresh_surface("usage")
            return result("RUNNING", message)
        except _Cancelled:
            return result("FAILED", "Deployment was cancelled")
        except Exception as error:
            self._set_runtime("ERROR", _message(error))
            return result("FAILED", _message(error))
        finally:
            if operation == self._operation_generation:
                self._restart_visible_surface_polling()

    async def _compile_target(
        self, project: CrowdyStudioProject, target: CrowdyStudioTarget, operation: int
    ) -> _Compiled | None:
        if not self.can_target(target, "write"):
            raise CrowdyStudioError(f"{target} authoring is unavailable on this grid")
        name = _module_name_for(project, target)
        files = [f for f in project.files if f.target == target]
        if not files:
            raise CrowdyStudioError(f"{target} has no project files")
        self._update(runtime=CrowdyStudioRuntimeStatus("COMPILING", target, f"Submitting {name}"))
        if target == "SERVER":
            return await self._build_mod(name, files, operation)
        return await self._build_client_half(name, files, operation)

    async def _build_mod(
        self, name: str, files: Sequence[CrowdyStudioProjectFile], operation: int
    ) -> _Compiled | None:
        problem = _mod_name_problem(self._require_project())
        if problem:
            raise CrowdyStudioError(problem)
        queued = await self._mods.mod_build(
            self._app_id, {"name": _crate_name(name), "files": _crate_files(files)}
        )
        self._check_operation(operation)
        built = await self._await_mod_build("SERVER", name, queued.build_id, operation)
        if built is None:
            return None
        mod = await self._mods.mod_deploy(self._app_id, self._grid_id, name, built.build_id)
        self._check_operation(operation)
        return _Compiled("SERVER", name, str(mod.version))

    async def _build_client_half(
        self, name: str, files: Sequence[CrowdyStudioProjectFile], operation: int
    ) -> _Compiled | None:
        problem = _mod_name_problem(self._require_project())
        if problem:
            raise CrowdyStudioError(problem)
        cargo = next((f.content for f in files if f.path == "Cargo.toml"), "")
        if _LEGACY_CLIENT.search(cargo):
            self._record_build("CLIENT", _LEGACY_CLIENT_CRATE)
            self._update(
                runtime=CrowdyStudioRuntimeStatus(
                    "COMPILE_FAILED",
                    "CLIENT",
                    f"{name} is a legacy player compute crate; a CLIENT half builds on crowdy-client-sdk",
                )
            )
            return None
        queued = await self._mods.mod_client_build(
            self._app_id, {"name": _crate_name(name), "files": _crate_files(files)}
        )
        self._check_operation(operation)
        built = await self._await_mod_build("CLIENT", name, queued.build_id, operation)
        return _Compiled("CLIENT", name, built.build_id) if built is not None else None

    async def _await_mod_build(
        self, target: CrowdyStudioTarget, name: str, build_id: str, operation: int
    ) -> Any:
        for _ in range(self._compile_poll_limit):
            build = await self._mods.mod_build_status(self._app_id, build_id)
            self._check_operation(operation)
            if build.status == "succeeded":
                self._record_build(target, build.log or "")
                return build
            if build.status == "failed":
                self._record_build(target, build.log or "Compilation failed without output")
                self._update(
                    runtime=CrowdyStudioRuntimeStatus(
                        "COMPILE_FAILED", target, f"{name} failed to compile"
                    )
                )
                return None
            await self._sleep(self._compile_poll_ms)
            self._check_operation(operation)
        timeout = f"Compilation timed out after {self._compile_poll_limit} polls"
        self._record_build(target, timeout)
        self._update(runtime=CrowdyStudioRuntimeStatus("COMPILE_FAILED", target, timeout))
        return None

    async def _attach_client_half(self, compiled: _Compiled, operation: int) -> Any:
        project = self._require_project()
        mod_name = _project_mod_name(project)
        if "SERVER" not in project_targets(project.kind):
            await self._ensure_client_only_mod(mod_name, operation)
        self._update(
            runtime=CrowdyStudioRuntimeStatus(
                "ENABLING", "CLIENT", f"Attaching {compiled.name} to {mod_name}"
            )
        )
        attached = await self._mods.mod_client_deploy(
            self._app_id, self._grid_id, mod_name, compiled.version_id
        )
        self._check_operation(operation)
        await self._mods.consent_client_mod(self._app_id, attached.mod_id, attached.capability_hash)
        self._check_operation(operation)
        self._record_note(
            "CLIENT",
            f"Attached to mod '{mod_name}' as CLIENT version {attached.client_version} (capabilities "
            f"{attached.capability_hash}; ticks every {attached.tick_interval_ms} ms). Visitors of grid "
            f"{self._grid_id} run it once they consent to it or trust you; you consented to it as its author.",
        )
        try:
            artifact = await self._mods.mod_client_artifact_bytes(self._app_id, attached.mod_id)
        except CrowdyGraphQLError as error:
            if error.code == "NOT_FOUND":
                raise CrowdyError(
                    f"{compiled.name} is attached to mod '{mod_name}', but its preview did not load: the "
                    "API serves a CLIENT half only while its mod is switched on and not held, to a player "
                    f"with run_client_code who stands in grid {self._grid_id}",
                    cause=error,
                ) from error
            if error.code == "RATE_LIMITED":
                raise CrowdyError(
                    f"{compiled.name} is attached to mod '{mod_name}', but its preview was fetched too "
                    "often (12 a minute); deploy again in a minute",
                    cause=error,
                ) from error
            raise
        self._check_operation(operation)
        if artifact.digest != attached.digest or artifact.client_version != attached.client_version:
            raise CrowdyStudioError(
                "The served CLIENT half is not the one just attached; deploy again"
            )
        return artifact

    async def _ensure_client_only_mod(self, name: str, operation: int) -> None:
        mine = next(
            (
                mod
                for mod in await self._mods.my_mods(self._app_id)
                if str(mod.grid_id) == self._grid_id and mod.name == name
            ),
            None,
        )
        self._check_operation(operation)
        if mine is None:
            if not self.can_target("SERVER", "write") or not self.can_target("SERVER", "run"):
                raise CrowdyStudioError(
                    f"A CLIENT half rides a mod, and grid {self._grid_id} has no mod '{name}' of yours; "
                    "deploying one needs SERVER write and run permissions here"
                )
            self._update(
                runtime=CrowdyStudioRuntimeStatus(
                    "COMPILING", "SERVER", f"Deploying the mod starter as {name}"
                )
            )
            starter = await self._mods.mod_starter(self._app_id)
            self._check_operation(operation)
            queued = await self._mods.mod_build(
                self._app_id,
                {
                    "name": _crate_name(name),
                    "files": [{"path": f.path, "content": f.content} for f in starter.files],
                },
            )
            self._check_operation(operation)
            built = await self._await_mod_build("SERVER", name, queued.build_id, operation)
            if built is None:
                raise CrowdyStudioError(
                    f"The mod starter did not build, so the CLIENT half has no mod '{name}' to ride"
                )
            await self._mods.mod_deploy(self._app_id, self._grid_id, name, built.build_id)
            self._check_operation(operation)
            self._record_note(
                "SERVER",
                f"Grid {self._grid_id} had no mod '{name}' of yours, and a CLIENT half rides a mod: deployed the "
                f"ck-exec mod starter ({starter.crate}) as '{name}', its server half.",
            )
        if mine is None or not mine.enabled:
            await self._mods.mod_set_enabled(self._app_id, self._grid_id, name, True)
            self._check_operation(operation)
            if mine is not None:
                self._record_note(
                    "SERVER", f"Switched mod '{name}' on, so its CLIENT half is served."
                )

    def _record_note(self, target: CrowdyStudioTarget, note: str) -> None:
        self._update(
            build_output="\n\n".join(
                s for s in (self._state.build_output, f"## {target}\n{note}") if s
            )
        )

    def _record_build(self, target: CrowdyStudioTarget, log: str) -> None:
        section = f"## {target}\n{log or 'Compiled successfully.'}"
        diagnostics = (
            *(d for d in self._state.authoritative_diagnostics if d.target != target),
            *parse_rustc_diagnostics(log, target),
        )
        self._update(
            build_output="\n\n".join(s for s in (self._state.build_output, section) if s),
            authoritative_diagnostics=diagnostics,
        )

    async def _enable_server(self, name: str, operation: int) -> None:
        if not self.can_target("SERVER", "run"):
            raise CrowdyStudioError(
                f"{name} compiled successfully, but run_server_code is unavailable on this grid"
            )
        self._update(runtime=CrowdyStudioRuntimeStatus("ENABLING", "SERVER", f"Enabling {name}"))
        await self._mods.mod_set_enabled(self._app_id, self._grid_id, name, True)
        self._check_operation(operation)

    async def _run_client(self, compiled: _Compiled, operation: int) -> None:
        if not self.can_target("CLIENT", "run"):
            raise CrowdyStudioError(
                f"{compiled.name} compiled successfully, but run_client_code is unavailable on this grid"
            )
        if self._broker_factory is None:
            raise CrowdyStudioError(
                "CLIENT projects need broker_factory: a host that runs the CLIENT half (the browser "
                "Studio runs it in a Web Worker)"
            )
        half = await self._attach_client_half(compiled, operation)
        holder: list[CrowdyStudioBroker] = []

        def on_log(level: str, message: str) -> None:
            if holder and self._broker is holder[0]:
                self._record_preview_log(compiled.name, level, message)

        summary = half.capability_summary if isinstance(half.capability_summary, Mapping) else {}
        broker = self._broker_factory(
            CrowdyStudioBrokerOptions(
                engine="ck-exec",
                module_name=half.name,
                artifact_hash=half.digest,
                fuel_per_dispatch=half.fuel_per_dispatch,
                consented_host_calls=tuple(summary.get("hostFunctions") or ()),
                tick_interval_ms=self._client_tick_interval_ms or half.tick_interval_ms,
                on_log=on_log,
                grid=self._grid,
                on_host_call=self._on_host_call,
                on_presentation=self._on_presentation,
            )
        )
        holder.append(broker)
        await broker.start(half.bytes)
        try:
            self._check_operation(operation)
        except _Cancelled:
            broker.stop()
            raise
        previous, self._broker = self._broker, broker
        if previous is not None:
            previous.stop()

    async def stop_project(self) -> CrowdyStudioStopResult:
        """Stop the CLIENT half and switch the mod off."""
        project = self._require_project()
        self._operation_generation += 1
        self._stop_surface_polling()
        self._update(runtime=CrowdyStudioRuntimeStatus("STOPPING"))
        failures: list[str] = []
        client_stopped: bool | None = None
        if "CLIENT" in project_targets(project.kind):
            client_stopped = True
            try:
                if self._broker is not None:
                    self._broker.stop()
            except Exception as error:
                client_stopped = False
                failures.append(f"Client: {_message(error)}")
            finally:
                self._broker = None
        server_stopped = False
        try:
            await self._mods.mod_set_enabled(
                self._app_id, self._grid_id, _project_mod_name(project), False
            )
            server_stopped = True
        except Exception as error:
            failures.append(f"Server: {_message(error)}")
        self._update(
            runtime=CrowdyStudioRuntimeStatus("STOPPED", message="Project stopped")
            if not failures
            else CrowdyStudioRuntimeStatus("PARTIAL_FAILURE", message=" \u00b7 ".join(failures)),
            runtime_sync=replace(self._state.runtime_sync, state="STOPPED"),
        )
        return CrowdyStudioStopResult(server_stopped, client_stopped, tuple(failures))

    async def invoke(
        self, export_name: str, params_json: str | None = None, agent_operation: int | None = None
    ) -> CrowdyStudioInvokeResult:
        """Call the SERVER mod's ``export_name`` (``state`` when blank) with JSON
        arguments, over the ck-exec gateway."""
        self._check_agent_operation(agent_operation)
        project = self._require_project()
        if "SERVER" not in project_targets(project.kind):
            raise CrowdyStudioError("Invoke requires a SERVER target")
        result = await self._call_mod(
            _module_name_for(project, "SERVER"), export_name.strip() or "state", params_json
        )
        self._check_agent_operation(agent_operation)
        self._update(invoke_result=result)
        return result

    async def _call_mod(
        self, name: str, method: str, params_json: str | None
    ) -> CrowdyStudioInvokeResult:
        text = (params_json or "").strip()
        args: Any = None
        if text:
            try:
                args = json.loads(text, parse_constant=_reject_constant)
            except ValueError as error:
                raise CrowdyStudioError("The call arguments must be JSON") from error
        if self._mod_connection is None or self._mod_connection[0] != name:
            self._close_mod_connection()
            future = asyncio.ensure_future(
                self._mods.connect(self._app_id, node_type=_mod_node_type(name), key=self._grid_id)
            )

            def forget(done: asyncio.Future[Any]) -> None:
                if (done.cancelled() or done.exception() is not None) and (
                    self._mod_connection is not None and self._mod_connection[1] is done
                ):
                    self._mod_connection = None

            future.add_done_callback(forget)
            self._mod_connection = (name, future)
        connection = await asyncio.shield(self._mod_connection[1])
        started = time.perf_counter()
        value = await connection.call(_mod_node_type(name), self._grid_id, method, args)
        return CrowdyStudioInvokeResult(
            result_json=json.dumps(_jsonable(value), separators=(",", ":"), ensure_ascii=False),
            duration_us=int((time.perf_counter() - started) * 1_000_000),
        )

    def _close_mod_connection(self) -> None:
        open_, self._mod_connection = self._mod_connection, None
        if open_ is None:
            return

        def close(done: asyncio.Future[Any]) -> None:
            if not done.cancelled() and done.exception() is None:
                self._spawn(done.result().close())

        open_[1].add_done_callback(close)

    # ------------------------------------------------------------------ surfaces

    def set_surface_visible(self, surface: CrowdyStudioPolledSurface, visible: bool) -> None:
        """Poll the logs or wallet surface while it is on screen (and the page is)."""
        if visible:
            self._visible_surfaces[surface] = None
            if self._page_visible:
                self._spawn(self.refresh_surface(surface))
                self._schedule_surface_poll(surface)
        else:
            self._visible_surfaces.pop(surface, None)
            self._clear_surface_timer(surface)

    def set_page_visible(self, visible: bool) -> None:
        if self._page_visible == visible:
            return
        self._page_visible = visible
        if visible:
            self._restart_visible_surface_polling()
        else:
            self._stop_surface_polling()

    async def refresh_surface(self, surface: CrowdyStudioPolledSurface) -> None:
        project = self._state.project
        if project is None:
            return
        if surface == "logs":
            name = _project_mod_name_or_none(project)
            lines = (
                await self._mods.mod_logs(self._app_id, self._grid_id, name, limit=50)
                if name
                else []
            )
            self._mod_log_lines = tuple(_mod_log_line(name or "", line) for line in lines)
            self._update(logs=self._merged_log_lines())
            return
        self._update(wallet=await self._wallet.balance() if self._wallet is not None else None)

    def destroy(self) -> None:
        """Stop everything: timers, polling, the CLIENT half and the gateway connection."""
        if self._destroyed:
            return
        self._destroyed = True
        self._operation_generation += 1
        self._clear_save_timers()
        self._stop_surface_polling()
        self._stop_broker()
        self._close_mod_connection()
        self._listeners.clear()
        self._human_edit_listeners.clear()

    def _schedule_surface_poll(self, surface: CrowdyStudioPolledSurface) -> None:
        self._clear_surface_timer(surface)
        if not self._page_visible or surface not in self._visible_surfaces or self._destroyed:
            return

        def fire() -> None:
            self._surface_timers.pop(surface, None)

            async def poll() -> None:
                try:
                    await self.refresh_surface(surface)
                finally:
                    self._schedule_surface_poll(surface)

            self._spawn(poll())

        timer = self._later(self._monitor_poll_ms, fire)
        if timer is not None:
            self._surface_timers[surface] = timer

    def _restart_visible_surface_polling(self) -> None:
        if not self._page_visible or self._destroyed:
            return
        for surface in list(self._visible_surfaces):
            self._spawn(self.refresh_surface(surface))
            self._schedule_surface_poll(surface)

    def _stop_surface_polling(self) -> None:
        for timer in self._surface_timers.values():
            timer.cancel()
        self._surface_timers.clear()

    def _clear_surface_timer(self, surface: CrowdyStudioPolledSurface) -> None:
        timer = self._surface_timers.pop(surface, None)
        if timer is not None:
            timer.cancel()

    def _clear_log_lines(self) -> tuple[CrowdyStudioLogLine, ...]:
        self._mod_log_lines = ()
        self._preview_log_lines = []
        return ()

    def _merged_log_lines(self) -> tuple[CrowdyStudioLogLine, ...]:
        return tuple(
            sorted(
                [*self._preview_log_lines, *self._mod_log_lines],
                key=lambda line: _parse_ms(line.at),
                reverse=True,
            )
        )

    def _record_preview_log(self, module_name: str, level: str, message: str) -> None:
        if self._destroyed:
            return
        self._preview_log_seq += 1
        line = CrowdyStudioLogLine(
            id=f"preview-{self._preview_log_seq}",
            source="preview",
            module_name=module_name,
            level=_log_level(level),
            at=_now_iso(),
            text=message,
        )
        self._preview_log_lines = [line, *self._preview_log_lines][:_PREVIEW_LOG_LINES_KEPT]
        self._update(logs=self._merged_log_lines())

    # ------------------------------------------------------------------ helpers

    def _assert_deployment_plan(
        self, project: CrowdyStudioProject, plan: CrowdyStudioDeploymentPlan, draft: bool
    ) -> None:
        if project.revision.id != plan.expected_revision_id:
            raise CrowdyStudioRevisionConflictError(
                f"Expected revision {plan.expected_revision_id}, found {project.revision.id}",
                project,
            )
        authoritative = sorted(project_targets(project.kind))
        if sorted(set(plan.targets)) != authoritative:
            raise CrowdyStudioError(
                f"Deployment targets must exactly match {', '.join(authoritative)}"
            )
        if (
            plan.pairing_preference is not None
            and plan.pairing_preference != project.metadata.pairing_preference
        ):
            raise CrowdyStudioError("Deployment pairing preference changed after approval")
        if (
            plan.project_content_hash is not None
            and plan.project_content_hash != project_content_hash(project)
        ):
            raise CrowdyStudioError("Deployment project content changed after approval")
        if not draft and (plan.pairing_preference is None or plan.project_content_hash is None):
            raise CrowdyStudioError(
                "Live deployment requires exact pairing and project content bindings"
            )

    def _check_operation(self, generation: int) -> None:
        if generation != self._operation_generation or self._destroyed:
            raise _Cancelled

    def _check_agent_operation(self, generation: int | None) -> None:
        if generation is not None and (
            generation != self._agent_operation_generation or self._destroyed
        ):
            raise _Cancelled

    async def _sleep(self, ms: float) -> None:
        if self._sleep_fn is not None:
            await self._sleep_fn(ms)
        else:
            await asyncio.sleep(ms / 1000)

    def _offline(self) -> bool:
        return self._is_online is not None and self._is_online() is False

    def _require_project(self) -> CrowdyStudioProject:
        if self._state.project is None:
            raise CrowdyStudioError("No Crowdy Studio project is open")
        return self._state.project

    def _require_file(
        self, ref: CrowdyStudioFileRef
    ) -> CrowdyStudioProjectFile | CrowdyStudioReferenceFile:
        if ref.source == "PROJECT":
            path = normalize_crowdy_studio_path(ref.path)
            for file in self._require_project().files:
                if file.target == ref.target and file.path == path:
                    return file
        else:
            references = (
                self._state.personal_library_files
                if ref.source == "PERSONAL_LIBRARY"
                else self._state.common_files
            )
            for reference in references:
                if (
                    reference.id == ref.reference_id
                    if ref.reference_id
                    else reference.path == ref.path and reference.target == ref.target
                ):
                    return reference
        raise CrowdyStudioError(f"File is not loaded: {ref.source}:{ref.path}")

    @staticmethod
    def _assert_project_target(project: CrowdyStudioProject, target: CrowdyStudioTarget) -> None:
        if target not in project_targets(project.kind):
            raise CrowdyStudioError(f"{project.kind} projects do not have a {target} target")

    def _set_runtime(self, phase: CrowdyStudioPhase, message: str) -> None:
        self._update(runtime=CrowdyStudioRuntimeStatus(phase, message=message))

    def _set_project(self, project: CrowdyStudioProject) -> None:
        self._state = replace(self._state, project=project)

    def _update(self, **patch: Any) -> None:
        self._state = replace(self._state, **patch)
        for listener in list(self._listeners):
            listener(self._state)

    def _ensure_alive(self) -> None:
        if self._destroyed:
            raise CrowdyStudioError("CrowdyStudioController is destroyed")

    def _stop_broker(self) -> None:
        broker, self._broker = self._broker, None
        if broker is not None:
            broker.stop()

    def _clear_save_timers(self) -> None:
        self._clear_timer("autosave")
        self._clear_timer("retry")

    def _clear_timer(self, kind: Literal["autosave", "retry"]) -> None:
        timer = self._autosave_timer if kind == "autosave" else self._retry_timer
        if timer is not None:
            timer.cancel()
        if kind == "autosave":
            self._autosave_timer = None
        else:
            self._retry_timer = None

    @staticmethod
    def _later(ms: float, callback: Callable[[], None]) -> asyncio.TimerHandle | None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None  # no loop to time on: the next explicit save or poll catches up
        return loop.call_later(ms / 1000, callback)

    def _spawn(self, awaitable: Awaitable[Any]) -> None:
        """Run in the background, failures swallowed (CrowdyJS's ``void p.catch(() => {})``)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            return
        task = asyncio.ensure_future(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Future[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()


# ---------------------------------------------------------------------- module helpers


def project_content_hash(project: CrowdyStudioProject) -> str:
    """The digest an approval binds a project's content to: its id, metadata and each
    file's content hash, as CrowdyJS computes it."""
    metadata = project.metadata
    fields = (
        ("name", metadata.name),
        ("description", metadata.description),
        ("serverModuleName", metadata.server_module_name),
        ("clientModuleName", metadata.client_module_name),
        ("pairingPreference", metadata.pairing_preference),
    )
    files = [
        {"target": f.target, "path": f.path, "contentHash": sha256_digest(f.content)}
        for f in sorted(project.files, key=_file_order)
    ]
    return digest_canonical_json(
        {
            "contract": "crowdy.studio-project-content/1",
            "projectId": project.project_id,
            "metadata": {key: value for key, value in fields if value is not None},
            "files": files,
        }
    )


def _apply_validated_patch(
    baseline: CrowdyStudioProject, input: CrowdyStudioAtomicPatchInput
) -> CrowdyStudioProject:
    if not 1 <= len(input.changes) <= 16:
        raise CrowdyStudioError("Atomic patch must contain 1 to 16 file changes")
    files = list(baseline.files)
    seen: set[str] = set()
    for change in input.changes:
        path = normalize_crowdy_studio_path(change.path)
        key = crowdy_studio_file_key(change.target, path)
        if key in seen:
            raise CrowdyStudioError(f"Atomic patch repeats {key}")
        seen.add(key)
        if change.target not in project_targets(baseline.kind):
            raise CrowdyStudioError(
                f"{baseline.kind} projects do not have a {change.target} target"
            )
        if len(change.content.encode("utf-8", "surrogatepass")) > 65_536:
            raise CrowdyStudioError(f"{key} exceeds the 65536-byte file limit")
        index = next(
            (i for i, f in enumerate(files) if f.target == change.target and f.path == path), -1
        )
        replacement = CrowdyStudioProjectFile(
            target=change.target, path=path, content=change.content
        )
        if change.operation == "CREATE":
            if change.expected_content_hash != "ABSENT" or index >= 0:
                raise CrowdyStudioRevisionConflictError(
                    f"{key} was expected to be absent", baseline
                )
            files.append(replacement)
            continue
        if index < 0:
            raise CrowdyStudioRevisionConflictError(f"{key} no longer exists", baseline)
        if change.expected_content_hash != sha256_digest(files[index].content):
            raise CrowdyStudioRevisionConflictError(f"{key} content hash changed", baseline)
        files[index] = replacement
    if len(files) > 128:
        raise CrowdyStudioError("Project exceeds the 128-file limit")
    return msgspec.structs.replace(baseline, files=sorted(files, key=_file_order))


def _resync(sync: CrowdyStudioRuntimeSync, saved_revision_id: str) -> CrowdyStudioRuntimeSync:
    state = sync.state
    if state in ("RUNNING_SAVED", "RUNNING_STALE"):
        state = (
            "RUNNING_SAVED" if sync.running_revision_id == saved_revision_id else "RUNNING_STALE"
        )
    return replace(sync, saved_revision_id=saved_revision_id, state=state)


def _file_order(file: CrowdyStudioProjectFile) -> str:
    return crowdy_studio_file_key(file.target, file.path)


def _project_file_ref(file: CrowdyStudioProjectFile) -> CrowdyStudioFileRef:
    return CrowdyStudioFileRef("PROJECT", file.path, file.target)


def _mod_node_type(name: str) -> str:
    return f"mod:{name}"


def _is_mod_crate_file(path: str) -> bool:
    return path in ("Cargo.toml", "README.md") or (path.startswith("src/") and path.endswith(".rs"))


def _crate_files(files: Sequence[CrowdyStudioProjectFile]) -> list[dict[str, str]]:
    return [{"path": f.path, "content": f.content} for f in files if _is_mod_crate_file(f.path)]


def _crate_name(name: str) -> str:
    return name if _CRATE_NAME.fullmatch(name) else f"mod-{name}"


def _module_name_for(project: CrowdyStudioProject, target: CrowdyStudioTarget) -> str:
    metadata = project.metadata
    name = metadata.server_module_name if target == "SERVER" else metadata.client_module_name
    if not name or not name.strip():
        raise CrowdyStudioError(f"{target} module name is required in Project settings")
    return name.strip()


def _project_mod_name(project: CrowdyStudioProject) -> str:
    return _module_name_for(
        project, "SERVER" if "SERVER" in project_targets(project.kind) else "CLIENT"
    )


def _project_mod_name_or_none(project: CrowdyStudioProject) -> str | None:
    try:
        return _project_mod_name(project)
    except CrowdyStudioError:
        return None


def _mod_name_problem(project: CrowdyStudioProject) -> str | None:
    name = _project_mod_name(project)
    if _MOD_NAME.fullmatch(name):
        return None
    if "SERVER" in project_targets(project.kind):
        return f"The server module name '{name}' must be 1-48 lowercase letters, digits, - or _ to run as a mod"
    return (
        f"The CLIENT module name '{name}' names the mod its CLIENT half rides, so it must be 1-48 "
        "lowercase letters, digits, - or _"
    )


def _log_level(level: str) -> CrowdyStudioLogLevel:
    for known in _LOG_LEVELS:
        if level == known:
            return known
    return "debug"


def _mod_log_line(module_name: str, line: Any) -> CrowdyStudioLogLine:
    level = line.level
    return CrowdyStudioLogLine(
        id=line.id,
        source="mod",
        module_name=module_name,
        level=_LOG_LEVELS[level]
        if isinstance(level, int) and 0 <= level < len(_LOG_LEVELS)
        else "debug",
        at=line.at,
        text=line.text,
    )


def _summary_of(project: CrowdyStudioProject) -> CrowdyStudioProjectSummary:
    github = project.github
    return CrowdyStudioProjectSummary(
        project_id=project.project_id,
        name=project.metadata.name,
        kind=project.kind,
        revision_id=project.revision.id,
        source=project.source,
        github=f"{github.owner}/{github.repo}@{github.branch}" if github else None,
        github_sha=github.sha if github else None,
        server_module_name=project.metadata.server_module_name or None,
        client_module_name=project.metadata.client_module_name or None,
        updated_at=project.updated_at,
    )


def _upsert_summary(
    projects: Sequence[CrowdyStudioProjectSummary], summary: CrowdyStudioProjectSummary
) -> tuple[CrowdyStudioProjectSummary, ...]:
    rest = [p for p in projects if p.project_id != summary.project_id]
    return tuple(sorted([*rest, summary], key=lambda p: p.updated_at, reverse=True))


def _upsert_reference(
    files: Sequence[CrowdyStudioReferenceFile], reference: CrowdyStudioReferenceFile
) -> tuple[CrowdyStudioReferenceFile, ...]:
    return (
        reference,
        *(f for f in files if f.source != reference.source or f.id != reference.id),
    )


def _upsert_checkpoint(
    checkpoints: Sequence[CrowdyStudioCheckpointMetadata],
    checkpoint: CrowdyStudioCheckpointMetadata,
) -> tuple[CrowdyStudioCheckpointMetadata, ...]:
    return (checkpoint, *(c for c in checkpoints if c.checkpoint_id != checkpoint.checkpoint_id))


def _file_ref_exists(
    project: CrowdyStudioProject, state: CrowdyStudioState, ref: CrowdyStudioFileRef
) -> bool:
    if ref.source == "PROJECT":
        path = normalize_crowdy_studio_path(ref.path)
        return any(f.target == ref.target and f.path == path for f in project.files)
    references = (
        state.personal_library_files if ref.source == "PERSONAL_LIBRARY" else state.common_files
    )
    return any(
        f.id == ref.reference_id
        if ref.reference_id
        else f.target == ref.target and f.path == ref.path
        for f in references
    )


def _jsonable(value: Any) -> Any:
    """A gateway reply as JSON: integers beyond 2**53 as strings (as CrowdyJS's BigInt
    replacer writes them), bytes as lists of octets."""
    if isinstance(value, bool) or value is None or isinstance(value, (str, float)):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > 2**53 - 1 else value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return list(bytes(value))
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def _reject_constant(name: str) -> Any:
    raise ValueError(name)


def _message(error: BaseException) -> str:
    return getattr(error, "message", None) or str(error)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_ms(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp() * 1000
    except (TypeError, ValueError):
        return 0.0
