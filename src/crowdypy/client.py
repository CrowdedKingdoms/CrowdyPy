"""The CrowdyPy client: one API origin, one token, every domain sub-client.

Two tokens, two clients. Sign in on an identity client (session token: account, studio
administration, minting), mint an app-scoped token per game with
``identity.portal.mint_app_token(app_id)``, and build one game client per app pointed at
the ``game_api_url`` the mint returns, with ``discovery_url`` set so it can recover if
that instance stops answering.

Async source: ``crowdypy._sync.client.CrowdyClient`` is generated from this module.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from crowdypy._concurrency import AsyncSingleFlight
from crowdypy._default_origin import CROWDY_DEFAULT_HTTP_ORIGIN, CROWDY_DEFAULT_WS_ORIGIN
from crowdypy._grid import GridBox
from crowdypy.auth_state import AuthState
from crowdypy.datacenter_redirect import DatacenterMove
from crowdypy.domains.actors import ActorsAPI
from crowdypy.domains.admin import AdminAPI
from crowdypy.domains.app_access import AppAccessAPI
from crowdypy.domains.apps import AppsAPI
from crowdypy.domains.auth import AuthAPI
from crowdypy.domains.avatars import AvatarsAPI
from crowdypy.domains.billing import BillingAPI
from crowdypy.domains.channels import ChannelsAPI
from crowdypy.domains.chunks import ChunksAPI
from crowdypy.domains.crowdy_studio import CrowdyStudioAPI
from crowdypy.domains.crowdy_studio_agent import CrowdyStudioAgentAPI
from crowdypy.domains.crowdy_studio_github import CrowdyStudioGitHubTransport
from crowdypy.domains.discovery import DiscoveryDomain
from crowdypy.domains.exec import ExecAPI
from crowdypy.domains.game_apps import GameAppsAPI
from crowdypy.domains.grids import GridsAPI
from crowdypy.domains.host import HostAPI
from crowdypy.domains.input_log import InputLogAPI
from crowdypy.domains.marketplace import MarketplaceAPI
from crowdypy.domains.organizations import OrganizationsAPI
from crowdypy.domains.payments import PaymentsAPI
from crowdypy.domains.platform import PlatformAPI
from crowdypy.domains.player_wallet import PlayerWalletAPI
from crowdypy.domains.portal import AppTokenResponse, PkceStore, PortalAPI
from crowdypy.domains.quotas import QuotasAPI
from crowdypy.domains.server_status import ServerStatusAPI
from crowdypy.domains.shared_environment import SharedEnvironmentAPI
from crowdypy.domains.state import StateAPI
from crowdypy.domains.teams import TeamsAPI
from crowdypy.domains.teleport import TeleportAPI
from crowdypy.domains.udp import UdpAPI
from crowdypy.domains.usage import UsageAPI
from crowdypy.domains.users import UsersAPI
from crowdypy.domains.voxels import VoxelsAPI
from crowdypy.estate import is_same_estate
from crowdypy.graphql import AsyncGraphQLClient, graphql_endpoint
from crowdypy.grid_scope import GridScope
from crowdypy.kit.kit import GameKitClient
from crowdypy.lb_cookie import LbCookieStore
from crowdypy.rediscover import RediscoverFn, create_bootstrap_rediscover
from crowdypy.replication import ConnState, token_material
from crowdypy.session import TokenStore
from crowdypy.world import WorldClient

__all__ = ["AsyncCrowdyClient", "create_crowdy_client"]


class AsyncCrowdyClient:
    """The async client. ``CrowdyClient`` (``crowdypy.CrowdyClient``) is its blocking twin.

    With no origin configured it dials the public API of the tier this build was
    published for (``crowdypy._default_origin``).
    """

    def __init__(
        self,
        *,
        http_url: str | None = None,
        ws_url: str | None = None,
        graphql_endpoint_url: str | None = None,
        ws_endpoint_url: str | None = None,
        timeout: float = 60.0,
        token_store: TokenStore | None = None,
        logger: logging.Logger | None = None,
        lb_cookie_store: LbCookieStore | None = None,
        http_client: httpx.AsyncClient | None = None,
        pkce_store: PkceStore | None = None,
        discovery_url: str | None = None,
        rediscover: RediscoverFn | None = None,
        rediscover_after_failures: int = 3,
    ) -> None:
        self._logger = logger or logging.getLogger("crowdypy")
        configured = any(
            value and value.strip()
            for value in (http_url, ws_url, graphql_endpoint_url, ws_endpoint_url)
        )
        if not configured:
            http_url, ws_url = CROWDY_DEFAULT_HTTP_ORIGIN, CROWDY_DEFAULT_WS_ORIGIN
        self.session = AuthState(token_store)
        self.lb_cookie_store = lb_cookie_store or LbCookieStore()
        self.graphql = AsyncGraphQLClient(
            self.session,
            endpoint=graphql_endpoint_url or graphql_endpoint(http_url),
            timeout=timeout,
            logger=self._logger,
            lb_cookie_store=self.lb_cookie_store,
            http_client=http_client,
        )
        #: The GraphQL WebSocket endpoint realtime subscriptions dial; moves with the HTTP one.
        self.ws_endpoint: str | None = ws_endpoint_url or graphql_endpoint(ws_url)
        self.discovery_url = discovery_url
        self.rediscover_after_failures = rediscover_after_failures
        self._rediscover: RediscoverFn | None = rediscover or (
            create_bootstrap_rediscover(discovery_url, self.session.get_token, logger=self._logger)
            if discovery_url
            else None
        )
        self._rediscover_flight: AsyncSingleFlight[bool] = AsyncSingleFlight()
        self._refresh_flight: AsyncSingleFlight[AppTokenResponse] = AsyncSingleFlight()
        self.graphql.set_wrong_datacenter_handler(self.move_to_datacenter)

        self.auth = AuthAPI(self.graphql, self.session)
        self.users = UsersAPI(self.graphql)
        self.apps = AppsAPI(self.graphql)
        self.portal = PortalAPI(self.graphql, self.session, pkce_store)
        self.discovery = DiscoveryDomain(self.graphql)
        self.platform = PlatformAPI(self.graphql)
        self.organizations = OrganizationsAPI(self.graphql)
        self.app_access = AppAccessAPI(self.graphql)
        self.billing = BillingAPI(self.graphql)
        self.payments = PaymentsAPI(self.graphql)
        self.quotas = QuotasAPI(self.graphql)
        self.usage = UsageAPI(self.graphql)
        self.shared_environment = SharedEnvironmentAPI(self.graphql)
        self.chunks = ChunksAPI(self.graphql)
        self.voxels = VoxelsAPI(self.graphql)
        self.actors = ActorsAPI(self.graphql)
        self.teleport = TeleportAPI(self.graphql)
        self.input_log = InputLogAPI(self.graphql)
        self.state = StateAPI(self.graphql)
        self.server_status = ServerStatusAPI(self.graphql)
        self.channels = ChannelsAPI(self.graphql)
        self.grids = GridsAPI(self.graphql)
        self.teams = TeamsAPI(self.graphql)
        self.exec = ExecAPI(self.graphql)
        self.crowdy_studio = CrowdyStudioAPI(self.graphql)
        self.crowdy_studio_github = CrowdyStudioGitHubTransport(self.graphql)
        self.crowdy_studio_agent = CrowdyStudioAgentAPI(self.graphql)
        self.player_wallet = PlayerWalletAPI(self.graphql)
        self.marketplace = MarketplaceAPI(self.graphql)
        self.avatars = AvatarsAPI(self.graphql)
        self.host = HostAPI(self.graphql)
        self.game_apps = GameAppsAPI(self.graphql)
        self.admin = AdminAPI(
            organizations=self.organizations,
            apps=self.apps,
            app_access=self.app_access,
            billing=self.billing,
            payments=self.payments,
            quotas=self.quotas,
            usage=self.usage,
            shared_environment=self.shared_environment,
            grids=self.game_apps,
        )
        #: Native UDP replication for this client's app (CrowdyCPP's connection).
        self.udp = UdpAPI(self)
        #: The app token this client last installed or refreshed, with what native UDP needs.
        self.app_token: AppTokenResponse | None = None

    # ------------------------------------------------------------------ the token

    def set_token(self, token: str | None) -> None:
        self.session.set_token(token)

    def get_token(self) -> str | None:
        return self.session.get_token()

    def set_app_token(self, token: AppTokenResponse) -> None:
        """Install a minted app token: the bearer, plus the token id and expiry ``udp`` signs
        with. Prefer it to ``set_token(minted.token)`` on a game client."""
        self.app_token = token
        self.session.set_token(token.token)

    async def refresh_gameplay_token(self) -> AppTokenResponse:
        """Refresh this game client's app token; concurrent callers share one refresh."""
        return await self._refresh_flight.run(self._perform_gameplay_token_refresh)

    async def _perform_gameplay_token_refresh(self) -> AppTokenResponse:
        # A live UDP connection is quiesced first and resumed under the new token, so nothing
        # leaves signed with a key the server no longer accepts once the bearer has changed.
        connection = self.udp.connection
        resume = connection is not None and connection.state in (
            ConnState.CONNECTING,
            ConnState.CONNECTED,
            ConnState.RECONNECTING,
        )
        if connection is not None and resume:
            await connection.disconnect()
        token = await self.portal.refresh()
        self.app_token = token
        if connection is not None and resume:
            connection.set_token(token_material(token))
            await connection.connect()
        return token

    async def wait_for_gameplay_token_refresh(self) -> None:
        """Wait for an in-flight refresh, if any, ignoring its outcome."""
        await self._refresh_flight.wait()

    # ------------------------------------------------------------------ scopes

    def grid(self, app_id: str | int, grid_id: str | int, box: GridBox | None = None) -> GridScope:
        """One grid, bound once: the grid calls with the app and grid filled in.

        The box is learned from the first :meth:`GridScope.mint_token`, or pass it here.
        """
        return GridScope(self.grids, self.channels, app_id, grid_id, box, self.udp)

    def world(self, app_id: str | int) -> WorldClient:
        """The app-scoped realtime facade: actors that remember their chunk, over ``udp``."""
        return WorldClient(app_id, self.udp)

    def kit(self, app_id: str | int, **options: Any) -> GameKitClient:
        """The app-scoped Game Kit: parties, guilds and chat rooms (``kit.social``)."""
        return GameKitClient(
            app_id,
            self.game_apps,
            teams=self.teams,
            channels=self.channels,
            udp=self.udp,
            **options,
        )

    # -------------------------------------------------------- moving the endpoint

    @property
    def graphql_endpoint(self) -> str:
        return self.graphql.endpoint

    def move_to_datacenter(self, move: DatacenterMove) -> bool:
        """Follow a ``WRONG_DATACENTER`` refusal: move HTTP and realtime endpoints together.

        Returns ``False`` (and moves nothing) when the endpoint is the one already held,
        so the transport's single retry cannot loop, and when a target is outside this
        client's estate (:func:`crowdypy.estate.is_same_estate`).
        """
        endpoint = graphql_endpoint(move.game_api_url) or move.game_api_url
        if endpoint == self.graphql.get_endpoint():
            return False
        ws = graphql_endpoint(move.game_api_ws_url)
        if not self._within_estate(endpoint, ws):
            return False
        self.graphql.set_endpoint(endpoint)
        if ws:
            self.ws_endpoint = ws
        if self.udp.connection is not None:
            # Assignment comes through the API client, so it now answers from the new
            # datacenter; without this the UDP session would stay in the old one.
            self.udp.connection.request_reassignment()
        self._logger.info("moved to %s", move.app_datacenter or endpoint)
        return True

    def _within_estate(self, *targets: str | None) -> bool:
        # A move target comes from the server over an authenticated connection; this bounds
        # what one compromised instance may ASK for, since the bearer token follows the move.
        current = self.graphql.get_endpoint()
        for target in targets:
            if target and not is_same_estate(current, target):
                self._logger.warning("refused a move to %s: outside this client's estate", target)
                return False
        return True

    async def rediscover_endpoint(self, app_id: str | None) -> bool:
        """Ask the discovery origin where this app lives now; True when the client moved.

        Concurrent triggers (a failed assignment, a dead socket) share one attempt.
        """
        if self._rediscover is None:
            return False
        rediscover = self._rediscover

        async def attempt() -> bool:
            found = await rediscover(app_id)
            if found is None:
                return False
            http = graphql_endpoint(found.http_url)
            ws = graphql_endpoint(found.ws_url)
            if not self._within_estate(http, ws):
                return False
            moved = False
            if http and http != self.graphql.get_endpoint():
                self.graphql.set_endpoint(http)
                moved = True
            if ws and ws != self.ws_endpoint:
                self.ws_endpoint = ws
                moved = True
            return moved

        return await self._rediscover_flight.run(attempt)

    # ------------------------------------------------------------------ lifecycle

    async def aclose(self) -> None:
        """Close the UDP connection and the HTTP transport, and forget the token held in memory."""
        await self.udp.disconnect()
        await self.graphql.aclose()
        self.session.set_token(None, persist=False)

    async def __aenter__(self) -> AsyncCrowdyClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


def create_crowdy_client(**kwargs: object) -> AsyncCrowdyClient:
    """Build an :class:`AsyncCrowdyClient` (``crowdypy.sync`` has the blocking factory)."""
    return AsyncCrowdyClient(**kwargs)  # type: ignore[arg-type]
