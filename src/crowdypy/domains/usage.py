"""Replication and GraphQL usage reporting (``client.usage``).

Read-only, and every method needs an identity session with the org's ``view_usage``
permission. ``since`` is an ISO-8601 ``DateTime`` string; byte and message counters come
back as strings because they can exceed 32 bits.
"""

from __future__ import annotations

from typing import Any

from crowdypy._generated import operations as ops
from crowdypy.domains._base import Domain, omit_none
from crowdypy.utils import bigint

__all__ = ["UsageAPI"]


class UsageAPI(Domain):
    """Usage of an organization's apps over a time window."""

    async def app_graphql_operations(
        self, org_id: str | int, app_id: str | int, since: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """An app's top GraphQL operations by bytes, from ``since`` until now.

        ``limit`` defaults to 20.
        """
        result: list[dict[str, Any]] = await self._request(
            ops.APP_GRAPHQL_OPERATIONS,
            omit_none(
                {"orgId": bigint(org_id), "appId": bigint(app_id), "since": since, "limit": limit}
            ),
        )
        return result

    async def app_summary(
        self,
        org_id: str | int,
        app_id: str | int,
        since: str,
        operation_limit: int | None = None,
    ) -> dict[str, Any]:
        """An app's replication and GraphQL byte totals plus its top operations since ``since``.

        ``operation_limit`` caps the top operations (default 20).
        """
        result: dict[str, Any] = await self._request(
            ops.APP_USAGE_SUMMARY,
            omit_none(
                {
                    "orgId": bigint(org_id),
                    "appId": bigint(app_id),
                    "since": since,
                    "operationLimit": operation_limit,
                }
            ),
        )
        return result

    async def player_pulse(self, org_id: str | int) -> dict[str, Any]:
        """An org's live concurrent players against its all-time peak.

        Also the site-wide live total (aggregate only) and the org's percentile among
        studios.
        """
        result: dict[str, Any] = await self._request(ops.PLAYER_PULSE, {"orgId": bigint(org_id)})
        return result
