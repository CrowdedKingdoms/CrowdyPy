"""The signed-in user's account (``client.users``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from crowdypy._generated import inputs
from crowdypy._generated import operations as ops
from crowdypy.domains._base import Domain
from crowdypy.utils import bigint

__all__ = ["UsersAPI"]


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
        result: dict[str, Any] | None = await self._request(ops.USER, {"id": bigint(id)})
        return result

    async def update_state(
        self, input: inputs.UpdateUserStateInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(ops.UPDATE_USER_STATE, {"input": input})
        return result
