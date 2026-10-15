"""The caller's own ``player`` hub (the ``player`` starter) and its views: inventory, wallet,
progression, achievements, quests, crafting, loot and production. CrowdyJS
``src/kit/modules/player.ts``. The economy lives in hub state and is not a ledger."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from crowdypy._concurrency import AsyncSingleFlight
from crowdypy.errors import CrowdyError
from crowdypy.kit.mirror import HubState, PrivateState
from crowdypy.kit.store import WritableKitStore

if TYPE_CHECKING:
    from crowdypy.kit.runtime import KitRuntime

__all__ = [
    "AchievementsKit",
    "CraftingKit",
    "InventoryKit",
    "LootKit",
    "PlayerKit",
    "ProductionKit",
    "ProgressionKit",
    "QuestsKit",
    "WalletKit",
]

TOPIC = "player"
PARTS = ("profile", "inventory", "wallet", "progress")


class _Relay(WritableKitStore[Any]):
    """A store fed by another once that one exists."""

    def __init__(self) -> None:
        super().__init__(None)

    def attach(self, source: Any) -> None:
        source.subscribe(self.set)
        if source.get() is not None:
            self.set(source.get())


class PlayerKit:
    """The caller's own ``player`` hub, keyed by their player id: what they own and what they
    did. Its stores (:attr:`profile`, :attr:`inventory`, :attr:`wallet`, :attr:`progress`) exist
    from the start and fill once :meth:`watch` has read them; only the player reads inventory,
    wallet and progress, and each is read again when the hub says it changed."""

    def __init__(
        self, runtime: KitRuntime, *, node_type: str = "player", key: str | None = None
    ) -> None:
        self._runtime = runtime
        self.node_type = node_type
        self._key = key
        self.profile = _Relay()
        self.inventory = _Relay()
        self.wallet = _Relay()
        self.progress = _Relay()
        self._parts: dict[str, Any] | None = None
        self._making: AsyncSingleFlight[dict[str, Any]] = AsyncSingleFlight()
        self._rules: Any = None
        self._listeners: builtins.list[Callable[[dict[str, Any]], Any]] = []

    async def key(self) -> str:
        """The hub's key: the player's id."""
        key = self._key if self._key is not None else await self._runtime.player_id()
        if key is None:
            raise CrowdyError(
                "the kit does not know which player it is: pass player_id to client.kit()"
            )
        return str(key)

    async def _ready(self) -> dict[str, Any]:
        if self._parts is None:
            self._parts = await self._making.run(self._make)
        return self._parts

    async def _make(self) -> dict[str, Any]:
        key = await self.key()
        runtime, node_type = self._runtime, self.node_type

        def reader(method: str) -> Any:
            async def read() -> Any:
                return await runtime.call(node_type, key, method)

            return read

        parts: dict[str, Any] = {"profile": HubState(runtime, node_type, key, TOPIC)}
        for what in PARTS[1:]:
            parts[what] = PrivateState(
                runtime, node_type=node_type, key=key, topic=TOPIC, what=what, read=reader(what)
            )
        for what in PARTS:
            getattr(self, what).attach(parts[what])
        parts["profile"].on_event(self._announce)
        return parts

    def _announce(self, body: dict[str, Any], _header: Any) -> None:
        for listener in list(self._listeners):
            listener(body)

    async def watch(self, *which: str) -> None:
        """Mirrors the profile and reads what only the player reads (all of it, or the parts
        named: ``profile``, ``inventory``, ``wallet``, ``progress``)."""
        parts = await self._ready()
        for what in which or PARTS:
            await parts[what].start()

    async def unwatch(self) -> None:
        if self._parts is None:
            return
        for what in PARTS:
            await self._parts[what].stop()

    def on_event(self, listener: Callable[[dict[str, Any]], Any]) -> Callable[[], None]:
        """Levels gained and achievements unlocked, as the ``player`` topic announces them."""
        self._listeners.append(listener)

        def stop() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return stop

    async def call(self, method: str, args: Any = None, *, change: bool = True) -> Any:
        """Calls the player's hub; a change carries an action id."""
        return await self._runtime.call(
            self.node_type, await self.key(), method, args, action=change
        )

    async def me(self) -> dict[str, Any]:
        """Everything the hub keeps for the player."""
        view: dict[str, Any] = await self.call("me", change=False)
        return view

    async def rules(self) -> dict[str, Any]:
        """The game's items, recipes, loot tables, quests and achievements (read once)."""
        if self._rules is None:
            self._rules = await self.call("rules", change=False)
        rules: dict[str, Any] = self._rules
        return rules

    async def profile_of(self, player: int | str) -> dict[str, Any]:
        """Another player's public profile."""
        profile: dict[str, Any] = await self._runtime.call(self.node_type, str(player), "profile")
        return profile


class InventoryKit:
    """The player's items: stacks in slots, and mail for what did not fit."""

    def __init__(self, player: PlayerKit) -> None:
        self._player = player

    @property
    def items(self) -> _Relay:
        return self._player.inventory

    async def watch(self) -> dict[str, Any] | None:
        await self._player.watch("inventory")
        view: dict[str, Any] | None = self._player.inventory.get()
        return view

    def count(self, item: str) -> int:
        """How many of ``item`` the player has, as last read."""
        stacks = (self._player.inventory.get() or {}).get("stacks") or []
        return sum(s["count"] for s in stacks if s["item"] == item)

    async def discard(self, item: str, count: int) -> bool:
        """Raises :class:`~crowdypy.kit.protocol.KitError` ``insufficient``."""
        done: bool = await self._player.call("discard", {"item": item, "count": count})
        return done

    async def take_mail(self) -> dict[str, Any]:
        """Moves what fits from the mail into the inventory; returns what moved."""
        moved: dict[str, Any] = await self._player.call("take_mail")
        return moved


