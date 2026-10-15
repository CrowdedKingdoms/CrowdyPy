"""Private runs (the ``instance`` starter). CrowdyJS ``src/kit/modules/instances.ts``."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from crowdypy.kit.mirror import HubState

if TYPE_CHECKING:
    from crowdypy.kit.runtime import KitRuntime

__all__ = ["InstanceRun", "InstancesKit"]


class InstancesKit:
    """Private runs for one party: :meth:`run` by key (one the game makes up per run)."""

    def __init__(self, runtime: KitRuntime, *, node_type: str = "instance") -> None:
        self._runtime = runtime
        self._node_type = node_type
        self._runs: dict[str, InstanceRun] = {}

    def run(self, key: str) -> InstanceRun:
        r = self._runs.get(key)
        if r is None:
            r = self._runs[key] = InstanceRun(self._runtime, self._node_type, key)
        return r


class InstanceRun:
    """One run: a party plays down floors the hub generates from the run's seed, the hub resolves
    each room, a party with nobody standing is wiped (permadeath), and a run nobody touches
    expires. A cleared or wiped run posts each member's depth to a leaderboard when the hub's
    config names one. :attr:`view` mirrors the ``run`` topic (``phase``, ``owner``, ``seed``,
    ``floor``, ``floors``, ``room``, ``rooms``, ``members``, ``invited``, ``gold``, ``relics``,
    ``grid``, ``log``)."""

    def __init__(self, runtime: KitRuntime, node_type: str, key: str) -> None:
        self._runtime = runtime
        self.node_type = node_type
        self.key = key
        self.view = HubState(runtime, node_type, key, "run")

    async def watch(self) -> dict[str, Any]:
        view: dict[str, Any] = await self.view.start()
        return view

    async def unwatch(self) -> None:
        await self.view.stop()

    async def open(self, *, seed: int | None = None, party: Sequence[int | str] = ()) -> int:
        """Opens the run as its owner (a developer kit opens one for ``party``); returns its seed.
        Raises ``started``, ``bad_request`` (party size), ``conflict`` (its grid could not be
        reserved)."""
        args: dict[str, Any] = {}
        if seed is not None:
            args["seed"] = seed
        if party:
            args["party"] = [int(p) for p in party]
        opened: int = await self._runtime.call(self.node_type, self.key, "open", args, action=True)
        return opened

    async def invite(self, player: int | str) -> bool:
        """The owner invites a player. Raises ``forbidden``, ``full``."""
        done: bool = await self._runtime.call(
            self.node_type, self.key, "invite", {"player": int(player)}, action=True
        )
        return done

    async def join(self) -> bool:
        """An invited player joins. Raises ``forbidden``."""
        done: bool = await self._runtime.call(self.node_type, self.key, "join", None, action=True)
        return done

    async def enter(self) -> str:
        """Resolves the room the party is at; returns what happened, as the log has it. Raises
        ``not_member``, ``forbidden`` (the caller is down), ``conflict`` (descend first),
        ``finished``."""
        line: str = await self._runtime.call(self.node_type, self.key, "enter", None, action=True)
        return line

    async def descend(self) -> int:
        """The owner leads the party to the next floor; returns it. Raises ``not_ready`` (rooms
        left), ``forbidden``."""
        floor: int = await self._runtime.call(
            self.node_type, self.key, "descend", None, action=True
        )
        return floor

    async def abandon(self) -> bool:
        """The owner (or a developer) ends the run."""
        done: bool = await self._runtime.call(
            self.node_type, self.key, "abandon", None, action=True
        )
        return done
