"""Organizations, members, roles and org API tokens (``client.organizations``).

The studio-administration surface: an organization owns apps, billing, quotas and
environments, and its role grants (``manage_members``, ``manage_tokens``, ...) gate the
other admin sub-clients. Every method needs an identity session except
:meth:`OrganizationsAPI.permissions`, which is public. ``BigInt`` ids (``org_id``,
``user_id``, ``org_role_id``, ``org_token_id``) take an ``int`` or a decimal string.

Errors: ``UNAUTHENTICATED`` without a session, ``FORBIDDEN`` / ``SCOPE_MISSING`` without
the org permission a method names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from crowdypy._generated import inputs
from crowdypy._generated import operations as ops
from crowdypy.domains._base import Domain
from crowdypy.utils import bigint

__all__ = ["OrganizationsAPI"]


class OrganizationsAPI(Domain):
    """Organizations, members, RBAC roles and org API tokens."""

    async def get(self, org_id: str | int) -> dict[str, Any] | None:
        """One organization by id, or ``None`` when there is none."""
        result: dict[str, Any] | None = await self._request(
            ops.ORGANIZATION, {"id": bigint(org_id)}
        )
        return result

    async def by_slug(self, slug: str) -> dict[str, Any] | None:
        """One organization by its URL slug (``"acme"``), or ``None``."""
        result: dict[str, Any] | None = await self._request(
            ops.ORGANIZATION_BY_SLUG, {"slug": slug}
        )
        return result

    async def mine(self) -> list[dict[str, Any]]:
        """The caller's memberships: each org with the caller's permission keys and roles."""
        result: list[dict[str, Any]] = await self._request(ops.MY_ORGANIZATIONS, {})
        return result

    async def members(self, org_id: str | int) -> list[dict[str, Any]]:
        """An organization's members. Needs ``manage_members``."""
        result: list[dict[str, Any]] = await self._request(
            ops.ORG_MEMBERS, {"orgId": bigint(org_id)}
        )
        return result

    async def roles(self, org_id: str | int) -> list[dict[str, Any]]:
        """An organization's custom and system roles. Needs ``manage_members``."""
        result: list[dict[str, Any]] = await self._request(ops.ORG_ROLES, {"orgId": bigint(org_id)})
        return result

    async def member_roles(self, org_member_id: str | int) -> list[dict[str, Any]]:
        """The roles one membership holds. ``org_member_id`` is the membership id, not a user id."""
        result: list[dict[str, Any]] = await self._request(
            ops.MEMBER_ROLES, {"orgMemberId": bigint(org_member_id)}
        )
        return result

    async def permissions(self) -> list[dict[str, Any]]:
        """Every assignable org permission key with its description, for a role editor. Public."""
        result: list[dict[str, Any]] = await self._request(ops.ORG_PERMISSIONS, {})
        return result

    async def tokens(self, org_id: str | int) -> list[dict[str, Any]]:
        """An organization's API tokens, metadata only. Needs ``manage_tokens``."""
        result: list[dict[str, Any]] = await self._request(
            ops.ORG_TOKENS, {"orgId": bigint(org_id)}
        )
        return result

    async def create(
        self, input: inputs.CreateOrganizationInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Create an organization (``name`` and a unique ``slug``); the caller becomes its owner."""
        result: dict[str, Any] = await self._request(ops.CREATE_ORGANIZATION, {"input": input})
        return result

    async def create_token(
        self, input: inputs.CreateOrgTokenInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Mint an org API token for a server-side studio backend. Needs ``manage_tokens``.

        The plaintext ``token`` is in this response and never again: save it now.
        """
        result: dict[str, Any] = await self._request(ops.CREATE_ORG_TOKEN, {"input": input})
        return result

    async def update_token(
        self, org_token_id: str | int, input: inputs.UpdateOrgTokenInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Change a token's label, active flag or expiry. Needs ``manage_tokens``.

        The secret is not rotated.
        """
        result: dict[str, Any] = await self._request(
            ops.UPDATE_ORG_TOKEN, {"orgTokenId": bigint(org_token_id), "input": input}
        )
        return result

    async def revoke_token(self, org_token_id: str | int) -> bool:
        """Permanently deactivate an org token. Needs ``manage_tokens``.

        Irreversible. ``False`` when there is no such token.
        """
        return bool(await self._request(ops.REVOKE_ORG_TOKEN, {"orgTokenId": bigint(org_token_id)}))

    async def invite_member(
        self, input: inputs.InviteOrgMemberInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Add a user to an organization. Needs ``manage_members``."""
        result: dict[str, Any] = await self._request(ops.INVITE_ORG_MEMBER, {"input": input})
        return result

    async def remove_member(self, org_id: str | int, user_id: str | int) -> bool:
        """Remove a member and their role assignments. Needs ``manage_members``.

        ``False`` when the user was not a member.
        """
        return bool(
            await self._request(
                ops.REMOVE_ORG_MEMBER, {"orgId": bigint(org_id), "userId": bigint(user_id)}
            )
        )

    async def set_member_roles(
        self, org_id: str | int, user_id: str | int, role_ids: Sequence[str | int]
    ) -> dict[str, Any]:
        """Replace a member's roles with exactly ``role_ids``. Needs ``manage_members``."""
        if isinstance(role_ids, str):
            raise TypeError("role_ids is a sequence of role ids, not one string")
        result: dict[str, Any] = await self._request(
            ops.UPDATE_ORG_MEMBER_ROLES,
            {
                "orgId": bigint(org_id),
                "userId": bigint(user_id),
                "roleIds": [bigint(role_id) for role_id in role_ids],
            },
        )
        return result

    async def create_role(
        self, input: inputs.CreateOrgRoleInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Create a custom role with a set of permission keys. Needs ``manage_members``."""
        result: dict[str, Any] = await self._request(ops.CREATE_ORG_ROLE, {"input": input})
        return result

    async def update_role(
        self, org_role_id: str | int, input: inputs.UpdateOrgRoleInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Rename a role or replace its permission set. Needs ``manage_members``.

        A system role's permissions are fixed: changing them is ``FORBIDDEN``.
        """
        result: dict[str, Any] = await self._request(
            ops.UPDATE_ORG_ROLE, {"orgRoleId": bigint(org_role_id), "input": input}
        )
        return result

    async def delete_role(self, org_role_id: str | int) -> bool:
        """Delete a custom role and unassign it from every member. Needs ``manage_members``.

        System roles cannot be deleted. ``False`` when there is no such role.
        """
        return bool(await self._request(ops.DELETE_ORG_ROLE, {"orgRoleId": bigint(org_role_id)}))
