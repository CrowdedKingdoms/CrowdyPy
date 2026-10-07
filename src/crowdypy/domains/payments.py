"""Payment checkouts: wallet top-ups and purchases (``client.payments``).

Every method needs a signed-in caller and acts on the caller's own checkouts. Amounts are
cents. :meth:`PaymentsAPI.create` starts a real Stripe or PayPal checkout: test with
sandbox provider keys only, never real charges.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from crowdypy._generated import inputs
from crowdypy._generated import operations as ops
from crowdypy.domains._base import Domain, omit_none

__all__ = ["PaymentsAPI"]


class PaymentsAPI(Domain):
    """Start, capture and list the caller's checkouts."""

    async def create(self, input: inputs.CreateCheckoutInput | Mapping[str, Any]) -> dict[str, Any]:
        """Start a checkout (e.g. an ``ORG_WALLET_TOPUP``) and get it back with ``externalUrl``.

        Send the buyer to ``externalUrl`` to pay; the checkout completes later. An org wallet
        top-up also needs ``manage_billing``. Set ``idempotency_key`` on the input so a retry
        returns the first checkout. Accepts an identity session or an app-scoped token.
        """
        result: dict[str, Any] = await self._request(ops.CREATE_CHECKOUT, {"input": input})
        return result

    async def mine(self, limit: int | None = None, offset: int | None = None) -> dict[str, Any]:
        """A page of the caller's checkouts, newest first (``items`` and ``pageInfo``).

        ``limit`` defaults to 50. Prefer :meth:`mine_connection`.
        """
        result: dict[str, Any] = await self._request(
            ops.MY_CHECKOUTS, omit_none({"limit": limit, "offset": offset})
        )
        return result

    async def capture_paypal(
        self, order_id: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Capture an approved PayPal order and finalize the checkout it belongs to.

        Call it once the buyer approves the order :meth:`create` returned. With
        ``idempotency_key`` a replay returns the first result instead of capturing again.
        """
        result: dict[str, Any] = await self._request(
            ops.CAPTURE_PAYPAL_CHECKOUT,
            omit_none({"orderId": order_id, "idempotencyKey": idempotency_key}),
        )
        return result

    async def mine_connection(
        self, first: int | None = None, after: str | None = None
    ) -> dict[str, Any]:
        """The caller's checkouts as a cursor connection, newest first.

        Page with ``first`` and the previous page's ``pageInfo.endCursor`` as ``after``.
        """
        result: dict[str, Any] = await self._request(
            ops.MY_CHECKOUTS_CONNECTION, omit_none({"first": first, "after": after})
        )
        return result