class WalletKit:
    """The player's soft currencies."""

    def __init__(self, player: PlayerKit) -> None:
        self._player = player

    @property
    def balances(self) -> _Relay:
        return self._player.wallet

    async def watch(self) -> dict[str, Any] | None:
        await self._player.watch("wallet")
        view: dict[str, Any] | None = self._player.wallet.get()
        return view

    def balance(self, currency: str) -> int:
        """The balance of ``currency``, as last read."""
        balance: int = ((self._player.wallet.get() or {}).get("balances") or {}).get(currency, 0)
        return balance


class ProgressionKit:
    """Experience and levels, and public profiles."""

    def __init__(self, player: PlayerKit) -> None:
        self._player = player

    @property
    def progress(self) -> _Relay:
        return self._player.progress

    @property
    def profile(self) -> _Relay:
        return self._player.profile

    async def watch(self) -> dict[str, Any] | None:
        await self._player.watch("profile", "progress")
        view: dict[str, Any] | None = self._player.progress.get()
        return view

    async def profile_of(self, player: int | str) -> dict[str, Any]:
        return await self._player.profile_of(player)

    def on_level(self, listener: Callable[[int], Any]) -> Callable[[], None]:
        """Called with each level the player reaches."""
        return self._player.on_event(
            lambda e: listener(e["level"]) if e.get("type") == "level" else None
        )


class AchievementsKit:
    """Achievements: counts of what the player did, unlocked once."""

    def __init__(self, player: PlayerKit) -> None:
        self._player = player

    async def watch(self) -> builtins.list[dict[str, Any]]:
        await self._player.watch("profile", "progress")
        return self.list()

    def list(self) -> builtins.list[dict[str, Any]]:
        """The achievements with their counts, as last read (hidden ones once unlocked)."""
        achievements: builtins.list[dict[str, Any]] = (self._player.progress.get() or {}).get(
            "achievements"
        ) or []
        return achievements

    def on_unlock(self, listener: Callable[[str], Any]) -> Callable[[], None]:
        """Called with each achievement the player unlocks."""
        return self._player.on_event(
            lambda e: listener(e["achievement"]) if e.get("type") == "achievement" else None
        )


class QuestsKit:
    """Quests, first-session tutorials among them."""

    def __init__(self, player: PlayerKit) -> None:
        self._player = player

    async def watch(self) -> builtins.list[dict[str, Any]]:
        await self._player.watch("progress")
        return self.list()

    def list(self) -> builtins.list[dict[str, Any]]:
        quests: builtins.list[dict[str, Any]] = (self._player.progress.get() or {}).get(
            "quests"
        ) or []
        return quests

    async def accept(self, quest: str) -> bool:
        """Raises :class:`~crowdypy.kit.protocol.KitError` ``conflict``, ``forbidden`` (level),
        ``not_found``."""
        done: bool = await self._player.call("accept", {"quest": quest})
        return done

    async def claim(self, quest: str) -> dict[str, Any]:
        """Claims a complete quest's reward. Raises ``not_ready``, ``full``."""
        reward: dict[str, Any] = await self._player.call("claim", {"quest": quest})
        return reward

    async def step(self, counter: str) -> dict[str, Any]:
        """Reports a counter the game lets a client report (a tutorial step). Raises
        ``forbidden`` for one the game's hubs count."""
        earned: dict[str, Any] = await self._player.call("step", {"counter": counter})
        return earned


class CraftingKit:
    """Crafting from the game's recipes."""

    def __init__(self, player: PlayerKit) -> None:
        self._player = player

    async def recipes(self) -> list[dict[str, Any]]:
        recipes: list[dict[str, Any]] = (await self._player.rules())["recipes"]
        return recipes

    async def craft(self, recipe: str, times: int | None = None) -> dict[str, Any]:
        """Crafts ``times`` (once without). Raises ``insufficient``, ``full``, ``forbidden``
        (level), ``not_found``."""
        args: dict[str, Any] = (
            {"recipe": recipe} if times is None else {"recipe": recipe, "times": times}
        )
        crafted: dict[str, Any] = await self._player.call("craft", args)
        return crafted


class LootKit:
    """Loot tables with pity."""

    def __init__(self, player: PlayerKit) -> None:
        self._player = player

    async def tables(self) -> list[dict[str, Any]]:
        tables: list[dict[str, Any]] = (await self._player.rules())["loot"]
        return tables

    async def open(self, table: str) -> dict[str, Any]:
        """Pays the table's cost and rolls once. Raises ``insufficient``, ``full``, ``not_found``."""
        opened: dict[str, Any] = await self._player.call("open", {"table": table})
        return opened


class ProductionKit:
    """Idle production that catches up on the time away."""

    def __init__(self, player: PlayerKit) -> None:
        self._player = player

    async def view(self) -> dict[str, Any]:
        production: dict[str, Any] = (await self._player.me())["production"]
        return production

    async def collect(self) -> dict[str, Any]:
        """Moves what the producers made into the inventory, as far as it fits."""
        collected: dict[str, Any] = await self._player.call("collect")
        return collected
