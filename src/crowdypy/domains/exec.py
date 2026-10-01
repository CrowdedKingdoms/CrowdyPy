"""ck-exec, the hub-and-spoke execution service (``client.exec``), a dev-tier preview.

An app's code runs as **hubs** (stateful nodes, one per key, one handler at a time) and
**spokes** (stateless, replicated) on execution hosts, built from ``ckx-sdk`` crates for
``wasm32-unknown-unknown``. A player reaches any node of the app through one host's gateway:
:meth:`ExecAPI.endpoint` asks the Game API for that host and a connect token, valid for about
a minute, with the app-scoped token of the app as the session token.

:class:`ExecAPI` also builds and deploys an app's nodes (and the starter packs), operates
them (logs, instances, versions, rollback, the kill switch, developer endpoints), and runs
players' mods on the grids they own, with their CLIENT halves: client-side WASM built from a
``crowdy-client-sdk`` crate, which :meth:`ExecAPI.mod_client_artifact_bytes` fetches and
refuses unless its bytes, ABI and capability summary check out.
"""

from __future__ import annotations

import builtins  # ExecModClientArtifactBytes.bytes shadows the builtin in its annotations
import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, TypeGuard

import msgspec

from crowdypy._generated import operations as ops
from crowdypy._generated.enums import ExecModScope
from crowdypy.domains._base import Domain, omit_none, sleep
from crowdypy.errors import CrowdyError, CrowdyProtocolError
from crowdypy.utils import bigint, decode_base64, encode_base64

if TYPE_CHECKING:
    from crowdypy.exec_gateway import AsyncExecConnection

__all__ = [
    "EXEC_CLIENT_ABI_IMPORTS",
    "EXEC_CLIENT_ABI_VERSION",
    "EXEC_STATUSES",
    "CrowdyExecError",
    "ExecAPI",
    "ExecAppStatus",
    "ExecBuild",
    "ExecBuildArtifact",
    "ExecCrate",
    "ExecDeployResult",
    "ExecEndpoint",
    "ExecEndpointStat",
    "ExecGridClientMod",
    "ExecInstance",
    "ExecLogLine",
    "ExecMod",
    "ExecModClient",
    "ExecModClientArtifact",
    "ExecModClientArtifactBytes",
    "ExecModListing",
    "ExecModScope",
    "ExecModSwitch",
    "ExecSourceFile",
    "ExecStarter",
    "ExecStarterPack",
    "ExecStatus",
    "ExecVersion",
    "exec_mod_type",
    "exec_status",
    "is_name_list",
]

ExecStatus = Literal[
    "Ok",
    "AppError",
    "Busy",
    "Moved",
    "NotFound",
    "DeadlineExceeded",
    "Denied",
    "RateLimited",
    "Unavailable",
    "Internal",
    "Trapped",
    "BadRequest",
    "Unknown",
]

#: Call statuses as the gateway sends them, by wire value.
EXEC_STATUSES: Final[tuple[ExecStatus, ...]] = (
    "Ok",
    "AppError",
    "Busy",
    "Moved",
    "NotFound",
    "DeadlineExceeded",
    "Denied",
    "RateLimited",
    "Unavailable",
    "Internal",
    "Trapped",
    "BadRequest",
)

#: The CLIENT ABI version this SDK runs (crowdy-client-sdk ``ABI_VERSION``).
EXEC_CLIENT_ABI_VERSION: Final = 0

#: Every import a ck-exec CLIENT half may have (CLIENT ABI 0), by module.
EXEC_CLIENT_ABI_IMPORTS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "ck": ("log", "now_ms", "state_get", "state_set", "host_call"),
        "wasi_snapshot_preview1": ("random_get",),
    }
)

_RETRYABLE: Final[frozenset[ExecStatus]] = frozenset(
    {"Busy", "Moved", "Unavailable", "RateLimited"}
)
_RETRY_IN = re.compile(r"retry in ([0-9]+) ms")


def exec_status(value: int) -> ExecStatus:
    """The status name for a wire value; ``Unknown`` for one this SDK does not know."""
    return EXEC_STATUSES[value] if 0 <= value < len(EXEC_STATUSES) else "Unknown"


def exec_mod_type(name: str) -> str:
    """The node type players call a mod by: ``mod:<name>``, keyed by its grid id."""
    return f"mod:{name}"


def is_name_list(value: object) -> TypeGuard[list[str]]:
    """A list of strings, as a capability summary's ``hostFunctions`` must be."""
    return isinstance(value, list) and all(isinstance(name, str) for name in value)


class CrowdyExecError(CrowdyError):
    """A call, subscription or connection that ck-exec refused or could not complete.

    ``status`` is the platform's (``AppError`` carries the handler's own message);
    ``retryable`` says whether trying again later can succeed.

    The SDK never retries a ``Busy`` reply. A gateway refuses a player's call over its limit
    (120 calls per 10 s per player and app on a host) as ``Busy`` with a message starting
    ``rate limited``: ``rate_limited`` is then true and ``retry_after_ms`` says how long to
    wait. Calling again sooner is refused again and does not shorten the wait.
    """

    def __init__(
        self, status: ExecStatus, message: str, cause: BaseException | object | None = None
    ) -> None:
        super().__init__(f"{status}: {message}", cause=cause)
        self.status: ExecStatus = status
        self.retryable: bool = status in _RETRYABLE
        #: The caller's call limit refused it (``Busy`` "rate limited ...", or ``RateLimited``).
        self.rate_limited: bool = status == "RateLimited" or (
            status == "Busy" and message.startswith("rate limited")
        )
        retry_in = _RETRY_IN.search(message) if self.rate_limited else None
        #: How long to wait before calling again, when the refusal says (``retry in N ms``).
        self.retry_after_ms: int | None = int(retry_in.group(1)) if retry_in else None


