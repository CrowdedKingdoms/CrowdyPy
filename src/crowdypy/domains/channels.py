"""Messaging channels: membership, roles and the channel policy (``client.channels``).

A channel is an app-scoped, location-independent subscriber set with role-gated
messaging, built on the same groups as teams. These methods manage who belongs to a
channel and what each member may do; they never carry a message. Messages travel over
the replication connection: a member holding ``send_messages`` publishes, and every
active member receives it.

Channels are gameplay: call these on a per-game client holding the app-scoped token
(``client.portal.mint_app_token``). Ids are BigInts and accept an ``int`` or a decimal
string. Membership, role and policy changes need a channel permission (``manage_group``,
``manage_members``, ``manage_roles``) or app-admin (``manage_apps``); a refusal raises
:class:`~crowdypy.errors.CrowdyGraphQLError` with a code such as ``UNAUTHENTICATED``,
``SCOPE_MISSING`` or ``FORBIDDEN``.
"""

from __future__ import annotations

import builtins  # ChannelsAPI.list shadows the builtin in the class's annotations
from collections.abc import Mapping
from typing import Any

from crowdypy._generated import inputs
from crowdypy._generated import operations as ops
from crowdypy.domains._base import Domain
from crowdypy.utils import bigint

__all__ = ["ChannelsAPI"]


