"""Leaderboards (the ``leaderboard`` starter). CrowdyJS ``src/kit/modules/leaderboards.ts``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from crowdypy.kit.mirror import HubState

if TYPE_CHECKING:
    from crowdypy.kit.runtime import KitRuntime

__all__ = ["LeaderboardHandle", "LeaderboardsKit"]


class LeaderboardsKit:
    """Boards the hubs rank: :meth:`board` by name."""

    def __init__(self, runtime: KitRuntime, *, node_type: str = "leaderboard") -> None:
        self._runtime = runtime
        self._node_type = node_type
        self._boards: dict[str, LeaderboardHandle] = {}

    def board(self, name: str) -> LeaderboardHandle:
        b = self._boards.get(name)
        if b is None:
            b = self._boards[name] = LeaderboardHandle(self._runtime, self._node_type, name)
        return b


class LeaderboardHandle:
    """One board. Hubs submit the scores they decided (a client never submits its own), and
    seasons roll on the board's schedule. :attr:`view` mirrors the ``board`` topic (``season``,
    ``season_started``, ``entries``, ``top``)."""

    def __init__(self, runtime: KitRuntime, node_type: str, name: str) -> None:
        self._runtime = runtime
        self.node_type = node_type
        self.name = name
        self.view = HubState(runtime, node_type, name, "board")

    async def watch(self) -> dict[str, Any]:
        view: dict[str, Any] = await self.view.start()
        return view

    async def unwatch(self) -> None:
        await self.view.stop()

    async def top(
        self, *, limit: int | None = None, offset: int | None = None
    ) -> list[dict[str, Any]]:
        """A page of the board, best first: ``[{"rank", "player", "score"}]``."""
        args: dict[str, Any] = {}
        if limit is not None:
            args["limit"] = limit
        if offset is not None:
            args["offset"] = offset
        page: list[dict[str, Any]] = await self._runtime.call(
            self.node_type, self.name, "top", args
        )
        return page

    async def around(
        self, *, player: int | str | None = None, radius: int | None = None
    ) -> list[dict[str, Any]]:
        """The places around a player (the caller's own, without ``player``)."""
        args: dict[str, Any] = {}
        if player is not None:
            args["player"] = int(player)
        if radius is not None:
            args["radius"] = radius
        page: list[dict[str, Any]] = await self._runtime.call(
            self.node_type, self.name, "around", args
        )
        return page

    async def me(self) -> dict[str, Any] | None:
        """The caller's own place, or ``None`` without a score this season."""
        place: dict[str, Any] | None = await self._runtime.call(self.node_type, self.name, "me")
        return place

    async def season(self) -> dict[str, Any]:
        """``{"season", "started", "entries", "last"}``."""
        season: dict[str, Any] = await self._runtime.call(self.node_type, self.name, "season")
        return season

    async def submit(self, player: int | str, score: int) -> int:
        """Submits a score for ``player``; returns the score kept. Developers only (a developer
        kit); players are refused ``forbidden``."""
        kept: int = await self._runtime.call(
            self.node_type,
            self.name,
            "submit",
            {"player": int(player), "score": score},
            action=True,
        )
        return kept

    async def configure(self, config: dict[str, Any]) -> bool:
        """Sets the board's config (``keep``, ``order``, ``top_size``, ``season_cron``,
        ``max_entries``). Developers only."""
        done: bool = await self._runtime.call(
            self.node_type, self.name, "configure", config, action=True
        )
        return done

    async def roll(self) -> int:
        """Ends the season now; returns the new season. Developers only."""
        season: int = await self._runtime.call(self.node_type, self.name, "roll", None, action=True)
        return season
