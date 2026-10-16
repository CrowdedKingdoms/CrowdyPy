"""Waves and bosses (the ``director`` starter). CrowdyJS ``src/kit/modules/encounters.ts``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from crowdypy.kit.mirror import HubState

if TYPE_CHECKING:
    from crowdypy.kit.runtime import KitRuntime

__all__ = ["Encounter", "EncountersKit"]


class EncountersKit:
    """Encounters: :meth:`encounter` by key."""

    def __init__(self, runtime: KitRuntime, *, node_type: str = "director") -> None:
        self._runtime = runtime
        self._node_type = node_type
        self._encounters: dict[str, Encounter] = {}

    def encounter(self, key: str) -> Encounter:
        e = self._encounters.get(key)
        if e is None:
            e = self._encounters[key] = Encounter(self._runtime, self._node_type, key)
        return e


class Encounter:
    """One encounter: waves down a lane on the hub's clock, towers that may not cut the lane, a
    boss with phases. :attr:`view` mirrors the ``encounter`` topic; its events (``type``:
    ``wave_started``, ``wave_cleared``, ``boss_phase``, ``won``, ``lost``) come through
    ``view.on_event``."""

    def __init__(self, runtime: KitRuntime, node_type: str, key: str) -> None:
        self._runtime = runtime
        self.node_type = node_type
        self.key = key
        self.view = HubState(runtime, node_type, key, "encounter")

    async def watch(self) -> dict[str, Any]:
        view: dict[str, Any] = await self.view.start()
        return view

    async def unwatch(self) -> None:
        await self.view.stop()

    async def join(self) -> bool:
        """Raises ``full``."""
        done: bool = await self._runtime.call(self.node_type, self.key, "join", None, action=True)
        return done

    async def start(self) -> bool:
        """The first to join, or a developer. Raises ``forbidden``, ``started``, ``bad_request``."""
        done: bool = await self._runtime.call(self.node_type, self.key, "start", None, action=True)
        return done

    async def build(self, x: int, z: int) -> int:
        """Builds a tower at ``(x, z)``; returns the gold left. Raises ``blocks_lane``,
        ``insufficient``, ``conflict``, ``bad_request``, ``not_member``."""
        gold: int = await self._runtime.call(
            self.node_type, self.key, "build", {"x": x, "z": z}, action=True
        )
        return gold

    async def hit(self, enemy: int) -> int:
        """Strikes enemy ``enemy``; returns its hp left. Raises ``cooldown``, ``not_found``,
        ``not_member``."""
        hp: int = await self._runtime.call(
            self.node_type, self.key, "hit", {"enemy": enemy}, action=True
        )
        return hp
