"""Card tables (the ``deck`` starter). CrowdyJS ``src/kit/modules/tables.ts``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from crowdypy.kit.mirror import HubState, PrivateState

if TYPE_CHECKING:
    from crowdypy.kit.runtime import KitRuntime

__all__ = ["CardTable", "TablesKit"]


class TablesKit:
    """Card tables with hidden hands the hub holds: :meth:`table` by key."""

    def __init__(self, runtime: KitRuntime, *, node_type: str = "deck") -> None:
        self._runtime = runtime
        self._node_type = node_type
        self._tables: dict[str, CardTable] = {}

    def table(self, key: str) -> CardTable:
        t = self._tables.get(key)
        if t is None:
            t = self._tables[key] = CardTable(self._runtime, self._node_type, key)
        return t


class CardTable:
    """One card table: the public table on the ``table`` topic (``phase``, ``players``, ``host``,
    ``hand_counts``, ``top``, ``suit``, ``draw_count``, ``turn``, ``turn_ends``, ``last``,
    ``winner``, ``scores``), the caller's hand as private state (read again when the hub says it
    changed for them), turns in the hub's order."""

    def __init__(self, runtime: KitRuntime, node_type: str, key: str) -> None:
        self._runtime = runtime
        self.node_type = node_type
        self.key = key
        self.table = HubState(runtime, node_type, key, "table")

        async def read() -> Any:
            return await runtime.call(node_type, key, "hand")

        #: ``{"cards", "playable", "drawn"}``, never on the topic.
        self.hand = PrivateState(
            runtime, node_type=node_type, key=key, topic="table", what="hand", read=read
        )

    async def watch(self) -> dict[str, Any]:
        """Mirrors the table and the caller's hand; returns the table."""
        view: dict[str, Any] = await self.table.start()
        await self.hand.start()
        return view

    async def unwatch(self) -> None:
        await self.table.stop()
        await self.hand.stop()

    async def _call(self, method: str, args: Any = None) -> bool:
        done: bool = await self._runtime.call(self.node_type, self.key, method, args, action=True)
        return done

    async def join(self) -> bool:
        """Raises :class:`~crowdypy.kit.protocol.KitError` ``started``, ``full``."""
        return await self._call("join")

    async def leave(self) -> bool:
        return await self._call("leave")

    async def start(self) -> bool:
        """Deals (the host, from two players). Raises ``forbidden``, ``too_few_players``, ``started``."""
        return await self._call("start")

    async def play(self, card: str, suit: str | None = None) -> bool:
        """Plays a card from the hand; an eight names ``suit`` (``S``, ``H``, ``D`` or ``C``).
        Raises ``not_your_turn``, ``not_found``, ``not_playable``, ``bad_request``."""
        return await self._call(
            "play", {"card": card} if suit is None else {"card": card, "suit": suit}
        )

    async def draw(self) -> bool:
        """Draws a card (once a turn). Raises ``not_your_turn``, ``conflict``, ``empty``."""
        return await self._call("draw")

    async def pass_(self) -> bool:
        """Passes, after drawing (CrowdyJS ``pass``). Raises ``conflict`` (draw first)."""
        return await self._call("pass")
