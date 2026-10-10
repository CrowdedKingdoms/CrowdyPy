"""The app catalog, player-code admission and game-api routing (``client.apps``).

An app may be served by its own game-api endpoint. The catalog carries each app's
``gameApiUrl`` so a per-app client can be pointed at it: :meth:`AppsAPI.route_for`
reduces an app to just that decision.

:meth:`AppsAPI.app_by_slug` and the marketplace listings are public. Everything else
needs an identity session, and the methods that change an app name the permission they
need. ``BigInt`` ids take an ``int`` or a decimal string.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import msgspec

from crowdypy._generated import inputs
from crowdypy._generated import operations as ops
from crowdypy._generated.enums import CodeAdmissionMode
from crowdypy._operation import inline_operation
from crowdypy.domains._base import Domain, omit_none
from crowdypy.utils import bigint

__all__ = ["AppRoute", "AppsAPI"]


class AppRoute(msgspec.Struct, rename="camel", frozen=True, kw_only=True):
    """Which game-api endpoint should serve an app (:meth:`AppsAPI.route_for`)."""

    #: The app id (decimal string).
    app_id: str
    #: The app's runtime data lives in its own game-api database, not the shared one.
    split_mode: bool = False
    #: Where the app runs (not deployed, the shared game-api, or a dedicated environment).
    deployment_target: str | None = None
    #: The game-api URL to route gameplay to; ``None`` means keep the client's own endpoint.
    game_api_url: str | None = None


def _route_from_app_row(row: object) -> AppRoute | None:
    if not isinstance(row, Mapping):
        return None
    app_id = row.get("appId")
    if not isinstance(app_id, str):
        return None
    split_mode = row.get("splitMode")
    deployment_target = row.get("deploymentTarget")
    game_api_url = row.get("gameApiUrl")
    return AppRoute(
        app_id=app_id,
        split_mode=split_mode if isinstance(split_mode, bool) else False,
        deployment_target=deployment_target if isinstance(deployment_target, str) else None,
        game_api_url=game_api_url if isinstance(game_api_url, str) and game_api_url else None,
    )


_BUDGET_FIELDS = "appId unitsPerMinute enforce note updatedAt"
# CrowdyCPP carries these documents; CrowdyJS sends none of them.
APP_COMPUTE_BUDGET = inline_operation(
    "AppComputeBudget",
    "query",
    "appComputeBudget",
    f"query AppComputeBudget($appId: BigInt!) {{ appComputeBudget(appId: $appId) {{ {_BUDGET_FIELDS} }} }}",
)
SET_APP_COMPUTE_BUDGET = inline_operation(
    "SetAppComputeBudget",
    "mutation",
    "setAppComputeBudget",
    "mutation SetAppComputeBudget($appId: BigInt!, $unitsPerMinute: Int!, $enforce: Boolean, "
    "$note: String) { setAppComputeBudget(appId: $appId, unitsPerMinute: $unitsPerMinute, "
    f"enforce: $enforce, note: $note) {{ {_BUDGET_FIELDS} }} }}",
)
CLEAR_APP_COMPUTE_BUDGET = inline_operation(
    "ClearAppComputeBudget",
    "mutation",
    "clearAppComputeBudget",
    "mutation ClearAppComputeBudget($appId: BigInt!) { clearAppComputeBudget(appId: $appId) }",
)
INLINE_OPERATIONS = (APP_COMPUTE_BUDGET, SET_APP_COMPUTE_BUDGET, CLEAR_APP_COMPUTE_BUDGET)


class AppsAPI(Domain):
    """App discovery, administration and routing."""

    async def code_admission_mode(self, app_id: str | int) -> str:
        """The app's player-code admission mode (``IMPLICIT_ALLOW`` or ``ALLOW_LIST``).

        Needs ``view_compute_diagnostics``.
        """
        result: str = await self._request(ops.APP_CODE_ADMISSION_MODE, {"appId": bigint(app_id)})
        return result

    async def code_admissions(
        self, app_id: str | int, include_revoked: bool = False
    ) -> list[dict[str, Any]]:
        """The app's code, author and org allow-list entries, newest first.

        Revoked audit rows are left out unless ``include_revoked``. Needs
        ``view_compute_diagnostics``.
        """
        result: list[dict[str, Any]] = await self._request(
            ops.APP_CODE_ADMISSIONS, {"appId": bigint(app_id), "includeRevoked": include_revoked}
        )
        return result

    async def set_code_admission_mode(
        self, app_id: str | int, mode: CodeAdmissionMode | str
    ) -> str:
        """Set the app's admission mode. Needs ``manage_compute``.

        Switching to ``ALLOW_LIST`` drains unadmitted code, self-authored code included, at
        activation; deploy and compile keep working.
        """
        result: str = await self._request(
            ops.SET_APP_CODE_ADMISSION_MODE, {"appId": bigint(app_id), "mode": mode}
        )
        return result

    async def admit_code(
        self, input: inputs.AdmitAppCodeInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Admit one code listing, author or org to the app's allow list. Needs ``manage_compute``.

        Admission controls execution only and never grants source visibility. An identical
        active entry is refused as a conflict rather than duplicated.
        """
        result: dict[str, Any] = await self._request(ops.ADMIT_APP_CODE, {"input": input})
        return result

    async def revoke_code_admission(self, app_id: str | int, admission_id: str) -> dict[str, Any]:
        """Revoke an active admission by its UUID. Needs ``manage_compute``.

        The change is audit-logged; affected server modules drain and the app's client
        artifacts are blocked.
        """
        result: dict[str, Any] = await self._request(
            ops.REVOKE_APP_CODE_ADMISSION, {"appId": bigint(app_id), "admissionId": admission_id}
        )
        return result

    async def app(self, app_id: str | int) -> dict[str, Any] | None:
        """One app by id, or ``None``.

        Any signed-in caller: no org or app permission is checked, so it reads apps the
        caller does not own, of any visibility or status. Prefer :meth:`app_by_slug` for
        marketplace lookups.
        """
        result: dict[str, Any] | None = await self._request(ops.APP, {"appId": bigint(app_id)})
        return result

    async def app_by_slug(self, org_slug: str, app_slug: str) -> dict[str, Any] | None:
        """One app by its marketplace path (``/<org_slug>/<app_slug>``), or ``None``.

        Public, and not filtered by visibility or status: exact slugs resolve unlisted and
        draft apps.
        """
        result: dict[str, Any] | None = await self._request(
            ops.APP_BY_SLUG, {"orgSlug": org_slug, "appSlug": app_slug}
        )
        return result

    async def my_apps(self) -> list[dict[str, Any]]:
        """The apps in the caller's account, newest first.

        Those owned by an org the caller is an active member of, or granted to the caller,
        of any visibility or status.
        """
        result: list[dict[str, Any]] = await self._request(ops.MY_APPS, {})
        return result

    async def route_for(self, app_id: str | int) -> AppRoute:
        """Which game-api endpoint should serve an app: :meth:`app` as an :class:`AppRoute`.

        Route gameplay to ``game_api_url`` when it is set, and otherwise keep the client's
        own endpoint. A missing app, or a row without the routing fields, gives that safe
        default. Needs a session, as :meth:`app` does.
        """
        route = _route_from_app_row(await self.app(app_id))
        return route if route is not None else AppRoute(app_id=bigint(app_id))

    async def for_org(self, org_slug: str) -> list[dict[str, Any]]:
        """Every app of an organization by the org's slug, newest first.

        Drafts and archived apps included; an unknown slug answers an empty list.
        """
        result: list[dict[str, Any]] = await self._request(ops.APPS_FOR_ORG, {"orgSlug": org_slug})
        return result

    async def marketplace(
        self,
        filter: inputs.AppMarketplaceFilterInput | Mapping[str, Any] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """One page of the public marketplace: live, public apps only. Public.

        Prefer :meth:`marketplace_connection`; these offset arguments are deprecated.
        """
        result: dict[str, Any] = await self._request(
            ops.MARKETPLACE_APPS, omit_none({"filter": filter, "limit": limit, "offset": offset})
        )
        return result

    async def marketplace_connection(
        self,
        first: int | None = None,
        after: str | None = None,
        filter: inputs.AppMarketplaceFilterInput | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The public marketplace as a cursor connection. Public.

        Page with ``first`` and the previous page's ``pageInfo.endCursor`` as ``after``.
        """
        result: dict[str, Any] = await self._request(
            ops.APPS_CONNECTION, omit_none({"first": first, "after": after, "filter": filter})
        )
        return result

    async def placeable_datacenters(self) -> dict[str, Any]:
        """The datacenters this deployment can create an app in. Call it before :meth:`create`.

        Offer only entries with ``placeable`` true: :meth:`create` refuses the others. A
        placeable entry that is ``NOT_SERVING`` holds an app fine, but its players cannot
        connect until it serves again. An empty ``datacenters`` list means app creation is
        unavailable on this deployment; ``placementEnforced`` false is a single-node
        deployment.
        """
        result: dict[str, Any] = await self._request(ops.PLACEABLE_DATACENTERS, {})
        return result

    async def create(self, input: inputs.CreateAppInput | Mapping[str, Any]) -> dict[str, Any]:
        """Create an app under an organization. Needs ``manage_apps`` on the org.

        ``datacenter`` is required and permanent: take a code from
        :meth:`placeable_datacenters` rather than hard-coding one. The app gets an open
        default access tier. ``BAD_USER_INPUT`` for a duplicate slug or a datacenter this
        deployment cannot place an app in.
        """
        result: dict[str, Any] = await self._request(ops.CREATE_APP, {"input": input})
        return result

    async def update(
        self, app_id: str | int, input: inputs.UpdateAppInput | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Change an app's mutable fields; fields left out stay as they are. Needs ``manage_apps``.

        ``wildernessWritesOpen`` false closes the app's wilderness (chunks no grid but the
        world grid covers): every voxel and chunk write there is refused with ``FORBIDDEN``,
        whoever makes it, within 15 seconds. Apps start open. ``claimOwnerKeys`` (at most 8
        active grid keys, never a player-code key) are the grid permission keys a player's
        claim grants its owner on the claimed grid, for claims made from then on; for
        example ``update_voxel_data`` makes a claim buildable without a hub granting it.
        """
        result: dict[str, Any] = await self._request(
            ops.UPDATE_APP, {"appId": bigint(app_id), "input": input}
        )
        return result

    async def archive(self, app_id: str | int) -> dict[str, Any]:
        """Archive (soft-delete) an app. Needs ``manage_apps``.

        :meth:`update` with a ``status`` of ``DRAFT`` or ``LIVE`` restores it.
        """
        result: dict[str, Any] = await self._request(ops.ARCHIVE_APP, {"appId": bigint(app_id)})
        return result

    async def compute_budget(self, app_id: str | int) -> dict[str, Any] | None:
        """The app's compute allowance, or ``None`` when none is set (its ck-exec code is
        then never paused for budget). App admin."""
        result: dict[str, Any] | None = await self._request(
            APP_COMPUTE_BUDGET, {"appId": bigint(app_id)}
        )
        return result

    async def set_compute_budget(
        self,
        app_id: str | int,
        units_per_minute: int,
        *,
        enforce: bool | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Set the app's compute allowance in units per minute (one unit is a millisecond of
        measured execution). With ``enforce``, code over budget is paused."""
        result: dict[str, Any] = await self._request(
            SET_APP_COMPUTE_BUDGET,
            omit_none(
                {
                    "appId": bigint(app_id),
                    "unitsPerMinute": units_per_minute,
                    "enforce": enforce,
                    "note": note,
                }
            ),
        )
        return result

    async def clear_compute_budget(self, app_id: str | int) -> bool:
        """Remove the app's compute allowance. ``True`` when one was removed."""
        return bool(await self._request(CLEAR_APP_COMPUTE_BUDGET, {"appId": bigint(app_id)}))
