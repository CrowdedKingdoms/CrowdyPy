"""Matchmaking queues (the ``matchmaker`` starter). CrowdyJS ``src/kit/modules/matchmaking.ts``."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from crowdypy.kit.mirror import HubState
from crowdypy.kit.store import WritableKitStore

if TYPE_CHECKING:
    from crowdypy.kit.runtime import KitRuntime

__all__ = ["MatchQueue", "MatchmakingKit"]

_ACTIVE = frozenset({"party", "queued", "proposed"})


class MatchmakingKit:
    """Matchmaking queues: :meth:`queue` by name."""

    def __init__(self, runtime: KitRuntime, *, node_type: str = "matchmaker") -> None:
        self._runtime = runtime
        self._node_type = node_type
        self._queues: dict[str, MatchQueue] = {}

    def queue(self, name: str) -> MatchQueue:
        """The queue named ``name`` (``ranked``, ``casual``, ...), one per name."""
        q = self._queues.get(name)
        if q is None:
            q = self._queues[name] = MatchQueue(self._runtime, self._node_type, name)
        return q


class MatchQueue:
    """One queue. Every call's answer updates :attr:`place`; :meth:`watch` keeps it current while
    the player waits (the queue never names its players on its topic, so a waiting client asks)
    and :meth:`on_matched` hears the session key once a match is made.

    A place is ``{"state": "idle"}``, ``party``, ``queued``, ``proposed`` or
    ``{"state": "matched", "session": ...}``.
    """

    def __init__(self, runtime: KitRuntime, node_type: str, name: str) -> None:
        self._runtime = runtime
        self.node_type = node_type
        self.name = name
        #: The ``queue`` topic: ``waiting``, ``proposals``, ``forming`` (how many, never who).
        self.counts = HubState(runtime, node_type, name, "queue")
        self._place: WritableKitStore[dict[str, Any]] = WritableKitStore({"state": "idle"})
        self._matched: list[Callable[[str], Any]] = []
        self._last_matched: str | None = None
        self._watching = False

    @property
    def place(self) -> WritableKitStore[dict[str, Any]]:
        """The player's place, as last answered."""
        return self._place

    def _settle(self, place: dict[str, Any]) -> dict[str, Any]:
        self._place.set(place)
        session = place.get("session")
        if (
            place.get("state") == "matched"
            and isinstance(session, str)
            and session != self._last_matched
        ):
            self._last_matched = session
            for listener in list(self._matched):
                with contextlib.suppress(Exception):
                    listener(session)
        return place

    async def _call(self, method: str, args: Any = None, *, action: bool = True) -> Any:
        return await self._runtime.call(self.node_type, self.name, method, args, action=action)

    async def join(
        self,
        rating: int,
        *,
        party: Sequence[int | str] | None = None,
        leader: int | str | None = None,
    ) -> dict[str, Any]:
        """Joins the queue (or updates the rating). With ``party``, the caller leads a party of
        them; each member then joins with ``leader``, and the party queues once all have.
        Raises :class:`~crowdypy.kit.protocol.KitError` ``conflict`` (a match to accept),
        ``limit`` (party size), ``forbidden``, ``full``."""
        args: dict[str, Any] = {"rating": rating}
        if party:
            args["party"] = [int(p) for p in party]
        if leader is not None:
            args["leader"] = int(leader)
        return self._settle(await self._call("join", args))

    async def leave(self) -> bool:
        """Leaves the queue, a forming party, or a proposed match."""
        left: bool = await self._call("leave")
        self._settle({"state": "idle"})
        return left

    async def accept(self) -> dict[str, Any]:
        """Accepts the proposed match; the answer is ``matched`` once everyone has."""
        return self._settle(await self._call("accept"))

    async def decline(self) -> bool:
        """Declines the proposed match; the others go back to the queue."""
        done: bool = await self._call("decline")
        self._settle({"state": "idle"})
        return done

    async def status(self) -> dict[str, Any]:
        """Asks for the player's place."""
        return self._settle(await self._call("status", action=False))

    async def configure(self, config: dict[str, Any]) -> bool:
        """Sets the queue's config. Developers only."""
        done: bool = await self._call("configure", config)
        return done

    async def watch(self, interval_ms: float = 2000) -> None:
        """Asks for the player's place every ``interval_ms`` (at least 500) while they wait in a
        party, the queue or a proposal, until :meth:`unwatch`. Run it beside the game (a task,
        or a thread in the blocking client)."""
        self._watching = True
        while self._watching:
            await asyncio.sleep(max(500.0, interval_ms) / 1000)
            if self._watching and self._place.get().get("state") in _ACTIVE:
                with contextlib.suppress(Exception):
                    await self.status()

    def unwatch(self) -> None:
        self._watching = False

    def on_matched(self, listener: Callable[[str], Any]) -> Callable[[], None]:
        """Called with the session key when a match is made for the player."""
        self._matched.append(listener)
        return lambda: self._matched.remove(listener) if listener in self._matched else None
