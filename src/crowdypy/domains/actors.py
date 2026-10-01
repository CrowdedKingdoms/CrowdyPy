"""Persisted actors: the durable record of a player or NPC in an app (``client.actors``).

An actor here is the server-stored row (owner, app, optional avatar, last-known chunk, public
and private state), not the realtime replication stream, which is far cheaper per update.
Actor ids are exactly 32 ASCII characters, the replication wire's actor id and not an RFC 4122
UUID (:func:`crowdypy.utils.generate_crowdy_uuid` mints one). ``appId``, ``avatarId`` and
``userId`` are BigInt decimal strings; state blobs are base64.

Actors are game-plane objects, used with an **app-scoped token for the actor's app**. The
by-id calls and :meth:`ActorsAPI.batch_lookup` answer ``NOT_FOUND``, as for a missing id, to
an identity session or another app's token. :meth:`ActorsAPI.list` and
:meth:`ActorsAPI.list_connection` are the caller's own actors: the token's app's under an
app-scoped token, every app's under an identity session. No token is ``UNAUTHENTICATED``.
"""

from __future__ import annotations

import builtins  # ActorsAPI.list shadows the builtin in the class's annotations
from collections.abc import Mapping
from typing import Any

from crowdypy._generated import inputs
from crowdypy._generated import operations as ops
from crowdypy.domains._base import Domain, omit_none

__all__ = ["ActorsAPI"]


class ActorsAPI(Domain):
    async def get(self, uuid: str) -> dict[str, Any]:
        """One actor by its 32-character id.

        The owner sees ``privateState``; anyone else in the app gets it as ``None``.
        ``NOT_FOUND`` for an id that is not in the token's app.
        """
        result: dict[str, Any] = await self._request(ops.ACTOR, {"uuid": uuid})
        return result

    async def list(
        self, filter: inputs.ActorFilterInput | Mapping[str, Any] | None = None
    ) -> builtins.list[dict[str, Any]]:
        """The caller's own actors (full state), narrowed by ``filter``, whose fields are ANDed.

        Under an app-scoped token, only that app's; a ``filter.appId`` naming another app is
        refused (``SCOPE_MISSING``). An identity session lists them across every app.
        """
        result: builtins.list[dict[str, Any]] = await self._request(
            ops.ACTORS, omit_none({"filter": filter})
        )
        return result

    async def list_connection(
        self,
        *,
        first: int | None = None,
        after: str | None = None,
        filter: inputs.ActorFilterInput | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Cursor-paginated :meth:`list`: ``edges { cursor node }``, ``pageInfo``, ``totalCount``.

        Page with ``first`` (default 50, max 200) and the previous page's
        ``pageInfo.endCursor`` as ``after``. Scoped as :meth:`list` is.
        """
        result: dict[str, Any] = await self._request(
            ops.ACTORS_CONNECTION, omit_none({"first": first, "after": after, "filter": filter})
        )
        return result

    async def batch_lookup(
        self, input: inputs.BatchActorLookupInput | Mapping[str, Any]
    ) -> builtins.list[dict[str, Any]]:
        """Resolve many actors (``uuids``) in one round-trip instead of :meth:`get` in a loop.

        Public state only: ``privateState`` is ``None`` on every result, and unknown ids and
        ids in other apps are left out. App-scoped tokens only; an identity session gets
        ``NOT_FOUND``.
        """
        result: builtins.list[dict[str, Any]] = await self._request(
            ops.BATCH_LOOKUP_ACTORS, {"input": input}
        )
        return result

    async def create(self, input: inputs.CreateActorInput | Mapping[str, Any]) -> dict[str, Any]:
        """Create an actor the caller owns, from a unique ``uuid``, its ``appId`` and ``chunk``.

        ``avatarId`` (an avatar the caller owns) and the state blobs are optional.
        ``BAD_USER_INPUT`` for a malformed or duplicate uuid, ``FORBIDDEN`` without access to
        the app.
        """
        result: dict[str, Any] = await self._request(ops.CREATE_ACTOR, {"input": input})
        return result

    async def update(
        self, uuid: str, input: inputs.UpdateActorInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Patch an actor; only the fields present on ``input`` change. Owner only.

        ``NOT_FOUND`` for an id that is not in the token's app; a caller who is not the owner
        gets ``UNAUTHENTICATED``.
        """
        result: dict[str, Any] = await self._request(
            ops.UPDATE_ACTOR, {"uuid": uuid, "input": input}
        )
        return result

    async def delete(self, uuid: str, idempotency_key: str | None = None) -> dict[str, Any]:
        """Delete an actor and return its identifying fields. Owner only.

        With ``idempotency_key`` a retry replays the first result, and the same key with a
        different ``uuid`` is ``IDEMPOTENCY_CONFLICT``; keys expire after 24 hours.
        ``NOT_FOUND`` for an id that is not in the token's app.
        """
        result: dict[str, Any] = await self._request(
            ops.DELETE_ACTOR, omit_none({"uuid": uuid, "idempotencyKey": idempotency_key})
        )
        return result

    async def update_state(
        self, uuid: str, input: inputs.UpdateActorStateInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Replace only ``privateState`` / ``publicState``, a lighter write than :meth:`update`.

        Owner only, as :meth:`update`; ``BAD_USER_INPUT`` for a malformed blob.
        """
        result: dict[str, Any] = await self._request(
            ops.UPDATE_ACTOR_STATE, {"uuid": uuid, "input": input}
        )
        return result
