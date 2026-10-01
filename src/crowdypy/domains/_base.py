"""Shared plumbing for the domain sub-clients.

Async source: ``crowdypy._sync.domains`` is generated from this package by
``scripts/unasync.py``, which rewrites ``asyncio.sleep`` to ``time.sleep`` and drops
``async``/``await``. Use nothing else from asyncio in a domain module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from crowdypy._operation import Operation
from crowdypy.errors import CrowdyProtocolError
from crowdypy.graphql import AsyncGraphQLClient

__all__ = ["Domain", "omit_none", "sleep"]


class Domain:
    """Base for every sub-client: holds the client's GraphQL transport."""

    def __init__(self, graphql: AsyncGraphQLClient) -> None:
        self._graphql = graphql

    async def _request(
        self, operation: Operation, variables: Mapping[str, Any] | None = None
    ) -> Any:
        """Send ``operation`` and return the value of its first root field."""
        data = await self._graphql.request(operation, variables)
        root = operation.root_fields[0]
        if not isinstance(data, Mapping) or root not in data:
            raise CrowdyProtocolError(f"{operation.name}: the response has no {root!r} field")
        return data[root]


def omit_none(values: Mapping[str, Any]) -> dict[str, Any]:
    """Drop arguments the caller left at ``None``, as CrowdyJS omits ``undefined``.

    An explicit null is still expressible through an input struct (or a mapping passed
    as ``input``), whose fields are sent exactly as given.
    """
    return {key: value for key, value in values.items() if value is not None}


async def sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)
