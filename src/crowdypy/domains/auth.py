"""Sign-in, passwords, linked identities and sign-out (``client.auth``).

Sign-in yields an **identity session token**: account, studio administration and
token minting. Gameplay rejects it; mint an app-scoped token with
``client.portal.mint_app_token``.

Direct sign-in (``login``, ``register``, magic link, social) is served to first-party
browser origins and to non-browser callers, which send no ``Origin`` header. CrowdyPy
never sends one, so these work from a game server, a tool, a bot or a test. A browser
game on its own domain signs players in through Studio's hosted ``/authorize``
instead (CrowdyJS ``portal.signIn``), and the API refuses it these mutations with
``HOSTED_SIGN_IN_REQUIRED``.
"""

from __future__ import annotations

import re
from typing import Any

import msgspec

from crowdypy._generated import operations as ops
from crowdypy._operation import inline_operation
from crowdypy.auth_state import AuthState
from crowdypy.domains._base import Domain
from crowdypy.graphql import AsyncGraphQLClient

__all__ = [
    "AuthAPI",
    "AuthResponse",
    "AuthUser",
    "UserIdentity",
    "is_already_registered_error",
    "is_invalid_current_password_error",
    "is_no_password_set_error",
    "is_password_already_set_error",
    "is_password_unconfirmed_error",
]


