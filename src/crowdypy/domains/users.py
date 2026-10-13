"""The signed-in user's account (``client.users``)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from crowdypy._generated import inputs
from crowdypy._generated import operations as ops
from crowdypy.domains._base import Domain
from crowdypy.utils import bigint

__all__ = ["PLAYER_PROFILES_MAX", "UsersAPI"]

#: Most ids :meth:`UsersAPI.player_profiles` takes in one call.
PLAYER_PROFILES_MAX = 100


class UsersAPI(Domain):
    async def me(self) -> dict[str, Any]:
        """The signed-in user."""
        result: dict[str, Any] = await self._request(ops.ME)
        return result

    async def update_gamertag(
        self, input: inputs.UpdateGamertagInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(ops.UPDATE_GAMERTAG, {"input": input})
        return result

    async def delete_my_account(self) -> bool:
        """Delete the signed-in account. Irreversible."""
        return bool(await self._request(ops.DELETE_MY_ACCOUNT))

    async def free_play_window(self) -> dict[str, Any] | None:
        result: dict[str, Any] | None = await self._request(ops.FREE_PLAY_WINDOW)
        return result

    async def get(self, id: str | int) -> dict[str, Any] | None:
        """A user by id, or ``None``. The private fields (``email``, ``state``,
        ``isConfirmed``, the early-access grants, ``orgId``, ``externalId``, ``userType``,
        ``isSuperAdmin``) come back ``None`` for anyone but yourself; for another player's
        nametag use :meth:`player_profile`."""
        result: dict[str, Any] | None = await self._request(ops.USER, {"id": bigint(id)})
        return result

    async def player_profile(self, user_id: str | int) -> dict[str, Any] | None:
        """A player's public profile (``userId``, ``gamertag``, ``disambiguation``) for
        nametags and friends lists, or ``None`` when there is no such user. Needs a game
        token; carries nothing private."""
        result: dict[str, Any] | None = await self._request(
            ops.PLAYER_PROFILE, {"userId": bigint(user_id)}
        )
        return result

    async def player_profiles(self, user_ids: Sequence[str | int]) -> list[dict[str, Any]]:
        """Public profiles for up to :data:`PLAYER_PROFILES_MAX` players in one call, in no
        particular order (duplicates are read once; unknown ids are left out). Raises
        ``ValueError`` for more than 100 ids, and sends nothing for none."""
        if isinstance(user_ids, (str, bytes)):
            raise TypeError("player_profiles takes a sequence of user ids, not one id")
        ids = [bigint(user_id) for user_id in user_ids]
        if len(ids) > PLAYER_PROFILES_MAX:
            raise ValueError(f"player_profiles takes at most {PLAYER_PROFILES_MAX} ids: {len(ids)}")
        if not ids:
            return []
        result: list[dict[str, Any]] = await self._request(ops.PLAYER_PROFILES, {"userIds": ids})
        return result

    async def update_state(
        self, input: inputs.UpdateUserStateInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(ops.UPDATE_USER_STATE, {"input": input})
        return result
