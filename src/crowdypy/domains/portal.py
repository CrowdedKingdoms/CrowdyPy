"""App-scoped tokens and the portal's authorization-code flow (``client.portal``).

Gameplay needs a short-lived **app-scoped token** per app. Mint one on the identity
client with :meth:`PortalAPI.mint_app_token` and hand it to a per-game client pointed at
the ``game_api_url`` the mint returns. The token is also the 64-octet HMAC key the native
replication client signs every datagram with.

A native client that refreshes while connected names the replication server it is on
(:meth:`PortalAPI.refresh` with ``current_server``) so the Game API authorizes the new
token on that same server; a server silently drops datagrams for a token it was never
told about.

Hosted sign-in (CrowdyJS ``portal.signIn`` / ``handleSignInCallback`` /
``handleAuthorizeRequest``) navigates a browser tab and reads its location, so it is a
browser exclusion here, as in CrowdyCPP. The PKCE steps underneath it are not:
:meth:`PortalAPI.begin_entry` builds the authorize URL (open it in the system browser)
and :meth:`PortalAPI.complete_entry` exchanges the code your redirect receives.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import msgspec

from crowdypy._operation import inline_operation
from crowdypy.domains._base import Domain, omit_none
from crowdypy.graphql import AsyncGraphQLClient
from crowdypy.pkce import generate_pkce_pair, generate_state
from crowdypy.session import SessionStore
from crowdypy.utils import bigint

__all__ = [
    "AppAuthorizationGrant",
    "AppRuntimeGate",
    "AppTokenResponse",
    "AuthorizedServer",
    "CurrentServer",
    "MemoryPkceStore",
    "PkceStore",
    "PortalAPI",
    "PortalConsentRequiredError",
    "default_hosted_sign_in_url",
    "is_hosted_sign_in_required_error",
    "is_legal_acceptance_required_error",
]


class AuthorizedServer(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    ip4: str
    client_port: int


class AppRuntimeGate(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """An app's runtime gate as a player's client may read it. Any ``status`` but ``ACTIVE``
    means the app is paused: replication delivers nothing and refuses sends with
    ``APP_PAUSED``, and hub calls are refused. Tell the player the world is paused instead of
    showing an empty one (:func:`crowdypy.is_app_paused`)."""

    #: ``ACTIVE``, ``GRACE``, ``DENIED`` or ``SUSPENDED`` (an open set).
    status: str
    #: ``insufficient_funds``, ``spend_cap`` or ``subscription_lapsed``; ``None`` while active.
    reason: str | None = None


class AppTokenResponse(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A gameplay token and where to use it. Its repr never shows the token."""

    token: str
    game_token_id: str
    app_id: str
    expires_at: str
    game_api_url: str | None = None
    game_api_ws_url: str | None = None
    discovery_url: str | None = None
    launch_url: str | None = None
    #: Set on a refresh that named its current server and was installed there.
    authorized_server: AuthorizedServer | None = None
    #: The app's runtime gate when the token was minted. A paused app still mints, so check
    #: :func:`crowdypy.is_app_paused` before entering the world. ``None`` when the server
    #: could not read it.
    runtime_gate: AppRuntimeGate | None = None

    def __repr__(self) -> str:
        return (
            f"AppTokenResponse(app_id={self.app_id!r}, game_token_id={self.game_token_id!r}, "
            f"expires_at={self.expires_at!r}, game_api_url={self.game_api_url!r}, token=<redacted>)"
        )


