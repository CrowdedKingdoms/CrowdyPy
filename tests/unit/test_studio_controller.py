"""The headless Studio controller against in-memory providers, and its digests against values
CrowdyJS 18.0.4's own controller computes for the same project."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import msgspec
import pytest

from crowdypy.domains.crowdy_studio import (
    CrowdyStudioOfflineError,
    CrowdyStudioProject,
    CrowdyStudioProjectFile,
    CrowdyStudioProjectMetadata,
    CrowdyStudioProjectRevision,
    CrowdyStudioProjectSummary,
    CrowdyStudioReferenceFile,
    CrowdyStudioRevisionConflictError,
)
from crowdypy.studio import (
    CrowdyStudioAtomicFileChange,
    CrowdyStudioAtomicPatchInput,
    CrowdyStudioAtomicPatchResult,
    CrowdyStudioBrokerOptions,
    CrowdyStudioCheckpointMetadata,
    CrowdyStudioCheckpointRestoreResult,
    CrowdyStudioController,
    CrowdyStudioDeploymentPlan,
    CrowdyStudioError,
    CrowdyStudioFileRef,
    canonical_json,
    digest_canonical_json,
    project_content_hash,
    sha256_digest,
)

# Computed by CrowdyJS 18.0.4 (dist at c8634082) for the same values.
TRICKY = {
    "b": 1,
    "a": [True, None, 'é\u2028"\\\n\u0001', 0.5, -0.0],
    "z": {"y": 2, "x": "x"},
    "😀": "astral",
    "ｚ": "fullwidth",
}
TRICKY_CANONICAL = (
    '{"a":[true,null,"é\u2028\\"\\\\\\n\\u0001",0.5,0],"b":1,"z":{"x":"x","y":2},'
    '"😀":"astral","ｚ":"fullwidth"}'
)
TRICKY_DIGEST = "sha256:cc471b5a084655ce66b9ec74ebab952f5ba40ee956dfba54252c17f7a6e188df"
TEXT_DIGEST = "sha256:a61203becd033405b6914a4a70e36eefeca025076dfff93a4e90e367c24e3d0a"
PROJECT_CONTENT_HASH = "sha256:094f808d4728115cfbfd6ac3380ff073c35641cba4da1cd39ebf183824f95640"
CONTEXT_VERSION = "sha256:b4a682b58fc481044d53ebd36ffc1a4f90921869e4bd4a0a78d259edad25b7fc"


def make_project(
    kind: str = "FULL_STACK",
    revision: str = "4",
    files: Sequence[tuple[str, str, str]] | None = None,
    pairing: str = "OPTIONAL",
    **metadata: Any,
) -> CrowdyStudioProject:
    if files is None:
        files = [
            ("SERVER", "src/lib.rs", "fn x() {}"),
            ("CLIENT", "Cargo.toml", '[package]\nname = "q"\n'),
            ("SERVER", "Cargo.toml", "café 値"),
        ]
    names = {
        "server_module_name": "quest-server" if kind != "CLIENT" else None,
        "client_module_name": "quest-client" if kind != "SERVER" else None,
    }
    names.update(metadata)
    return CrowdyStudioProject(
        project_id="p-1",
        app_id="42",
        grid_id="7",
        kind=kind,  # type: ignore[arg-type]
        metadata=CrowdyStudioProjectMetadata(
            name="Quest",
            pairing_preference=pairing,
            **names,  # type: ignore[arg-type]
        ),
        files=[CrowdyStudioProjectFile(target=t, path=p, content=c) for t, p, c in files],  # type: ignore[arg-type]
        sdk_version="1",
        abi_version=1,
        revision=CrowdyStudioProjectRevision(id=revision, saved_at="2026-09-02T00:00:00Z"),
        source="STUDIO",
        github=None,
        created_at="2026-09-01T00:00:00Z",
        updated_at="2026-09-02T00:00:00Z",
    )


def summary(project: CrowdyStudioProject) -> CrowdyStudioProjectSummary:
    return CrowdyStudioProjectSummary(
        project_id=project.project_id,
        name=project.metadata.name,
        kind=project.kind,
        revision_id=project.revision.id,
        source=project.source,
        github_sha=None,
        updated_at=project.updated_at,
    )


class Provider:
    def __init__(self, project: CrowdyStudioProject) -> None:
        self.project = project
        self.saves: list[tuple[str, list[CrowdyStudioProjectFile]]] = []
        self.fail_next: BaseException | None = None
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.library = [
            CrowdyStudioReferenceFile(
                id="l-1",
                source="PERSONAL_LIBRARY",
                title="Helpers",
                target="SERVER",
                path="src/helpers.rs",
                content="pub fn h() {}",
            )
        ]

    async def list_projects(self, app_id: Any) -> list[CrowdyStudioProjectSummary]:
        return [summary(self.project)]

    async def get_project(self, app_id: Any, grid_id: Any, project_id: str) -> CrowdyStudioProject:
        return self.project

    async def create_project(
        self, app_id: Any, grid_id: Any, kind: Any, metadata: Any, files: Any
    ) -> CrowdyStudioProject:
        self.project = msgspec.structs.replace(
            make_project(kind=kind), project_id="p-new", metadata=metadata, files=list(files)
        )
        return self.project

    async def save_project(
        self, app_id: Any, grid_id: Any, project_id: str, expected: Any, metadata: Any, files: Any
    ) -> CrowdyStudioProject:
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        if str(expected) != self.project.revision.id:
            raise CrowdyStudioRevisionConflictError("stale", self.project)
        self.saves.append((str(expected), list(files)))
        self.project = msgspec.structs.replace(
            self.project,
            metadata=metadata,
            files=list(files),
            revision=CrowdyStudioProjectRevision(
                id=str(int(self.project.revision.id) + 1), saved_at="2026-09-03T00:00:00Z"
            ),
            updated_at=f"2026-09-03T00:00:{len(self.saves):02d}Z",
        )
        return self.project

    async def list_personal_library_files(self, app_id: Any) -> list[CrowdyStudioReferenceFile]:
        return list(self.library)

    async def list_common_files(self, app_id: Any) -> list[CrowdyStudioReferenceFile]:
        return []

    async def import_reference_file(
        self,
        app_id: Any,
        grid_id: Any,
        project_id: str,
        expected: Any,
        source: str,
        reference_id: str,
        destination: str | None = None,
    ) -> CrowdyStudioProject:
        ref = next(r for r in self.library if r.id == reference_id)
        self.project = msgspec.structs.replace(
            self.project,
            files=[
                *self.project.files,
                CrowdyStudioProjectFile(
                    target="SERVER", path=destination or ref.path, content=ref.content
                ),
            ],
            revision=CrowdyStudioProjectRevision(
                id=str(int(self.project.revision.id) + 1), saved_at="x"
            ),
        )
        return self.project

    async def save_personal_library_file(
        self, app_id: Any, title: str, target: Any, path: str, content: str, tags: Any = None
    ) -> CrowdyStudioReferenceFile:
        return CrowdyStudioReferenceFile(
            id="l-2",
            source="PERSONAL_LIBRARY",
            title=title,
            target=target,
            path=path,
            content=content,
        )


class Mods:
    def __init__(self, statuses: Sequence[str] = ("running", "succeeded"), log: str = "") -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.statuses = list(statuses)
        self.log = log
        self.mods: list[Any] = []
        self.connection = SimpleNamespace(calls=[], closed=False)

        async def call(node_type: str, key: str, method: str, args: Any) -> Any:
            self.connection.calls.append((node_type, key, method, args))
            return {"ok": True, "big": 2**60}

        async def close() -> None:
            self.connection.closed = True

        self.connection.call = call
        self.connection.close = close

    async def mod_starter(self, app_id: Any) -> Any:
        self.calls.append(("starter",))
        return SimpleNamespace(
            crate="starter",
            files=[
                SimpleNamespace(path="Cargo.toml", content='[package]\nname = "starter"\n'),
                SimpleNamespace(path="src/lib.rs", content="// mod"),
            ],
        )

    async def mod_build(self, app_id: Any, crate: Any) -> Any:
        self.calls.append(("build", crate["name"], [f["path"] for f in crate["files"]]))
        return SimpleNamespace(build_id="b-server")

    async def mod_client_build(self, app_id: Any, crate: Any) -> Any:
        self.calls.append(("client_build", crate["name"]))
        return SimpleNamespace(build_id="b-client")

    async def mod_build_status(self, app_id: Any, build_id: str) -> Any:
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        self.calls.append(("status", build_id, status))
        return SimpleNamespace(build_id=build_id, status=status, log=self.log)

    async def mod_deploy(self, app_id: Any, grid_id: Any, name: str, build_id: str) -> Any:
        self.calls.append(("deploy", name, build_id))
        return SimpleNamespace(version=3)

    async def mod_set_enabled(self, app_id: Any, grid_id: Any, name: str, enabled: bool) -> Any:
        self.calls.append(("enabled", name, enabled))
        return SimpleNamespace(enabled=enabled)

    async def mod_client_deploy(self, app_id: Any, grid_id: Any, name: str, build_id: str) -> Any:
        self.calls.append(("client_deploy", name, build_id))
        return SimpleNamespace(
            mod_id="m-1", capability_hash="cap", client_version=2, tick_interval_ms=50, digest="d-1"
        )

    async def consent_client_mod(self, app_id: Any, mod_id: str, capability_hash: str) -> bool:
        self.calls.append(("consent", mod_id, capability_hash))
        return True

    async def mod_client_artifact_bytes(self, app_id: Any, mod_id: str) -> Any:
        return SimpleNamespace(
            name="quest-client",
            digest="d-1",
            client_version=2,
            bytes=b"\0asm",
            fuel_per_dispatch=1000,
            tick_interval_ms=50,
            capability_summary={"hostFunctions": ["hud_set"]},
        )

    async def my_mods(self, app_id: Any) -> list[Any]:
        return self.mods

    async def mod_logs(
        self, app_id: Any, grid_id: Any, name: str, limit: int | None = None
    ) -> list[Any]:
        return [SimpleNamespace(id="1", level=2, at="2026-09-03T00:00:01Z", text=f"{name} up")]

    async def connect(
        self, app_id: Any, node_type: str | None = None, key: str | None = None
    ) -> Any:
        self.calls.append(("connect", node_type, key))
        return self.connection


class Broker:
    def __init__(self, options: CrowdyStudioBrokerOptions) -> None:
        self.options = options
        self.started: bytes | None = None
        self.stopped = False

    async def start(self, artifact: bytes) -> None:
        self.started = artifact

    def stop(self) -> None:
        self.stopped = True


async def no_sleep(ms: float) -> None:
    await asyncio.sleep(0)


async def studio(
    project: CrowdyStudioProject | None = None, **options: Any
) -> tuple[CrowdyStudioController, Provider, Mods]:
    provider = Provider(project or make_project())
    mods = options.pop("mods", None) or Mods()
    controller = CrowdyStudioController(
        project_provider=provider,
        mods=mods,
        app_id="42",
        grid_id="7",
        sleep=no_sleep,
        autosave_ms=options.pop("autosave_ms", 10_000),
        **options,
    )
    await controller.initialize()
    return controller, provider, mods


def test_digests_are_crowdyjs_s() -> None:
    assert canonical_json(TRICKY) == TRICKY_CANONICAL
    assert digest_canonical_json(TRICKY) == TRICKY_DIGEST
    assert sha256_digest("café 値") == TEXT_DIGEST
    assert project_content_hash(make_project()) == PROJECT_CONTENT_HASH
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


async def test_initialize_opens_the_project_and_the_agent_context_is_crowdyjs_s() -> None:
    controller, _, _ = await studio()
    state = controller.get_state()
    assert state.project is not None
    assert state.project.project_id == "p-1"
    assert state.active_file == CrowdyStudioFileRef("PROJECT", "src/lib.rs", "SERVER")
    assert state.runtime_sync.saved_revision_id == "4"
    context = controller.get_agent_context()
    assert context.project_content_hash == PROJECT_CONTENT_HASH
    assert context.context_version == CONTEXT_VERSION


async def test_offline_initialization_is_a_state_not_an_error() -> None:
    provider = Provider(make_project())

    async def offline(app_id: Any) -> Any:
        raise CrowdyStudioOfflineError("down")

    provider.list_projects = offline  # type: ignore[method-assign]
    controller = CrowdyStudioController(
        project_provider=provider, mods=Mods(), app_id="42", grid_id="7"
    )
    await controller.initialize()
    assert (controller.get_state().save_state, controller.get_state().save_message) == (
        "OFFLINE",
        "down",
    )


async def test_edits_save_with_the_revision_they_started_from() -> None:
    controller, provider, _ = await studio()
    seen: list[str] = []
    controller.subscribe(lambda state: seen.append(state.save_state))
    controller.update_file("SERVER", "src/lib.rs", "fn y() {}")
    controller.add_file("SERVER", "./src/extra.rs", "// extra")
    controller.rename_file("SERVER", "src/extra.rs", "src/more.rs")
    assert controller.get_state().active_file == CrowdyStudioFileRef(
        "PROJECT", "src/more.rs", "SERVER"
    )
    assert controller.get_state().save_state == "SAVING"
    assert await controller.save_now() is True
    expected, files = provider.saves[-1]
    assert expected == "4"
    assert [(f.target, f.path) for f in files] == [
        ("CLIENT", "Cargo.toml"),
        ("SERVER", "Cargo.toml"),
        ("SERVER", "src/lib.rs"),
        ("SERVER", "src/more.rs"),
    ]
    state = controller.get_state()
    assert (state.save_state, state.project.revision.id) == ("SAVED", "5")  # type: ignore[union-attr]
    assert "SAVING" in seen
    assert seen[-1] == "SAVED"
    controller.delete_file("SERVER", "src/more.rs")
    assert controller.get_state().active_file != CrowdyStudioFileRef(
        "PROJECT", "src/more.rs", "SERVER"
    )
    with pytest.raises(CrowdyStudioError, match="already exists"):
        controller.add_file("SERVER", "src/lib.rs")


async def test_autosave_fires_on_the_loop() -> None:
    controller, provider, _ = await studio(autosave_ms=5)
    controller.update_file("SERVER", "src/lib.rs", "fn z() {}")
    for _ in range(50):
        await asyncio.sleep(0.01)
        if provider.saves:
            break
    assert provider.saves
    assert controller.get_state().save_state == "SAVED"


async def test_an_edit_during_a_save_saves_again() -> None:
    controller, provider, _ = await studio()
    provider.gate = asyncio.Event()
    controller.update_file("SERVER", "src/lib.rs", "one")
    saving = asyncio.ensure_future(controller.save_now())
    await provider.entered.wait()
    controller.update_file("SERVER", "src/lib.rs", "two")
    provider.gate.set()
    assert await saving is True
    lib = [next(f.content for f in files if f.path == "src/lib.rs") for _, files in provider.saves]
    assert lib == ["one", "two"]
    assert controller.get_state().project.revision.id == "6"  # type: ignore[union-attr]


async def test_conflicts_resolve_either_way() -> None:
    controller, provider, _ = await studio()
    provider.project = make_project(revision="9")
    controller.update_file("SERVER", "src/lib.rs", "mine")
    assert await controller.save_now() is False
    assert controller.get_state().save_state == "CONFLICT"
    assert await controller.overwrite_conflict() is True
    assert provider.saves[-1][0] == "9"

    provider.project = make_project(revision="20")
    controller.update_file("SERVER", "src/lib.rs", "mine again")
    assert await controller.save_now() is False
    await controller.accept_remote_conflict()
    state = controller.get_state()
    assert (state.save_state, state.project.revision.id) == ("SAVED", "20")  # type: ignore[union-attr]


async def test_offline_saves_retry() -> None:
    controller, provider, _ = await studio(retry_ms=5)
    provider.fail_next = CrowdyStudioOfflineError("down")
    controller.update_file("SERVER", "src/lib.rs", "x")
    assert await controller.save_now() is False
    assert controller.get_state().save_state == "OFFLINE"
    for _ in range(50):
        await asyncio.sleep(0.01)
        if controller.get_state().save_state == "SAVED":
            break
    assert controller.get_state().save_state == "SAVED"
    assert provider.saves


async def test_create_project_starts_from_the_starters() -> None:
    controller, _provider, mods = await studio()
    project = await controller.create_project(name="Space Race!", kind="FULL_STACK")
    assert project.metadata.server_module_name == "space-race-server"
    assert project.metadata.client_module_name == "space-race-client"
    assert ("starter",) in mods.calls
    paths = [(f.target, f.path) for f in project.files]
    assert ("SERVER", "Cargo.toml") in paths
    assert ("CLIENT", "src/lib.rs") in paths
    assert 'name = "space-race-server"' in next(
        f.content for f in project.files if f.target == "SERVER" and f.path == "Cargo.toml"
    )
    assert controller.get_state().project.project_id == "p-new"  # type: ignore[union-attr]


async def test_a_server_draft_builds_deploys_and_runs() -> None:
    log = "error[E0425]: cannot find value `x`\n  --> src/lib.rs:3:5"
    controller, _, mods = await studio(
        make_project(
            kind="SERVER", files=[("SERVER", "src/lib.rs", "x"), ("SERVER", "Cargo.toml", "")]
        ),
        mods=Mods(log=log),
    )
    result = await controller.test_draft()
    assert (result.status, result.targets, result.deployment) == ("RUNNING", ("SERVER",), "DRAFT")
    assert ("build", "quest-server", ["src/lib.rs", "Cargo.toml"]) in mods.calls
    assert ("deploy", "quest-server", "b-server") in mods.calls
    assert ("enabled", "quest-server", True) in mods.calls
    state = controller.get_state()
    assert state.runtime.phase == "RUNNING"
    assert state.runtime_sync.state == "RUNNING_SAVED"
    assert [(d.path, d.line, d.code) for d in state.authoritative_diagnostics] == [
        ("src/lib.rs", 3, "E0425")
    ]
    controller.update_file("SERVER", "src/lib.rs", "y")
    assert controller.get_state().runtime_sync.state == "RUNNING_STALE"

    stopped = await controller.stop_project()
    assert (stopped.server_stopped, stopped.client_stopped, stopped.failures) == (True, None, ())
    assert controller.get_state().runtime.phase == "STOPPED"


async def test_compile_failures_and_timeouts() -> None:
    project = make_project(kind="SERVER", files=[("SERVER", "src/lib.rs", "x")])
    controller, _, _ = await studio(project, mods=Mods(statuses=["failed"], log="boom"))
    result = await controller.test_draft()
    assert (result.status, result.message) == ("COMPILE_FAILED", "quest-server failed to compile")

    controller, _, _ = await studio(project, mods=Mods(statuses=["running"]), compile_poll_limit=3)
    result = await controller.test_draft()
    assert (result.status, result.message) == (
        "COMPILE_FAILED",
        "Compilation timed out after 3 polls",
    )


async def test_a_full_stack_run_needs_a_client_host_and_runs_in_it() -> None:
    controller, _, _ = await studio()
    result = await controller.test_draft()
    assert result.status == "FAILED"
    assert "broker_factory" in result.message

    brokers: list[Broker] = []

    def factory(options: CrowdyStudioBrokerOptions) -> Broker:
        brokers.append(Broker(options))
        return brokers[-1]

    controller, _, mods = await studio(broker_factory=factory)
    result = await controller.test_draft()
    assert result.status == "RUNNING", result.message
    order = [call[0] for call in mods.calls]
    assert order.index("client_build") < order.index("build")
    assert ("consent", "m-1", "cap") in mods.calls
    broker = brokers[0]
    assert broker.started == b"\0asm"
    assert (
        broker.options.module_name,
        broker.options.consented_host_calls,
        broker.options.tick_interval_ms,
    ) == ("quest-client", ("hud_set",), 50)
    broker.options.on_log("warn", "hello")
    assert controller.get_state().logs[0].text == "hello"
    await controller.stop_project()
    assert broker.stopped


async def test_deployment_plans_bind_what_was_approved() -> None:
    controller, _, _ = await studio()
    project = controller.get_state().project
    assert project is not None
    with pytest.raises(CrowdyStudioRevisionConflictError):
        await controller.deploy_live_plan(CrowdyStudioDeploymentPlan("3", ["SERVER", "CLIENT"]))
    with pytest.raises(CrowdyStudioError, match="exactly match"):
        await controller.test_draft_plan(CrowdyStudioDeploymentPlan("4", ["SERVER"]))
    with pytest.raises(CrowdyStudioError, match="exact pairing"):
        await controller.deploy_live_plan(CrowdyStudioDeploymentPlan("4", ["CLIENT", "SERVER"]))
    with pytest.raises(CrowdyStudioError, match="content changed"):
        await controller.deploy_live_plan(
            CrowdyStudioDeploymentPlan("4", ["CLIENT", "SERVER"], "OPTIONAL", "sha256:00")
        )


async def test_invoke_calls_the_mod_over_the_gateway() -> None:
    controller, _, mods = await studio()
    result = await controller.invoke(" ", '{"n": 1}')
    assert mods.connection.calls == [("mod:quest-server", "7", "state", {"n": 1})]
    assert result.result_json == '{"ok":true,"big":"1152921504606846976"}'
    with pytest.raises(CrowdyStudioError, match="must be JSON"):
        await controller.invoke("state", "{nope")
    controller.destroy()
    for _ in range(5):
        await asyncio.sleep(0)
    assert mods.connection.closed


async def test_agent_patches_are_validated_then_synchronized() -> None:
    project = make_project()
    patched = msgspec.structs.replace(
        project,
        files=[
            *project.files,
            CrowdyStudioProjectFile(target="SERVER", path="src/new.rs", content="// new"),
        ],
        revision=CrowdyStudioProjectRevision(id="5", saved_at="x"),
    )
    checkpoint = CrowdyStudioCheckpointMetadata(
        checkpoint_id="c-1",
        project_revision_id="4",
        content_hash="sha256:1",
        reason="AGENT_WRITE",
        files=[],
        created_at="x",
    )

    class Sync:
        async def apply_atomic_patch(self, **kwargs: Any) -> CrowdyStudioAtomicPatchResult:
            return CrowdyStudioAtomicPatchResult(
                project=patched, checkpoint=checkpoint, changed_files=[]
            )

        async def list_checkpoints(self, **kwargs: Any) -> list[CrowdyStudioCheckpointMetadata]:
            return []

        async def restore_checkpoint(self, **kwargs: Any) -> CrowdyStudioCheckpointRestoreResult:
            restored = msgspec.structs.replace(
                project, revision=CrowdyStudioProjectRevision(id="6", saved_at="y")
            )
            pre = msgspec.structs.replace(checkpoint, checkpoint_id="c-2", project_revision_id="5")
            return CrowdyStudioCheckpointRestoreResult(project=restored, pre_restore_checkpoint=pre)

    synchronized: list[Any] = []
    controller, _, _ = await studio(
        project,
        synchronization_provider=Sync(),
        on_project_synchronized=lambda p, s: synchronized.append(s),
    )
    create = CrowdyStudioAtomicFileChange(
        target="SERVER",
        path="src/new.rs",
        operation="CREATE",
        content="// new",
        expected_content_hash="ABSENT",
    )
    stale = CrowdyStudioAtomicFileChange(
        target="SERVER",
        path="src/lib.rs",
        operation="REPLACE",
        content="z",
        expected_content_hash="sha256:wrong",
    )
    with pytest.raises(CrowdyStudioRevisionConflictError, match="content hash changed"):
        await controller.apply_atomic_patch(
            CrowdyStudioAtomicPatchInput(expected_revision_id="4", changes=[stale])
        )
    result = await controller.apply_atomic_patch(
        CrowdyStudioAtomicPatchInput(expected_revision_id="4", changes=[create])
    )
    state = controller.get_state()
    assert result.project.revision.id == "5"
    assert state.project.revision.id == "5"
    assert state.checkpoints == (checkpoint,)
    assert synchronized[0].checkpoint == checkpoint

    with pytest.raises(CrowdyStudioError, match="approval grant"):
        await controller.restore_checkpoint("c-1", "short")
    pre = await controller.restore_checkpoint("c-1", "grant-0123456789")
    assert pre.checkpoint_id == "c-2"
    assert controller.get_state().project.revision.id == "6"

    # Unsaved human edits win over an incoming revision.
    controller.update_file("SERVER", "src/lib.rs", "human")
    incoming = msgspec.structs.replace(
        project, revision=CrowdyStudioProjectRevision(id="7", saved_at="z")
    )
    with pytest.raises(CrowdyStudioRevisionConflictError, match="preempted"):
        controller.synchronize_project(incoming, synchronized[0].__class__("REMOTE"))
    assert controller.get_state().save_state == "CONFLICT"


async def test_github_binding_and_repository_page() -> None:
    calls: list[Any] = []

    class GitHub:
        async def status(self, **kwargs: Any) -> Any:
            calls.append(("status", kwargs))
            return SimpleNamespace(
                connected=True, account_login="me", repository_selection="all", github_sha=None
            )

        async def connect_url(self) -> Any:
            return SimpleNamespace(connect_url="https://github.com/apps/x/installations/new")

        async def bind(self, input: Any) -> Any:
            calls.append(("bind", input))
            return SimpleNamespace(owner="me", repo="quest", branch="main", github_sha="abc1234567")

    controller, _, _ = await studio(github=GitHub())
    await asyncio.sleep(0)
    assert calls[0] == ("status", {"app_id": "42", "project_id": "p-1"})
    assert controller.get_state().github.connected
    url = controller.create_github_repository()
    assert (
        url
        == "https://github.com/new?owner=me&name=quest&description=Crowdy+Studio+mod%3A+Quest&visibility=private"
    )
    assert controller.get_state().github_pending_repo == "me/quest"
    with pytest.raises(CrowdyStudioError, match="owner/repo"):
        await controller.bind_github_repo("quest")
    await controller.bind_github_repo("me/quest@main")
    assert calls[-1] == (
        "bind",
        {
            "appId": "42",
            "projectId": "p-1",
            "owner": "me",
            "repo": "quest",
            "initial": "PUSH_PROJECT",
            "branch": "main",
        },
    )
    assert controller.get_state().github_message.startswith("Pushed the project to me/quest@main")  # type: ignore[union-attr]
    assert await controller.connect_github() == "https://github.com/apps/x/installations/new"


async def test_surfaces_poll_while_visible_and_reference_files_import() -> None:
    controller, provider, _ = await studio(monitor_poll_ms=5)
    controller.set_surface_visible("logs", True)
    await asyncio.sleep(0.03)
    logs = controller.get_state().logs
    assert logs
    assert (logs[0].text, logs[0].level, logs[0].source) == (
        "quest-server up",
        "info",
        "mod",
    )
    controller.set_page_visible(False)
    assert not controller._surface_timers

    reference = provider.library[0]
    await controller.import_reference_file(reference)
    assert controller.get_state().active_file == CrowdyStudioFileRef(
        "PROJECT", "src/helpers.rs", "SERVER"
    )
    saved = await controller.save_project_file_to_library("SERVER", "src/lib.rs")
    assert controller.get_state().personal_library_files[0] == saved
    assert (
        controller.file_content(
            CrowdyStudioFileRef("PERSONAL_LIBRARY", "src/lib.rs", reference_id="l-2")
        )
        == "fn x() {}"
    )

    controller.destroy()
    with pytest.raises(CrowdyStudioError, match="destroyed"):
        await controller.save_now()


async def test_agent_work_brackets() -> None:
    controller, _, _ = await studio()
    work = await controller.prepare_for_agent_work()
    assert (work.project_id, work.project_revision_id) == ("p-1", "4")
    assert controller.get_state().agent_activity == "WORKING"
    controller.update_file("SERVER", "src/lib.rs", "human")
    assert controller.get_state().agent_activity == "PAUSED"
    token = controller.begin_agent_operation()
    controller.cancel_agent_operation()
    with pytest.raises(CrowdyStudioError, match="superseded"):
        await controller.test_draft(token)
    assert controller.get_state().runtime.message == "Agent operation cancelled"
    assert controller.can_target("SERVER", "write")
    limited = CrowdyStudioController(
        project_provider=Provider(make_project()),
        mods=Mods(),
        app_id="1",
        grid_id="2",
        target_permissions={"CLIENT": {"canWrite": True, "canRun": False}},
    )
    assert limited.can_target("CLIENT", "write")
    assert not limited.can_target("CLIENT", "run")
