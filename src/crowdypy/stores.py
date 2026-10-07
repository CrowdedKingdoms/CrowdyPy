"""World Stores: the data structures every game otherwise writes by hand, driven by one
replication connection and one tick (CrowdyJS's ``@crowdedkingdoms/crowdyjs/stores``).

Ingest is native. :meth:`WorldSessionCore.tick` runs CrowdyCPP's ``WorldSession`` on the
calling thread: every notification since the last tick lands in the stores (your actor's
echoes and errors, everyone else's updates, voxel edits, inboxes, events) and your actor's
send loop runs. Then the callbacks you registered fire, in Python, from the queue the tick
filled. Nothing a tick does waits on the network: host election, save state, avatar state
and chunk persistence are GraphQL calls these classes make themselves, started by the
session's timers.

    session = create_world_session(game, app_id, actor_codec=pose)  # game.udp connected
    session.self.join((0, 0, 0), pose.encode(my_pose))
    session.actors.on_join(lambda actor: print("joined", actor.uuid))
    asyncio.create_task(session.run())                              # tick 60 times a second
"""

from __future__ import annotations

import builtins
import contextlib
import functools
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from crowdypy import _native
from crowdypy._concurrency import run_soon
from crowdypy.codecs import StateCodec, json_codec, raw_codec
from crowdypy.domains._base import sleep
from crowdypy.errors import CrowdyError
from crowdypy.utils import decode_base64, encode_base64
from crowdypy.wire import DecayRate, ErrorCode

if TYPE_CHECKING:
    from crowdypy.client import AsyncCrowdyClient
    from crowdypy.replication import AsyncReplicationConnection, Notification

__all__ = [
    "ActorInbox",
    "ActorSnapshot",
    "AttributedError",
    "AvatarStateStore",
    "CachedChunk",
    "ChannelInbox",
    "ChunkStore",
    "ChunkWriteBackFailure",
    "ErrorStore",
    "EventRouter",
    "HostTracker",
    "InboxMessage",
    "LocalActorStore",
    "RemoteActor",
    "RemoteActorLane",
    "RemoteActorStore",
    "RoutedEvent",
    "SaveStateStore",
    "WorldSessionCore",
    "create_world_session",
]

_s: Any = _native.session
CHUNK_VOLUME = 4096
Coord = tuple[int, int, int]


def _coord(chunk: Sequence[int | str] | Mapping[str, Any]) -> Coord:
    if isinstance(chunk, Mapping):
        return int(chunk["x"]), int(chunk["y"]), int(chunk["z"])
    return int(chunk[0]), int(chunk[1]), int(chunk[2])


def _now_ms() -> int:
    return int(time.time() * 1000)


def _chunk_input(coord: Coord) -> dict[str, str]:
    return {"x": str(coord[0]), "y": str(coord[1]), "z": str(coord[2])}


def _distance(a: Coord, b: Coord) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def _decode(codec: StateCodec[Any], data: bytes, counter: list[int]) -> Any:
    try:
        return codec.decode(data)
    except Exception:
        counter[0] += 1
        return None


# ------------------------------------------------------------------ the session


@dataclass
class _Timer:
    interval_ms: int
    job: Callable[[], Any]
    due_ms: int = 0


