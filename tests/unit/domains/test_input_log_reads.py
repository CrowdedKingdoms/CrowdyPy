"""The input log's reads as CrowdyJS 18.9.0 has them.

Every sessions read selects ``missingRecords``; a read the caller cancels (CrowdyJS's
``signal``; here, cancelling the task) stops and closes its request, which frees the server's
read share for the next one; and the refusals carry what the caller acts on: ``retryable`` on
the two read codes worth repeating with the same cursor, the balance figures on
``INPUT_LOG_FUNDS_NEEDED``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from conftest import MockApi
from crowdypy._generated import operations as ops
from crowdypy.auth_state import AuthState
from crowdypy.domains.input_log import InputLogAPI
from crowdypy.errors import CrowdyGraphQLError
from crowdypy.graphql import AsyncGraphQLClient


def test_sessions_selects_missing_records() -> None:
    assert "missingRecords" in ops.INPUT_LOG_SESSIONS.document


async def test_a_cancelled_read_closes_its_request(session: AuthState) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    async def never_answers(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
        raise AssertionError("unreachable")

    http = httpx.AsyncClient(transport=httpx.MockTransport(never_answers))
    graphql = AsyncGraphQLClient(
        session, endpoint="https://ck.example.test/graphql", http_client=http
    )
    reading = asyncio.create_task(InputLogAPI(graphql).messages(42, 9001, first=200))
    await asyncio.wait_for(started.wait(), 5)

    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    assert closed.is_set()
    await http.aclose()


async def _refusal(
    api: MockApi, graphql: AsyncGraphQLClient, extensions: dict[str, Any]
) -> CrowdyGraphQLError:
    api.responder = lambda sent: {"errors": [{"message": "refused", "extensions": extensions}]}
    with pytest.raises(CrowdyGraphQLError) as caught:
        await InputLogAPI(graphql).messages(42, 9001, after="aWw6MzoxMDAw")
    return caught.value


@pytest.mark.parametrize("code", ["INPUT_LOG_TEMPORARILY_UNAVAILABLE", "INPUT_LOG_RATE_LIMITED"])
async def test_the_read_codes_say_retry(
    api: MockApi, graphql: AsyncGraphQLClient, code: str
) -> None:
    error = await _refusal(api, graphql, {"code": code, "retryable": True})
    assert error.code == code
    assert error.extensions is not None
    assert error.extensions["retryable"] is True
    # The cursor the caller retries with is the one it sent: nothing is skipped.
    assert api.last.variables["after"] == "aWw6MzoxMDAw"


async def test_a_cursor_from_another_session_is_bad_input(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    error = await _refusal(api, graphql, {"code": "BAD_USER_INPUT", "retryable": False})
    assert error.code == "BAD_USER_INPUT"
    assert error.extensions is not None
    assert error.extensions["retryable"] is False


async def test_funds_needed_carries_the_balance_figures(
    api: MockApi, graphql: AsyncGraphQLClient
) -> None:
    error = await _refusal(
        api,
        graphql,
        {"code": "INPUT_LOG_FUNDS_NEEDED", "requiredMicrousd": "2500000", "spendableMicrousd": "0"},
    )
    assert error.code == "INPUT_LOG_FUNDS_NEEDED"
    assert error.extensions is not None
    assert error.extensions["requiredMicrousd"] == "2500000"
    assert error.extensions["spendableMicrousd"] == "0"
