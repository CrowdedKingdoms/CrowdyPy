"""Shared fixtures: a GraphQL transport over an in-memory HTTP mock."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import msgspec
import pytest

from crowdypy.auth_state import AuthState
from crowdypy.graphql import AsyncGraphQLClient
from crowdypy.lb_cookie import LbCookieStore


@dataclass
class Sent:
    """One request the mock received."""

    operation_name: str | None
    query: str
    variables: dict[str, Any]
    headers: httpx.Headers


@dataclass
class MockApi:
    """Answers each request from ``responder`` and records what was sent."""

    responder: Callable[[Sent], Any] = lambda sent: {"data": {}}
    sent: list[Sent] = field(default_factory=list)
    status: int = 200
    set_cookie: list[str] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = msgspec.json.decode(request.content)
        sent = Sent(
            operation_name=body.get("operationName"),
            query=body["query"],
            variables=body.get("variables") or {},
            headers=request.headers,
        )
        self.sent.append(sent)
        payload = self.responder(sent)
        headers = [("set-cookie", c) for c in self.set_cookie]
        return httpx.Response(self.status, content=msgspec.json.encode(payload), headers=headers)

    def reply_with_root(self, value: Any) -> None:
        """Answer every operation with ``{data: {<its first root field>: value}}``."""

        def responder(sent: Sent) -> Any:
            return {"data": _root_of(sent.query, value)}

        self.responder = responder

    @property
    def last(self) -> Sent:
        return self.sent[-1]


def _root_of(query: str, value: Any) -> dict[str, Any]:
    # The first field after the operation's opening brace is its first root field.
    body = query[query.index("{") + 1 :].strip()
    name = ""
    for ch in body:
        if ch.isalnum() or ch == "_":
            name += ch
        else:
            break
    return {name: value}


@pytest.fixture
def api() -> MockApi:
    return MockApi()


@pytest.fixture
def session() -> AuthState:
    return AuthState()


@pytest.fixture
def graphql(api: MockApi, session: AuthState) -> AsyncGraphQLClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    return AsyncGraphQLClient(
        session,
        endpoint="https://ck.example.test/graphql",
        lb_cookie_store=LbCookieStore(),
        http_client=http,
    )
