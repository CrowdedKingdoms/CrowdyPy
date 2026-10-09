"""The e2e harness: black-box suites against a live deployment, provisioned through the
public API the way an integrator does it (owner sign-in, an access tier, ``grant``), with
replication over CrowdyPy's native UDP client. Unconfigured, every suite skips.

Configuration (the variables CrowdyCPP's suites read):

- ``CROWDY_E2E_API_URL``: the API's entry origin (required).
- ``CROWDY_E2E_EMAIL``: base address; players are plus-addressed per run (required).
- ``CROWDY_E2E_APP_ID``: the app under test (required).
- ``CROWDY_E2E_OWNER_EMAIL``: an account with ``manage_apps`` and ``manage_access_tiers``
  on the app (required to provision players); ``CROWDY_E2E_OWNER_PASSWORD`` signs it in,
  otherwise a fresh derived owner registers.
- ``CROWDY_E2E_HTTP_URL``: the game API origin, when not the minted ``game_api_url``.
- ``CROWDY_E2E_STUDIO_GRID_ID``: an owner grid for the Studio suite.
- ``CROWDY_E2E_PROVISIONING_TOKEN``: sent as ``X-CK-Provisioning-Token`` when a player
  registers. Dev and test are staff-only, so without it a new derived account is refused
  there (``TIER_ACCESS_REQUIRED``); it must cover ``<local>+*@<domain>`` of
  ``CROWDY_E2E_EMAIL`` (Secrets Manager ``infra-cp/<tier>/loadtest/provisioning-token-sdk-e2e``).
- ``CROWDY_E2E_SLOW=1``: the long-running suites.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

import crowdypy
from crowdypy.domains.portal import AppTokenResponse
from crowdypy.graphql import graphql_endpoint

TIER_NAME = "crowdypy-e2e-video"
TIER_PERMISSIONS = ["access", "teleport", "update_voxel_data", "use_voice_chat", "use_video_chat"]
RUN = secrets.token_hex(3)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


@dataclass(frozen=True)
class E2eConfig:
    api_url: str = field(
        default_factory=lambda: _env("CROWDY_E2E_API_URL") or _env("CROWDY_E2E_MANAGEMENT_URL")
    )
    http_url: str = field(default_factory=lambda: _env("CROWDY_E2E_HTTP_URL"))
    email: str = field(default_factory=lambda: _env("CROWDY_E2E_EMAIL"))
    app_id: str = field(default_factory=lambda: _env("CROWDY_E2E_APP_ID"))
    owner_email: str = field(default_factory=lambda: _env("CROWDY_E2E_OWNER_EMAIL"))
    owner_password: str = field(default_factory=lambda: _env("CROWDY_E2E_OWNER_PASSWORD"))
    studio_grid_id: str = field(default_factory=lambda: _env("CROWDY_E2E_STUDIO_GRID_ID"))
    slow: bool = field(default_factory=lambda: _env("CROWDY_E2E_SLOW") == "1")


CONFIG = E2eConfig()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    missing = [
        name
        for name, value in (
            ("CROWDY_E2E_API_URL", CONFIG.api_url),
            ("CROWDY_E2E_EMAIL", CONFIG.email),
            ("CROWDY_E2E_APP_ID", CONFIG.app_id),
            ("CROWDY_E2E_OWNER_EMAIL", CONFIG.owner_email),
        )
        if not value
    ]
    for item in items:
        if "tests/e2e/" not in item.nodeid.replace("\\", "/") and not item.nodeid.startswith(
            "e2e/"
        ):
            continue
        item.add_marker(pytest.mark.e2e)
        if missing:
            item.add_marker(pytest.mark.skip(reason=f"e2e unconfigured: {', '.join(missing)}"))
        elif item.get_closest_marker("slow") and not CONFIG.slow:
            item.add_marker(pytest.mark.skip(reason="slow suite: set CROWDY_E2E_SLOW=1"))


def derive_email(tag: str) -> str:
    """``alice@x.com`` + ``voxel`` -> ``alice+voxel-<run>@x.com``: fresh accounts per run."""
    local, _, domain = CONFIG.email.partition("@")
    return f"{local}+{tag}-{RUN}@{domain}"


def derive_password(email: str) -> str:
    return "Aa1!e2e-" + email


@dataclass
class Player:
    identity: crowdypy.AsyncCrowdyClient
    game: crowdypy.AsyncCrowdyClient
    minted: AppTokenResponse
    user_id: str
    email: str

    async def aclose(self) -> None:
        await self.game.aclose()
        await self.identity.aclose()


_REGISTER = (
    "mutation R($i: RegisterUserInput!) "
    "{ register(registerUserInput: $i) { token user { userId } } }"
)


async def register_with_provisioning_token(email: str, token: str) -> tuple[str, str]:
    """Dev and test are staff-only: a new account there needs the provisioning token, which
    only ``register`` reads. The SDK sends no test-only header, so this one request goes
    direct; the session it returns is handed to an ordinary client."""
    variables = {
        "i": {
            "email": email,
            "password": derive_password(email),
            "acceptLegal": True,
            "attestAgeOfMajority": True,
        }
    }
    async with httpx.AsyncClient(timeout=60.0) as http:
        res = await http.post(
            graphql_endpoint(CONFIG.api_url) or CONFIG.api_url,
            json={"query": _REGISTER, "variables": variables},
            headers={"x-ck-provisioning-token": token},
        )
    payload = res.json()
    if payload.get("errors"):
        raise RuntimeError(f"register {email}: {payload['errors']}")
    registered = payload["data"]["register"]
    return registered["token"], str(registered["user"]["userId"])


async def identity_client(
    email: str, password: str | None = None
) -> tuple[crowdypy.AsyncCrowdyClient, str]:
    """Signed in: ``login`` with ``password``, otherwise ``register`` a fresh account that
    has accepted the legal documents and attested its age, as a player ticking both boxes."""
    client = crowdypy.AsyncCrowdyClient(http_url=CONFIG.api_url)
    provisioning = _env("CROWDY_E2E_PROVISIONING_TOKEN")
    if not password and provisioning:
        try:
            token, user_id = await register_with_provisioning_token(email, provisioning)
        except BaseException:
            await client.aclose()
            raise
        client.set_token(token)
        return client, user_id
    try:
        auth = (
            await client.auth.login(email, password)
            if password
            else await client.auth.register(
                email, derive_password(email), accept_legal=True, attest_age_of_majority=True
            )
        )
    except BaseException:
        await client.aclose()
        raise
    return client, auth.user.user_id


async def owner_client() -> crowdypy.AsyncCrowdyClient:
    client, _ = await identity_client(CONFIG.owner_email, CONFIG.owner_password or None)
    return client


_TIERS: dict[str, str] = {}


async def ensure_tier(owner: crowdypy.AsyncCrowdyClient, app_id: str) -> str:
    """Find or create the tier carrying every gameplay permission (idempotent by name)."""
    if app_id in _TIERS:
        return _TIERS[app_id]
    for tier in await owner.admin.app_access.tiers(app_id):
        if tier.get("name") == TIER_NAME and len(tier.get("permissionKeys") or []) >= len(
            TIER_PERMISSIONS
        ):
            _TIERS[app_id] = str(tier["tierId"])
            return _TIERS[app_id]
    created = await owner.admin.app_access.create_tier(
        {
            "appId": app_id,
            "name": TIER_NAME,
            "isFree": True,
            "description": "CrowdyPy e2e tier: every gameplay runtime permission",
            "permissionKeys": TIER_PERMISSIONS,
        }
    )
    _TIERS[app_id] = str(created["tierId"])
    return _TIERS[app_id]


#: Players registered this run, by tag: later suites sign them in rather than register
#: again (registration is rate-limited per address and per IP).
_ACCOUNTS: dict[str, tuple[str, str]] = {}
_ENTITLED: set[tuple[str, str]] = set()


async def provision_player(tag: str, app_id: str | None = None) -> Player:
    """A player of this run, entitled on the e2e tier by the owner, with a minted app token
    and a game client at the app's datacenter."""
    app = app_id or CONFIG.app_id
    email = derive_email(tag)
    if tag in _ACCOUNTS:
        identity, user_id = await identity_client(email, derive_password(email))
    else:
        identity, user_id = await identity_client(email)
        _ACCOUNTS[tag] = (email, user_id)
    if (app, user_id) not in _ENTITLED:
        owner = await owner_client()
        try:
            tier = await ensure_tier(owner, app)
            await owner.admin.app_access.grant({"appId": app, "userId": user_id, "tierId": tier})
        finally:
            await owner.aclose()
        _ENTITLED.add((app, user_id))
    minted = await identity.portal.mint_app_token(app)
    assert len(minted.token) == 64
    game = crowdypy.AsyncCrowdyClient(
        http_url=CONFIG.http_url or minted.game_api_url or CONFIG.api_url,
        discovery_url=minted.discovery_url or CONFIG.api_url,
    )
    game.set_app_token(minted)
    return Player(identity=identity, game=game, minted=minted, user_id=user_id, email=email)


