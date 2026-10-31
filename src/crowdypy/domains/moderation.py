"""An app's safety reports, for its moderators (``client.moderation``, and
``client.admin.moderation``).

Players file reports with ``client.safety.file_report``. Each one links both players' recorded
sessions around it, and the platform preserves those with both identities while the report is
open. A moderator reads the reports, reads the sessions a report covers, and decides it:
:meth:`ModerationAPI.resolve_report` closes it and deletes the preserved copy;
:meth:`ModerationAPI.escalate_report` records a CyberTipline submission and keeps the copy for a
year.

**Who.** Every call needs ``moderate_safety`` on the app: an organization role that grants it,
or an admin of the app's organization. Send the moderator's identity session (not an app token)
to the app's datacenter (``apps.route_for``, or ``discovery``).

**Reading a reported session.** A report's ``sessions`` names the recorded sessions
(``InputLogSession.gameTokenId``) it covers. A moderator mints an app token for the app
(``portal.mint_app_token``) and reads them with ``client.input_log.messages(app_id,
game_token_id)`` on the app's client while the report is open or escalated; every read is
audited.
"""

from __future__ import annotations

from typing import Any, Literal

from crowdypy._generated import operations as ops
from crowdypy.domains._base import Domain, omit_none
from crowdypy.utils import bigint

__all__ = ["ModerationAPI", "SafetyReportStatusValue"]

#: Where a safety report stands: waiting, closed with an outcome, or sent to the CyberTipline.
SafetyReportStatusValue = Literal["OPEN", "RESOLVED", "ESCALATED"]


class ModerationAPI(Domain):
    """Read, resolve and escalate an app's safety reports (``moderate_safety``)."""

    async def reports(
        self,
        app_id: str | int,
        *,
        status: SafetyReportStatusValue | None = None,
        first: int | None = None,
    ) -> list[dict[str, Any]]:
        """The app's reports, newest first, with open ``CHILD_SAFETY`` reports ahead of the
        rest. ``status`` keeps one kind; ``first`` defaults to 50 (1 to 200)."""
        result: list[dict[str, Any]] = await self._request(
            ops.SAFETY_REPORTS,
            {"appId": bigint(app_id), **omit_none({"status": status, "first": first})},
        )
        return result

    async def resolve_report(
        self, app_id: str | int, report_id: str | int, outcome: str
    ) -> dict[str, Any]:
        """Resolve an open report with its outcome (at most 1,000 characters): its preserved
        copy is deleted, and a legal hold its reported account got on deletion is released.
        Resolving it again changes nothing; an escalated report cannot be resolved
        (``BAD_REQUEST``)."""
        result: dict[str, Any] = await self._request(
            ops.RESOLVE_SAFETY_REPORT,
            {"appId": bigint(app_id), "reportId": bigint(report_id), "outcome": outcome},
        )
        return result

    async def escalate_report(
        self, app_id: str | int, report_id: str | int, ncmec_report_id: str
    ) -> dict[str, Any]:
        """Record that an open report was submitted to NCMEC's CyberTipline, under the id NCMEC
        gave the submission: its preserved copy is kept under retention for a year from now,
        and the reported account's legal hold, if any, as long. Again under the same id changes
        nothing; under another id is ``CONFLICT``; a resolved report cannot be escalated
        (``BAD_REQUEST``)."""
        result: dict[str, Any] = await self._request(
            ops.ESCALATE_SAFETY_REPORT,
            {
                "appId": bigint(app_id),
                "reportId": bigint(report_id),
                "ncmecReportId": ncmec_report_id,
            },
        )
        return result
