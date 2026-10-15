"""Duels (the ``minigame`` starter). CrowdyJS ``src/kit/modules/minigames.ts``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from crowdypy.kit.mirror import HubState

if TYPE_CHECKING:
    from crowdypy.kit.runtime import KitRuntime

__all__ = ["Duel", "MinigamesKit"]


class MinigamesKit:
    """Two-player duels of hidden throws: :meth:`duel` by table."""

    def __init__(self, runtime: KitRuntime, *, node_type: str = "minigame") -> None:
        self._runtime = runtime
        self._node_type = node_type
        self._duels: dict[str, Duel] = {}

    def duel(self, key: str) -> Duel:
        d = self._duels.get(key)
        if d is None:
            d = self._duels[key] = Duel(self._runtime, self._node_type, key)
        return d


class Duel:
    """One duel: the hub keeps each round's throws until both are in (or the round's time runs
    out), then reveals them; the first to win a majority of ``best_of`` rounds wins. The
    starter's game is rock, paper, scissors. :attr:`view` mirrors the ``duel`` topic (``phase``,
    ``seats`` with who has thrown but never what, ``best_of``, ``round``, ``deadline``, ``last``
    reveal, ``winner``)."""

    def __init__(self, runtime: KitRuntime, node_type: str, key: str) -> None:
        self._runtime = runtime
        self.node_type = node_type
        self.key = key
        self.view = HubState(runtime, node_type, key, "duel")

    async def watch(self) -> dict[str, Any]:
        view: dict[str, Any] = await self.view.start()
        return view

    async def unwatch(self) -> None:
        await self.view.stop()

    async def _call(self, method: str, args: Any = None) -> bool:
        done: bool = await self._runtime.call(self.node_type, self.key, method, args, action=True)
        return done

    async def join(self) -> bool:
        """Takes a seat; returns whether this seat started the first round. Raises ``full``."""
        return await self._call("join")

    async def leave(self) -> bool:
        """Before the duel starts. Raises ``started``."""
        return await self._call("leave")

    async def throw(self, hand: str) -> bool:
        """The caller's hidden throw this round; returns whether it completed the round. Raises
        ``conflict`` (already in, or no round open), ``bad_request`` (not a hand), ``not_member``."""
        return await self._call("throw", {"hand": hand})

    async def rematch(self) -> bool:
        """Once it is over: play again. Raises ``conflict``, ``not_member``."""
        return await self._call("rematch")