async def connect_udp(player: Player) -> None:
    """Assign a server and open the native connection, retrying while server-status
    heartbeats settle, then announce ourselves so notifications can reach us."""
    last: BaseException | None = None
    for _ in range(10):
        try:
            await player.game.udp.connect(player.minted)
            break
        except crowdypy.CrowdyError as error:
            last = error
            await asyncio.sleep(3)
    else:
        raise AssertionError(f"no replication server assigned: {last}")
    player.game.udp.connection.send_heartbeat((0, 0, 0), uuid.uuid4().hex)


async def warm_up(player: Player, chunk: tuple[int, int, int], budget_s: float = 15.0) -> bool:
    """Send actor updates until one is acknowledged: the session's grid-permission window
    can deny the first ones after connect."""
    actor = uuid.uuid4().hex
    deadline = asyncio.get_running_loop().time() + budget_s
    while asyncio.get_running_loop().time() < deadline:
        try:
            await player.game.udp.send_actor_update_and_wait(chunk, actor, b"\0", timeout=1.0)
        except crowdypy.CrowdyError:
            continue
        return True
    return False


def chunk_band(suite: int) -> tuple[int, int, int]:
    """A chunk only this suite and run use, so parallel runs never cross fan-out."""
    return (700_000 + suite * 100 + int(RUN, 16) % 97, 0, 700_000 + int(RUN, 16) % 89)


@pytest.fixture
async def player_pair() -> AsyncIterator[tuple[Player, Player]]:
    a = await provision_player("py-a")
    b = await provision_player("py-b")
    try:
        yield a, b
    finally:
        await a.aclose()
        await b.aclose()


async def eventually(check: Any, timeout_s: float = 10.0, every_s: float = 0.05) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if check():
            return True
        await asyncio.sleep(every_s)
    return bool(check())
