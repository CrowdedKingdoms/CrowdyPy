"""CrowdyPy: the official Python SDK for Crowded Kingdoms.

Typed clients for the platform's one GraphQL API (identity, studio administration, world
data, ck-exec) and a native UDP replication core, CrowdyCPP's, bound with nanobind.

    import crowdypy

    identity = crowdypy.AsyncCrowdyClient(http_url="https://ck.dev.crowdedkingdoms.com")
    await identity.auth.login("player@example.com", "correct-horse-battery")
    minted = await identity.portal.mint_app_token(app_id)
    game = crowdypy.AsyncCrowdyClient(http_url=minted.game_api_url, discovery_url=minted.discovery_url)
    game.set_app_token(minted)

Names resolve lazily, so ``import crowdypy`` stays cheap; ``crowdypy.sync`` is the blocking
client.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from crowdypy._default_origin import (
    CROWDY_DEFAULT_HOST,
    CROWDY_DEFAULT_HTTP_ORIGIN,
    CROWDY_DEFAULT_TIER,
    CROWDY_DEFAULT_WS_ORIGIN,
)
from crowdypy._version import __version__

_LAZY: dict[str, str] = {
    # clients and transport
    "AsyncCrowdyClient": "crowdypy.client",
    "create_crowdy_client": "crowdypy.client",
    "CrowdyClient": "crowdypy._sync.client",
    "AsyncGraphQLClient": "crowdypy.graphql",
    "GraphQLClient": "crowdypy._sync.graphql",
    "Operation": "crowdypy._operation",
    "GridBox": "crowdypy._grid",
    "GridChunk": "crowdypy._grid",
    "GridScope": "crowdypy.grid_scope",
    "WorldClient": "crowdypy.world",
    "ActorClient": "crowdypy.world",
    "GameKitClient": "crowdypy.kit",
    "run_optimistic_action": "crowdypy.kit",
    "WorldSessionCore": "crowdypy.stores",
    "create_world_session": "crowdypy.stores",
    "GraphQLSubscriptions": "crowdypy.subscriptions",
    "AsyncExecConnection": "crowdypy.exec_gateway",
    "ExecConnection": "crowdypy.exec_gateway",
    # errors
    "ACCESS_NOT_GRANTED_CODE": "crowdypy.errors",
    "ACCESS_REVOKED_CODE": "crowdypy.errors",
    "ACCESS_SUSPENDED_CODE": "crowdypy.errors",
    "ACTOR_EXISTS_CODE": "crowdypy.errors",
    "APP_PAUSED_CODE": "crowdypy.errors",
    "APP_UNAVAILABLE_CODE": "crowdypy.errors",
    "WRONG_DATACENTER_CODE": "crowdypy.errors",
    "CrowdyAccessRefusal": "crowdypy.errors",
    "CrowdyActorExists": "crowdypy.errors",
    "CrowdyAppPaused": "crowdypy.errors",
    "CrowdyAppUnavailableError": "crowdypy.errors",
    "CrowdyError": "crowdypy.errors",
    "CrowdyGraphQLError": "crowdypy.errors",
    "CrowdyHttpError": "crowdypy.errors",
    "CrowdyNetworkError": "crowdypy.errors",
    "CrowdyPlayerFault": "crowdypy.errors",
    "CrowdyProtocolError": "crowdypy.errors",
    "CrowdyRealtimeError": "crowdypy.errors",
    "CrowdyReplicationError": "crowdypy.errors",
    "CrowdyTimeoutError": "crowdypy.errors",
    "CrowdyUserCodeFaultError": "crowdypy.errors",
    "GridScopeError": "crowdypy.errors",
    "access_refusal_of": "crowdypy.errors",
    "actor_exists_of": "crowdypy.errors",
    "app_paused_of": "crowdypy.errors",
    "is_app_paused": "crowdypy.errors",
    "player_fault_of": "crowdypy.errors",
    # sessions, tokens, routing
    "AuthState": "crowdypy.auth_state",
    "FileTokenStore": "crowdypy.session",
    "MemoryTokenStore": "crowdypy.session",
    "SessionStore": "crowdypy.session",
    "TokenStore": "crowdypy.session",
    "LbCookieStore": "crowdypy.lb_cookie",
    "DatacenterMove": "crowdypy.datacenter_redirect",
    "move_from_error": "crowdypy.datacenter_redirect",
    "move_from_errors": "crowdypy.datacenter_redirect",
    "is_same_estate": "crowdypy.estate",
    "Endpoint": "crowdypy.rediscover",
    "create_bootstrap_rediscover": "crowdypy.rediscover",
    "create_mint_rediscover": "crowdypy.rediscover",
    "PkcePair": "crowdypy.pkce",
    "generate_pkce_pair": "crowdypy.pkce",
    "generate_state": "crowdypy.pkce",
    # helpers
    "SequenceAllocator": "crowdypy.utils",
    "decode_base64": "crowdypy.utils",
    "encode_base64": "crowdypy.utils",
    "generate_crowdy_uuid": "crowdypy.utils",
    "validate_chunk_coordinates": "crowdypy.utils",
    "validate_crowdy_uuid": "crowdypy.utils",
    # identity results and helpers
    "AppRuntimeGate": "crowdypy.domains.portal",
    "AppTokenResponse": "crowdypy.domains.portal",
    "CurrentServer": "crowdypy.domains.portal",
    "MemoryPkceStore": "crowdypy.domains.portal",
    "PortalConsentRequiredError": "crowdypy.domains.portal",
    "default_hosted_sign_in_url": "crowdypy.domains.portal",
    "is_hosted_sign_in_required_error": "crowdypy.domains.portal",
    "is_legal_acceptance_required_error": "crowdypy.domains.portal",
    "AuthResponse": "crowdypy.domains.auth",
    "is_already_registered_error": "crowdypy.domains.auth",
    "is_invalid_current_password_error": "crowdypy.domains.auth",
    "is_no_password_set_error": "crowdypy.domains.auth",
    "is_password_already_set_error": "crowdypy.domains.auth",
    "is_password_unconfirmed_error": "crowdypy.domains.auth",
    # domain sub-clients
    "ActorsAPI": "crowdypy.domains.actors",
    "AdminAPI": "crowdypy.domains.admin",
    "AppAccessAPI": "crowdypy.domains.app_access",
    "AppsAPI": "crowdypy.domains.apps",
    "AuthAPI": "crowdypy.domains.auth",
    "AvatarsAPI": "crowdypy.domains.avatars",
    "BillingAPI": "crowdypy.domains.billing",
    "ChannelsAPI": "crowdypy.domains.channels",
    "ChunksAPI": "crowdypy.domains.chunks",
    "CrowdyStudioAPI": "crowdypy.domains.crowdy_studio",
    "CrowdyStudioAgentAPI": "crowdypy.domains.crowdy_studio_agent",
    "CrowdyStudioGitHubTransport": "crowdypy.domains.crowdy_studio_github",
    "DiscoveryDomain": "crowdypy.domains.discovery",
    "ExecAPI": "crowdypy.domains.exec",
    "CrowdyExecError": "crowdypy.domains.exec",
    "GameAppsAPI": "crowdypy.domains.game_apps",
    "GridsAPI": "crowdypy.domains.grids",
    "HostAPI": "crowdypy.domains.host",
    "MarketplaceAPI": "crowdypy.domains.marketplace",
    "OrganizationsAPI": "crowdypy.domains.organizations",
    "PaymentsAPI": "crowdypy.domains.payments",
    "PlatformAPI": "crowdypy.domains.platform",
    "PlayerWalletAPI": "crowdypy.domains.player_wallet",
    "PortalAPI": "crowdypy.domains.portal",
    "QuotasAPI": "crowdypy.domains.quotas",
    "ServerStatusAPI": "crowdypy.domains.server_status",
    "SharedEnvironmentAPI": "crowdypy.domains.shared_environment",
    "StateAPI": "crowdypy.domains.state",
    "TeamsAPI": "crowdypy.domains.teams",
    "TeleportAPI": "crowdypy.domains.teleport",
    "InputLogAPI": "crowdypy.domains.input_log",
    "UsageAPI": "crowdypy.domains.usage",
    "UsersAPI": "crowdypy.domains.users",
    "VoxelsAPI": "crowdypy.domains.voxels",
    "UdpAPI": "crowdypy.domains.udp",
    "ConnectionStatus": "crowdypy.domains.udp",
    # native replication
    "AsyncReplicationConnection": "crowdypy.replication",
    "ReplicationConnection": "crowdypy.replication",
    "NotificationBatch": "crowdypy.replication",
    "Notification": "crowdypy.replication",
    "ConnState": "crowdypy.replication",
    "TokenMaterial": "crowdypy.replication",
}
_SUBMODULES = {
    "codecs",
    "enums",
    "errors",
    "exec_gateway",
    "inputs",
    "kit",
    "media",
    "player_host",
    "replication",
    "stores",
    "studio",
    "subscriptions",
    "sync",
    "wire",
}

__all__ = [
    "CROWDY_DEFAULT_HOST",
    "CROWDY_DEFAULT_HTTP_ORIGIN",
    "CROWDY_DEFAULT_TIER",
    "CROWDY_DEFAULT_WS_ORIGIN",
    "__version__",
    *sorted(_LAZY),
]


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        value = getattr(importlib.import_module(_LAZY[name]), name)
        globals()[name] = value
        return value
    if name in _SUBMODULES:
        module = importlib.import_module(f"crowdypy.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module 'crowdypy' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*__all__, *_SUBMODULES})


if TYPE_CHECKING:
    from crowdypy._grid import (
        GridBox as GridBox,
    )
    from crowdypy._grid import (
        GridChunk as GridChunk,
    )
    from crowdypy._operation import (
        Operation as Operation,
    )
    from crowdypy._sync.client import (
        CrowdyClient as CrowdyClient,
    )
    from crowdypy._sync.graphql import (
        GraphQLClient as GraphQLClient,
    )
    from crowdypy.auth_state import (
        AuthState as AuthState,
    )
    from crowdypy.client import (
        AsyncCrowdyClient as AsyncCrowdyClient,
    )
    from crowdypy.client import (
        create_crowdy_client as create_crowdy_client,
    )
    from crowdypy.datacenter_redirect import (
        DatacenterMove as DatacenterMove,
    )
    from crowdypy.datacenter_redirect import (
        move_from_error as move_from_error,
    )
    from crowdypy.datacenter_redirect import (
        move_from_errors as move_from_errors,
    )
    from crowdypy.domains.actors import (
        ActorsAPI as ActorsAPI,
    )
    from crowdypy.domains.admin import (
        AdminAPI as AdminAPI,
    )
    from crowdypy.domains.app_access import (
        AppAccessAPI as AppAccessAPI,
    )
    from crowdypy.domains.apps import (
        AppsAPI as AppsAPI,
    )
    from crowdypy.domains.auth import (
        AuthAPI as AuthAPI,
    )
    from crowdypy.domains.auth import (
        AuthResponse as AuthResponse,
    )
    from crowdypy.domains.auth import (
        is_already_registered_error as is_already_registered_error,
    )
    from crowdypy.domains.auth import (
        is_invalid_current_password_error as is_invalid_current_password_error,
    )
    from crowdypy.domains.auth import (
        is_no_password_set_error as is_no_password_set_error,
    )
    from crowdypy.domains.auth import (
        is_password_already_set_error as is_password_already_set_error,
    )
    from crowdypy.domains.auth import (
        is_password_unconfirmed_error as is_password_unconfirmed_error,
    )
    from crowdypy.domains.avatars import (
        AvatarsAPI as AvatarsAPI,
    )
    from crowdypy.domains.billing import (
        BillingAPI as BillingAPI,
    )
    from crowdypy.domains.channels import (
        ChannelsAPI as ChannelsAPI,
    )
    from crowdypy.domains.chunks import (
        ChunksAPI as ChunksAPI,
    )
    from crowdypy.domains.crowdy_studio import (
        CrowdyStudioAPI as CrowdyStudioAPI,
    )
    from crowdypy.domains.crowdy_studio_agent import (
        CrowdyStudioAgentAPI as CrowdyStudioAgentAPI,
    )
    from crowdypy.domains.crowdy_studio_github import (
        CrowdyStudioGitHubTransport as CrowdyStudioGitHubTransport,
    )
    from crowdypy.domains.discovery import (
        DiscoveryDomain as DiscoveryDomain,
    )
    from crowdypy.domains.exec import (
        CrowdyExecError as CrowdyExecError,
    )
    from crowdypy.domains.exec import (
        ExecAPI as ExecAPI,
    )
    from crowdypy.domains.game_apps import (
        GameAppsAPI as GameAppsAPI,
    )
    from crowdypy.domains.grids import (
        GridsAPI as GridsAPI,
    )
    from crowdypy.domains.host import (
        HostAPI as HostAPI,
    )
    from crowdypy.domains.input_log import (
        InputLogAPI as InputLogAPI,
    )
    from crowdypy.domains.marketplace import (
        MarketplaceAPI as MarketplaceAPI,
    )
    from crowdypy.domains.organizations import (
        OrganizationsAPI as OrganizationsAPI,
    )
    from crowdypy.domains.payments import (
        PaymentsAPI as PaymentsAPI,
    )
    from crowdypy.domains.platform import (
        PlatformAPI as PlatformAPI,
    )
    from crowdypy.domains.player_wallet import (
        PlayerWalletAPI as PlayerWalletAPI,
    )
    from crowdypy.domains.portal import (
        AppRuntimeGate as AppRuntimeGate,
    )
    from crowdypy.domains.portal import (
        AppTokenResponse as AppTokenResponse,
    )
    from crowdypy.domains.portal import (
        CurrentServer as CurrentServer,
    )
    from crowdypy.domains.portal import (
        MemoryPkceStore as MemoryPkceStore,
    )
    from crowdypy.domains.portal import (
        PortalAPI as PortalAPI,
    )
    from crowdypy.domains.portal import (
        PortalConsentRequiredError as PortalConsentRequiredError,
    )
    from crowdypy.domains.portal import (
        default_hosted_sign_in_url as default_hosted_sign_in_url,
    )
    from crowdypy.domains.portal import (
        is_hosted_sign_in_required_error as is_hosted_sign_in_required_error,
    )
    from crowdypy.domains.portal import (
        is_legal_acceptance_required_error as is_legal_acceptance_required_error,
    )
    from crowdypy.domains.quotas import (
        QuotasAPI as QuotasAPI,
    )
    from crowdypy.domains.server_status import (
        ServerStatusAPI as ServerStatusAPI,
    )
    from crowdypy.domains.shared_environment import (
        SharedEnvironmentAPI as SharedEnvironmentAPI,
    )
    from crowdypy.domains.state import (
        StateAPI as StateAPI,
    )
    from crowdypy.domains.teams import (
        TeamsAPI as TeamsAPI,
    )
    from crowdypy.domains.teleport import (
        TeleportAPI as TeleportAPI,
    )
    from crowdypy.domains.udp import (
        ConnectionStatus as ConnectionStatus,
    )
    from crowdypy.domains.udp import (
        UdpAPI as UdpAPI,
    )
    from crowdypy.domains.usage import (
        UsageAPI as UsageAPI,
    )
    from crowdypy.domains.users import (
        UsersAPI as UsersAPI,
    )
    from crowdypy.domains.voxels import (
        VoxelsAPI as VoxelsAPI,
    )
    from crowdypy.errors import (
        ACCESS_NOT_GRANTED_CODE as ACCESS_NOT_GRANTED_CODE,
    )
    from crowdypy.errors import (
        ACCESS_REVOKED_CODE as ACCESS_REVOKED_CODE,
    )
    from crowdypy.errors import (
        ACCESS_SUSPENDED_CODE as ACCESS_SUSPENDED_CODE,
    )
    from crowdypy.errors import (
        ACTOR_EXISTS_CODE as ACTOR_EXISTS_CODE,
    )
    from crowdypy.errors import (
        APP_PAUSED_CODE as APP_PAUSED_CODE,
    )
    from crowdypy.errors import (
        APP_UNAVAILABLE_CODE as APP_UNAVAILABLE_CODE,
    )
    from crowdypy.errors import (
        WRONG_DATACENTER_CODE as WRONG_DATACENTER_CODE,
    )
    from crowdypy.errors import (
        CrowdyAccessRefusal as CrowdyAccessRefusal,
    )
    from crowdypy.errors import (
        CrowdyActorExists as CrowdyActorExists,
    )
    from crowdypy.errors import (
        CrowdyAppPaused as CrowdyAppPaused,
    )
    from crowdypy.errors import (
        CrowdyAppUnavailableError as CrowdyAppUnavailableError,
    )
    from crowdypy.errors import (
        CrowdyError as CrowdyError,
    )
    from crowdypy.errors import (
        CrowdyGraphQLError as CrowdyGraphQLError,
    )
    from crowdypy.errors import (
        CrowdyHttpError as CrowdyHttpError,
    )
    from crowdypy.errors import (
        CrowdyNetworkError as CrowdyNetworkError,
    )
    from crowdypy.errors import (
        CrowdyPlayerFault as CrowdyPlayerFault,
    )
    from crowdypy.errors import (
        CrowdyProtocolError as CrowdyProtocolError,
    )
    from crowdypy.errors import (
        CrowdyRealtimeError as CrowdyRealtimeError,
    )
    from crowdypy.errors import (
        CrowdyReplicationError as CrowdyReplicationError,
    )
    from crowdypy.errors import (
        CrowdyTimeoutError as CrowdyTimeoutError,
    )
    from crowdypy.errors import (
        CrowdyUserCodeFaultError as CrowdyUserCodeFaultError,
    )
    from crowdypy.errors import (
        GridScopeError as GridScopeError,
    )
    from crowdypy.errors import (
        access_refusal_of as access_refusal_of,
    )
    from crowdypy.errors import (
        actor_exists_of as actor_exists_of,
    )
    from crowdypy.errors import (
        app_paused_of as app_paused_of,
    )
    from crowdypy.errors import (
        is_app_paused as is_app_paused,
    )
    from crowdypy.errors import (
        player_fault_of as player_fault_of,
    )
    from crowdypy.estate import (
        is_same_estate as is_same_estate,
    )
    from crowdypy.exec_gateway import (
        AsyncExecConnection as AsyncExecConnection,
    )
    from crowdypy.exec_gateway import (
        ExecConnection as ExecConnection,
    )
    from crowdypy.graphql import (
        AsyncGraphQLClient as AsyncGraphQLClient,
    )
    from crowdypy.grid_scope import (
        GridScope as GridScope,
    )
    from crowdypy.kit import (
        GameKitClient as GameKitClient,
    )
    from crowdypy.kit import (
        run_optimistic_action as run_optimistic_action,
    )
    from crowdypy.lb_cookie import (
        LbCookieStore as LbCookieStore,
    )
    from crowdypy.pkce import (
        PkcePair as PkcePair,
    )
    from crowdypy.pkce import (
        generate_pkce_pair as generate_pkce_pair,
    )
    from crowdypy.pkce import (
        generate_state as generate_state,
    )
    from crowdypy.rediscover import (
        Endpoint as Endpoint,
    )
    from crowdypy.rediscover import (
        create_bootstrap_rediscover as create_bootstrap_rediscover,
    )
    from crowdypy.rediscover import (
        create_mint_rediscover as create_mint_rediscover,
    )
    from crowdypy.replication import (
        AsyncReplicationConnection as AsyncReplicationConnection,
    )
    from crowdypy.replication import (
        ConnState as ConnState,
    )
    from crowdypy.replication import (
        Notification as Notification,
    )
    from crowdypy.replication import (
        NotificationBatch as NotificationBatch,
    )
    from crowdypy.replication import (
        ReplicationConnection as ReplicationConnection,
    )
    from crowdypy.replication import (
        TokenMaterial as TokenMaterial,
    )
    from crowdypy.session import (
        FileTokenStore as FileTokenStore,
    )
    from crowdypy.session import (
        MemoryTokenStore as MemoryTokenStore,
    )
    from crowdypy.session import (
        SessionStore as SessionStore,
    )
    from crowdypy.session import (
        TokenStore as TokenStore,
    )
    from crowdypy.stores import (
        WorldSessionCore as WorldSessionCore,
    )
    from crowdypy.stores import (
        create_world_session as create_world_session,
    )
    from crowdypy.subscriptions import (
        GraphQLSubscriptions as GraphQLSubscriptions,
    )
    from crowdypy.utils import (
        SequenceAllocator as SequenceAllocator,
    )
    from crowdypy.utils import (
        decode_base64 as decode_base64,
    )
    from crowdypy.utils import (
        encode_base64 as encode_base64,
    )
    from crowdypy.utils import (
        generate_crowdy_uuid as generate_crowdy_uuid,
    )
    from crowdypy.utils import (
        validate_chunk_coordinates as validate_chunk_coordinates,
    )
    from crowdypy.utils import (
        validate_crowdy_uuid as validate_crowdy_uuid,
    )
    from crowdypy.world import (
        ActorClient as ActorClient,
    )
    from crowdypy.world import (
        WorldClient as WorldClient,
    )
