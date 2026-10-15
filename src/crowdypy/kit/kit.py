"""The app-scoped Game Kit: ``client.kit(app_id)``. CrowdyJS ``src/kit/kit.ts``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from crowdypy.kit.modules.arenas import ArenasKit
from crowdypy.kit.modules.encounters import EncountersKit
from crowdypy.kit.modules.instances import InstancesKit
from crowdypy.kit.modules.leaderboards import LeaderboardsKit
from crowdypy.kit.modules.market import MarketCore, MarketsKit, ShopsKit, TradesKit
from crowdypy.kit.modules.matchmaking import MatchmakingKit
from crowdypy.kit.modules.minigames import MinigamesKit
from crowdypy.kit.modules.npcs import NpcsKit
from crowdypy.kit.modules.player import (
    AchievementsKit,
    CraftingKit,
    InventoryKit,
    LootKit,
    PlayerKit,
    ProductionKit,
    ProgressionKit,
    QuestsKit,
    WalletKit,
)
from crowdypy.kit.modules.presence import EmotesKit, SpectateKit
from crowdypy.kit.modules.racing import RacingKit
from crowdypy.kit.modules.sessions import SessionsKit
from crowdypy.kit.modules.tables import TablesKit
from crowdypy.kit.modules.tactics import TacticsKit
from crowdypy.kit.modules.territory import TerritoryKit
from crowdypy.kit.modules.world import WorldKit
from crowdypy.kit.protocol import shard_key, shard_keys_around
from crowdypy.kit.runtime import KitRuntime
from crowdypy.kit.social import SocialKit
from crowdypy.kit.topology import KitTopology

if TYPE_CHECKING:
    from crowdypy.domains.channels import ChannelsAPI
    from crowdypy.domains.exec import ExecAPI
    from crowdypy.domains.game_apps import GameAppsAPI
    from crowdypy.domains.teams import TeamsAPI
    from crowdypy.domains.udp import UdpAPI
    from crowdypy.exec_gateway import AsyncExecConnection

__all__ = ["GameKitClient", "ShardKit"]


class ShardKit:
    """Shard keys: the hub for where a player stands, computed the way the hubs compute it."""

    def key(self, x: int, z: int, size: int) -> str:
        """The key of the ``size``-chunk shard holding chunk ``(x, z)``."""
        return shard_key(x, z, size)

    def around(self, x: int, z: int, size: int, radius: int = 1) -> list[str]:
        """The keys of the shards within ``radius`` of the one holding ``(x, z)``, its own first."""
        return shard_keys_around(x, z, size, radius)


class GameKitClient:
    """The app-scoped **Game Kit** that ``client.kit(app_id)`` returns: typed clients for the
    ck-exec starter hubs (the root :attr:`world` with liveops, :attr:`matchmaking` queues with
    parties, game :attr:`sessions`, areas of :attr:`npcs` with mobs and pets,
    :attr:`leaderboards`, card :attr:`tables`, :attr:`tactics` battles, :attr:`encounters`,
    :attr:`minigames`, private :attr:`instances`, :attr:`arenas`, :attr:`racing`,
    :attr:`territory`), the :attr:`topology` that deploys them, the client-side conventions (:attr:`spectate`,
    :attr:`emotes`, :attr:`shards`), and :attr:`social` (parties, guilds, chat over teams and
    channels).

    The hub modules share one connection (:attr:`runtime`), opened when first needed, which paces
    calls under the gateway's limit, resends safely (a change carries an action id the hub
    answers once), and mirrors topics with resync on gaps and reconnects. ``connection``,
    ``connect``, ``developer``, ``call_budget``, ``budget_window_ms``, ``retries`` and
    ``player_id`` are :class:`~crowdypy.kit.runtime.KitRuntime`'s; ``nodes`` renames the starter
    types a game deployed under other names (``world``, ``matchmaker``, ``session``, ``npcs``,
    ``leaderboard``, ``deck``, ``tactics``, ``director``, ``minigame``, ``instance``, ``arena``,
    ``race``, ``ball``, ``territory``, ``zone``, ``player``, ``market``). The economy
    (:attr:`player` and its views, :attr:`market`, :attr:`shops`, :attr:`trades`) lives in hub
    state and is not a ledger.
    """

    def __init__(
        self,
        app_id: str | int,
        game_apps: GameAppsAPI,
        *,
        teams: TeamsAPI | None = None,
        channels: ChannelsAPI | None = None,
        udp: UdpAPI | None = None,
        exec: ExecAPI | None = None,
        who_am_i: Callable[[], Awaitable[str | None]] | None = None,
        actor_uuid: str | None = None,
        party_prefix: str = "party:",
        guild_prefix: str = "guild:",
        connection: AsyncExecConnection | None = None,
        connect: dict[str, Any] | None = None,
        developer: bool = False,
        call_budget: int = 100,
        budget_window_ms: float = 10_000,
        retries: int = 2,
        player_id: str | int | None = None,
        nodes: dict[str, str] | None = None,
        world_key: str = "",
        area_size: int = 8,
    ) -> None:
        self.app_id = str(app_id)
        #: Parties, guilds and chat rooms.
        self.social = SocialKit(
            app_id, teams, channels, udp, game_apps,
            actor_uuid=actor_uuid, party_prefix=party_prefix, guild_prefix=guild_prefix,
        )  # fmt: skip
        #: The shared connection to the app's hubs.
        self.runtime = KitRuntime(
            app_id, exec,
            connection=connection, connect=connect, developer=developer,
            call_budget=call_budget, budget_window_ms=budget_window_ms, retries=retries,
            player_id=player_id, who_am_i=who_am_i,
        )  # fmt: skip
        self._exec = exec
        self._udp = udp
        self._nodes = dict(nodes or {})
        self._world_key = world_key
        self._area_size = area_size
        self._world: WorldKit | None = None
        self._matchmaking: MatchmakingKit | None = None
        self._sessions: SessionsKit | None = None
        self._npcs: NpcsKit | None = None
        self._leaderboards: LeaderboardsKit | None = None
        self._tables: TablesKit | None = None
        self._tactics: TacticsKit | None = None
        self._encounters: EncountersKit | None = None
        self._minigames: MinigamesKit | None = None
        self._instances: InstancesKit | None = None
        self._arenas: ArenasKit | None = None
        self._racing: RacingKit | None = None
        self._territory: TerritoryKit | None = None
        self._player: PlayerKit | None = None
        self._views: dict[str, Any] = {}
        self._markets: dict[str, MarketCore] = {}
        self._topology: KitTopology | None = None
        self._spectate: SpectateKit | None = None
        self._emotes: EmotesKit | None = None
        self._shards: ShardKit | None = None

    @property
    def world(self) -> WorldKit:
        """The root world hub: conditions, resource nodes, liveops."""
        if self._world is None:
            self._world = WorldKit(
                self.runtime, node_type=self._nodes.get("world", "world"), key=self._world_key
            )
        return self._world

    @property
    def matchmaking(self) -> MatchmakingKit:
        """Matchmaking queues, with parties matched whole."""
        if self._matchmaking is None:
            self._matchmaking = MatchmakingKit(
                self.runtime, node_type=self._nodes.get("matchmaker", "matchmaker")
            )
        return self._matchmaking

    @property
    def sessions(self) -> SessionsKit:
        """Game sessions: lobby, ready checks, turns, scores."""
        if self._sessions is None:
            self._sessions = SessionsKit(
                self.runtime, node_type=self._nodes.get("session", "session")
            )
        return self._sessions

    @property
    def npcs(self) -> NpcsKit:
        """Areas of mobs, NPCs and players' pets."""
        if self._npcs is None:
            self._npcs = NpcsKit(
                self.runtime, node_type=self._nodes.get("npcs", "npcs"), area_size=self._area_size
            )
        return self._npcs

    def _node(self, name: str) -> str:
        return self._nodes.get(name, name)

    @property
    def leaderboards(self) -> LeaderboardsKit:
        """Leaderboards the hubs rank (a client never submits its own score)."""
        if self._leaderboards is None:
            self._leaderboards = LeaderboardsKit(self.runtime, node_type=self._node("leaderboard"))
        return self._leaderboards

    @property
    def tables(self) -> TablesKit:
        """Card tables with hidden hands the hub holds."""
        if self._tables is None:
            self._tables = TablesKit(self.runtime, node_type=self._node("deck"))
        return self._tables

    @property
    def tactics(self) -> TacticsKit:
        """Grid battles in turns, against the hub's units."""
        if self._tactics is None:
            self._tactics = TacticsKit(self.runtime, node_type=self._node("tactics"))
        return self._tactics

    @property
    def encounters(self) -> EncountersKit:
        """Encounters: waves down a lane, towers, a boss with phases."""
        if self._encounters is None:
            self._encounters = EncountersKit(self.runtime, node_type=self._node("director"))
        return self._encounters

    @property
    def minigames(self) -> MinigamesKit:
        """Duels of hidden throws, revealed together."""
        if self._minigames is None:
            self._minigames = MinigamesKit(self.runtime, node_type=self._node("minigame"))
        return self._minigames

    @property
    def instances(self) -> InstancesKit:
        """Private runs for one party: seeded floors, permadeath, expiry."""
        if self._instances is None:
            self._instances = InstancesKit(self.runtime, node_type=self._node("instance"))
        return self._instances

    @property
    def arenas(self) -> ArenasKit:
        """Real-time arenas: classes, abilities, the hub's referee."""
        if self._arenas is None:
            self._arenas = ArenasKit(self.runtime, node_type=self._node("arena"))
        return self._arenas

    @property
    def racing(self) -> RacingKit:
        """Races and ball games, on positions the hub's warden keeps."""
        if self._racing is None:
            self._racing = RacingKit(
                self.runtime, race_type=self._node("race"), ball_type=self._node("ball")
            )
        return self._racing

    @property
    def territory(self) -> TerritoryKit:
        """Control points factions capture, and shrinking circles."""
        if self._territory is None:
            self._territory = TerritoryKit(
                self.runtime, territory_type=self._node("territory"), zone_type=self._node("zone")
            )
        return self._territory

    @property
    def player(self) -> PlayerKit:
        """The caller's own ``player`` hub: what they own and what they did."""
        if self._player is None:
            self._player = PlayerKit(self.runtime, node_type=self._node("player"))
        return self._player

    def _view(self, name: str, make: Callable[[], Any]) -> Any:
        if name not in self._views:
            self._views[name] = make()
        return self._views[name]

    @property
    def inventory(self) -> InventoryKit:
        """The player's items: stacks in slots, and mail for what did not fit."""
        view: InventoryKit = self._view("inventory", lambda: InventoryKit(self.player))
        return view

    @property
    def wallet(self) -> WalletKit:
        """The player's soft currencies."""
        view: WalletKit = self._view("wallet", lambda: WalletKit(self.player))
        return view

    @property
    def progression(self) -> ProgressionKit:
        """Experience, levels and public profiles."""
        view: ProgressionKit = self._view("progression", lambda: ProgressionKit(self.player))
        return view

    @property
    def achievements(self) -> AchievementsKit:
        view: AchievementsKit = self._view("achievements", lambda: AchievementsKit(self.player))
        return view

    @property
    def quests(self) -> QuestsKit:
        """Quests, first-session tutorials among them."""
        view: QuestsKit = self._view("quests", lambda: QuestsKit(self.player))
        return view

    @property
    def crafting(self) -> CraftingKit:
        view: CraftingKit = self._view("crafting", lambda: CraftingKit(self.player))
        return view

    @property
    def loot(self) -> LootKit:
        """Loot tables with pity."""
        view: LootKit = self._view("loot", lambda: LootKit(self.player))
        return view

    @property
    def production(self) -> ProductionKit:
        """Idle production that catches up on the time away."""
        view: ProductionKit = self._view("production", lambda: ProductionKit(self.player))
        return view

    def _market(self, key: str) -> MarketCore:
        core = self._markets.get(key)
        if core is None:
            core = self._markets[key] = MarketCore(self.runtime, self._node("market"), key)
        return core

    @property
    def market(self) -> MarketsKit:
        """Markets' order books."""
        view: MarketsKit = self._view("market", lambda: MarketsKit(self._market))
        return view

    @property
    def shops(self) -> ShopsKit:
        """Markets' vendors."""
        view: ShopsKit = self._view("shops", lambda: ShopsKit(self._market))
        return view

    @property
    def trades(self) -> TradesKit:
        """Trades between two players at a market."""
        view: TradesKit = self._view("trades", lambda: TradesKit(self._market))
        return view

    @property
    def topology(self) -> KitTopology:
        """Composing and deploying the app's tree of starter hubs (developers)."""
        if self._topology is None:
            self._topology = KitTopology(self.app_id, self._exec)
        return self._topology

    @property
    def spectate(self) -> SpectateKit:
        """Keeping a viewer with no avatar where the world's messages reach it."""
        if self._spectate is None:
            self._spectate = SpectateKit(self.app_id, self._udp)
        return self._spectate

    @property
    def emotes(self) -> EmotesKit:
        """Emotes between players, a client event every kit client reads."""
        if self._emotes is None:
            self._emotes = EmotesKit(self.app_id, self._udp)
        return self._emotes

    @property
    def shards(self) -> ShardKit:
        """Shard keys, as the hubs compute them."""
        if self._shards is None:
            self._shards = ShardKit()
        return self._shards

    async def close(self) -> None:
        """Closes the kit's connection (one it was given stays open)."""
        await self.runtime.close()
