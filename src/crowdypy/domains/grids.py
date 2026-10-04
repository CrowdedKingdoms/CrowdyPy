"""Grid-scoped tokens and grid channels (``client.grids``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from crowdypy._generated import inputs
from crowdypy._generated import operations as ops
from crowdypy.domains._base import Domain
from crowdypy.utils import bigint

__all__ = ["GridsAPI"]


class GridsAPI(Domain):
    async def mint_token(
        self, input: inputs.MintGridTokenInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Narrow the app-scoped token this client holds to one grid.

        The result admits only a short fixed list of gameplay fields, each confined to the
        grid, and cannot refresh or sign out the token it came from: hand it to code that
        should act inside the grid and nowhere else. Needs an app-scoped token (not an
        identity session or another grid token) and ownership of the grid or
        ``run_client_code`` on it. ``ttlSeconds`` is 60-3600 (default 900).
        """
        result: dict[str, Any] = await self._request(ops.MINT_GRID_TOKEN, {"input": input})
        return result

    async def create_channel(
        self, input: inputs.CreateGridChannelInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Create a channel that belongs to a grid you own (at most 8 active per grid).

        The grid's player modules may ``emit_channel`` into it, and their messages carry the
        sender uuid ``grid:<gridId>``. ``membershipPolicy`` defaults to ``open`` so visitors
        can join.
        """
        result: dict[str, Any] = await self._request(ops.CREATE_GRID_CHANNEL, {"input": input})
        return result

    async def channels(self, app_id: str | int, grid_id: str | int) -> list[dict[str, Any]]:
        """The active channels of one grid, oldest first. Any token for the app may list them."""
        result: list[dict[str, Any]] = await self._request(
            ops.GRID_CHANNELS, {"appId": bigint(app_id), "gridId": bigint(grid_id)}
        )
        return result
