"""Grid boxes, shared by the async and the blocking ``GridScope``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

__all__ = ["GridBox", "GridChunk", "chunk_coords"]


class GridChunk(NamedTuple):
    """A chunk address. The wire carries these as decimal strings; here they are ints."""

    x: int
    y: int
    z: int


class GridBox(NamedTuple):
    """A grid's chunk bounds, inclusive at both corners."""

    low: GridChunk
    high: GridChunk

    def contains(self, chunk: Any) -> bool:
        x, y, z = chunk_coords(chunk)
        return (
            self.low.x <= x <= self.high.x
            and self.low.y <= y <= self.high.y
            and self.low.z <= z <= self.high.z
        )


def chunk_coords(chunk: Any) -> GridChunk:
    """Read ``x``/``y``/``z`` from a 3-sequence, a mapping, or an object with those attributes.

    Values may be ints or decimal strings (the GraphQL ``BigInt`` form).
    """
    if isinstance(chunk, Mapping):
        values = (chunk["x"], chunk["y"], chunk["z"])
    elif isinstance(chunk, Sequence) and not isinstance(chunk, (str, bytes)):
        if len(chunk) != 3:
            raise ValueError(f"a chunk has three coordinates, got {len(chunk)}")
        values = (chunk[0], chunk[1], chunk[2])
    else:
        values = (chunk.x, chunk.y, chunk.z)
    return GridChunk(int(values[0]), int(values[1]), int(values[2]))
