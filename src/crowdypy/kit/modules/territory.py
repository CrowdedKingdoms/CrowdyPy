"""Control points and shrinking circles (the ``territory`` starter, types ``territory`` and
``zone``). CrowdyJS ``src/kit/modules/territory.ts``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from crowdypy.kit.mirror import HubState

if TYPE_CHECKING:
    from crowdypy.kit.runtime import KitRuntime

__all__ = ["TerritoryKit", "TerritoryMap", "ZoneMatch"]


class TerritoryKit:
    """Wars over control points (:meth:`map`) and shrinking circles (:meth:`zone`), by key."""

    def __init__(
        self, runtime: KitRuntime, *, territory_type: str = "territory", zone_type: str = "zone"
    ) -> None:
        self._runtime = runtime
        self._territory_type = territory_type
        self._zone_type = zone_type
        self._maps: dict[str, TerritoryMap] = {}
        self._zones: dict[str, ZoneMatch] = {}

    def map(self, key: str) -> TerritoryMap:
        m = self._maps.get(key)
        if m is None:
            m = self._maps[key] = TerritoryMap(self._runtime, self._territory_type, key)
        return m

    def zone(self, key: str) -> ZoneMatch:
        z = self._zones.get(key)
        if z is None:
            z = self._zones[key] = ZoneMatch(self._runtime, self._zone_type, key)
        return z


class TerritoryMap:
    """One war over a map's control points: a faction captures a point by having more members
    standing on it than any other (the hub reads where players are; a client never says it is on
    a point), a capture decays when nobody presses it, each point held pays its faction, and the
    first to the winning score wins. :attr:`view` mirrors the ``territory`` topic (``phase``,
    ``points``, ``scores``, ``members``, ``winner``); ``view.on_event`` hears ``captured``,
    ``income`` and ``won``."""

    def __init__(self, runtime: KitRuntime, node_type: str, key: str) -> None:
        self._runtime = runtime
        self.node_type = node_type
        self.key = key
        self.view = HubState(runtime, node_type, key, "territory")

    async def watch(self) -> dict[str, Any]:
        view: dict[str, Any] = await self.view.start()
        return view

    async def unwatch(self) -> None:
        await self.view.stop()

    async def join(self, faction: str) -> bool:
        """Joins a faction, once: a player keeps theirs. Raises ``conflict``, ``bad_request``, ``full``."""
        done: bool = await self._runtime.call(
            self.node_type, self.key, "join", {"faction": faction}, action=True
        )
        return done

    async def reset(self) -> bool:
        """A new war. Developers only."""
        done: bool = await self._runtime.call(self.node_type, self.key, "reset", None, action=True)
        return done


class ZoneMatch:
    """One match's circle: it holds, then shrinks, phase by phase, and wears down every player
    outside it (the hub reads where players are) until one is left. :attr:`view` mirrors the
    ``zone`` topic (``phase``, ``center``, ``radius``, ``stage``, ``next_radius``, ``players``,
    ``alive``, ``winner``); ``view.on_event`` hears ``warning``, ``shrinking``, ``settled``,
    ``final``, ``eliminated`` and ``won``."""

    def __init__(self, runtime: KitRuntime, node_type: str, key: str) -> None:
        self._runtime = runtime
        self.node_type = node_type
        self.key = key
        self.view = HubState(runtime, node_type, key, "zone")

    async def watch(self) -> dict[str, Any]:
        view: dict[str, Any] = await self.view.start()
        return view

    async def unwatch(self) -> None:
        await self.view.stop()

    async def join(self) -> bool:
        """Raises ``started``, ``full``."""
        done: bool = await self._runtime.call(self.node_type, self.key, "join", None, action=True)
        return done

    async def start(self) -> bool:
        """The first to join, or a developer. Raises ``forbidden``, ``not_ready``, ``started``."""
        done: bool = await self._runtime.call(self.node_type, self.key, "start", None, action=True)
        return done
