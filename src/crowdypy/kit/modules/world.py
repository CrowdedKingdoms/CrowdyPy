"""The root world hub (the ``world-tick`` starter). CrowdyJS ``src/kit/modules/world.ts``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from crowdypy.kit.mirror import CollectionMirror, HubState

if TYPE_CHECKING:
    from crowdypy.kit.runtime import KitRuntime

__all__ = ["WorldKit"]


class WorldKit:
    """The root world hub: its clock, weather and liveops modifiers as the mirrored
    :attr:`conditions` topic (``time_of_day``, ``night``, ``weather``, ``modifiers``), its
    resource nodes as the mirrored :attr:`nodes` collection, harvesting, and for developers the
    liveops schedule. The root hub takes 50 calls a second for the whole app, so read it from
    the mirrors (``await conditions.start()``), not by polling :meth:`state`."""

    def __init__(self, runtime: KitRuntime, *, node_type: str = "world", key: str = "") -> None:
        self._runtime = runtime
        self._node_type = node_type
        self._key = key
        #: The ``world`` topic: conditions now.
        self.conditions = HubState(runtime, node_type, key, "world")
        #: The ``nodes`` topic: each resource node's ``amount`` and ``regrow_at``, by node id.
        self.nodes = CollectionMirror(runtime, node_type, key, "nodes")

    async def state(self) -> dict[str, Any]:
        """Everything the world shows: ``conditions``, ``clock_ms``, ``nodes``, ``events``,
        ``online``."""
        result: dict[str, Any] = await self._runtime.call(self._node_type, self._key, "state")
        return result

    async def harvest(self, node: int) -> int:
        """Takes one from resource node ``node``; returns what it holds now. Raises
        :class:`~crowdypy.kit.protocol.KitError` ``cooldown``, ``empty`` (it grows back),
        ``not_found``."""
        left: int = await self._runtime.call(
            self._node_type, self._key, "harvest", {"node": node}, action=True
        )
        return left

    async def schedule(self, event: dict[str, Any]) -> bool:
        """Schedules (or replaces) a liveops event ``{name, start_ms, end_ms, modifiers}``.
        Developers only (a developer kit)."""
        done: bool = await self._runtime.call(
            self._node_type, self._key, "schedule", event, action=True
        )
        return done

    async def cancel(self, name: str) -> bool:
        """Cancels a liveops event; returns whether there was one. Developers only."""
        done: bool = await self._runtime.call(
            self._node_type, self._key, "cancel", {"name": name}, action=True
        )
        return done
