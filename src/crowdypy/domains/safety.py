"""A player's own safety and communication controls in one app (``client.safety``).

**Game plane.** Every call takes the app-scoped token for ``app_id`` on the client built for
the app (``portal.mint_app_token``, ``portal.exchange_code``, or a managed player's token), as
gameplay does; an identity session token is refused.

What it controls, all enforced by the platform rather than by your client:

- **Voice and video scopes** (:meth:`SafetyAPI.comm_settings`,
  :meth:`SafetyAPI.set_comm_settings`): for each of voice and video, whom the player sends to
  and hears from: ``OFF``, ``CONTACTS`` or ``EVERYONE``. The realtime servers enforce the
  EFFECTIVE scopes on both sides. A player can only ever lower them below what policy allows:
  an adult may choose anything (``EVERYONE`` by default); a player under 18 has each medium
  ``OFF`` until a parent consents (:meth:`SafetyAPI.request_parental_consent`) and then at
  most ``CONTACTS``.
- **Contacts** (:meth:`SafetyAPI.contacts`, :meth:`SafetyAPI.contact_requests`,
  :meth:`SafetyAPI.request_contact`, :meth:`SafetyAPI.accept_contact`,
  :meth:`SafetyAPI.remove_contact`): a mutual list per app. It is what a ``CONTACTS`` scope
  admits.
- **Blocks** (:meth:`SafetyAPI.blocks`, :meth:`SafetyAPI.block`, :meth:`SafetyAPI.unblock`):
  nothing of any kind (voice, video, text, direct messages) passes between two players while
  either has blocked the other, both ways, and the blocked player is not told.
- **Reports** (:meth:`SafetyAPI.file_report`): the app's moderators read them
  (``client.moderation``).

Every change that affects what reaches the player (scopes, contacts, blocks, a parent's
consent) reaches its live sessions within a second. Ids are ``BigInt`` decimal strings.
``SOCIAL_LIMIT_REACHED`` (:func:`crowdypy.errors.social_limit_reached_of`) refuses a request
past the limits (100 waiting requests, 1,000 contacts and 1,000 blocks per app); ``NOT_FOUND``
names a player the app does not know.

Show the player the notice and the recording indicator the app's safety log needs
(``apps.recording(app_id)``): what a player says and sends may be recorded for moderators.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from crowdypy._generated import operations as ops
from crowdypy.domains._base import Domain, omit_none
from crowdypy.utils import bigint

__all__ = ["CommScopeValue", "ParentalConsentFeatureValue", "SafetyAPI", "SafetyReportReasonValue"]

#: Who a medium reaches (send) or is heard from (receive): ``OFF`` nobody, ``CONTACTS`` only
#: the player's accepted contacts in the app, ``EVERYONE`` anyone the app's own rules allow.
CommScopeValue = Literal["OFF", "CONTACTS", "EVERYONE"]
#: What a parent consents to for a player under 18: voice (with channel audio) or webcam video.
ParentalConsentFeatureValue = Literal["VOICE", "VIDEO"]
#: Why a player is reported. ``CHILD_SAFETY`` reports come first in the moderation queue.
SafetyReportReasonValue = Literal[
    "CHILD_SAFETY", "HARASSMENT", "SEXUAL_CONTENT", "HATE", "SELF_HARM", "CHEATING", "OTHER"
]


class SafetyAPI(Domain):
    """A player's own voice and video scopes, contacts, blocks, a parent's consent, and
    reports to the app's moderators."""

    async def comm_settings(self, app_id: str | int) -> dict[str, Any]:
        """The player's voice and video settings in the app: the effective scopes
        (``voiceSend``, ``voiceReceive``, ``videoSend``, ``videoReceive``), what the player
        picked (``choices``, ``None`` where it kept the default), what policy allows now
        (``voiceAllowed``, ``videoAllowed``), whether a parent consented (``voiceConsent``,
        ``videoConsent``) and ``isMinor``."""
        result: dict[str, Any] = await self._request(
            ops.MY_COMM_SETTINGS, {"appId": bigint(app_id)}
        )
        return result

    async def set_comm_settings(
        self,
        app_id: str | int,
        *,
        voice_send: CommScopeValue | None = None,
        voice_receive: CommScopeValue | None = None,
        video_send: CommScopeValue | None = None,
        video_receive: CommScopeValue | None = None,
    ) -> dict[str, Any]:
        """Change the player's choices; a scope left at ``None`` keeps its choice. A choice
        above what policy allows is kept and applies only up to the cap, so the effective scope
        can only go down. Returns the settings after the change."""
        choices = omit_none(
            {
                "voiceSend": voice_send,
                "voiceReceive": voice_receive,
                "videoSend": video_send,
                "videoReceive": video_receive,
            }
        )
        result: dict[str, Any] = await self._request(
            ops.SET_MY_COMM_SETTINGS, {"appId": bigint(app_id), "input": choices}
        )
        return result

    async def request_parental_consent(
        self, app_id: str | int, feature: ParentalConsentFeatureValue, parent_email: str
    ) -> dict[str, Any]:
        """For a player under 18: ask a parent, through the platform's consent service, to
        allow one medium in this app. The service emails ``parent_email``; once the parent
        consents the medium opens, up to ``CONTACTS``. Crowded Kingdoms does not store the
        address.

        ``status`` is ``REQUESTED`` (the email went out), ``ALREADY_GRANTED`` or
        ``NOT_REQUIRED`` (the player is 18 or over; nothing was sent). Refused with
        ``PARENTAL_CONSENT_UNAVAILABLE``
        (:func:`crowdypy.errors.parental_consent_unavailable_of`) while the service is not
        configured on the deployment, and ``RATE_LIMITED`` after three requests in an hour."""
        result: dict[str, Any] = await self._request(
            ops.REQUEST_PARENTAL_CONSENT,
            {"appId": bigint(app_id), "feature": feature, "parentEmail": parent_email},
        )
        return result

    async def contacts(self, app_id: str | int) -> list[dict[str, Any]]:
        """The player's accepted contacts in the app, most recent first (at most 1,000)."""
        result: list[dict[str, Any]] = await self._request(
            ops.MY_CONTACTS, {"appId": bigint(app_id)}
        )
        return result

    async def contact_requests(self, app_id: str | int) -> list[dict[str, Any]]:
        """Contact requests waiting in the app: ``INCOMING`` ones other players sent (answer
        with :meth:`accept_contact` or :meth:`remove_contact`), then ``OUTGOING`` ones the
        player sent (:meth:`remove_contact` cancels one), newest first."""
        result: list[dict[str, Any]] = await self._request(
            ops.CONTACT_REQUESTS, {"appId": bigint(app_id)}
        )
        return result

    async def request_contact(self, app_id: str | int, user_id: str | int) -> dict[str, Any]:
        """Ask another player of the app to be a contact. ``state`` is ``PENDING``, or
        ``ACCEPTED`` when they had already asked the player; asking again changes nothing.
        Refused while the player has blocked them (:meth:`unblock` first)."""
        result: dict[str, Any] = await self._request(
            ops.REQUEST_CONTACT, {"appId": bigint(app_id), "userId": bigint(user_id)}
        )
        return result

    async def accept_contact(self, app_id: str | int, user_id: str | int) -> dict[str, Any]:
        """Accept another player's request: both become each other's contacts. ``NOT_FOUND``
        when they have not asked."""
        result: dict[str, Any] = await self._request(
            ops.ACCEPT_CONTACT, {"appId": bigint(app_id), "userId": bigint(user_id)}
        )
        return result

    async def remove_contact(self, app_id: str | int, user_id: str | int) -> bool:
        """Remove a contact, decline the other player's request, or cancel the player's own:
        everything between the two in the app goes, both ways. ``False`` when there was
        nothing."""
        return bool(
            await self._request(
                ops.REMOVE_CONTACT, {"appId": bigint(app_id), "userId": bigint(user_id)}
            )
        )

    async def blocks(self, app_id: str | int) -> list[dict[str, Any]]:
        """The players the player blocked in the app, most recent first (at most 1,000)."""
        result: list[dict[str, Any]] = await self._request(ops.MY_BLOCKS, {"appId": bigint(app_id)})
        return result

    async def block(self, app_id: str | int, user_id: str | int) -> dict[str, Any]:
        """Block another player of the app: nothing passes between the two, either way, on
        every session of both within a second, and the app's hubs can ask
        (``players.blocked``). Any contact or request between them goes too. The blocked player
        is not told, and its contact requests to the player are dropped. Blocking again changes
        nothing."""
        result: dict[str, Any] = await self._request(
            ops.BLOCK_PLAYER, {"appId": bigint(app_id), "userId": bigint(user_id)}
        )
        return result

    async def unblock(self, app_id: str | int, user_id: str | int) -> bool:
        """Lift a block the player set. A contact the block removed does not come back.
        ``False`` when there was no such block."""
        return bool(
            await self._request(
                ops.UNBLOCK_PLAYER, {"appId": bigint(app_id), "userId": bigint(user_id)}
            )
        )

    async def file_report(
        self,
        app_id: str | int,
        reported_user_id: str | int,
        reason: SafetyReportReasonValue,
        occurred_at: str | datetime,
        *,
        reported_actor_uuid: str | None = None,
        details: str | None = None,
    ) -> dict[str, Any]:
        """Report another player of the app to its moderators. The report covers both players'
        recorded sessions (the app's safety log) from 15 minutes before ``occurred_at`` to now,
        and the platform preserves them with both identities for the moderators. The reported
        player is not told. Returns the receipt (``reportId``, ``filedAt``).

        ``occurred_at`` is when it happened, within the past year (an ISO-8601 string, or an
        aware :class:`~datetime.datetime`). ``reported_actor_uuid`` is the reported player's
        actor when the client knows it: the 32 hex digits the realtime wire uses (as
        :func:`crowdypy.generate_crowdy_uuid` makes them). ``details`` is what happened, in the
        reporter's words: at most 1,000 characters.

        Refused with ``NOT_FOUND`` for a player the app does not know, ``BAD_USER_INPUT`` for
        the player itself, ``details`` over 1,000 characters, an ``occurred_at`` outside the
        past year or an actor id that is not 32 hex digits, and ``RATE_LIMITED`` after 20
        reports in an hour."""
        if isinstance(occurred_at, datetime) and occurred_at.tzinfo is None:
            raise ValueError(
                "occurred_at must be a timezone-aware datetime (or an ISO-8601 string)"
            )
        report = {
            "appId": bigint(app_id),
            "reportedUserId": bigint(reported_user_id),
            "reason": reason,
            "occurredAt": (
                occurred_at.astimezone(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
                if isinstance(occurred_at, datetime)
                else occurred_at
            ),
            **omit_none({"reportedActorUuid": reported_actor_uuid, "details": details}),
        }
        result: dict[str, Any] = await self._request(ops.FILE_SAFETY_REPORT, {"input": report})
        return result