class ChannelsAPI(Domain):
    async def mine(self, app_id: str | int) -> builtins.list[dict[str, Any]]:
        """The caller's channels in an app, with the caller's roles and effective permissions.

        Use it to find the channels the caller can read and post in (``send_messages``).
        """
        result: builtins.list[dict[str, Any]] = await self._request(
            ops.MY_CHANNELS, {"appId": bigint(app_id)}
        )
        return result

    async def list(self, app_id: str | int) -> builtins.list[dict[str, Any]]:
        """Every active channel in an app, not only the caller's (that is :meth:`mine`)."""
        result: builtins.list[dict[str, Any]] = await self._request(
            ops.CHANNELS, {"appId": bigint(app_id)}
        )
        return result

    async def get(self, group_id: str | int) -> dict[str, Any]:
        """One channel by its group id. Raises when the id is not a channel (a team, or nothing)."""
        result: dict[str, Any] = await self._request(ops.CHANNEL, {"groupId": bigint(group_id)})
        return result

    async def members(self, group_id: str | int) -> builtins.list[dict[str, Any]]:
        """A channel's members, pending join requests included, with their status and roles."""
        result: builtins.list[dict[str, Any]] = await self._request(
            ops.CHANNEL_MEMBERS, {"groupId": bigint(group_id)}
        )
        return result

    async def roles(self, group_id: str | int) -> builtins.list[dict[str, Any]]:
        """A channel's roles and the permission keys each grants.

        They include the system ``leader`` role and any default ``member`` role, which
        typically grants ``send_messages``.
        """
        result: builtins.list[dict[str, Any]] = await self._request(
            ops.CHANNEL_ROLES, {"groupId": bigint(group_id)}
        )
        return result

    async def policy(self, app_id: str | int) -> dict[str, Any]:
        """The app's channel policy: who may create channels, and new channels' membership policy.

        Falls back to the app defaults when none is set.
        """
        result: dict[str, Any] = await self._request(ops.CHANNEL_POLICY, {"appId": bigint(app_id)})
        return result

    async def create(self, input: inputs.CreateChannelInput | Mapping[str, Any]) -> dict[str, Any]:
        """Create a channel; the caller becomes its owner with the system ``leader`` role.

        The app's creation policy (``admin``, ``member`` or ``anyone``) decides whether the
        caller may. With ``members_can_send`` true (the default) joiners get a default
        ``member`` role granting ``send_messages``, an open chat channel; false makes an
        announce channel where only roles you grant may post. ``members_can_speak`` (default
        false) also gives that role ``send_voice``, the right to send channel audio
        (``client.udp.send_channel_audio``) for party or guild voice. ``name`` is at most 128
        characters and unique in the app. Raises ``BAD_USER_INPUT`` (a long or taken name)
        or ``FORBIDDEN`` (the policy refuses).
        """
        result: dict[str, Any] = await self._request(ops.CREATE_CHANNEL, {"input": input})
        return result

    async def update(self, input: inputs.UpdateChannelInput | Mapping[str, Any]) -> dict[str, Any]:
        """Change a channel's name, description and/or membership policy. Needs ``manage_group``.

        Fields left out stay as they are.
        """
        result: dict[str, Any] = await self._request(ops.UPDATE_CHANNEL, {"input": input})
        return result

    async def remove(self, group_id: str | int) -> bool:
        """Delete a channel. Needs ``manage_group``.

        Destructive: its members and roles go with it, and the replication servers stop
        routing its messages.
        """
        return bool(await self._request(ops.DELETE_CHANNEL, {"groupId": bigint(group_id)}))

    async def set_policy(
        self, input: inputs.SetChannelPolicyInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Set who may create channels in the app and new channels' default membership policy.

        App-admin (``manage_apps``). It applies to channels created from now on; existing
        ones are unchanged. A ``max_members`` or ``max_groups_per_user`` of ``None`` means
        unlimited.
        """
        result: dict[str, Any] = await self._request(ops.SET_CHANNEL_POLICY, {"input": input})
        return result

    async def join(self, group_id: str | int) -> dict[str, Any]:
        """Join a channel as the caller, as its membership policy allows.

        ``open`` admits at once and routing starts; ``request`` leaves the membership
        ``pending`` until a manager approves; ``invite`` and ``admin`` refuse
        (``FORBIDDEN``). The returned member's ``status`` says which.
        """
        result: dict[str, Any] = await self._request(
            ops.JOIN_CHANNEL, {"groupId": bigint(group_id)}
        )
        return result

    async def request_to_join(self, group_id: str | int) -> dict[str, Any]:
        """Ask to join a request-only channel; the pending membership waits for :meth:`add_member`.

        It behaves exactly as :meth:`join`, named for request-policy UIs.
        """
        result: dict[str, Any] = await self._request(
            ops.REQUEST_TO_JOIN_CHANNEL, {"groupId": bigint(group_id)}
        )
        return result

    async def leave(self, group_id: str | int) -> bool:
        """Leave a channel; the caller stops receiving its messages.

        ``True`` if a membership was removed, ``False`` if the caller was not a member.
        """
        return bool(await self._request(ops.LEAVE_CHANNEL, {"groupId": bigint(group_id)}))

    async def add_member(self, group_id: str | int, user_id: str | int) -> dict[str, Any]:
        """Add a user to a channel, or approve their pending request. Needs ``manage_members``.

        The membership becomes ``active``, with the channel's default role when one is
        configured, and the member's messages start routing.
        """
        result: dict[str, Any] = await self._request(
            ops.ADD_CHANNEL_MEMBER, {"groupId": bigint(group_id), "userId": bigint(user_id)}
        )
        return result

    async def remove_member(self, group_id: str | int, user_id: str | int) -> bool:
        """Remove a member from a channel. ``True`` if a membership was removed.

        Needs ``manage_members``, except that any member may remove themselves (pass your
        own ``user_id``).
        """
        return bool(
            await self._request(
                ops.REMOVE_CHANNEL_MEMBER,
                {"groupId": bigint(group_id), "userId": bigint(user_id)},
            )
        )

    async def set_member_roles(
        self, input: inputs.SetMemberRolesInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Replace a member's channel roles with ``role_ids``. Needs ``manage_roles``.

        Not additive: roles left out are removed, and ids that are unknown or belong to
        another group are ignored. The member's right to post changes at once.
        """
        result: dict[str, Any] = await self._request(ops.SET_CHANNEL_MEMBER_ROLES, {"input": input})
        return result

    async def create_role(
        self, input: inputs.CreateGroupRoleInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Create a custom channel role granting permission keys. Needs ``manage_roles``.

        Grant ``send_messages`` for posting rights. ``role_name`` is at most 128 characters
        and unique in the channel, each key at most 64; ``rank`` (higher is more senior)
        defaults to 0. Raises ``BAD_USER_INPUT`` on a bad or taken name or an unknown key.
        """
        result: dict[str, Any] = await self._request(ops.CREATE_CHANNEL_ROLE, {"input": input})
        return result

    async def update_role(
        self, input: inputs.UpdateGroupRoleInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Change a channel role's name, rank and/or permission keys. Needs ``manage_roles``.

        ``permissions`` replaces the role's keys; fields left out stay. System roles cannot
        be renamed or re-ranked. A changed ``send_messages`` reaches the members holding the
        role once their roles are applied again with :meth:`set_member_roles`.
        """
        result: dict[str, Any] = await self._request(ops.UPDATE_CHANNEL_ROLE, {"input": input})
        return result

    async def delete_role(self, group_role_id: str | int) -> bool:
        """Delete a custom channel role, taking it from every member. Needs ``manage_roles``.

        The system ``leader`` role cannot be deleted (``BAD_USER_INPUT``). ``True`` if a
        role was deleted.
        """
        return bool(
            await self._request(ops.DELETE_CHANNEL_ROLE, {"groupRoleId": bigint(group_role_id)})
        )
