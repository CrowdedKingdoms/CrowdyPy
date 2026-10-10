"""Durable chunk reads and writes for an app's voxel world (``client.chunks``).

A chunk is a persisted 16x16x16 cube of voxels: the packed voxel-type grid (``voxels``),
sparse per-voxel ``voxelStates``, an optional opaque ``chunkState`` blob and level-of-detail
meshes (``lods``). Use it for authoritative persistence and bulk region loads; per-voxel
realtime edits are far cheaper over the UDP replication path.

Chunk coordinates are int64 decimal strings (``+1`` on an axis is the next chunk) and voxel
positions are 0-15 per axis. Binary fields (the 4096-byte ``voxels`` grid, per-voxel
``state``, ``chunkState``, LOD ``data``) travel base64-encoded and come back as the API sends
them; :func:`crowdypy.utils.decode_base64` turns one into bytes.

Every call needs a bearer token, and an app-scoped token reaches only its own app
(``UNAUTHENTICATED`` / ``FORBIDDEN``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from crowdypy._generated import inputs
from crowdypy._generated import operations as ops
from crowdypy.domains._base import Domain

__all__ = ["ChunksAPI"]


class ChunksAPI(Domain):
    async def get(self, input: inputs.GetChunkInput | Mapping[str, Any]) -> dict[str, Any] | None:
        """One chunk (voxel grid, per-voxel states, chunk state, LODs), or ``None`` if absent.

        Read-only. ``requestedLodLevels`` / ``includeAllLods`` on the input limit which LODs
        come back. ``voxelStates`` positions and types are the app's signed 16-bit values;
        ``voxelStatesTruncated`` is true when the chunk holds more recorded edits than one
        read applies (65,536), so read the whole log with :meth:`voxel_list`.
        """
        result: dict[str, Any] | None = await self._request(ops.GET_CHUNK, {"input": input})
        return result

    async def get_lods(
        self, input: inputs.GetChunkLodsInput | Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Only the ``lodLevels`` asked for (0 is finest) of one chunk, or ``None`` if absent.

        Cheaper than :meth:`get` when you need only LODs. Read-only.
        """
        result: dict[str, Any] | None = await self._request(ops.GET_CHUNK_LODS, {"input": input})
        return result

    async def by_distance(
        self, input: inputs.GetChunksByDistanceInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Every chunk within ``maxDistance`` chunks (1-8) of ``centerCoordinate`` on each axis.

        Paginated with ``limit`` (default 1000) and ``skip``, which the answer echoes.
        ``BAD_USER_INPUT`` for a ``maxDistance`` outside 1-8. Read-only. A chunk's
        ``voxelStatesTruncated`` is true when one call holds more recorded edits than it
        applies (262,144 across the call).
        """
        result: dict[str, Any] = await self._request(ops.GET_CHUNKS_BY_DISTANCE, {"input": input})
        return result

    async def voxel_list(
        self, input: inputs.GetVoxelListInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """The recorded voxel edits of one chunk, newest first: the edit log, not the grid.

        Read-only; use :meth:`get` for the packed grid.
        """
        result: dict[str, Any] = await self._request(ops.GET_VOXEL_LIST, {"input": input})
        return result

    async def update(self, input: inputs.ChunkUpdateInput | Mapping[str, Any]) -> dict[str, Any]:
        """Create or replace a chunk's voxel grid and/or per-voxel states. Writes world state.

        ``voxels`` must decode to exactly 4096 bytes, one type byte per voxel at index
        ``x + y*16 + z*256`` (``BAD_USER_INPUT`` otherwise); ``chunkState`` and LODs are left
        untouched. Needs an app-scoped token for the app and ``manage_apps`` or edit
        permission on the chunk: ``FORBIDDEN`` without it, and while the app's wilderness is
        closed for a chunk only its world grid covers.
        """
        result: dict[str, Any] = await self._request(ops.UPDATE_CHUNK, {"input": input})
        return result

    async def update_state(
        self, input: inputs.UpdateChunkStateInput | Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Upsert only the chunk-level ``chunkState`` blob (omit or null it to store none).

        Voxels, per-voxel states and LODs are preserved. Needs ``manage_apps`` on the org that
        owns the app (``SCOPE_MISSING`` / ``FORBIDDEN``). ``None`` if it could not be written.
        """
        result: dict[str, Any] | None = await self._request(
            ops.UPDATE_CHUNK_STATE, {"input": input}
        )
        return result

    async def update_lods(
        self, input: inputs.UpdateChunkLodsInput | Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Replace a chunk's entire LOD set (each a ``level`` >= 0 with base64 ``data``).

        Everything else on the chunk is preserved. Needs ``manage_apps`` on the org that owns
        the app (``SCOPE_MISSING`` / ``FORBIDDEN``). ``None`` if it could not be written.
        """
        result: dict[str, Any] | None = await self._request(ops.UPDATE_CHUNK_LODS, {"input": input})
        return result
