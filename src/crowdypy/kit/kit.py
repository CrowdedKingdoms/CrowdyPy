"""The app-scoped Game Kit: ``client.kit(app_id)``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from crowdypy.kit.social import SocialKit

if TYPE_CHECKING:
    from crowdypy.domains.channels import ChannelsAPI
    from crowdypy.domains.game_apps import GameAppsAPI
    from crowdypy.domains.teams import TeamsAPI
    from crowdypy.domains.udp import UdpAPI

__all__ = ["GameKitClient"]


class GameKitClient:
    def __init__(
        self,
        app_id: str | int,
        game_apps: GameAppsAPI,
        *,
        teams: TeamsAPI | None = None,
        channels: ChannelsAPI | None = None,
        udp: UdpAPI | None = None,
        actor_uuid: str | None = None,
        party_prefix: str = "party:",
        guild_prefix: str = "guild:",
    ) -> None:
        #: Parties, guilds and chat rooms.
        self.social = SocialKit(
            app_id, teams, channels, udp, game_apps,
            actor_uuid=actor_uuid, party_prefix=party_prefix, guild_prefix=guild_prefix,
        )  # fmt: skip
