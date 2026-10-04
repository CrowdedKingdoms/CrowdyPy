"""Parties, guilds and chat rooms over teams, channels and ``client.udp``.

A party or a guild is a team and a channel of the same name (``party:`` / ``guild:`` prefixed),
so membership and chat travel together.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from crowdypy.errors import CrowdyError
from crowdypy.utils import generate_crowdy_uuid

if TYPE_CHECKING:
    from crowdypy.domains.channels import ChannelsAPI
    from crowdypy.domains.game_apps import GameAppsAPI
    from crowdypy.domains.teams import TeamsAPI
    from crowdypy.domains.udp import UdpAPI
    from crowdypy.replication import Notification

__all__ = ["KitChatMessage", "KitGroupWithChannel", "SocialKit"]


@dataclass(frozen=True, slots=True)
class KitGroupWithChannel:
    team_id: str
    channel_id: str
    name: str


@dataclass(frozen=True, slots=True)
class KitChatMessage:
    channel_id: str
    sender_uuid: str
    text: str
    epoch_ms: int


class SocialKit:
    def __init__(
        self,
        app_id: str | int,
        teams: TeamsAPI | None,
        channels: ChannelsAPI | None,
        udp: UdpAPI | None,
        game_apps: GameAppsAPI,
        *,
        actor_uuid: str | None = None,
        party_prefix: str = "party:",
        guild_prefix: str = "guild:",
    ) -> None:
        self.app_id = str(app_id)
        self._teams = teams
        self._channels = channels
        self._udp = udp
        self._game_apps = game_apps
        self.actor_uuid = actor_uuid or generate_crowdy_uuid()
        self._party_prefix = party_prefix
        self._guild_prefix = guild_prefix
        self.party = _Parties(self)
        self.guild = _Guilds(self)
        self.chat = _Chat(self)

    def _need_teams(self) -> TeamsAPI:
        if self._teams is None:
            raise CrowdyError(
                "kit.social needs the teams domain: build the kit with client.kit(app_id)"
            )
        return self._teams

    def _need_channels(self) -> ChannelsAPI:
        if self._channels is None:
            raise CrowdyError(
                "kit.social needs the channels domain: build the kit with client.kit(app_id)"
            )
        return self._channels

    def _need_udp(self) -> UdpAPI:
        if self._udp is None:
            raise CrowdyError(
                "kit.social needs the udp domain: build the kit with client.kit(app_id)"
            )
        return self._udp

    async def _create_pair(
        self, name: str, policy: str | None, description: str
    ) -> KitGroupWithChannel:
        team_input: dict[str, Any] = {
            "appId": self.app_id,
            "name": name,
            "description": description,
        }
        if policy is not None:
            team_input["membershipPolicy"] = policy
        team = await self._need_teams().create(team_input)
        channel = await self._need_channels().create(
            {"appId": self.app_id, "name": name, "description": f"Chat for {name}"}
        )
        return KitGroupWithChannel(str(team["groupId"]), str(channel["groupId"]), name)

    async def _find_pair(self, name: str) -> KitGroupWithChannel | None:
        teams = await self._need_teams().list(self.app_id)
        channels = await self._need_channels().list(self.app_id)
        team = next((t for t in teams if t.get("name") == name), None)
        if team is None:
            return None
        channel = next((c for c in channels if c.get("name") == name), None)
        return KitGroupWithChannel(
            str(team["groupId"]), str(channel["groupId"]) if channel else "", name
        )


class _Parties:
    def __init__(self, kit: SocialKit) -> None:
        self._kit = kit

    async def create(self, name: str) -> KitGroupWithChannel:
        return await self._kit._create_pair(f"{self._kit._party_prefix}{name}", "invite", "Party")

    async def find(self, name: str) -> KitGroupWithChannel | None:
        return await self._kit._find_pair(f"{self._kit._party_prefix}{name}")

    async def invite(self, party: KitGroupWithChannel, user_id: str | int) -> Any:
        member = await self._kit._need_teams().add_member(party.team_id, user_id)
        if party.channel_id:
            await self._kit._need_channels().add_member(party.channel_id, user_id)
        return member

    async def join(self, party: KitGroupWithChannel) -> Any:
        member = await self._kit._need_teams().join(party.team_id)
        if party.channel_id:
            await self._kit._need_channels().join(party.channel_id)
        return member

    async def leave(self, party: KitGroupWithChannel) -> Any:
        if party.channel_id:
            await self._kit._need_channels().leave(party.channel_id)
        return await self._kit._need_teams().leave(party.team_id)

    async def members(self, party: KitGroupWithChannel) -> Any:
        return await self._kit._need_teams().members(party.team_id)


class _Guilds:
    def __init__(self, kit: SocialKit) -> None:
        self._kit = kit

    async def create(
        self, name: str, *, membership_policy: str = "request", description: str = "Guild"
    ) -> KitGroupWithChannel:
        return await self._kit._create_pair(
            f"{self._kit._guild_prefix}{name}", membership_policy, description
        )

    async def find(self, name: str) -> KitGroupWithChannel | None:
        return await self._kit._find_pair(f"{self._kit._guild_prefix}{name}")

    async def roster(self, guild: KitGroupWithChannel) -> Any:
        return await self._kit._need_teams().members(guild.team_id)

    async def roles(self, guild: KitGroupWithChannel) -> Any:
        return await self._kit._need_teams().roles(guild.team_id)

    async def create_role(
        self,
        guild: KitGroupWithChannel,
        role_name: str,
        *,
        permissions: list[str] | None = None,
        rank: int | None = None,
    ) -> Any:
        role: dict[str, Any] = {"groupId": guild.team_id, "roleName": role_name}
        if permissions is not None:
            role["permissions"] = permissions
        if rank is not None:
            role["rank"] = rank
        return await self._kit._need_teams().create_role(role)

    async def promote(
        self, guild: KitGroupWithChannel, user_id: str | int, role_ids: list[str]
    ) -> Any:
        return await self._kit._need_teams().set_member_roles(
            {"groupId": guild.team_id, "userId": str(user_id), "roleIds": role_ids}
        )

    async def claim_territory(
        self,
        guild: KitGroupWithChannel,
        grid_id: str | int,
        *,
        permission_keys: list[str] | None = None,
        group_role_id: str | None = None,
    ) -> Any:
        """Grant the guild permissions on a grid (by default ``access`` and
        ``update_voxel_data``)."""
        grant: dict[str, Any] = {
            "appId": self._kit.app_id,
            "gridId": str(grid_id),
            "groupId": guild.team_id,
            "permissionKeys": permission_keys or ["access", "update_voxel_data"],
        }
        if group_role_id is not None:
            grant["groupRoleId"] = group_role_id
        return await self._kit._game_apps.assign_group(grant)


class _Chat:
    def __init__(self, kit: SocialKit) -> None:
        self._kit = kit

    async def room(self, name: str) -> Any:
        """The channel named ``name``, created when it does not exist."""
        channels = self._kit._need_channels()
        existing = next(
            (c for c in await channels.list(self._kit.app_id) if c.get("name") == name), None
        )
        if existing is not None:
            return existing
        return await channels.create({"appId": self._kit.app_id, "name": name})

    async def join(self, channel_id: str | int) -> Any:
        return await self._kit._need_channels().join(channel_id)

    async def send(self, channel_id: str | int, text: str) -> int:
        """Send UTF-8 text to a channel over ``client.udp``; returns the sequence."""
        return await self._kit._need_udp().send_channel_message(
            channel_id, self._kit.actor_uuid, text.encode("utf-8")
        )

    def on_message(
        self, channel_id: str | int, callback: Callable[[KitChatMessage], Any]
    ) -> Callable[[], None]:
        """Call ``callback`` for each message on one channel. Returns the unsubscribe function."""
        wanted = str(channel_id)

        def deliver(notification: Notification) -> None:
            if str(notification.channel_id) != wanted:
                return
            text = notification.payload.decode("utf-8", errors="replace")
            callback(KitChatMessage(wanted, notification.uuid, text, notification.epoch_ms))

        return self._kit._need_udp().subscribe({"channel_message": deliver})
