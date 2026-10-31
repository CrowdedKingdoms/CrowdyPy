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
from typing import Any, Literal

import msgspec

from crowdypy._generated import operations as ops
from crowdypy._operation import inline_operation
from crowdypy.auth_state import AuthState
from crowdypy.domains._base import Domain, omit_none
from crowdypy.graphql import AsyncGraphQLClient
from crowdypy.utils import bigint

__all__ = [
    "AuthAPI",
    "AuthResponse",
    "AuthUser",
    "LegalAcceptanceMethodValue",
    "PlayerAge",
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


#: How a player accepted documents: on a Crowded Kingdoms or SDK form, or on the app's own
#: screen.
LegalAcceptanceMethodValue = Literal["CLICKWRAP", "IN_GAME"]


class PlayerAge(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """The account after :meth:`AuthAPI.record_player_age`."""

    user_id: str
    #: ``unknown`` until an age is given, then ``under_13``, ``13_15``, ``16_17`` or
    #: ``18_plus`` (an open set). For your logic; never show it to a player as an age.
    age_bracket: str | None = None
    #: The declared country (ISO 3166-1 alpha-2).
    country: str | None = None


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
# An API before v2.60.0 declares the attestation Boolean!, so this document stays as it was
# and is sent whenever the call is the old one (both flags, no app, no method).
RECORD_PLAYER_CONSENTS = inline_operation(
    "RecordPlayerConsents",
    "mutation",
    "recordPlayerConsents",
    "mutation RecordPlayerConsents($acceptLegal: Boolean!, $attestAgeOfMajority: Boolean!) { "
    "recordPlayerConsents(acceptLegal: $acceptLegal, attestAgeOfMajority: $attestAgeOfMajority) }",
)
RECORD_PLAYER_CONSENTS_FOR_APP = inline_operation(
    "RecordPlayerConsentsForApp",
    "mutation",
    "recordPlayerConsents",
    "mutation RecordPlayerConsentsForApp($acceptLegal: Boolean!, $attestAgeOfMajority: Boolean, "
    "$appId: BigInt, $method: LegalAcceptanceMethod) { recordPlayerConsents(acceptLegal: "
    "$acceptLegal, attestAgeOfMajority: $attestAgeOfMajority, appId: $appId, method: $method) }",
)
PLAYER_LEGAL_ACCEPTANCE = inline_operation(
    "PlayerLegalAcceptance",
    "query",
    "playerLegalAcceptance",
    "query PlayerLegalAcceptance { playerLegalAcceptance }",
)
PLAYER_LEGAL_ACCEPTANCE_FOR_APP = inline_operation(
    "PlayerLegalAcceptanceForApp",
    "query",
    "playerLegalAcceptance",
    "query PlayerLegalAcceptanceForApp($appId: BigInt!) { playerLegalAcceptance(appId: $appId) }",
)
RECORD_PLAYER_AGE = inline_operation(
    "RecordPlayerAge",
    "mutation",
    "recordPlayerAge",
    "mutation RecordPlayerAge($input: RecordPlayerAgeInput!) { recordPlayerAge(input: $input) "
    "{ userId ageBracket country } }",
)
_LEGAL_DOCUMENT_FIELDS = "documentId version title url appId"
PLAYER_LEGAL_DOCUMENTS = inline_operation(
    "PlayerLegalDocuments",
    "query",
    "playerLegalDocuments",
    "query PlayerLegalDocuments { playerLegalDocuments { " + _LEGAL_DOCUMENT_FIELDS + " } }",
)
APP_REQUIRED_DOCUMENTS = inline_operation(
    "AppRequiredDocuments",
    "query",
    "appRequiredDocuments",
    "query AppRequiredDocuments($appId: BigInt!) { appRequiredDocuments(appId: $appId) { "
    + _LEGAL_DOCUMENT_FIELDS
    + " } }",
)
DEVELOPER_LEGAL_DOCUMENTS = inline_operation(
    "DeveloperLegalDocuments",
    "query",
    "developerLegalDocuments",
    "query DeveloperLegalDocuments { developerLegalDocuments { " + _LEGAL_DOCUMENT_FIELDS + " } }",
)
DEVELOPER_LEGAL_ACCEPTANCE = inline_operation(
    "DeveloperLegalAcceptance",
    "query",
    "developerLegalAcceptance",
    "query DeveloperLegalAcceptance { developerLegalAcceptance }",
)
RECORD_DEVELOPER_CONSENTS = inline_operation(
    "RecordDeveloperConsents",
    "mutation",
    "recordDeveloperConsents",
    "mutation RecordDeveloperConsents($acceptDeveloperTerms: Boolean!) { "
    "recordDeveloperConsents(acceptDeveloperTerms: $acceptDeveloperTerms) }",
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

CONFIRM_EMAIL = inline_operation(
    "ConfirmEmail",
    "mutation",
    "confirmEmail",
    "mutation ConfirmEmail($token: String!) { confirmEmail(token: $token) }",
)
RESEND_CONFIRMATION_EMAIL = inline_operation(
    "ResendConfirmationEmail",
    "mutation",
    "resendConfirmationEmail",
    "mutation ResendConfirmationEmail($email: String!) { resendConfirmationEmail(email: $email) }",
)

#: Documents this module declares inline: the ones CrowdyJS declares inline, and the email
#: confirmation pair CrowdyCPP's AuthAPI sends.
INLINE_OPERATIONS = (
    REQUEST_LOGIN_LINK,
    COMPLETE_LOGIN_LINK,
    SOCIAL_LOGIN_START,
    SOCIAL_LOGIN_COMPLETE,
    LOGIN,
    REGISTER,
    RECORD_PLAYER_CONSENTS,
    RECORD_PLAYER_CONSENTS_FOR_APP,
    PLAYER_LEGAL_ACCEPTANCE,
    PLAYER_LEGAL_ACCEPTANCE_FOR_APP,
    RECORD_PLAYER_AGE,
    PLAYER_LEGAL_DOCUMENTS,
    APP_REQUIRED_DOCUMENTS,
    DEVELOPER_LEGAL_DOCUMENTS,
    DEVELOPER_LEGAL_ACCEPTANCE,
    RECORD_DEVELOPER_CONSENTS,
    REQUEST_PASSWORD_RESET,
    RESET_PASSWORD,
    CHANGE_PASSWORD,
    SET_INITIAL_PASSWORD,
    CHECK_AUTH_METHOD,
    AVAILABLE_LOGIN_PROVIDERS,
    MY_IDENTITIES,
    LINK_IDENTITY,
    UNLINK_IDENTITY,
    CONFIRM_EMAIL,
    RESEND_CONFIRMATION_EMAIL,
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
        self,
        email: str,
        password: str,
        gamertag: str | None = None,
        *,
        accept_legal: bool | None = None,
        attest_age_of_majority: bool | None = None,
        birth_date: str | None = None,
        country: str | None = None,
        parental_confirmation: bool | None = None,
    ) -> AuthResponse:
        """Create an account and sign in to it.

        ``accept_legal``: the player agreed to the current required legal documents.
        ``attest_age_of_majority``: the player is at least 18, or the age of majority where
        they live if higher. Send both ``True`` once the player has ticked both boxes and
        the account starts accepted; omit them and call :meth:`record_player_consents`
        before the first gameplay token.

        **The age (ck-api v2.60.0).** ``birth_date`` (``YYYY-MM-DD``, or ``YYYY-MM``; only the
        year and month are kept), ``country`` (ISO 3166-1 alpha-2) and, for a player under
        18, ``parental_confirmation`` are the answers of a neutral age screen: it asks the
        date of birth and the country and shows no age, and only a player under 18 sees the
        line that a parent or guardian agreed. A client with no browser origin may leave them
        out and give them later with :meth:`record_player_age`. A country Crowded Kingdoms
        does not serve, declared or detected, is refused ``TERRITORY_NOT_SUPPORTED``
        (:func:`crowdypy.errors.territory_not_supported_of`), and an age the platform does not
        admit ``AGE_REQUIREMENT_NOT_MET`` (:func:`crowdypy.errors.age_requirement_not_met_of`);
        either way no account is made.
        """
        input_: dict[str, Any] = {"email": email, "password": password}
        if gamertag is not None:
            input_["gamertag"] = gamertag
        input_ |= omit_none(
            {
                "acceptLegal": accept_legal,
                "attestAgeOfMajority": attest_age_of_majority,
                "birthDate": birth_date,
                "country": country,
                "parentalConfirmation": parental_confirmation,
            }
        )
        return self._signed_in(await self._request(REGISTER, {"registerUserInput": input_}))

    async def record_player_consents(
        self,
        *,
        accept_legal: bool,
        attest_age_of_majority: bool | None = None,
        app_id: str | int | None = None,
        method: LegalAcceptanceMethodValue | None = None,
    ) -> bool:
        """Store the signed-in player's agreement to the platform's current player documents
        (:meth:`player_legal_documents`) and, with ``app_id``, to that app's own
        (:meth:`app_required_documents`). Requires the identity session token;
        ``accept_legal`` must be ``True``, and repeating it is harmless.

        ``attest_age_of_majority`` (the player is at least 18, or the age of majority where
        they live if higher) must be ``True`` while the platform still uses the attestation;
        once players give a birth date instead (:meth:`record_player_age`) the server ignores
        it, and it may be left out. ``method`` records how the player accepted: ``CLICKWRAP``
        (the default; a Crowded Kingdoms or SDK form) or ``IN_GAME`` (the app's own screen).

        A gameplay token (``portal.mint_app_token``, ``portal.create_authorization_code``,
        ``portal.refresh``) is refused with ``LEGAL_ACCEPTANCE_REQUIRED``
        (:func:`crowdypy.errors.legal_acceptance_required_of` lists the missing documents)
        until this is stored. It records the PLAYER's agreement: call it only after they
        ticked the box in your own UI, which links each document.

        ``app_id``, ``method`` and leaving the attestation out need ck-api v2.60.0; the call
        with both flags and neither of those works against any API since v2.35.0.
        """
        if attest_age_of_majority is not None and app_id is None and method is None:
            return bool(
                await self._request(
                    RECORD_PLAYER_CONSENTS,
                    {"acceptLegal": accept_legal, "attestAgeOfMajority": attest_age_of_majority},
                )
            )
        return bool(
            await self._request(
                RECORD_PLAYER_CONSENTS_FOR_APP,
                {
                    "acceptLegal": accept_legal,
                    **omit_none(
                        {
                            "attestAgeOfMajority": attest_age_of_majority,
                            "appId": None if app_id is None else bigint(app_id),
                            "method": method,
                        }
                    ),
                },
            )
        )

    async def player_legal_acceptance(self, *, app_id: str | int | None = None) -> bool:
        """Whether the signed-in player has stored the platform's current player documents
        and, while the platform asks for one, the age attestation or a birth date: whether a
        gameplay token would be issued. With ``app_id`` the app's own documents count too
        (ck-api v2.60.0). Requires the identity session token."""
        if app_id is not None:
            return bool(
                await self._request(PLAYER_LEGAL_ACCEPTANCE_FOR_APP, {"appId": bigint(app_id)})
            )
        return bool(await self._request(PLAYER_LEGAL_ACCEPTANCE))

    async def player_legal_documents(self) -> list[dict[str, Any]]:
        """The platform's documents every player accepts before playing, at the versions
        required now: show them (with the app's own, :meth:`app_required_documents`) before
        :meth:`record_player_consents`. Each has ``documentId``, ``version``, ``title`` and
        ``url``. Public. ck-api v2.60.0."""
        result: list[dict[str, Any]] = await self._request(PLAYER_LEGAL_DOCUMENTS)
        return result

    async def app_required_documents(self, app_id: str | int) -> list[dict[str, Any]]:
        """An app's own documents (its rules, an addendum), which its players accept on top of
        the platform's: record them with :meth:`record_player_consents` and the ``app_id``
        before the player's next mint. Empty when the app requires none. Its admins set them
        with ``apps.set_required_documents``. Public. ck-api v2.60.0."""
        result: list[dict[str, Any]] = await self._request(
            APP_REQUIRED_DOCUMENTS, {"appId": bigint(app_id)}
        )
        return result

    async def record_player_age(
        self, *, birth_date: str, country: str, parental_confirmation: bool | None = None
    ) -> PlayerAge:
        """Record the signed-in player's birth date and country, and under 18 that a parent or
        guardian agreed, for an account whose age is not known yet (``users.me()``'s
        ``ageBracket`` is ``unknown``, and a mint answers ``AGE_REQUIRED``). ``birth_date`` is
        ``YYYY-MM-DD`` (``YYYY-MM`` is accepted too); only the year and month are kept, and the
        age is counted from the last day of that month. Requires the identity session token.
        ck-api v2.60.0.

        Ask on a neutral screen: the date of birth and the country, with no age shown, and the
        parental line only for a player under 18. Refused with ``CONFLICT`` once the account
        has an age, ``TERRITORY_NOT_SUPPORTED`` for a country Crowded Kingdoms does not serve
        (declared or detected), and ``AGE_REQUIREMENT_NOT_MET`` under the platform's youngest
        age, in which case the account keeps that answer and asking again changes nothing.
        Whether the player may play a given app is decided at the mint. Returns the account's
        ``age_bracket`` and ``country``.
        """
        payload = await self._request(
            RECORD_PLAYER_AGE,
            {
                "input": {
                    "birthDate": birth_date,
                    "country": country,
                    **omit_none({"parentalConfirmation": parental_confirmation}),
                }
            },
        )
        return msgspec.convert(payload, PlayerAge)

    async def developer_legal_documents(self) -> list[dict[str, Any]]:
        """The documents a developer accepts before creating or administering an
        organization, at the versions required now. Empty while the platform asks developers
        for nothing beyond the player documents. Public. ck-api v2.60.0."""
        result: list[dict[str, Any]] = await self._request(DEVELOPER_LEGAL_DOCUMENTS)
        return result

    async def developer_legal_acceptance(self) -> bool:
        """Whether the signed-in account has accepted the current developer documents.
        ``False`` means creating or administering an organization is refused with
        ``LEGAL_ACCEPTANCE_REQUIRED`` until :meth:`record_developer_consents`. Requires the
        identity session token. ck-api v2.60.0."""
        return bool(await self._request(DEVELOPER_LEGAL_ACCEPTANCE))

    async def record_developer_consents(self, *, accept_developer_terms: bool) -> bool:
        """Store the signed-in account's agreement to the current developer documents
        (:meth:`developer_legal_documents`); ``accept_developer_terms`` must be ``True``, and
        repeating it is harmless. It records a person's agreement: call it only after they
        ticked the box in your own UI, linking each document. The developer documents are for
        adults: an account with no age is refused ``AGE_REQUIRED`` (:meth:`record_player_age`
        first) and one under 18 ``AGE_REQUIREMENT_NOT_MET``. Requires the identity session
        token. ck-api v2.60.0."""
        return bool(
            await self._request(
                RECORD_DEVELOPER_CONSENTS, {"acceptDeveloperTerms": accept_developer_terms}
            )
        )

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

    async def confirm_email(self, token: str) -> bool:
        """Confirm an email address with the emailed token, which also enables password
        sign-in. ``False`` for an invalid or expired token."""
        return bool(await self._request(CONFIRM_EMAIL, {"token": token}))

    async def resend_confirmation_email(self, email: str) -> bool:
        """Send the confirmation link again. Answers ``True`` whether or not the account
        exists or is already confirmed."""
        return bool(await self._request(RESEND_CONFIRMATION_EMAIL, {"email": email}))

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
