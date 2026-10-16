"""Real-time arenas (the ``arena`` starter). CrowdyJS ``src/kit/modules/arenas.ts``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from crowdypy.kit.mirror import HubState

if TYPE_CHECKING:
    from crowdypy.kit.runtime import KitRuntime

__all__ = ["ArenaMatch", "ArenasKit"]


class ArenasKit:
    """Real-time arenas: :meth:`arena` by key."""

    def __init__(self, runtime: KitRuntime, *, node_type: str = "arena") -> None:
        self._runtime = runtime
        self._node_type = node_type
        self._arenas: dict[str, ArenaMatch] = {}

    def arena(self, key: str) -> ArenaMatch:
        a = self._arenas.get(key)
        if a is None:
            a = self._arenas[key] = ArenaMatch(self._runtime, self._node_type, key)
        return a


class ArenaMatch:
    """One arena: players fight with their class's abilities and the hub referees every hit. A
    client reports where its player is (:meth:`move`, a few times a second) and the hub keeps
    what the class's speed allows; casts are checked for cooldown, resource and range, and land
    as the hub decides. :attr:`view` mirrors the ``arena`` topic (``phase``, ``width``,
    ``depth``, ``fighters``, ``projectiles``, ``areas``, ``kill_target``, ``ends_at``,
    ``winner``); its events (``type``: ``cast``, ``impact``, ``kill``, ``respawn``,
    ``violation``, ``removed``, ``finished``) come through ``view.on_event``."""

    def __init__(self, runtime: KitRuntime, node_type: str, key: str) -> None:
        self._runtime = runtime
        self.node_type = node_type
        self.key = key
        self.view = HubState(runtime, node_type, key, "arena")

    async def watch(self) -> dict[str, Any]:
        view: dict[str, Any] = await self.view.start()
        return view

    async def unwatch(self) -> None:
        await self.view.stop()

    async def join(self, hero_class: str) -> bool:
        """Joins as ``hero_class`` (the starter's are ``warrior`` and ``mage``). Raises
        ``bad_request``, ``full``, ``started``."""
        done: bool = await self._runtime.call(
            self.node_type, self.key, "join", {"class": hero_class}, action=True
        )
        return done

    async def leave(self) -> bool:
        done: bool = await self._runtime.call(self.node_type, self.key, "leave", None, action=True)
        return done

    async def start(self) -> bool:
        """The first to join, or a developer. Raises ``forbidden``, ``not_ready``, ``started``."""
        done: bool = await self._runtime.call(self.node_type, self.key, "start", None, action=True)
        return done

    async def move(self, x: float, z: float) -> list[float]:
        """Reports where the player is; returns ``[x, z]``, the position the hub kept (a move too
        fast is cut short, a jump refused). Not sent with an action id: the next report replaces
        it. Raises ``forbidden`` (removed by the warden), ``conflict`` (down)."""
        kept: list[float] = await self._runtime.call(
            self.node_type, self.key, "move", {"x": x, "z": z}
        )
        return kept

    async def cast(self, ability: str, x: float, z: float) -> bool:
        """Casts ``ability`` aimed at ``(x, z)``; returns whether a hit-scan strike hit someone at
        once (a projectile or an area lands later, as an ``impact`` event). Raises ``cooldown``,
        ``insufficient``, ``out_of_reach``, ``not_found``."""
        hit: bool = await self._runtime.call(
            self.node_type, self.key, "cast", {"ability": ability, "x": x, "z": z}, action=True
        )
        return hit