class CurrentServer(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """The replication server a native client is connected to."""

    ip4: str
    client_port: int


class AppAuthorizationGrant(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    grant_id: str
    app_id: str
    status: str
    scopes: list[str] = msgspec.field(default_factory=list)
    app_name: str | None = None
    granted_at: str | None = None
    revoked_at: str | None = None


class PortalConsentRequiredError(Exception):
    """The player has not consented to this app yet."""

    def __init__(self, app_id: str, app_name: str | None) -> None:
        super().__init__(f"Consent required for app {app_id}")
        self.app_id = app_id
        self.app_name = app_name


class PkceStore(Protocol):
    """Keeps each PKCE verifier between :meth:`PortalAPI.begin_entry` and the redirect."""

    def get(self, state: str) -> str | None: ...

    def set(self, state: str, verifier: str) -> None: ...

    def remove(self, state: str) -> None: ...


class MemoryPkceStore:
    """The default: verifiers live in this process only and never leave it."""

    def __init__(self) -> None:
        self._verifiers: dict[str, str] = {}

    def get(self, state: str) -> str | None:
        return self._verifiers.get(state)

    def set(self, state: str, verifier: str) -> None:
        self._verifiers[state] = verifier

    def remove(self, state: str) -> None:
        self._verifiers.pop(state, None)


def default_hosted_sign_in_url(graphql_endpoint: str) -> str:
    """Studio's hosted ``/authorize`` for the API an endpoint names.

    ``ck.<tier>.`` maps to ``studio.<tier>.``; a local API on port 3000 maps to the local
    Studio on 3001.
    """
    parts = urlsplit(graphql_endpoint)
    host = (parts.hostname or "").lower()
    if host in ("localhost", "127.0.0.1"):
        return f"{parts.scheme}://{host}:3001/authorize"
    if host.startswith("ck."):
        return f"https://studio.{host[len('ck.') :]}/authorize"
    raise ValueError(
        f"Cannot derive the hosted sign-in page from {graphql_endpoint}; pass authorize_url "
        "explicitly (the tier's Studio origin + /authorize)."
    )


def is_hosted_sign_in_required_error(error: object) -> bool:
    """The API refused direct sign-in because the caller presented a browser origin."""
    code = getattr(error, "code", None)
    if code == "HOSTED_SIGN_IN_REQUIRED":
        return True
    for entry in getattr(error, "graphql_errors", None) or ():
        if (entry.get("extensions") or {}).get("code") == "HOSTED_SIGN_IN_REQUIRED":
            return True
    message = str(error)
    return (
        "HOSTED_SIGN_IN_REQUIRED" in message or "only available to first-party" in message.lower()
    )


def is_legal_acceptance_required_error(error: object) -> bool:
    """``LEGAL_ACCEPTANCE_REQUIRED``: the player has not stored the current required legal
    documents and the age-of-majority attestation, so no gameplay token is issued
    (:meth:`PortalAPI.mint_app_token`, :meth:`PortalAPI.create_authorization_code`,
    :meth:`PortalAPI.refresh`). Show your own clickwrap, then call
    ``auth.record_player_consents``."""
    code = getattr(error, "code", None)
    if code == "LEGAL_ACCEPTANCE_REQUIRED":
        return True
    extensions = getattr(error, "extensions", None)
    if isinstance(extensions, dict) and extensions.get("code") == "LEGAL_ACCEPTANCE_REQUIRED":
        return True
    for entry in getattr(error, "graphql_errors", None) or ():
        if (entry.get("extensions") or {}).get("code") == "LEGAL_ACCEPTANCE_REQUIRED":
            return True
    return "LEGAL_ACCEPTANCE_REQUIRED" in str(error)


_APP_TOKEN_FIELDS = (
    "token gameTokenId appId expiresAt gameApiUrl gameApiWsUrl discoveryUrl launchUrl "
    "runtimeGate { status reason }"
)

MINT_APP_TOKEN = inline_operation(
    "MintAppToken",
    "mutation",
    "mintAppToken",
    "mutation MintAppToken($input: MintAppTokenInput!) { mintAppToken(input: $input) { "
    + _APP_TOKEN_FIELDS
    + " } }",
)
CREATE_PORTAL_AUTHORIZATION_CODE = inline_operation(
    "CreatePortalAuthorizationCode",
    "mutation",
    "createPortalAuthorizationCode",
    "mutation CreatePortalAuthorizationCode($input: CreatePortalAuthorizationCodeInput!) { "
    "createPortalAuthorizationCode(input: $input) { code redirectUri expiresAt } }",
)
EXCHANGE_PORTAL_CODE = inline_operation(
    "ExchangePortalCode",
    "mutation",
    "exchangePortalCode",
    "mutation ExchangePortalCode($input: ExchangePortalCodeInput!) { exchangePortalCode(input: $input) { "
    + _APP_TOKEN_FIELDS
    + " } }",
)
REFRESH_APP_TOKEN = inline_operation(
    "RefreshAppToken",
    "mutation",
    "refreshAppToken",
    "mutation RefreshAppToken { refreshAppToken { " + _APP_TOKEN_FIELDS + " } }",
)
REFRESH_APP_TOKEN_ON_SERVER = inline_operation(
    "RefreshAppTokenOnServer",
    "mutation",
    "refreshAppToken",
    "mutation RefreshAppTokenOnServer($currentServer: CurrentServerInput) { "
    "refreshAppToken(currentServer: $currentServer) { "
    + _APP_TOKEN_FIELDS
    + " authorizedServer { ip4 clientPort } } }",
)
PORTAL_CONSENT = inline_operation(
    "PortalConsent",
    "query",
    "portalConsent",
    "query PortalConsent($appId: BigInt!) { portalConsent(appId: $appId) { "
    "appId appName trusted alreadyGranted consentRequired } }",
)
AUTHORIZE_APP = inline_operation(
    "AuthorizeApp",
    "mutation",
    "authorizeApp",
    "mutation AuthorizeApp($input: AuthorizeAppInput!) { authorizeApp(input: $input) { "
    "grantId appId status scopes } }",
)
REVOKE_APP_AUTHORIZATION = inline_operation(
    "RevokeAppAuthorization",
    "mutation",
    "revokeAppAuthorization",
    "mutation RevokeAppAuthorization($appId: BigInt!) { revokeAppAuthorization(appId: $appId) }",
)
MY_AUTHORIZED_APPS = inline_operation(
    "MyAuthorizedApps",
    "query",
    "myAuthorizedApps",
    "query MyAuthorizedApps { myAuthorizedApps { "
    "grantId appId appName scopes status grantedAt revokedAt } }",
)
SET_APP_CLIENT_SETTINGS = inline_operation(
    "SetAppClientSettings",
    "mutation",
    "setAppClientSettings",
    "mutation SetAppClientSettings($input: SetAppClientSettingsInput!) { "
    "setAppClientSettings(input: $input) { appId appName trusted consentRequired } }",
)

INLINE_OPERATIONS = (
    MINT_APP_TOKEN,
    CREATE_PORTAL_AUTHORIZATION_CODE,
    EXCHANGE_PORTAL_CODE,
    REFRESH_APP_TOKEN,
    REFRESH_APP_TOKEN_ON_SERVER,
    PORTAL_CONSENT,
    AUTHORIZE_APP,
    REVOKE_APP_AUTHORIZATION,
    MY_AUTHORIZED_APPS,
    SET_APP_CLIENT_SETTINGS,
)


def _server_input(server: CurrentServer | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(server, CurrentServer):
        return {"ip4": server.ip4, "clientPort": server.client_port}
    port = server.get("clientPort", server.get("client_port"))
    return {"ip4": str(server["ip4"]), "clientPort": int(port) if port is not None else 0}


def _query_params(url_or_query: str) -> dict[str, str]:
    query = urlsplit(url_or_query).query if "://" in url_or_query else url_or_query.lstrip("?")
    return {key: values[0] for key, values in parse_qs(query, keep_blank_values=True).items()}


class PortalAPI(Domain):
    def __init__(
        self,
        graphql: AsyncGraphQLClient,
        session: SessionStore,
        pkce_store: PkceStore | None = None,
    ) -> None:
        super().__init__(graphql)
        self._session = session
        self._pkce_store: PkceStore = pkce_store or MemoryPkceStore()

    async def mint_app_token(self, app_id: str | int) -> AppTokenResponse:
        """Mint an app-scoped token on the identity client. It is NOT stored here.

        Give it to the per-game client (``game.set_token(minted.token)``).
        """
        payload = await self._request(MINT_APP_TOKEN, {"input": {"appId": bigint(app_id)}})
        return msgspec.convert(payload, AppTokenResponse)

    async def create_authorization_code(
        self,
        app_id: str | int,
        code_challenge: str,
        redirect_uri: str,
        code_challenge_method: str | None = None,
    ) -> dict[str, Any]:
        input_ = omit_none(
            {
                "appId": bigint(app_id),
                "codeChallenge": code_challenge,
                "codeChallengeMethod": code_challenge_method,
                "redirectUri": redirect_uri,
            }
        )
        result: dict[str, Any] = await self._request(
            CREATE_PORTAL_AUTHORIZATION_CODE, {"input": input_}
        )
        return result

    async def exchange_code(self, code: str, code_verifier: str | None = None) -> AppTokenResponse:
        """Exchange an authorization code for an app token, and hold it on this client."""
        payload = await self._request(
            EXCHANGE_PORTAL_CODE,
            {"input": omit_none({"code": code, "codeVerifier": code_verifier})},
        )
        token = msgspec.convert(payload, AppTokenResponse)
        self._session.set_token(token.token)
        return token

    async def refresh(
        self, current_server: CurrentServer | Mapping[str, Any] | None = None
    ) -> AppTokenResponse:
        """Refresh this client's app token and hold the new one.

        With ``current_server`` (the ``ip4`` and ``client_port`` the client is connected
        to) the Game API also authorizes the new token on that server and answers
        ``authorized_server``: when it is set, keep the socket and sign with the new
        token; when it is ``None``, place again.
        """
        if current_server is None:
            payload = await self._request(REFRESH_APP_TOKEN, {})
        else:
            payload = await self._request(
                REFRESH_APP_TOKEN_ON_SERVER, {"currentServer": _server_input(current_server)}
            )
        token = msgspec.convert(payload, AppTokenResponse)
        self._session.set_token(token.token)
        return token

    async def get_consent(self, app_id: str | int) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(PORTAL_CONSENT, {"appId": bigint(app_id)})
        return result

    async def authorize_app(
        self, app_id: str | int, scopes: Sequence[str] | None = None
    ) -> AppAuthorizationGrant:
        input_: dict[str, Any] = {"appId": bigint(app_id)}
        if scopes is not None:
            input_["scopes"] = list(scopes)
        return msgspec.convert(
            await self._request(AUTHORIZE_APP, {"input": input_}), AppAuthorizationGrant
        )

    async def revoke_app_authorization(self, app_id: str | int) -> bool:
        return bool(await self._request(REVOKE_APP_AUTHORIZATION, {"appId": bigint(app_id)}))

    async def my_authorized_apps(self) -> list[AppAuthorizationGrant]:
        return msgspec.convert(await self._request(MY_AUTHORIZED_APPS), list[AppAuthorizationGrant])

    async def set_app_client_settings(
        self,
        app_id: str | int,
        redirect_uris: Sequence[str] | None = None,
        client_type: str | None = None,
        launch_url: str | None = None,
    ) -> dict[str, Any]:
        input_ = omit_none(
            {
                "appId": bigint(app_id),
                "redirectUris": list(redirect_uris) if redirect_uris is not None else None,
                "clientType": client_type,
                "launchUrl": launch_url,
            }
        )
        result: dict[str, Any] = await self._request(SET_APP_CLIENT_SETTINGS, {"input": input_})
        return result

    async def begin_entry(
        self, app_id: str | int, authorize_url: str, redirect_uri: str, state: str | None = None
    ) -> str:
        """Start the PKCE flow: remember a verifier and return the authorize URL to open.

        The verifier never leaves this process; :meth:`complete_entry` looks it up by the
        ``state`` the redirect carries back.
        """
        state = state or generate_state()
        pkce = generate_pkce_pair()
        self._pkce_store.set(state, pkce.verifier)
        parts = urlsplit(authorize_url)
        params = {
            key: values[0] for key, values in parse_qs(parts.query, keep_blank_values=True).items()
        }
        params.update(
            {
                "app_id": str(app_id),
                "code_challenge": pkce.challenge,
                "code_challenge_method": pkce.method,
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment)
        )

    async def complete_entry(self, redirect: str) -> AppTokenResponse | None:
        """Finish the PKCE flow from the URL (or query string) your redirect received.

        Returns ``None`` when it carries no ``code``, and for a GitHub App callback, whose
        ``code`` is GitHub's and must never be spent as a portal code.
        """
        params = _query_params(redirect)
        if {"github", "installation_id", "setup_action"} & params.keys():
            return None
        code = params.get("code")
        if not code:
            return None
        state = params.get("state", "")
        verifier = self._pkce_store.get(state)
        token = await self.exchange_code(code, verifier)
        if state:
            self._pkce_store.remove(state)
        return token