class ExecEndpoint(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """Where to dial an execution host's gateway, and the token to dial it with.

    Its repr never shows the token.
    """

    gateway_url: str
    #: The connect token, bound to the caller, the app and ``host``.
    token: str
    host: str
    #: When the token expires, about a minute after it was issued.
    expires_at: str | None = None

    def __repr__(self) -> str:
        return (
            f"ExecEndpoint(gateway_url={self.gateway_url!r}, host={self.host!r}, "
            f"expires_at={self.expires_at!r}, token=<redacted>)"
        )


class ExecLogLine(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """One guest log line (``ctx.log``); ``level`` is 0 error, 1 warn, 2 info, 3 debug.

    ``flow`` is the call it was written in (32 lowercase hex digits, shared by everything
    that call caused, on any host), or ``None`` outside a call; pass it as ``flow`` to
    :meth:`ExecAPI.logs` to follow it.
    """

    id: str
    node_type: str
    key: str
    level: int
    host: str
    at: str
    text: str
    flow: str | None = None


class ExecInstance(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """An instance the execution manager has placed."""

    instance_id: str
    node_type: str
    key: str
    kind: str
    phase: str
    host: str | None = None
    epoch: int
    since_ms: float
    held_back: str | None = None


class ExecVersion(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A deployed version: ``manifest`` is ``manifest_json`` parsed.

    Both are ``None`` when the version's row is gone.
    """

    version: int
    created_by: str | None = None
    created_at: str
    types: int
    active: bool
    manifest_json: str | None = None
    manifest: dict[str, Any] | None = None


class ExecEndpointStat(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """Calls to one endpoint (a node type's ``method``) over a window, by outcome.

    ``busy`` includes calls refused by the caller's call limit; the latencies are over
    ``timed_calls`` and ``None`` when none was timed.
    """

    node_type: str
    method: str
    calls: float
    app_errors: float
    busy: float
    denied: float
    deadline_exceeded: float
    other_errors: float
    timed_calls: float
    latency_ms_avg: float | None = None
    latency_ms_max: float | None = None
    first_minute: str
    last_minute: str


class ExecAppStatus(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """An app's active version, kill switches and budget pause."""

    active_version: int | None = None
    disabled: bool
    disabled_types: list[str]
    budget_paused: bool


class ExecSourceFile(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """One file of a crate: its path (``Cargo.toml``, ``src/lib.rs``, ...) and content."""

    path: str
    content: str


class ExecCrate(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """One crate to build: its files by path, or as a list."""

    name: str
    files: dict[str, str] | list[ExecSourceFile]


class ExecStarter(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A starter crate, with its files ready for :meth:`ExecAPI.build`."""

    crate: str
    node_type: str
    description: str
    files: list[ExecSourceFile]


class ExecStarterPack(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """The starter crates, and the manifest that deploys them as one app.

    The manifest's types name their crates, for :meth:`ExecAPI.deploy` with the build's id.
    """

    manifest: dict[str, Any]
    starters: list[ExecStarter]


class ExecBuildArtifact(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """One module of a build.

    A CLIENT build's carries its capability summary (``capability_summary``, parsed), the
    hash visitors consent to and its tick interval; a ck-exec module's are ``None``.
    """

    crate: str
    digest: str
    size_bytes: int
    capability_summary_json: str | None = None
    capability_hash: str | None = None
    tick_interval_ms: int | None = None
    capability_summary: dict[str, Any] | None = None


class ExecBuild(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A build, its log and one module per crate.

    ``status`` is ``queued``, ``building``, ``succeeded`` or ``failed``; ``kind`` is ``exec``
    for ck-exec modules and ``client`` for the CLIENT half of a mod.
    """

    build_id: str
    status: str
    kind: str
    log: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    artifacts: list[ExecBuildArtifact]


class ExecMod(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A mod: a player's code on a grid they own.

    Players in the grid call it as the node type ``mod:<name>`` (:func:`exec_mod_type`)
    keyed by the grid id. It runs as its owner while ``enabled`` and ``blocked`` is ``None``.
    """

    mod_id: str
    grid_id: str
    name: str
    owner_id: str
    version: int
    digest: str
    enabled: bool
    listing_id: str | None = None
    blocked: str | None = None
    running: bool
    updated_at: str


class ExecModListing(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A published mod other grid owners may install (no payments).

    The ``client_*`` fields describe the CLIENT half it had when published (``None``
    without one), which an install attaches to the installer's mod;
    ``client_capability_summary`` is that summary parsed, for an installer to review first.
    """

    listing_id: str
    title: str
    description: str | None = None
    publisher_id: str
    source_mod_id: str
    source_version: int
    digest: str
    installs: int
    client_digest: str | None = None
    client_capability_summary_json: str | None = None
    client_capability_hash: str | None = None
    client_tick_interval_ms: int | None = None
    created_at: str
    delisted_at: str | None = None
    client_capability_summary: dict[str, Any] | None = None


class ExecModSwitch(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A rung of the app's mods kill ladder that is off; ``scope`` is an :class:`ExecModScope`."""

    scope: str
    target: str
    reason: str | None = None
    created_by: str | None = None
    created_at: str


class ExecModClient(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """The CLIENT half attached to a mod.

    The mod's grid serves it to visitors who consent to its ``capability_hash`` or trust its
    author; ``capability_summary`` is ``capability_summary_json`` parsed.
    """

    mod_id: str
    grid_id: str
    name: str
    owner_id: str
    client_version: int
    digest: str
    size_bytes: int
    capability_summary_json: str
    capability_hash: str
    tick_interval_ms: int
    updated_at: str
    capability_summary: dict[str, Any] | None = None


class ExecGridClientMod(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A CLIENT half a grid serves, with the caller's consent and trust in its author.

    ``capability_summary`` and ``author_capability_summary`` are the two JSON fields parsed
    (``None`` when they do not parse); the author's is the union a one-per-author trust
    prompt shows.
    """

    mod_id: str
    name: str
    grid_id: str
    author_id: str
    listing_id: str | None = None
    client_version: int
    digest: str
    capability_summary_json: str
    capability_hash: str
    tick_interval_ms: int
    caller_consented: bool
    author_capability_summary_json: str
    author_capability_hash: str
    caller_trusts_author: bool
    updated_at: str
    capability_summary: dict[str, Any] | None = None
    author_capability_summary: dict[str, Any] | None = None


class ExecModClientArtifact(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A served CLIENT half's module as the Game API returns it.

    ``wasm_base64`` is the module, ``digest`` what to check it against, and
    ``fuel_per_dispatch`` (a decimal string) the budget for its ``ck_fuel`` global. Its repr
    shows the module's length, not the module.
    """

    mod_id: str
    name: str
    grid_id: str
    client_version: int
    digest: str
    wasm_base64: str
    size_bytes: int
    capability_summary_json: str
    capability_hash: str
    tick_interval_ms: int
    fuel_per_dispatch: str
    abi_version: int
    capability_summary: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return (
            f"ExecModClientArtifact(mod_id={self.mod_id!r}, name={self.name!r}, "
            f"client_version={self.client_version!r}, digest={self.digest!r}, "
            f"wasm_base64=<{len(self.wasm_base64)} chars>)"
        )


class ExecModClientArtifactBytes(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A CLIENT half's module, decoded and checked by :meth:`ExecAPI.mod_client_artifact_bytes`.

    Run it with ``digest`` as its identity, ``fuel_per_dispatch``, ``tick_interval_ms``, and
    only the host calls in ``capability_summary["hostFunctions"]``. Its repr shows the
    module's length, not the module.
    """

    mod_id: str
    #: The mod's name: its name on the grid event bus.
    name: str
    grid_id: str
    client_version: int
    #: The module; its SHA-256 is ``digest``.
    bytes: builtins.bytes
    #: SHA-256 of ``bytes``, lowercase hex.
    digest: str
    size_bytes: int
    #: Fuel for each dispatch (init, tick, invoke, event).
    fuel_per_dispatch: int
    #: How often to tick it, in milliseconds (16-1000).
    tick_interval_ms: int
    capability_summary_json: str
    #: What the player consented to; its ``hostFunctions`` bound the module's host calls.
    capability_summary: dict[str, Any]
    capability_hash: str
    abi_version: int

    def __repr__(self) -> str:
        return (
            f"ExecModClientArtifactBytes(mod_id={self.mod_id!r}, name={self.name!r}, "
            f"client_version={self.client_version!r}, digest={self.digest!r}, "
            f"bytes=<{len(self.bytes)} bytes>)"
        )


class ExecDeployResult(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    version: int


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _parse_object(text: str | None) -> dict[str, Any] | None:
    """A JSON-object field parsed: ``None`` when empty, not JSON, or not an object."""
    if not text:
        return None
    try:
        # As JSON.parse: NaN and Infinity are not JSON.
        parsed = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _build(payload: Mapping[str, Any]) -> ExecBuild:
    artifacts = [
        {**artifact, "capabilitySummary": _parse_object(artifact.get("capabilitySummaryJson"))}
        for artifact in payload["artifacts"]
    ]
    return msgspec.convert({**payload, "artifacts": artifacts}, ExecBuild)


def _listing(payload: Mapping[str, Any]) -> ExecModListing:
    summary = _parse_object(payload.get("clientCapabilitySummaryJson"))
    return msgspec.convert({**payload, "clientCapabilitySummary": summary}, ExecModListing)


def _source_file(file: ExecSourceFile | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(file, ExecSourceFile):
        return {"path": file.path, "content": file.content}
    return dict(file)


def _crate_input(crate: ExecCrate | Mapping[str, Any]) -> dict[str, Any]:
    name, files = (
        (crate.name, crate.files)
        if isinstance(crate, ExecCrate)
        else (crate["name"], crate["files"])
    )
    if isinstance(files, Mapping):
        return {
            "name": name,
            "files": [{"path": path, "content": content} for path, content in files.items()],
        }
    return {"name": name, "files": [_source_file(file) for file in files]}


def _optional_bigint(value: str | int | None) -> str | None:
    return None if value is None else bigint(value)


class ExecAPI(Domain):
    """ck-exec on the Game API: gateway endpoints for players and developers, building,
    deploying and operating an app's nodes, and players' mods with their CLIENT halves.
    """

    async def developer_endpoint(
        self, app_id: str | int, *, node_type: str | None = None, key: str | None = None
    ) -> ExecEndpoint:
        """A host and a developer connect token for ``app_id``, valid for about a minute.

        The session's calls arrive as ``Caller::Developer`` with your user id and may reach
        any node type, not only ``client`` ones. Requires the org ``manage_compute``
        permission and your own session token, not an app token.
        """
        payload = await self._request(
            ops.EXEC_CONNECT_AS_DEVELOPER,
            omit_none({"appId": bigint(app_id), "nodeType": node_type, "key": key}),
        )
        return msgspec.convert(payload, ExecEndpoint)

    async def connect_as_developer(
        self,
        app_id: str | int,
        *,
        node_type: str | None = None,
        key: str | None = None,
        **options: Any,
    ) -> AsyncExecConnection:
        """A gateway connection as the developer (see :meth:`developer_endpoint`); it redials
        with a fresh token whenever it reconnects."""
        from crowdypy.exec_gateway import AsyncExecConnection

        async def dial() -> ExecEndpoint:
            return await self.developer_endpoint(app_id, node_type=node_type, key=key)

        connection = AsyncExecConnection(dial, **options)
        await connection.connect()
        return connection

    async def logs(
        self,
        app_id: str | int,
        *,
        node_type: str | None = None,
        key: str | None = None,
        max_level: int | None = None,
        before: str | None = None,
        limit: int | None = None,
        flow: str | None = None,
    ) -> list[ExecLogLine]:
        """Guest log lines, newest first, kept for 24 hours.

        ``max_level`` is the least severe level included (0 errors only ... 3 everything, the
        default); ``before`` pages back from a line id; ``limit`` defaults to 100, at most
        500; ``flow`` (a line's ``flow``) follows one call through every hub and host.
        Requires ``view_compute_diagnostics``.
        """
        rows = await self._request(
            ops.EXEC_LOGS,
            omit_none(
                {
                    "appId": bigint(app_id),
                    "nodeType": node_type,
                    "key": key,
                    "maxLevel": max_level,
                    "before": before,
                    "limit": limit,
                    "flow": flow,
                }
            ),
        )
        return msgspec.convert(rows, list[ExecLogLine])

    async def instances(self, app_id: str | int) -> list[ExecInstance]:
        """What the execution manager has placed for the app.

        Requires ``view_compute_diagnostics``.
        """
        rows = await self._request(ops.EXEC_INSTANCES, {"appId": bigint(app_id)})
        return msgspec.convert(rows, list[ExecInstance])

    async def versions(self, app_id: str | int) -> list[ExecVersion]:
        """The app's versions, newest first, with their manifests.

        Requires ``view_compute_diagnostics``.
        """
        rows: list[dict[str, Any]] = await self._request(
            ops.EXEC_VERSIONS, {"appId": bigint(app_id)}
        )
        return [
            msgspec.convert(
                {**row, "manifest": _parse_object(row.get("manifestJson"))}, ExecVersion
            )
            for row in rows
        ]

    async def endpoint_stats(
        self,
        app_id: str | int,
        *,
        node_type: str | None = None,
        since_minutes: int | None = None,
    ) -> list[ExecEndpointStat]:
        """Calls to each endpoint of the app's code by outcome, with latency, most called first.

        Over the last ``since_minutes`` (default 60, at most 10 080: 7 days); each host
        reports a minute once it ends. Requires ``view_compute_diagnostics``.
        """
        rows = await self._request(
            ops.EXEC_ENDPOINT_STATS,
            omit_none(
                {"appId": bigint(app_id), "nodeType": node_type, "sinceMinutes": since_minutes}
            ),
        )
        return msgspec.convert(rows, list[ExecEndpointStat])

    async def status(self, app_id: str | int) -> ExecAppStatus:
        """The active version and the switches. Requires ``view_compute_diagnostics``."""
        payload = await self._request(ops.EXEC_APP_STATUS, {"appId": bigint(app_id)})
        return msgspec.convert(payload, ExecAppStatus)

    async def activate_version(self, app_id: str | int, version: int) -> ExecAppStatus:
        """Make an earlier version active again, a rollback.

        Instances pick it up when they next start. Requires ``manage_compute``.
        """
        payload = await self._request(
            ops.EXEC_ACTIVATE_VERSION, {"appId": bigint(app_id), "version": version}
        )
        return msgspec.convert(payload, ExecAppStatus)

    async def set_enabled(
        self, app_id: str | int, enabled: bool, node_type: str | None = None
    ) -> ExecAppStatus:
        """The kill switch, for the whole app or one node type.

        Off: nothing of it is placed, what runs is persisted and stopped, and calls are
        refused with ``Denied``. Requires ``manage_compute``.
        """
        payload = await self._request(
            ops.EXEC_SET_ENABLED,
            omit_none({"appId": bigint(app_id), "enabled": enabled, "nodeType": node_type}),
        )
        return msgspec.convert(payload, ExecAppStatus)

    async def starters(self, app_id: str | int) -> ExecStarterPack:
        """The starter packs: a world tick (the root hub), a matchmaker, game sessions and an
        NPC and mob engine.

        Build them with :meth:`build` and deploy the build with ``manifest``. Requires
        ``manage_compute``.
        """
        pack: dict[str, Any] = await self._request(ops.EXEC_STARTERS, {"appId": bigint(app_id)})
        manifest = _parse_object(pack["manifestJson"])
        if manifest is None:
            raise CrowdyProtocolError("the starter pack's manifestJson is not a JSON object")
        return msgspec.convert(
            {"manifest": manifest, "starters": pack["starters"]}, ExecStarterPack
        )

    async def build(
        self, app_id: str | int, crates: Sequence[ExecCrate | Mapping[str, Any]]
    ) -> ExecBuild:
        """Build crates into modules on the platform (``ckx-sdk``, ``wasm32-unknown-unknown``).

        No Rust toolchain needed. Returns at once with the build queued; wait with
        :meth:`wait_for_build`, then :meth:`deploy` with its ``build_id``. A mapping crate
        has ``name`` and ``files``. Requires ``manage_compute``.
        """
        payload = await self._request(
            ops.EXEC_BUILD,
            {"input": {"appId": bigint(app_id), "crates": [_crate_input(c) for c in crates]}},
        )
        return _build(payload)

    async def build_status(self, app_id: str | int, build_id: str) -> ExecBuild | None:
        """A build's status, log and modules, or ``None``. Builds are kept for 7 days.

        Requires ``view_compute_diagnostics``.
        """
        payload = await self._request(
            ops.EXEC_BUILD_STATUS, {"appId": bigint(app_id), "buildId": build_id}
        )
        return None if payload is None else _build(payload)

    async def wait_for_build(
        self,
        app_id: str | int,
        build_id: str,
        *,
        interval_ms: float | None = None,
        timeout_ms: float | None = None,
    ) -> ExecBuild:
        """Poll a build every ``interval_ms`` (default 2000) until it succeeds or fails.

        Returns it either way; a failed build's ``log`` says why. Raises
        :class:`~crowdypy.errors.CrowdyError` when there is no such build, or when it is not
        done in ``timeout_ms`` (default 10 minutes).
        """
        until = time.monotonic() + (600_000 if timeout_ms is None else timeout_ms) / 1000
        while True:
            build = await self.build_status(app_id, build_id)
            if build is None:
                raise CrowdyError(f"no build {build_id} in app {app_id}")
            if build.status in ("succeeded", "failed"):
                return build
            if time.monotonic() > until:
                raise CrowdyError(f"build {build_id} is still {build.status}")
            await sleep((2_000 if interval_ms is None else interval_ms) / 1000)

    async def mod_starter(self, app_id: str | int) -> ExecStarter:
        """The mod starter (``grid-mod``), a crate to build with :meth:`mod_build`.

        Requires access to the app.
        """
        payload = await self._request(ops.EXEC_MOD_STARTER, {"appId": bigint(app_id)})
        return msgspec.convert(payload, ExecStarter)

    async def mod_build(self, app_id: str | int, crate: ExecCrate | Mapping[str, Any]) -> ExecBuild:
        """Build a mod from one ``ckx-sdk`` crate, as :meth:`build` builds a developer's.

        Returns at once; wait with :meth:`wait_for_mod_build`, then :meth:`mod_deploy`. One
        build at a time per player. Requires ``write_server_code`` in the app.
        """
        payload = await self._request(
            ops.EXEC_MOD_BUILD, {"appId": bigint(app_id), "crate": _crate_input(crate)}
        )
        return _build(payload)

    async def mod_build_status(self, app_id: str | int, build_id: str) -> ExecBuild:
        """A mod build of yours: a mod's (``kind`` ``exec``) or a CLIENT half's (``client``)."""
        payload = await self._request(
            ops.EXEC_MOD_BUILD_STATUS, {"appId": bigint(app_id), "buildId": build_id}
        )
        return _build(payload)

    async def wait_for_mod_build(
        self,
        app_id: str | int,
        build_id: str,
        *,
        interval_ms: float | None = None,
        timeout_ms: float | None = None,
    ) -> ExecBuild:
        """Poll a mod build, server or CLIENT, until it succeeds or fails, like
        :meth:`wait_for_build`.
        """
        until = time.monotonic() + (600_000 if timeout_ms is None else timeout_ms) / 1000
        while True:
            build = await self.mod_build_status(app_id, build_id)
            if build.status in ("succeeded", "failed"):
                return build
            if time.monotonic() > until:
                raise CrowdyError(f"build {build_id} is still {build.status}")
            await sleep((2_000 if interval_ms is None else interval_ms) / 1000)

    async def mod_deploy(
        self, app_id: str | int, grid_id: str | int, name: str, build_id: str
    ) -> ExecMod:
        """Deploy a mod build of yours to a grid you own.

        A new mod starts switched off; a running one restarts on the new version. Requires
        being the grid's owner and ``write_server_code`` on the app tier and the grid.
        """
        payload = await self._request(
            ops.EXEC_MOD_DEPLOY,
            {"appId": bigint(app_id), "gridId": bigint(grid_id), "name": name, "buildId": build_id},
        )
        return msgspec.convert(payload, ExecMod)

    async def mod_set_enabled(
        self, app_id: str | int, grid_id: str | int, name: str, enabled: bool
    ) -> ExecMod:
        """Switch a mod on your grid on or off.

        On, it runs as you once the app's code admission admits it. Requires
        ``run_server_code`` on the app tier and the grid.
        """
        payload = await self._request(
            ops.EXEC_MOD_SET_ENABLED,
            {"appId": bigint(app_id), "gridId": bigint(grid_id), "name": name, "enabled": enabled},
        )
        return msgspec.convert(payload, ExecMod)

    async def mod_delete(self, app_id: str | int, grid_id: str | int, name: str) -> bool:
        """Stop and remove a mod on your grid, with its state."""
        return bool(
            await self._request(
                ops.EXEC_MOD_DELETE,
                {"appId": bigint(app_id), "gridId": bigint(grid_id), "name": name},
            )
        )

    async def mods(self, app_id: str | int, grid_id: str | int) -> list[ExecMod]:
        """A grid's mods, which players in it call as ``mod:<name>`` keyed by the grid id."""
        rows = await self._request(
            ops.EXEC_MODS, {"appId": bigint(app_id), "gridId": bigint(grid_id)}
        )
        return msgspec.convert(rows, list[ExecMod])

    async def my_mods(self, app_id: str | int) -> list[ExecMod]:
        """Your mods in the app, on every grid."""
        rows = await self._request(ops.EXEC_MY_MODS, {"appId": bigint(app_id)})
        return msgspec.convert(rows, list[ExecMod])

    async def mod_logs(
        self,
        app_id: str | int,
        grid_id: str | int,
        name: str,
        *,
        max_level: int | None = None,
        before: str | None = None,
        limit: int | None = None,
    ) -> list[ExecLogLine]:
        """A mod of yours' guest log lines, newest first, kept for 24 hours.

        ``max_level``, ``before`` and ``limit`` work as in :meth:`logs`.
        """
        rows = await self._request(
            ops.EXEC_MOD_LOGS,
            omit_none(
                {
                    "appId": bigint(app_id),
                    "gridId": bigint(grid_id),
                    "name": name,
                    "maxLevel": max_level,
                    "before": before,
                    "limit": limit,
                }
            ),
        )
        return msgspec.convert(rows, list[ExecLogLine])

    async def mod_publish(
        self,
        app_id: str | int,
        grid_id: str | int,
        name: str,
        title: str,
        description: str | None = None,
    ) -> ExecModListing:
        """Publish a mod of yours, at its current version, for other grid owners to install.

        ``title`` is 1-80 characters and ``description`` at most 2000.
        """
        payload = await self._request(
            ops.EXEC_MOD_PUBLISH,
            omit_none(
                {
                    "appId": bigint(app_id),
                    "gridId": bigint(grid_id),
                    "name": name,
                    "title": title,
                    "description": description,
                }
            ),
        )
        return _listing(payload)

    async def mod_listings(self, app_id: str | int) -> list[ExecModListing]:
        """The app's listed mods, most installed first, each with its CLIENT half, if any."""
        rows: list[dict[str, Any]] = await self._request(
            ops.EXEC_MOD_LISTINGS, {"appId": bigint(app_id)}
        )
        return [_listing(row) for row in rows]

    async def mod_unpublish(self, app_id: str | int, listing_id: str | int) -> bool:
        """Delist a listing you published; installed copies keep running."""
        return bool(
            await self._request(
                ops.EXEC_MOD_UNPUBLISH, {"appId": bigint(app_id), "listingId": bigint(listing_id)}
            )
        )

    async def mod_install(
        self, app_id: str | int, grid_id: str | int, name: str, listing_id: str | int
    ) -> ExecMod:
        """Install a listing onto a grid you own as your own mod, switched off.

        It comes with the listing's CLIENT half if it has one; visitors, you too, consent to
        that CLIENT half afresh.
        """
        payload = await self._request(
            ops.EXEC_MOD_INSTALL,
            {
                "appId": bigint(app_id),
                "gridId": bigint(grid_id),
                "name": name,
                "listingId": bigint(listing_id),
            },
        )
        return msgspec.convert(payload, ExecMod)

    async def app_mods(
        self,
        app_id: str | int,
        *,
        grid_id: str | int | None = None,
        owner_id: str | int | None = None,
    ) -> list[ExecMod]:
        """The app's mods on one grid or of one owner, or all of them (at most 1000).

        Requires ``view_compute_diagnostics``.
        """
        rows = await self._request(
            ops.EXEC_APP_MODS,
            omit_none(
                {
                    "appId": bigint(app_id),
                    "gridId": _optional_bigint(grid_id),
                    "ownerId": _optional_bigint(owner_id),
                }
            ),
        )
        return msgspec.convert(rows, list[ExecMod])

    async def mod_switches(self, app_id: str | int) -> list[ExecModSwitch]:
        """The app's mod switches that are off. Requires ``view_compute_diagnostics``."""
        rows = await self._request(ops.EXEC_MOD_SWITCHES, {"appId": bigint(app_id)})
        return msgspec.convert(rows, list[ExecModSwitch])

    async def mod_set_switch(
        self,
        app_id: str | int,
        scope: ExecModScope | str,
        off: bool,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> list[ExecModSwitch]:
        """The mods kill ladder: switch off (or on) what ``scope`` names.

        ``target`` is the mod id for ``MOD``, the player's user id for ``PLAYER``, the grid id
        for ``GRID``, the listing id for ``LISTING``, and none for ``ALL``; ``reason`` (at
        most 200 characters) reaches the players whose calls it refuses. Returns the switches
        that are off. Requires ``manage_compute``.
        """
        rows = await self._request(
            ops.EXEC_MOD_SET_SWITCH,
            omit_none(
                {
                    "appId": bigint(app_id),
                    "scope": scope,
                    "off": off,
                    "target": target,
                    "reason": reason,
                }
            ),
        )
        return msgspec.convert(rows, list[ExecModSwitch])

    async def mod_client_build(
        self, app_id: str | int, crate: ExecCrate | Mapping[str, Any]
    ) -> ExecBuild:
        """Build the CLIENT half of a mod from one ``crowdy-client-sdk`` crate.

        Compiled for ``wasm32-unknown-unknown`` in the build sandbox, fuel-metered and
        optimized there, checked against the CLIENT ABI and at most 512 KiB, with its
        capability summary derived from the module. Its ``Cargo.toml`` may have only
        ``[package]``, ``[lib]`` as a cdylib, ``[dependencies]`` on ``crowdy-client-sdk``,
        ``serde`` and ``serde_json``, and ``[package.metadata.crowdy] tick_interval_ms``.
        Returns at once with the build queued (``kind`` ``client``); wait with
        :meth:`wait_for_mod_build`, then attach it with :meth:`mod_client_deploy`. One build,
        server or CLIENT, at a time per player. Requires ``write_client_code`` in the app.
        """
        payload = await self._request(
            ops.EXEC_MOD_CLIENT_BUILD, {"appId": bigint(app_id), "crate": _crate_input(crate)}
        )
        return _build(payload)

    async def mod_client_deploy(
        self, app_id: str | int, grid_id: str | int, name: str, build_id: str
    ) -> ExecModClient:
        """Attach a succeeded CLIENT build of yours to your mod ``name`` on a grid you own.

        It replaces the CLIENT half the mod had, and ``client_version`` rises by one; a
        visitor's consent carries over only while the capability hash is unchanged. The mod
        must exist and run as you. Requires being the grid's owner, ``write_client_code`` on
        the app tier and the grid, and the app's code admission admitting the new version.
        """
        payload: dict[str, Any] = await self._request(
            ops.EXEC_MOD_CLIENT_DEPLOY,
            {"appId": bigint(app_id), "gridId": bigint(grid_id), "name": name, "buildId": build_id},
        )
        summary = _parse_object(payload.get("capabilitySummaryJson"))
        return msgspec.convert({**payload, "capabilitySummary": summary}, ExecModClient)

    async def mod_client_delete(self, app_id: str | int, grid_id: str | int, name: str) -> bool:
        """Detach the CLIENT half of a mod on your grid, with every visitor's consent to it.

        The mod keeps running.
        """
        return bool(
            await self._request(
                ops.EXEC_MOD_CLIENT_DELETE,
                {"appId": bigint(app_id), "gridId": bigint(grid_id), "name": name},
            )
        )

    async def grid_client_mods(
        self, app_id: str | int, grid_id: str | int
    ) -> list[ExecGridClientMod]:
        """The CLIENT halves a grid serves.

        Those of its mods that are switched on, not stopped by the kill ladder, running as
        the grid's owner and admitted, each with whether you consented to it and whether you
        trust its author. Prompt once per author (:meth:`trust_author`) or per CLIENT half
        (:meth:`consent_client_mod`), fetch with :meth:`mod_client_artifact_bytes`, cache by
        ``digest``, and poll this to stop the CLIENT halves no longer listed or whose digest
        changed. Requires access to the app.
        """
        rows: list[dict[str, Any]] = await self._request(
            ops.EXEC_GRID_CLIENT_MODS, {"appId": bigint(app_id), "gridId": bigint(grid_id)}
        )
        return [
            msgspec.convert(
                {
                    **row,
                    "capabilitySummary": _parse_object(row.get("capabilitySummaryJson")),
                    "authorCapabilitySummary": _parse_object(
                        row.get("authorCapabilitySummaryJson")
                    ),
                },
                ExecGridClientMod,
            )
            for row in rows
        ]

    async def consent_client_mod(
        self, app_id: str | int, mod_id: str, capability_hash: str
    ) -> bool:
        """Consent to run one mod's CLIENT half at ``capability_hash``, as
        :meth:`grid_client_mods` showed it.

        A CLIENT half whose capabilities change carries a new hash, and the consent stops
        holding until you consent again; a hash that is not the current one is refused as
        ``CONFLICT``. Requires access to the app.
        """
        return bool(
            await self._request(
                ops.EXEC_CONSENT_CLIENT_MOD,
                {"appId": bigint(app_id), "modId": mod_id, "capabilityHash": capability_hash},
            )
        )

    async def trust_author(
        self, app_id: str | int, grid_id: str | int, author_id: str | int, capability_hash: str
    ) -> bool:
        """Trust one author's CLIENT halves on a grid you stand in, at the hash of their union.

        The trust covers their CLIENT halves there while the union is no wider, and consents
        to each current one at its own hash. A hash that is not the current one is
        ``CONFLICT``; not standing in the grid, or an author with nothing served there, is
        ``NOT_FOUND``. Requires access to the app.
        """
        return bool(
            await self._request(
                ops.EXEC_TRUST_AUTHOR,
                {
                    "appId": bigint(app_id),
                    "gridId": bigint(grid_id),
                    "authorId": bigint(author_id),
                    "capabilityHash": capability_hash,
                },
            )
        )

    async def revoke_client_mod_consent(self, app_id: str | int, mod_id: str) -> bool:
        """Take back your consent to one mod's CLIENT half; ``True`` when you had consented.

        While you trust its author on its grid it is still served to you:
        :meth:`revoke_author_trust` takes that back. Needs only the app's app-scoped token.
        """
        return bool(
            await self._request(
                ops.EXEC_REVOKE_CLIENT_MOD_CONSENT, {"appId": bigint(app_id), "modId": mod_id}
            )
        )

    async def revoke_author_trust(
        self, app_id: str | int, grid_id: str | int, author_id: str | int
    ) -> bool:
        """Stop trusting an author on a grid, and take back your consent to each of their
        CLIENT halves there; ``True`` when anything was taken back.

        Works from anywhere, not only inside the grid. Needs only the app's app-scoped token.
        """
        return bool(
            await self._request(
                ops.EXEC_REVOKE_AUTHOR_TRUST,
                {"appId": bigint(app_id), "gridId": bigint(grid_id), "authorId": bigint(author_id)},
            )
        )

    async def mod_client_artifact(self, app_id: str | int, mod_id: str) -> ExecModClientArtifact:
        """A served CLIENT half's module, base64, with what a runtime needs to run it.

        Served only to a player holding ``run_client_code`` in the app, standing in the mod's
        grid now, who consented to it at its current hash or trusts its author at a union no
        wider; every refusal is ``NOT_FOUND``. At most 12 fetches a minute per player and mod
        on each API instance (``RATE_LIMITED``): the module never changes for its digest, so
        cache it by ``digest``.
        """
        payload: dict[str, Any] = await self._request(
            ops.EXEC_MOD_CLIENT_ARTIFACT, {"appId": bigint(app_id), "modId": mod_id}
        )
        summary = _parse_object(payload.get("capabilitySummaryJson"))
        return msgspec.convert({**payload, "capabilitySummary": summary}, ExecModClientArtifact)

    async def mod_client_artifact_bytes(
        self, app_id: str | int, mod_id: str
    ) -> ExecModClientArtifactBytes:
        """:meth:`mod_client_artifact` decoded and checked: the module's bytes, their SHA-256
        recomputed, and the fuel budget as an ``int``.

        A module built for a CLIENT ABI other than :data:`EXEC_CLIENT_ABI_VERSION`, a
        capability summary that does not parse, or bytes that differ from ``digest`` raise
        :class:`~crowdypy.errors.CrowdyProtocolError` and are never returned. ``NOT_FOUND``
        and ``RATE_LIMITED`` pass through as the GraphQL errors they are.
        """
        artifact = await self.mod_client_artifact(app_id, mod_id)
        if artifact.abi_version != EXEC_CLIENT_ABI_VERSION:
            raise CrowdyProtocolError(
                f"CLIENT half of mod {artifact.mod_id} is built for CLIENT ABI "
                f"{artifact.abi_version}; this SDK runs ABI {EXEC_CLIENT_ABI_VERSION}"
            )
        summary = artifact.capability_summary
        if summary is None or not is_name_list(summary.get("hostFunctions")):
            raise CrowdyProtocolError(
                f"CLIENT half of mod {artifact.mod_id}: its capability summary does not parse, "
                "so nothing bounds its host calls"
            )
        try:
            module = decode_base64(artifact.wasm_base64)
        except ValueError as exc:
            raise CrowdyProtocolError(
                f"CLIENT half of mod {artifact.mod_id}: its module is not base64"
            ) from exc
        digest = artifact.digest.lower()
        actual = hashlib.sha256(module).hexdigest()
        if actual != digest:
            raise CrowdyProtocolError(
                f"CLIENT half of mod {artifact.mod_id}: the module's SHA-256 is {actual}, "
                f"not the digest {digest} it was served with"
            )
        try:
            fuel_per_dispatch = int(artifact.fuel_per_dispatch)
        except ValueError as exc:
            raise CrowdyProtocolError(
                f"CLIENT half of mod {artifact.mod_id}: fuelPerDispatch "
                f"{artifact.fuel_per_dispatch!r} is not an integer"
            ) from exc
        return ExecModClientArtifactBytes(
            mod_id=artifact.mod_id,
            name=artifact.name,
            grid_id=artifact.grid_id,
            client_version=artifact.client_version,
            bytes=module,
            digest=digest,
            size_bytes=artifact.size_bytes,
            fuel_per_dispatch=fuel_per_dispatch,
            tick_interval_ms=artifact.tick_interval_ms,
            capability_summary_json=artifact.capability_summary_json,
            capability_summary=summary,
            capability_hash=artifact.capability_hash,
            abi_version=artifact.abi_version,
        )

    async def endpoint(
        self, app_id: str | int, *, node_type: str | None = None, key: str | None = None
    ) -> ExecEndpoint:
        """A host for this player and its connect token, valid for about a minute.

        The session token must be the app-scoped token of ``app_id``. With ``node_type``
        (and ``key``) the host is the one running that instance, placing it if needed.
        """
        payload = await self._request(
            ops.EXEC_CONNECT,
            omit_none({"appId": bigint(app_id), "nodeType": node_type, "key": key}),
        )
        return msgspec.convert(payload, ExecEndpoint)

    async def connect(
        self,
        app_id: str | int,
        *,
        node_type: str | None = None,
        key: str | None = None,
        **options: Any,
    ) -> AsyncExecConnection:
        """A gateway connection as this player (see :meth:`endpoint`); it redials with a fresh
        token whenever it reconnects. ``options``: ``call_timeout``, ``reconnect``,
        ``open_timeout``."""
        from crowdypy.exec_gateway import AsyncExecConnection

        async def dial() -> ExecEndpoint:
            return await self.endpoint(app_id, node_type=node_type, key=key)

        connection = AsyncExecConnection(dial, **options)
        await connection.connect()
        return connection

    async def deploy(
        self,
        app_id: str | int,
        root: str,
        types: Mapping[str, Mapping[str, Any]],
        build_id: str | None = None,
    ) -> ExecDeployResult:
        """Deploy a new version of the app's nodes and make it active.

        ``root`` is the root hub's type and ``types`` each node type's manifest settings
        (``kind``, ``parent``, ``client``, ``calls``, ``persist_every_ms``, ...). A type gives
        its compiled module as ``wasm`` (bytes), or names a ``crate`` of ``build_id``, whose
        modules the platform already holds; each distinct module is uploaded once, by its
        SHA-256. Running instances pick the version up when they next start. Requires the
        org ``manage_compute`` permission.
        """
        manifest_types: dict[str, Any] = {}
        artifacts: dict[str, str] = {}
        for name, node_type in types.items():
            spec = {field: value for field, value in node_type.items() if field != "wasm"}
            wasm = node_type.get("wasm")
            if wasm is None:
                if not spec.get("crate") or not build_id:
                    raise CrowdyError(
                        f"type '{name}' needs its wasm, or a crate of the deploy's buildId"
                    )
                manifest_types[name] = spec
                continue
            module = bytes(wasm)
            digest = hashlib.sha256(module).hexdigest()
            if digest not in artifacts:
                artifacts[digest] = encode_base64(module)
            manifest_types[name] = {**spec, "digest": digest}
        payload = await self._request(
            ops.EXEC_DEPLOY,
            {
                "input": omit_none(
                    {
                        "appId": bigint(app_id),
                        # As JSON.stringify: compact, keys in insertion order, non-ASCII as is.
                        "manifestJson": json.dumps(
                            {"root": root, "types": manifest_types},
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        "artifacts": [
                            {"digest": digest, "wasmBase64": wasm_base64}
                            for digest, wasm_base64 in artifacts.items()
                        ],
                        "buildId": build_id,
                    }
                )
            },
        )
        return msgspec.convert(payload, ExecDeployResult)