class WorldSessionCore:
    """One app's world over one connection: the stores, the tick, timers and dispose hooks."""

    def __init__(
        self,
        client: AsyncCrowdyClient,
        app_id: str | int,
        connection: AsyncReplicationConnection,
        *,
        actor_uuid: str | None = None,
        actor_codec: StateCodec[Any] = raw_codec,
        send_hz: int = 5,
        keyframe_interval_ms: int = 3000,
        heartbeat_interval_ms: int = 2000,
        distance: int = 8,
        decay: int = DecayRate.EXPONENTIAL,
        stale_after_ms: int = 12000,
        history_size: int = 2,
        reap_interval_ms: int = 1000,
        voxel_send_distance: int = 8,
        inbox_codec: StateCodec[Any] = raw_codec,
        event_codec: StateCodec[Any] = raw_codec,
        chunk_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.app_id = str(app_id)
        self.client = client
        self.connection = connection
        self._native: Any = _s.WorldSession(
            connection.native, self.app_id, actor_uuid or "", send_hz, keyframe_interval_ms,
            heartbeat_interval_ms, distance, int(decay), stale_after_ms, history_size,
            reap_interval_ms, voxel_send_distance,
        )  # fmt: skip
        self._timers: list[_Timer] = []
        self._dispose: list[Callable[[], Any]] = []
        self._send_tracker: Callable[[Mapping[str, Any]], Any] | None = None
        self._disposed = False
        self.self = LocalActorStore(self, actor_codec)
        self.actors = RemoteActorStore(self, actor_codec)
        self.chunks = ChunkStore(self, **(chunk_options or {}))
        self.errors = ErrorStore(self)
        self.direct_inbox = ActorInbox(self, inbox_codec)
        self.channel_inbox = ChannelInbox(self, inbox_codec)
        self.events = EventRouter(self, event_codec)
        #: Attached by :func:`create_world_session` (``host=``, ``save=``, ``avatar=``).
        self.host: HostTracker | None = None
        self.save: SaveStateStore | None = None
        self.avatar: AvatarStateStore | None = None

    @property
    def actor_uuid(self) -> str:
        """Your actor's 32-character id (minted unless one was configured)."""
        result: str = self._native.actor_uuid
        return result

    def tick(self) -> None:
        """Drive everything once: drain into the stores, run the send loop and reaping, fire
        the callbacks the tick queued, start any timer that is due. Call once per frame."""
        if self._disposed:
            return
        if self._native.tick():
            for event in self._native.drain_events():
                self._dispatch(event)
        if self._timers:
            now = _now_ms()
            for timer in self._timers:
                if now >= timer.due_ms:
                    timer.due_ms = now + timer.interval_ms
                    run_soon(timer.job)

    async def run(self, interval: float = 1 / 60) -> None:
        """Tick every ``interval`` seconds until :meth:`dispose`."""
        while not self._disposed:
            self.tick()
            await sleep(interval)

    def every(self, interval_ms: int, job: Callable[[], Any]) -> Callable[[], None]:
        """Run ``job`` (a coroutine function in the async client) every ``interval_ms`` from
        the tick, without waiting for it. Returns the cancel function."""
        timer = _Timer(interval_ms, job, _now_ms() + interval_ms)
        self._timers.append(timer)

        def cancel() -> None:
            if timer in self._timers:
                self._timers.remove(timer)

        return cancel

    def on(self, name: str, handler: Callable[[Notification], Any]) -> Callable[[], None]:
        """Per-event handlers for what the stores do not keep (``audio``, ``video``,
        ``text``, ``status``); the stores hold the rest. Returns the unsubscribe function."""
        return self.connection.subscribe({name: handler})

    def on_dispose(self, cleanup: Callable[[], Any]) -> None:
        self._dispose.append(cleanup)

    def set_send_tracker(self, sink: Callable[[Mapping[str, Any]], Any]) -> None:
        """Observe every send the stores make (kind, sequence, time, uuid, detail)."""
        self._send_tracker = sink

    def track_send(self, record: Mapping[str, Any]) -> None:
        if self._send_tracker is not None:
            self._send_tracker(record)

    def dispose(self) -> None:
        """Stop the timers, run the dispose hooks and release the native session (the
        connection stays open; close it on ``client.udp``)."""
        if self._disposed:
            return
        self._disposed = True
        self._timers.clear()
        for cleanup in reversed(self._dispose):
            cleanup()
        self._native.close()

    def _dispatch(self, event: tuple[Any, ...]) -> None:
        kind = event[0]
        if kind in (_s.JOIN, _s.LEAVE, _s.UPDATE):
            self.actors._event(event)
        elif kind == _s.CHUNK_CHANGED:
            self.chunks._changed(_coord(event[3]))
        elif kind == _s.ERROR:
            self.errors._event(event)
        elif kind == _s.CHANNEL_MESSAGE:
            self.channel_inbox._event(event)
        elif kind == _s.DIRECT_MESSAGE:
            self.direct_inbox._event(event)
        elif kind == _s.EVENT:
            self.events._event(event)


class _Listeners:
    def __init__(self) -> None:
        self._items: list[Callable[..., Any]] = []

    def add(self, listener: Callable[..., Any]) -> Callable[[], None]:
        self._items.append(listener)

        def remove() -> None:
            if listener in self._items:
                self._items.remove(listener)

        return remove

    def __bool__(self) -> bool:
        return bool(self._items)

    def fire(self, *args: Any) -> None:
        for listener in list(self._items):
            listener(*args)


# ------------------------------------------------------------------ your actor


class LocalActorStore:
    """Your actor: the first update joins, then a send loop at ``send_hz`` resends on change,
    sends a keyframe at least every ``keyframe_interval_ms`` and a cheap heartbeat while
    idle. ``status`` is ``idle``, ``pending``, ``acked`` or ``error``."""

    _STATUS = ("idle", "pending", "acked", "error")

    def __init__(self, session: WorldSessionCore, codec: StateCodec[Any]) -> None:
        self._session = session
        self._codec = codec

    @property
    def chunk(self) -> Coord:
        result: Coord = self._session._native.self_chunk()
        return result

    @property
    def joined(self) -> bool:
        result: bool = self._session._native.self_joined()
        return result

    @property
    def state(self) -> Any:
        """Your current state, decoded with the session's actor codec."""
        return self._codec.decode(self._session._native.self_state())

    @property
    def status(self) -> str:
        return self._STATUS[self._session._native.self_status()]

    def join(self, chunk: Sequence[int | str], state: Any = b"\x00") -> None:
        """Enter ``chunk`` with an initial state (encoded with the actor codec)."""
        x, y, z = _coord(chunk)
        self._session._native.join(x, y, z, self._codec.encode(state))

    def set_state(self, state: Any) -> None:
        """Replace your state; the send loop sends it on its next slot."""
        self._session._native.set_state(self._codec.encode(state))

    def patch_state(self, patch: Mapping[str, Any]) -> None:
        """Merge ``patch`` into a mapping state and set the result."""
        current = self.state
        self.set_state({**(current if isinstance(current, Mapping) else {}), **patch})

    def move_to(self, chunk: Sequence[int | str]) -> None:
        """Move to ``chunk`` and send now."""
        x, y, z = _coord(chunk)
        self._session._native.move_to(x, y, z)

    def refresh(self) -> None:
        """Send your current state now (outside the cadence)."""
        self._session._native.refresh()

    def send_now(self) -> None:
        """Send your current state now; :meth:`refresh` under CrowdyJS's other name."""
        self.refresh()

    def last_ack(self) -> dict[str, Any] | None:
        """The latest echo of your actor: ``sequence``, ``server_epoch_ms``,
        ``received_at_ms``, ``state``."""
        ack = self._session._native.last_ack()
        if ack is None:
            return None
        return {
            "sequence": ack[0],
            "server_epoch_ms": ack[1],
            "received_at_ms": ack[2],
            "state": ack[3],
        }

    def last_sent(self) -> dict[str, Any] | None:
        sent = self._session._native.last_sent()
        if sent is None:
            return None
        return {
            "state": sent[0],
            "chunk": sent[1],
            "sequence": sent[2],
            "sent_at_ms": sent[3],
            "reason": sent[4],
        }

    def last_error(self) -> dict[str, Any] | None:
        err = self._session._native.last_error()
        if err is None:
            return None
        return {
            "status": err[0],
            "server_code": err[1],
            "sequence": err[2],
            "received_at_ms": err[3],
        }


# ------------------------------------------------------------------ everyone else


@dataclass(frozen=True, slots=True)
class RemoteActor:
    """Another actor as the store holds it. ``value`` is ``state`` decoded with the
    session's actor codec (``None`` when it did not decode)."""

    uuid: str
    chunk: Coord
    last_seen_ms: int
    last_server_epoch_ms: int
    state: bytes
    samples: tuple[tuple[bytes, int, int], ...] = ()
    value: Any = None


class ActorSnapshot:
    """Every actor of a lane at one moment, in zero-copy columns (numpy arrays when numpy is
    installed). Decode all states at once with ``codec.decode_many(snap.state_offsets,
    snap.state_data)`` (:class:`crowdypy.codecs.StructCodec`)."""

    __slots__ = ("_native",)

    def __init__(self, native: Any) -> None:
        self._native = native

    def __len__(self) -> int:
        return len(self._native)

    @staticmethod
    def _view(view: Any) -> Any:
        from crowdypy.replication import _column

        return _column(view)

    @property
    def uuids(self) -> Any:
        return self._view(self._native.uuids)

    @property
    def chunks(self) -> Any:
        return self._view(self._native.chunks)

    @property
    def epoch_ms(self) -> Any:
        return self._view(self._native.epoch_ms)

    @property
    def state_offsets(self) -> Any:
        return self._view(self._native.state_offsets)

    @property
    def state_data(self) -> Any:
        return self._view(self._native.state_data)


class RemoteActorLane:
    """A filtered view of the remote actors (players, mobs...), kept natively: an update
    joins the lane when ``state[tag_offset] & tag_mask == tag_value``."""

    def __init__(self, session: WorldSessionCore, name: str, codec: StateCodec[Any]) -> None:
        self._session = session
        self.name = name
        self._codec = codec
        self._failures = [0]
        self._join = _Listeners()
        self._leave = _Listeners()
        self._update = _Listeners()

    @property
    def count(self) -> int:
        result: int = self._session._native.actor_count(self.name)
        return result

    @property
    def revision(self) -> int:
        result: int = self._session._native.actor_revision(self.name)
        return result

    @property
    def decode_failures(self) -> int:
        return self._failures[0]

    def get(self, uuid: str) -> RemoteActor | None:
        raw = self._session._native.actor(self.name, uuid)
        return self._actor(raw) if raw is not None else None

    def list(self) -> builtins.list[RemoteActor]:
        return [self._actor(raw) for raw in self._session._native.actors(self.name)]

    def snapshot(self) -> ActorSnapshot:
        return ActorSnapshot(self._session._native.actor_columns(self.name))

    def remove(self, uuid: str) -> bool:
        result: bool = self._session._native.remove_actor(uuid)
        return result

    def reap(self) -> None:
        self._session._native.reap()

    def clear(self) -> None:
        self._session._native.clear_actors(self.name)

    def apply(self, notification: Notification) -> None:
        """The native session applies every update to every lane as it arrives; there is
        nothing to feed by hand."""
        raise CrowdyError("lanes are fed by the session's native ingest")

    def on_join(self, listener: Callable[[RemoteActor], Any]) -> Callable[[], None]:
        return self._join.add(listener)

    def on_leave(self, listener: Callable[[RemoteActor], Any]) -> Callable[[], None]:
        return self._leave.add(listener)

    def on_update(self, listener: Callable[[RemoteActor], Any]) -> Callable[[], None]:
        """Called for every update to every actor: one Python call per update. Prefer
        :meth:`snapshot` once a frame on a busy world."""
        self._session._native.watch_updates(True)
        return self._update.add(listener)

    def _actor(self, raw: tuple[Any, ...]) -> RemoteActor:
        uuid, chunk, seen, epoch, samples = raw
        state = samples[0][0] if samples else b""
        return RemoteActor(
            uuid,
            chunk,
            seen,
            epoch,
            state,
            tuple(samples),
            _decode(self._codec, state, self._failures),
        )

    def _event(self, event: tuple[Any, ...]) -> None:
        kind, _, uuid, chunk, _, _, _, epoch, received, payload = event
        actor = RemoteActor(
            uuid, chunk, received, epoch, payload, (), _decode(self._codec, payload, self._failures)
        )
        if kind == _s.JOIN:
            self._join.fire(actor)
        elif kind == _s.LEAVE:
            self._leave.fire(actor)
        else:
            self._update.fire(actor)


class RemoteActorStore(RemoteActorLane):
    """Everyone else: a native registry with staleness, sample history and named lanes."""

    def __init__(self, session: WorldSessionCore, codec: StateCodec[Any]) -> None:
        super().__init__(session, "", codec)
        self._lanes: dict[str, RemoteActorLane] = {}

    def lane(
        self,
        name: str,
        *,
        tag_offset: int = -1,
        tag_mask: int = 0xFF,
        tag_value: int = 0,
        stale_after_ms: int = 12000,
        history_size: int = 2,
        codec: StateCodec[Any] | None = None,
    ) -> RemoteActorLane:
        """The lane ``name``, made on first use: actors whose state has
        ``state[tag_offset] & tag_mask == tag_value`` (every actor when ``tag_offset`` < 0).
        :func:`crowdypy.kit.engine_lanes` gives the engine pose's lanes."""
        found = self._lanes.get(name)
        if found is None:
            self._session._native.add_lane(
                name, tag_offset, tag_mask, tag_value, stale_after_ms, history_size
            )
            found = self._lanes[name] = RemoteActorLane(self._session, name, codec or self._codec)
        return found

    def _event(self, event: tuple[Any, ...]) -> None:
        lane = event[1]
        if lane:
            target = self._lanes.get(lane)
            if target is not None:
                target._event(event)
        else:
            super()._event(event)


# ------------------------------------------------------------------ chunks

_WRITE_BACK_ATTEMPTS = 5
_WRITE_BACK_BACKOFF_MS = 700
_BUSY_ATTEMPTS = 4
_REFUSAL_CODES = frozenset(
    {"FORBIDDEN", "SCOPE_MISSING", "NOT_ALLOWED", "BAD_REQUEST", "BAD_USER_INPUT",
     "INVALID_REQUEST", "GRAPHQL_VALIDATION_FAILED", "NOT_FOUND"}
)  # fmt: skip
_REFUSAL_STATUSES = frozenset({400, 403, 404, 413, 422})


def _error_code(error: BaseException) -> str | None:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else None


def _busy(error: BaseException) -> bool:
    extensions = getattr(error, "extensions", None) or {}
    return _error_code(error) == "PLATFORM_BUSY" or extensions.get("retryable") is True


def _write_back_retryable(error: BaseException) -> bool:
    """A failure that can clear is retried; a refusal the server will repeat is not."""
    if _busy(error):
        return True
    extensions = getattr(error, "extensions", None) or {}
    if extensions.get("retryable") is False or getattr(error, "retryable", None) is False:
        return False
    if _error_code(error) in _REFUSAL_CODES:
        return False
    status = getattr(error, "status", None) or extensions.get("httpStatus")
    return not (isinstance(status, int) and status in _REFUSAL_STATUSES)


@dataclass
class CachedChunk:
    """A chunk's place in the store. ``load_state``: ``loading``, ``loaded`` (the server's
    copy), ``missing`` (never stored), ``seeded`` (generated or edited first) or ``failed``.
    Voxels live natively: read them with :meth:`ChunkStore.voxels`."""

    coord: Coord
    load_state: str = "loading"
    revision: int = 0
    updated_at: int = 0
    hydrated: bool = False
    dirty: bool = False
    chunk_state: Any = None
    hydrated_states: dict[int, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChunkWriteBackFailure:
    """A write-back the store gave up on: ``refused`` (the server will refuse it again) or
    ``exhausted`` (every attempt failed for a reason that could have cleared). The chunk
    keeps its local voxels and is no longer dirty; undo or flag the edit here."""

    chunk: CachedChunk
    coord: Coord
    error: BaseException
    reason: str
    attempts: int


class ChunkStore:
    """A chunk and voxel cache: real-time voxel edits merge natively, local edits apply at
    once and replicate over UDP, and chunks load from and persist to the Game API."""

    def __init__(
        self,
        session: WorldSessionCore,
        *,
        voxel_state_codec: StateCodec[Any] = raw_codec,
        chunk_state_codec: StateCodec[Any] = raw_codec,
        hydrate_voxel_states: bool = False,
        on_missing: Callable[[Coord], Any] | None = None,
        write_back_interval_ms: int | None = 700,
    ) -> None:
        self._session = session
        self._voxel_codec = voxel_state_codec
        self._chunk_codec = chunk_state_codec
        self._hydrate_states = hydrate_voxel_states
        self._on_missing = on_missing
        self._meta: dict[Coord, CachedChunk] = {}
        self._queue: list[Coord] = []
        self._attempts: dict[Coord, int] = {}
        self._due: dict[Coord, int] = {}
        self._changed_listeners = _Listeners()
        self._failure_listeners = _Listeners()
        self._revision = 0
        if write_back_interval_ms:
            session.on_dispose(session.every(write_back_interval_ms, self._persist_due))

    @property
    def revision(self) -> int:
        result: int = self._session._native.chunk_revision() + self._revision
        return result

    @property
    def pending_write_backs(self) -> int:
        return len(self._queue)

    def get(self, chunk: Sequence[int | str]) -> CachedChunk | None:
        coord = _coord(chunk)
        found = self._meta.get(coord)
        if found is None and self._session._native.chunk(*coord) is not None:
            found = self._entry(coord)
            found.load_state = "loaded"
        return found

    def list(self) -> builtins.list[CachedChunk]:
        for coord in self._session._native.chunk_coords():
            self.get(coord)
        return builtins.list(self._meta.values())

    def voxels(self, chunk: Sequence[int | str]) -> bytes | None:
        """The 4096 voxel types of a cached chunk (index ``x + y*16 + z*256``)."""
        raw = self._session._native.chunk(*_coord(chunk))
        return raw[0] if raw is not None else None

    def voxel_type_at(self, chunk: Sequence[int | str], x: int, y: int, z: int) -> int:
        result: int = self._session._native.voxel_type_at(*_coord(chunk), x, y, z)
        return result

    def voxel_state_at(self, chunk: Sequence[int | str], x: int, y: int, z: int) -> Any:
        coord = _coord(chunk)
        live = self._session._native.voxel_state_at(*coord, x, y, z)
        if live is not None:
            try:
                return self._voxel_codec.decode(live[1])
            except Exception:
                return None
        meta = self._meta.get(coord)
        return meta.hydrated_states.get(x + y * 16 + z * 256) if meta else None

    def on_chunk_changed(self, listener: Callable[[CachedChunk], Any]) -> Callable[[], None]:
        self._session._native.watch_chunks(True)
        return self._changed_listeners.add(listener)

    def on_write_back_failed(
        self, listener: Callable[[ChunkWriteBackFailure], Any]
    ) -> Callable[[], None]:
        return self._failure_listeners.add(listener)

    async def ensure_around(self, center: Sequence[int | str], radius: int) -> None:
        """Load every stored chunk within ``radius`` (Chebyshev) of ``center`` in one request;
        a requested chunk the server has never stored is ``missing`` (and goes to
        ``on_missing``, the worldgen hook, when configured).

        A chunk already loaded keeps what the store holds for it when a later bulk load
        returns it again: its stored ``voxels`` carry none of the edits hydration and
        realtime merges applied. Prune it to load it afresh."""
        middle = _coord(center)
        around = [
            (middle[0] + dx, middle[1] + dy, middle[2] + dz)
            for dx in range(-radius, radius + 1)
            for dy in range(-radius, radius + 1)
            for dz in range(-radius, radius + 1)
        ]
        wanted = [c for c in around if c not in self._meta or self._meta[c].load_state == "failed"]
        returned: set[Coord] = set()
        if wanted:
            try:
                response = await self._when_not_busy(
                    lambda: self._session.client.chunks.by_distance(
                        {
                            "appId": self._session.app_id,
                            "centerCoordinate": _chunk_input(middle),
                            "maxDistance": max(1, min(8, radius)),
                            "limit": (2 * radius + 1) ** 3,
                        }
                    )
                )
            except Exception:
                for coord in wanted:
                    self._entry(coord).load_state = "failed"
                raise
            for row in response.get("chunks") or []:
                coord = _coord(row["coordinates"])
                # The cube around a new center includes chunks loaded from an earlier one.
                known = self._meta.get(coord)
                if known is not None and known.load_state == "loaded":
                    continue
                returned.add(coord)
                self._apply_server_chunk(coord, row.get("voxels"), row.get("chunkState"))
            for coord in wanted:
                if coord not in returned:
                    self._mark_missing(coord)
        if self._hydrate_states:
            for coord in [c for c in returned if not self._meta[c].hydrated]:
                await self._when_not_busy(functools.partial(self.hydrate, coord))

    async def hydrate(self, chunk: Sequence[int | str]) -> None:
        """Load one chunk in full: voxels, chunk state and per-voxel states.

        Each ``voxelStates`` entry goes over the dense grid: its voxel type at its voxel,
        and its state (an entry without one clears the hydrated state there). Since
        ck-api v2.33.0 every voxel edit recorded for a chunk (a hub's or mod's
        ``world.set_voxels``, ``updateVoxel``, realtime voxel updates) arrives only that
        way, so without hydration none of them shows after a reload."""
        coord = _coord(chunk)
        full = await self._session.client.chunks.get(
            {"appId": self._session.app_id, "coordinates": _chunk_input(coord)}
        )
        if not full:
            self._mark_missing(coord)
            return
        entries = []
        for entry in full.get("voxelStates") or []:
            voxel = entry["voxelCoord"]
            if all(0 <= int(voxel[axis]) < 16 for axis in ("x", "y", "z")):
                entries.append(
                    (int(voxel["x"]) + int(voxel["y"]) * 16 + int(voxel["z"]) * 256, entry)
                )
        voxels = full.get("voxels")
        if entries:
            raw = decode_base64(voxels) if voxels else b""
            # A chunk stored with `voxels: null` still carries its recorded edits.
            grid = bytearray(raw) if len(raw) == CHUNK_VOLUME else bytearray(CHUNK_VOLUME)
            for index, entry in entries:
                grid[index] = int(entry["voxelType"]) & 0xFF
            voxels = encode_base64(bytes(grid))
        self._apply_server_chunk(coord, voxels, full.get("chunkState"))
        meta = self._meta[coord]
        for index, entry in entries:
            if not entry.get("state"):
                meta.hydrated_states.pop(index, None)
                continue
            with contextlib.suppress(Exception):
                meta.hydrated_states[index] = self._voxel_codec.decode(
                    decode_base64(entry["state"])
                )
        meta.hydrated = True
        self._touch(meta)

    def set_voxel(
        self,
        chunk: Sequence[int | str],
        x: int,
        y: int,
        z: int,
        voxel_type: int,
        state: Any = None,
    ) -> int:
        """Apply an edit locally, replicate it, and queue the chunk for write-back. Returns
        the send's sequence number."""
        coord = _coord(chunk)
        encoded = self._voxel_codec.encode(state) if state is not None else b""
        sequence: int = self._session._native.set_voxel(*coord, x, y, z, voxel_type, encoded)
        self._session.track_send(
            {"kind": "voxelUpdate", "sequence": sequence, "sent_at": _now_ms(),
             "uuid": self._session.actor_uuid, "detail": {"chunk": coord, "x": x, "y": y, "z": z}}
        )  # fmt: skip
        meta = self._entry(coord)
        if meta.load_state in ("loading", "missing"):
            meta.load_state = "seeded"
        self.mark_dirty(coord)
        return sequence

    def seed(self, chunk: Sequence[int | str], voxels: Any, *, write_back: bool = True) -> None:
        """Insert a generated chunk (4096 voxel types), queued for write-back by default."""
        coord = _coord(chunk)
        if len(voxels) != CHUNK_VOLUME:
            raise ValueError(f"seed() needs a {CHUNK_VOLUME}-byte dense grid, got {len(voxels)}")
        self._session._native.seed(*coord, voxels)
        meta = self._entry(coord)
        meta.load_state = "seeded"
        if write_back:
            self.mark_dirty(coord)
        self._touch(meta)

    def mark_dirty(self, chunk: Sequence[int | str]) -> None:
        coord = _coord(chunk)
        meta = self._entry(coord)
        meta.dirty = True
        if coord not in self._queue:
            self._queue.append(coord)

    async def flush(self) -> builtins.list[ChunkWriteBackFailure]:
        """Persist every queued chunk now, waiting out the backoff of one whose write can
        still succeed. Returns the write-backs given up on."""
        failures: builtins.list[ChunkWriteBackFailure] = []
        while self._queue:
            wait = self._due.get(self._queue[0], 0) - _now_ms()
            if wait > 0:
                await sleep(wait / 1000)
            failure = await self._persist(now=True)
            if failure is not None:
                failures.append(failure)
        return failures

    def prune_beyond(self, center: Sequence[int | str], radius: int) -> None:
        """Forget clean chunks farther than ``radius`` from ``center`` (dirty ones stay until
        written back)."""
        middle = _coord(center)
        dirty_far = [c for c, m in self._meta.items() if m.dirty and _distance(c, middle) > radius]
        # The native cache prunes by distance alone; keep the dirty chunks' voxels.
        keep = {c: self._session._native.chunk(*c) for c in dirty_far}
        self._session._native.prune_beyond(*middle, radius)
        for coord, raw in keep.items():
            if raw is not None:
                self._session._native.seed(*coord, raw[0])
        for coord in [
            c for c, m in self._meta.items() if not m.dirty and _distance(c, middle) > radius
        ]:
            del self._meta[coord]
            self._revision += 1

    async def _persist_due(self) -> None:
        await self._persist(now=False)

    async def _persist(self, *, now: bool) -> ChunkWriteBackFailure | None:
        at = _now_ms()
        index = (
            0
            if now
            else next((i for i, c in enumerate(self._queue) if self._due.get(c, 0) <= at), -1)
        )
        if index < 0 or index >= len(self._queue):
            return None
        coord = self._queue.pop(index)
        voxels = self.voxels(coord)
        meta = self._meta.get(coord)
        if voxels is None or meta is None:
            self._forget(coord)
            return None
        try:
            await self._session.client.chunks.update(
                {
                    "appId": self._session.app_id,
                    "coordinates": _chunk_input(coord),
                    "voxels": encode_base64(voxels),
                }
            )
        except Exception as error:
            attempts = self._attempts.get(coord, 0) + 1
            retryable = _write_back_retryable(error)
            if retryable and attempts < _WRITE_BACK_ATTEMPTS:
                self._attempts[coord] = attempts
                self._due[coord] = _now_ms() + _WRITE_BACK_BACKOFF_MS * 2 ** (attempts - 1)
                if coord not in self._queue:
                    self._queue.append(coord)
                return None
            self._forget(coord)
            if coord in self._queue:
                self._queue.remove(coord)
            meta.dirty = False
            failure = ChunkWriteBackFailure(
                meta, coord, error, "exhausted" if retryable else "refused", attempts
            )
            self._failure_listeners.fire(failure)
            return failure
        self._forget(coord)
        meta.dirty = False
        if meta.load_state == "seeded":
            meta.load_state = "loaded"
        self._touch(meta)
        return None

    def _forget(self, coord: Coord) -> None:
        self._attempts.pop(coord, None)
        self._due.pop(coord, None)

    async def _when_not_busy(self, call: Callable[[], Any]) -> Any:
        attempt = 1
        while True:
            try:
                return await call()
            except Exception as error:
                if attempt >= _BUSY_ATTEMPTS or not _busy(error):
                    raise
                await sleep(0.2 * 2 ** (attempt - 1))
                attempt += 1

    def _apply_server_chunk(
        self, coord: Coord, voxels: str | None, chunk_state: str | None
    ) -> None:
        if voxels:
            raw = decode_base64(voxels)
            if len(raw) == CHUNK_VOLUME:
                self._session._native.seed(*coord, raw)
        meta = self._entry(coord)
        meta.chunk_state = self._decode_chunk_state(chunk_state)
        meta.load_state = "loaded"
        self._touch(meta)

    def _decode_chunk_state(self, encoded: str | None) -> Any:
        if not encoded:
            return None
        try:
            return self._chunk_codec.decode(decode_base64(encoded))
        except Exception:
            return None

    def _mark_missing(self, coord: Coord) -> None:
        meta = self._entry(coord)
        if meta.load_state in ("loaded", "seeded"):
            return
        meta.load_state = "missing"
        self._touch(meta)
        if self._on_missing is None:
            return
        generated = self._on_missing(coord)
        if isinstance(generated, (bytes, bytearray, memoryview)):
            self.seed(coord, generated)
        elif isinstance(generated, Mapping):
            self.seed(coord, generated["voxels"], write_back=generated.get("write_back", True))

    def _entry(self, coord: Coord) -> CachedChunk:
        meta = self._meta.get(coord)
        if meta is None:
            meta = self._meta[coord] = CachedChunk(coord, updated_at=_now_ms())
        return meta

    def _touch(self, meta: CachedChunk) -> None:
        meta.revision += 1
        meta.updated_at = _now_ms()
        self._revision += 1
        if self._changed_listeners:
            self._changed_listeners.fire(meta)

    def _changed(self, coord: Coord) -> None:
        meta = self._entry(coord)
        if meta.load_state == "loading":
            meta.load_state = "loaded"
        meta.revision += 1
        meta.updated_at = _now_ms()
        self._changed_listeners.fire(meta)


# ------------------------------------------------------------------ errors


@dataclass(frozen=True, slots=True)
class AttributedError:
    """A server error matched to the send that drew it: what ``kind`` of send (by its
    sequence) and, for your actor's sends, its uuid."""

    code: ErrorCode | int
    sequence: int
    kind: str
    uuid: str | None
    at_ms: int


_SEND_KINDS = ("unknown", "actorUpdate", "voxelUpdate", "audio", "text", "clientEvent",
               "genericSpatial", "singleActor", "channel", "heartbeat")  # fmt: skip


def _attributed(raw: tuple[Any, ...]) -> AttributedError:
    code, sequence, kind, uuid, at = raw
    try:
        named: ErrorCode | int = ErrorCode(code)
    except ValueError:
        named = code
    return AttributedError(
        named, sequence, _SEND_KINDS[kind] if kind < len(_SEND_KINDS) else "unknown", uuid, at
    )


class ErrorStore:
    def __init__(self, session: WorldSessionCore) -> None:
        self._session = session
        self._listeners = _Listeners()

    @property
    def total(self) -> int:
        result: int = self._session._native.error_total()
        return result

    def recent(self, limit: int | None = None) -> builtins.list[AttributedError]:
        rows = (
            self._session._native.recent_errors()
            if limit is None
            else self._session._native.recent_errors(limit)
        )
        return [_attributed(row) for row in rows]

    def last(self) -> AttributedError | None:
        rows = self.recent()
        return rows[-1] if rows else None

    def last_for(self, kind_or_uuid: str) -> AttributedError | None:
        """The latest error for a send kind (``actorUpdate``...) or an actor uuid."""
        for error in reversed(self.recent()):
            if kind_or_uuid in (error.kind, error.uuid):
                return error
        return None

    def clear(self) -> None:
        self._session._native.clear_errors()

    def on_error(self, listener: Callable[[AttributedError], Any]) -> Callable[[], None]:
        return self._listeners.add(listener)

    def _event(self, event: tuple[Any, ...]) -> None:
        _, _, uuid, _, code, sequence, kind, _, received, _ = event
        self._listeners.fire(_attributed((code, sequence, kind, uuid, received)))


# ------------------------------------------------------------------ inboxes and events


@dataclass(frozen=True, slots=True)
class InboxMessage:
    """A channel message (``channel_id`` > 0, ``sender`` its sender) or a direct one
    (``channel_id`` 0, ``sender`` is you: put the sender in the payload)."""

    channel_id: int
    sender: str
    payload: bytes
    server_epoch_ms: int
    received_at_ms: int
    value: Any = None


class _Inbox:
    def __init__(self, session: WorldSessionCore, codec: StateCodec[Any], channel: bool) -> None:
        self._session = session
        self._codec = codec
        self._channel = channel
        self._failures = [0]
        self._listeners = _Listeners()

    @property
    def decode_failures(self) -> int:
        return self._failures[0]

    def messages(
        self, channel_id: int | None = None, *, drain: bool = False
    ) -> builtins.list[InboxMessage]:
        """The retained messages, oldest first; ``drain`` takes them out of the inbox."""
        rows = self._session._native.inbox(self._channel, channel_id, drain)
        return [self._message(row) for row in rows]

    def clear(self) -> None:
        self._session._native.clear_inbox(self._channel)

    def on_message(self, listener: Callable[[InboxMessage], Any]) -> Callable[[], None]:
        return self._listeners.add(listener)

    def _message(self, row: tuple[Any, ...]) -> InboxMessage:
        channel_id, sender, payload, epoch, received = row
        return InboxMessage(
            channel_id,
            sender,
            payload,
            epoch,
            received,
            _decode(self._codec, payload, self._failures),
        )

    def _event(self, event: tuple[Any, ...]) -> None:
        _, _, uuid, _, channel_id, _, _, epoch, received, payload = event
        self._listeners.fire(self._message((channel_id, uuid, payload, epoch, received)))


class ActorInbox(_Inbox):
    """Direct (single-actor) messages to your actor."""

    def __init__(self, session: WorldSessionCore, codec: StateCodec[Any]) -> None:
        super().__init__(session, codec, channel=False)

    async def send(self, target_uuid: str, value: Any, target_chunk: Sequence[int | str]) -> int:
        """Message one actor (encoded with the inbox codec); returns the sequence."""
        return await self._session.client.udp.send_single_actor_message(
            _coord(target_chunk), target_uuid, self._codec.encode(value)
        )


class ChannelInbox(_Inbox):
    """Channel messages, from every channel you are a member of."""

    def __init__(self, session: WorldSessionCore, codec: StateCodec[Any]) -> None:
        super().__init__(session, codec, channel=True)

    def channels(self) -> builtins.list[int]:
        result: builtins.list[int] = self._session._native.inbox_channels()
        return result

    async def send(self, channel_id: str | int, value: Any) -> int:
        """Publish to a channel as your actor; returns the sequence."""
        return await self._session.client.udp.send_channel_message(
            channel_id, self._session.actor_uuid, self._codec.encode(value)
        )


@dataclass(frozen=True, slots=True)
class RoutedEvent:
    event_type: int
    from_server: bool
    sender: str
    chunk: Coord
    state: bytes
    server_epoch_ms: int
    received_at_ms: int
    value: Any = None


class EventRouter:
    """Client and server events by type, with the latest of each type retained natively."""

    def __init__(self, session: WorldSessionCore, codec: StateCodec[Any]) -> None:
        self._session = session
        self._codec = codec
        self._failures = [0]
        self._handlers: dict[int, _Listeners] = {}

    @property
    def decode_failures(self) -> int:
        return self._failures[0]

    def on(self, event_type: int, handler: Callable[[RoutedEvent], Any]) -> Callable[[], None]:
        listeners = self._handlers.get(event_type)
        if listeners is None:
            self._session._native.watch_event(event_type)
            listeners = self._handlers[event_type] = _Listeners()
        return listeners.add(handler)

    def last_event(self, event_type: int) -> RoutedEvent | None:
        raw = self._session._native.last_event(event_type)
        return self._routed(raw) if raw is not None else None

    async def send(
        self,
        event_type: int,
        value: Any = b"",
        *,
        chunk: Sequence[int | str] | None = None,
        distance: int = 8,
        decay: int = DecayRate.NONE,
    ) -> int:
        """Send a client event from your actor (at its chunk unless ``chunk`` is given)."""
        target = _coord(chunk) if chunk is not None else self._session.self.chunk
        return await self._session.client.udp.send_client_event(
            target, self._session.actor_uuid, event_type, self._codec.encode(value),
            distance=distance, decay=decay,
        )  # fmt: skip

    def _routed(self, raw: tuple[Any, ...]) -> RoutedEvent:
        event_type, from_server, sender, chunk, state, epoch, received = raw
        return RoutedEvent(
            event_type, bool(from_server), sender, chunk, state, epoch, received,
            _decode(self._codec, state, self._failures),
        )  # fmt: skip

    def _event(self, event: tuple[Any, ...]) -> None:
        _, _, uuid, chunk, event_type, from_server, _, epoch, received, payload = event
        listeners = self._handlers.get(event_type)
        if listeners:
            listeners.fire(
                self._routed((event_type, from_server, uuid, chunk, payload, epoch, received))
            )


# ------------------------------------------------------------------ durable state


class HostTracker:
    """Host election: heartbeats on the session's timer (which also keep you eligible),
    caches the elected host and reports changes. Election is informational; gate
    authoritative writes with server-side ``is_host`` policies. A failed heartbeat keeps
    the last known host."""

    def __init__(
        self,
        session: WorldSessionCore,
        *,
        my_user_id: str | Callable[[], str | None] | None = None,
        interval_ms: int = 3000,
        heartbeat_immediately: bool = True,
    ) -> None:
        self._session = session
        self._my_user_id = my_user_id
        self._host: str | None = None
        self._listeners = _Listeners()
        session.on_dispose(session.every(interval_ms, self.beat))
        if heartbeat_immediately:
            run_soon(self.beat)

    @property
    def host_user_id(self) -> str | None:
        return self._host

    @property
    def is_host(self) -> bool:
        mine = self._my_user_id() if callable(self._my_user_id) else self._my_user_id
        return mine is not None and self._host is not None and str(mine) == self._host

    def on_host_changed(self, listener: Callable[[str | None], Any]) -> Callable[[], None]:
        return self._listeners.add(listener)

    async def beat(self) -> None:
        """Send one heartbeat now and apply the result."""
        try:
            result = await self._session.client.host.heartbeat(self._session.app_id)
        except Exception:
            return
        host = result.get("hostUserId") if result else None
        following = str(host) if host is not None else None
        if following != self._host:
            self._host = following
            self._listeners.fire(following)


class SaveStateStore:
    """A typed local copy of the per-user, per-app ``client.state`` blob: :meth:`load`
    fetches it, :meth:`set` changes it (and autosave persists it when configured),
    :meth:`save` persists it now."""

    def __init__(
        self,
        session: WorldSessionCore,
        *,
        codec: StateCodec[Any] | None = None,
        autosave_ms: int | None = None,
    ) -> None:
        self._session = session
        self._codec = codec or json_codec()
        self._value: Any = None
        self._dirty = False
        self._saving = False
        self._last_saved_at: int | None = None
        if autosave_ms:
            session.on_dispose(session.every(autosave_ms, self._autosave))

    @property
    def value(self) -> Any:
        return self._value

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def last_saved_at(self) -> int | None:
        """Local time (epoch ms) of the last successful save."""
        return self._last_saved_at

    async def load(self) -> Any:
        record = await self._session.client.state.get_one(self._session.app_id)
        if not record or record.get("state") is None:
            self._value = None
            return None
        try:
            self._value = self._codec.decode(decode_base64(record["state"]))
        except Exception:
            self._value = None
        self._dirty = False
        return self._value

    def set(self, value: Any) -> None:
        self._value = value
        self._dirty = True

    def patch(self, patch: Mapping[str, Any]) -> None:
        self.set({**(self._value if isinstance(self._value, Mapping) else {}), **patch})

    async def save(self) -> None:
        if self._value is None:
            return
        self._saving = True
        try:
            await self._session.client.state.update(
                {
                    "appId": self._session.app_id,
                    "state": encode_base64(self._codec.encode(self._value)),
                }
            )
            self._dirty = False
            self._last_saved_at = _now_ms()
        finally:
            self._saving = False

    async def _autosave(self) -> None:
        if self._dirty and not self._saving:
            await self.save()


class AvatarStateStore:
    """Typed copies of one avatar's public, private and per-app state, each with its own
    codec (they rarely share a shape)."""

    def __init__(
        self,
        session: WorldSessionCore,
        *,
        avatar_id: str | None = None,
        public_codec: StateCodec[Any] | None = None,
        private_codec: StateCodec[Any] | None = None,
        app_codec: StateCodec[Any] | None = None,
    ) -> None:
        self._session = session
        self._avatar_id = avatar_id
        self._public_codec = public_codec or json_codec()
        self._private_codec = private_codec or json_codec()
        self._app_codec = app_codec or json_codec()
        self._public: Any = None
        self._private: Any = None
        self._app: Any = None

    @property
    def avatar_id(self) -> str | None:
        return self._avatar_id

    @property
    def public_state(self) -> Any:
        return self._public

    @property
    def private_state(self) -> Any:
        return self._private

    @property
    def app_state(self) -> Any:
        return self._app

    async def load(self) -> None:
        """Resolve the avatar (your first when none was configured) and fetch its states."""
        client = self._session.client
        if not self._avatar_id:
            mine = await client.avatars.mine()
            if not mine:
                raise CrowdyError("No avatar to bind: create one with client.avatars.create()")
            self._avatar_id = str(mine[0]["avatarId"])
        avatar = await client.avatars.get(self._avatar_id)
        self._public = self._decode(self._public_codec, (avatar or {}).get("publicState"))
        self._private = self._decode(self._private_codec, (avatar or {}).get("privateState"))
        record = await client.avatars.app_state(self._session.app_id, self._avatar_id)
        self._app = self._decode(self._app_codec, (record or {}).get("state"))

    async def set_identity_state(
        self, *, public_state: Any = None, private_state: Any = None
    ) -> None:
        avatar_id = self._require()
        update: dict[str, str] = {}
        if public_state is not None:
            update["publicState"] = encode_base64(self._public_codec.encode(public_state))
        if private_state is not None:
            update["privateState"] = encode_base64(self._private_codec.encode(private_state))
        await self._session.client.avatars.update_state(avatar_id, update)
        if public_state is not None:
            self._public = public_state
        if private_state is not None:
            self._private = private_state

    async def set_app_state(self, value: Any) -> None:
        avatar_id = self._require()
        await self._session.client.avatars.update_app_state(
            {
                "appId": self._session.app_id,
                "avatarId": avatar_id,
                "state": encode_base64(self._app_codec.encode(value)),
            }
        )
        self._app = value

    def _require(self) -> str:
        if not self._avatar_id:
            raise CrowdyError(
                "AvatarStateStore is unbound: call load() first or configure avatar_id"
            )
        return self._avatar_id

    @staticmethod
    def _decode(codec: StateCodec[Any], encoded: str | None) -> Any:
        if not encoded:
            return None
        try:
            return codec.decode(decode_base64(encoded))
        except Exception:
            return None


def create_world_session(
    client: AsyncCrowdyClient,
    app_id: str | int | None = None,
    *,
    connection: AsyncReplicationConnection | None = None,
    host: Mapping[str, Any] | bool = False,
    save: Mapping[str, Any] | bool = False,
    avatar: Mapping[str, Any] | bool = False,
    **options: Any,
) -> WorldSessionCore:
    """A world session over ``client.udp``'s connection (connect it first). ``host``,
    ``save`` and ``avatar`` attach a :class:`HostTracker`, :class:`SaveStateStore` and
    :class:`AvatarStateStore` (``True``, or their options) as ``session.host`` /
    ``session.save`` / ``session.avatar``. Other ``options`` go to :class:`WorldSessionCore`."""
    conn = connection or client.udp.connection
    if conn is None:
        raise CrowdyError("connect client.udp first: the session runs over its connection")
    app = str(app_id) if app_id is not None else conn.app_id
    session = WorldSessionCore(client, app, conn, **options)
    if host:
        session.host = HostTracker(session, **(host if isinstance(host, Mapping) else {}))
    if save:
        session.save = SaveStateStore(session, **(save if isinstance(save, Mapping) else {}))
    if avatar:
        session.avatar = AvatarStateStore(
            session, **(avatar if isinstance(avatar, Mapping) else {})
        )
    return session