class AuthUser(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    user_id: str
    email: str | None = None
    gamertag: str | None = None


class AuthResponse(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """A session token and who it belongs to. Its repr never shows the token."""

    token: str
    game_token_id: str
    user: AuthUser

    def __repr__(self) -> str:
        return f"AuthResponse(user={self.user!r}, game_token_id={self.game_token_id!r}, token=<redacted>)"


class UserIdentity(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    identity_id: str
    provider: str
    subject: str
    email: str | None
    email_verified: bool
    created_at: str
    last_login_at: str | None


def _code_of(error: object) -> str | None:
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    extensions = getattr(error, "extensions", None)
    if isinstance(extensions, dict) and isinstance(extensions.get("code"), str):
        return str(extensions["code"])
    return None


def _matches(error: object, code: str, pattern: str) -> bool:
    return _code_of(error) == code or re.search(pattern, str(error), re.I) is not None


def is_already_registered_error(error: object) -> bool:
    """``register`` was refused because the email already has an account."""
    return _matches(error, "EMAIL_ALREADY_REGISTERED", r"account with this email already exists")


def is_password_unconfirmed_error(error: object) -> bool:
    """``login`` was refused until the account's email is confirmed."""
    return re.search(r"confirm your email to enable password sign-in", str(error), re.I) is not None


def is_password_already_set_error(error: object) -> bool:
    """``set_initial_password`` on an account that already has one."""
    return _matches(error, "PASSWORD_ALREADY_SET", r"this account already has a password")


def is_no_password_set_error(error: object) -> bool:
    """``change_password`` on an account that has no password yet."""
    return _matches(error, "PASSWORD_NOT_SET", r"no password is set on this account")


def is_invalid_current_password_error(error: object) -> bool:
    return _matches(error, "INVALID_CURRENT_PASSWORD", r"invalid current password")


_AUTH_RESPONSE_FIELDS = "token gameTokenId user { userId email gamertag }"
_IDENTITY_FIELDS = "identityId provider subject email emailVerified createdAt lastLoginAt"


REQUEST_LOGIN_LINK = inline_operation(
    "RequestLoginLink",
    "mutation",
    "requestLoginLink",
    "mutation RequestLoginLink($input: RequestLoginLinkInput!) { requestLoginLink(input: $input) { sent } }",
)
COMPLETE_LOGIN_LINK = inline_operation(
    "CompleteLoginLink",
    "mutation",
    "completeLoginLink",
    "mutation CompleteLoginLink($input: CompleteLoginLinkInput!) { completeLoginLink(input: $input) { "
    + _AUTH_RESPONSE_FIELDS
    + " } }",
)
SOCIAL_LOGIN_START = inline_operation(
    "SocialLoginStart",
    "mutation",
    "socialLoginStart",
    "mutation SocialLoginStart($input: SocialLoginStartInput!) { socialLoginStart(input: $input) { authorizeUrl state } }",
)
SOCIAL_LOGIN_COMPLETE = inline_operation(
    "SocialLoginComplete",
    "mutation",
    "socialLoginComplete",
    "mutation SocialLoginComplete($input: SocialLoginCompleteInput!) { socialLoginComplete(input: $input) { "
    + _AUTH_RESPONSE_FIELDS
    + " } }",
)
LOGIN = inline_operation(
    "Login",
    "mutation",
    "login",
    "mutation Login($loginUserInput: LoginUserInput!) { login(loginUserInput: $loginUserInput) { "
    + _AUTH_RESPONSE_FIELDS
    + " } }",
)
REGISTER = inline_operation(
    "Register",
    "mutation",
    "register",
    "mutation Register($registerUserInput: RegisterUserInput!) { register(registerUserInput: $registerUserInput) { "
    + _AUTH_RESPONSE_FIELDS
    + " } }",
)
REQUEST_PASSWORD_RESET = inline_operation(
    "RequestPasswordReset",
    "mutation",
    "requestPasswordReset",
    "mutation RequestPasswordReset($email: String!) { requestPasswordReset(email: $email) }",
)
RESET_PASSWORD = inline_operation(
    "ResetPassword",
    "mutation",
    "resetPassword",
    "mutation ResetPassword($resetPasswordInput: ResetPasswordInput!) { resetPassword(resetPasswordInput: $resetPasswordInput) }",
)
CHANGE_PASSWORD = inline_operation(
    "ChangePassword",
    "mutation",
    "changePassword",
    "mutation ChangePassword($currentPassword: String!, $newPassword: String!) { "
    "changePassword(currentPassword: $currentPassword, newPassword: $newPassword) }",
)
SET_INITIAL_PASSWORD = inline_operation(
    "SetInitialPassword",
    "mutation",
    "setInitialPassword",
    "mutation SetInitialPassword($newPassword: String!) { setInitialPassword(newPassword: $newPassword) }",
)
CHECK_AUTH_METHOD = inline_operation(
    "CheckAuthMethod",
    "query",
    "checkAuthMethod",
    "query CheckAuthMethod($input: CheckAuthMethodInput!) { checkAuthMethod(input: $input) { hasPassword } }",
)
AVAILABLE_LOGIN_PROVIDERS = inline_operation(
    "AvailableLoginProviders",
    "query",
    "availableLoginProviders",
    "query AvailableLoginProviders { availableLoginProviders }",
)
MY_IDENTITIES = inline_operation(
    "MyIdentities",
    "query",
    "myIdentities",
    "query MyIdentities { myIdentities { " + _IDENTITY_FIELDS + " } }",
)
LINK_IDENTITY = inline_operation(
    "LinkIdentity",
    "mutation",
    "linkIdentity",
    "mutation LinkIdentity($input: LinkIdentityInput!) { linkIdentity(input: $input) { "
    + _IDENTITY_FIELDS
    + " } }",
)
UNLINK_IDENTITY = inline_operation(
    "UnlinkIdentity",
    "mutation",
    "unlinkIdentity",
    "mutation UnlinkIdentity($identityId: String!) { unlinkIdentity(identityId: $identityId) }",
)

#: Documents this module declares inline (CrowdyJS declares the same ones inline).
INLINE_OPERATIONS = (
    REQUEST_LOGIN_LINK,
    COMPLETE_LOGIN_LINK,
    SOCIAL_LOGIN_START,
    SOCIAL_LOGIN_COMPLETE,
    LOGIN,
    REGISTER,
    REQUEST_PASSWORD_RESET,
    RESET_PASSWORD,
    CHANGE_PASSWORD,
    SET_INITIAL_PASSWORD,
    CHECK_AUTH_METHOD,
    AVAILABLE_LOGIN_PROVIDERS,
    MY_IDENTITIES,
    LINK_IDENTITY,
    UNLINK_IDENTITY,
)


class AuthAPI(Domain):
    """Sign-in and account security on the identity client."""

    def __init__(self, graphql: AsyncGraphQLClient, session: AuthState) -> None:
        super().__init__(graphql)
        self._session = session

    def _signed_in(self, payload: Any) -> AuthResponse:
        response = msgspec.convert(payload, AuthResponse)
        if response.token:
            self._session.set_token(response.token)
        return response

    async def available_login_providers(self) -> list[str]:
        result: list[str] = await self._request(AVAILABLE_LOGIN_PROVIDERS)
        return result

    async def request_login_link(
        self, email: str, redirect_uri: str | None = None
    ) -> dict[str, Any]:
        """Email a one-time sign-in link; complete it with :meth:`complete_login_link`."""
        input_ = {"email": email, **({"redirectUri": redirect_uri} if redirect_uri else {})}
        result: dict[str, Any] = await self._request(REQUEST_LOGIN_LINK, {"input": input_})
        return result

    async def complete_login_link(self, token: str) -> AuthResponse:
        return self._signed_in(
            await self._request(COMPLETE_LOGIN_LINK, {"input": {"token": token}})
        )

    async def social_login_start(self, provider: str, redirect_uri: str) -> dict[str, Any]:
        """Start an OIDC sign-in: returns ``authorizeUrl`` to open and the ``state`` to keep."""
        result: dict[str, Any] = await self._request(
            SOCIAL_LOGIN_START, {"input": {"provider": provider, "redirectUri": redirect_uri}}
        )
        return result

    async def social_login_complete(self, provider: str, code: str, state: str) -> AuthResponse:
        payload = await self._request(
            SOCIAL_LOGIN_COMPLETE, {"input": {"provider": provider, "code": code, "state": state}}
        )
        return self._signed_in(payload)

    async def login(self, email: str, password: str) -> AuthResponse:
        """Sign in with email and password; the client then holds the session token."""
        payload = await self._request(
            LOGIN, {"loginUserInput": {"email": email, "password": password}}
        )
        return self._signed_in(payload)

    async def register(
        self, email: str, password: str, gamertag: str | None = None
    ) -> AuthResponse:
        """Create an account and sign in to it."""
        input_: dict[str, Any] = {"email": email, "password": password}
        if gamertag is not None:
            input_["gamertag"] = gamertag
        return self._signed_in(await self._request(REGISTER, {"registerUserInput": input_}))

    async def check_auth_method(self, email: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(CHECK_AUTH_METHOD, {"input": {"email": email}})
        return result

    async def request_password_reset(self, email: str) -> bool:
        """Email a reset token. Answers ``True`` whether or not the account exists."""
        return bool(await self._request(REQUEST_PASSWORD_RESET, {"email": email}))

    async def reset_password(self, token: str, new_password: str) -> bool:
        """Set a new password with the emailed token."""
        return bool(
            await self._request(
                RESET_PASSWORD,
                {"resetPasswordInput": {"token": token, "newPassword": new_password}},
            )
        )

    async def change_password(self, current_password: str, new_password: str) -> bool:
        """Change the password, proving the current one."""
        return bool(
            await self._request(
                CHANGE_PASSWORD,
                {"currentPassword": current_password, "newPassword": new_password},
            )
        )

    async def set_initial_password(self, new_password: str) -> bool:
        """Add a password to an account created by magic link or a social provider.

        Refused (``PASSWORD_ALREADY_SET``) on an account that already has one: holding a
        session is not proof of the current password.
        """
        return bool(await self._request(SET_INITIAL_PASSWORD, {"newPassword": new_password}))

    async def my_identities(self) -> list[UserIdentity]:
        return msgspec.convert(await self._request(MY_IDENTITIES), list[UserIdentity])

    async def link_identity(self, provider: str, code: str, state: str) -> UserIdentity:
        payload = await self._request(
            LINK_IDENTITY, {"input": {"provider": provider, "code": code, "state": state}}
        )
        return msgspec.convert(payload, UserIdentity)

    async def unlink_identity(self, identity_id: str) -> bool:
        return bool(await self._request(UNLINK_IDENTITY, {"identityId": identity_id}))

    async def logout(self) -> bool:
        """Revoke this session server-side and forget the token locally."""
        result = await self._request(ops.LOGOUT)
        self._session.set_token(None)
        return bool(result)

    async def logout_all_devices(self) -> bool:
        return bool(await self._request(ops.LOGOUT_ALL_DEVICES))

    def set_token(self, token: str | None) -> None:
        self._session.set_token(token)

    def get_token(self) -> str | None:
        return self._session.get_token()
