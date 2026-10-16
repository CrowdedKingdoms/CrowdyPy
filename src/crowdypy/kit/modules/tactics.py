"""Grid battles (the ``tactics`` starter). CrowdyJS ``src/kit/modules/tactics.ts``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from crowdypy.kit.mirror import HubState

if TYPE_CHECKING:
    from crowdypy.kit.runtime import KitRuntime

__all__ = ["TacticsBattle", "TacticsKit"]


class TacticsKit:
    """Grid battles in turns, against the hub's units: :meth:`battle` by key."""

    def __init__(self, runtime: KitRuntime, *, node_type: str = "tactics") -> None:
        self._runtime = runtime
        self._node_type = node_type
        self._battles: dict[str, TacticsBattle] = {}

    def battle(self, key: str) -> TacticsBattle:
        b = self._battles.get(key)
        if b is None:
            b = self._battles[key] = TacticsBattle(self._runtime, self._node_type, key)
        return b


class TacticsBattle:
    """One grid battle in phases: in the players' phase each moves and strikes with their unit,
    and the hub's units act once every player has ended the phase. :attr:`battle` mirrors the
    ``battle`` topic (``phase``, ``round``, ``width``, ``height``, ``obstacles``, ``units``,
    ``players``, ``host``, ``ended_turn``, ``phase_ends``, ``log``)."""

    def __init__(self, runtime: KitRuntime, node_type: str, key: str) -> None:
        self._runtime = runtime
        self.node_type = node_type
        self.key = key
        self.battle = HubState(runtime, node_type, key, "battle")

    async def watch(self) -> dict[str, Any]:
        view: dict[str, Any] = await self.battle.start()
        return view

    async def unwatch(self) -> None:
        await self.battle.stop()

    async def my_units(self) -> list[dict[str, Any]]:
        """The player's units, as the mirror last saw them."""
        me = await self._runtime.player_id()
        units = (self.battle.get() or {}).get("units") or []
        return [u for u in units if me is not None and str(u.get("owner")) == me]

    async def _call(self, method: str, args: Any = None) -> bool:
        done: bool = await self._runtime.call(self.node_type, self.key, method, args, action=True)
        return done

    async def join(self) -> bool:
        """Raises ``started``, ``full``."""
        return await self._call("join")

    async def start(self) -> bool:
        """Raises ``forbidden`` (the host starts it), ``started``."""
        return await self._call("start")

    async def move(self, x: int, z: int) -> bool:
        """Moves the player's unit to ``(x, z)``. Raises ``not_your_turn``, ``conflict`` (it
        moved), ``out_of_reach``, ``not_member``."""
        return await self._call("move", {"x": x, "z": z})

    async def attack(self, target: int) -> bool:
        """Attacks unit ``target``. Raises ``not_your_turn``, ``conflict``, ``out_of_reach``,
        ``forbidden`` (its own side), ``not_found``."""
        return await self._call("attack", {"target": target})

    async def end_turn(self) -> bool:
        """Ends the player's turn this phase."""
        return await self._call("end_turn")
