"""Races and ball games (the ``racing`` starter, types ``race`` and ``ball``). CrowdyJS
``src/kit/modules/racing.ts``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from crowdypy.kit.mirror import HubState

if TYPE_CHECKING:
    from crowdypy.kit.runtime import KitRuntime

__all__ = ["BallMatch", "Race", "RacingKit"]


class RacingKit:
    """Races (:meth:`race`) and ball games (:meth:`ball`), by key."""

    def __init__(
        self, runtime: KitRuntime, *, race_type: str = "race", ball_type: str = "ball"
    ) -> None:
        self._runtime = runtime
        self._race_type = race_type
        self._ball_type = ball_type
        self._races: dict[str, Race] = {}
        self._matches: dict[str, BallMatch] = {}

    def race(self, key: str) -> Race:
        r = self._races.get(key)
        if r is None:
            r = self._races[key] = Race(self._runtime, self._race_type, key)
        return r

    def ball(self, key: str) -> BallMatch:
        m = self._matches.get(key)
        if m is None:
            m = self._matches[key] = BallMatch(self._runtime, self._ball_type, key)
        return m


class Race:
    """One race: a course of gates passed in order, a countdown, laps and splits, places in
    finishing order, and a ghost of the winner's run. A client reports where its racer is
    (:meth:`pos`, a few times a second) and the hub times the positions its movement warden kept.
    :attr:`view` mirrors the ``race`` topic (``phase``, ``course_id``, ``gates``, ``laps``,
    ``go_at``, ``closes_at``, ``racers``); ``view.on_event`` hears ``go``, ``timing`` (with its
    ``stage``: ``started``, ``checkpoint``, ``lap``, ``finished``), ``violation`` and
    ``finished``."""

    def __init__(self, runtime: KitRuntime, node_type: str, key: str) -> None:
        self._runtime = runtime
        self.node_type = node_type
        self.key = key
        self.view = HubState(runtime, node_type, key, "race")

    async def watch(self) -> dict[str, Any]:
        view: dict[str, Any] = await self.view.start()
        return view

    async def unwatch(self) -> None:
        await self.view.stop()

    async def _call(self, method: str) -> bool:
        done: bool = await self._runtime.call(self.node_type, self.key, method, None, action=True)
        return done

    async def join(self) -> bool:
        """Raises ``started``, ``full``."""
        return await self._call("join")

    async def leave(self) -> bool:
        return await self._call("leave")

    async def start(self) -> bool:
        """Starts the countdown (the first to join, or a developer). Raises ``forbidden``, ``started``."""
        return await self._call("start")

    async def pos(self, x: float, z: float) -> list[float]:
        """Reports where the racer is; returns ``[x, z]``, the position the hub kept."""
        kept: list[float] = await self._runtime.call(
            self.node_type, self.key, "pos", {"x": x, "z": z}
        )
        return kept

    async def ghost(self) -> dict[str, Any] | None:
        """The winner's run (``player``, ``total_ms``, ``frames`` of ``[ms, x, y, z, yaw]``), once
        there is one."""
        run: dict[str, Any] | None = await self._runtime.call(self.node_type, self.key, "ghost")
        return run


class BallMatch:
    """One ball game: two teams, a ball the hub moves (passes, shots, friction, the side walls),
    possession (claim a loose ball near it, or take it from an opponent whose protection is over),
    a goal in each end wall. :attr:`view` mirrors the ``ball`` topic (``phase``, ``width``,
    ``depth``, ``goal_width``, ``ball``, ``players``, ``score`` as red's then blue's, ``ends_at``,
    ``winner``); ``view.on_event`` hears ``claimed``, ``passed``, ``shot``, ``goal``,
    ``violation`` and ``finished``."""

    def __init__(self, runtime: KitRuntime, node_type: str, key: str) -> None:
        self._runtime = runtime
        self.node_type = node_type
        self.key = key
        self.view = HubState(runtime, node_type, key, "ball")

    async def watch(self) -> dict[str, Any]:
        view: dict[str, Any] = await self.view.start()
        return view

    async def unwatch(self) -> None:
        await self.view.stop()

    async def _call(self, method: str, args: Any = None) -> bool:
        done: bool = await self._runtime.call(self.node_type, self.key, method, args, action=True)
        return done

    async def join(self, team: str) -> bool:
        """``team`` is ``red`` or ``blue``. Raises ``bad_request``, ``full``, ``started``."""
        return await self._call("join", {"team": team})

    async def start(self) -> bool:
        """Raises ``forbidden``, ``not_ready`` (each team needs a player), ``started``."""
        return await self._call("start")

    async def pos(self, x: float, z: float) -> list[float]:
        """Reports where the player is; returns ``[x, z]``, the position the hub kept."""
        kept: list[float] = await self._runtime.call(
            self.node_type, self.key, "pos", {"x": x, "z": z}
        )
        return kept

    async def claim(self) -> bool:
        """Claims the ball. Raises ``out_of_reach``, ``cooldown`` (too soon, or its holder's
        protection), ``conflict`` (a teammate has it)."""
        return await self._call("claim")

    async def pass_(self, to: int | str) -> bool:
        """Passes to a teammate (CrowdyJS ``pass``). Raises ``conflict`` (not the holder),
        ``bad_request`` (not a teammate)."""
        return await self._call("pass", {"to": int(to)})

    async def shoot(self, x: float, z: float) -> bool:
        """Shoots toward ``(x, z)``. Raises ``conflict`` (not the holder)."""
        return await self._call("shoot", {"x": x, "z": z})
