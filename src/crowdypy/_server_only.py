"""Calls that take an organization's org token run on a server the organization runs.

An org token signs in any of the organization's players and opens its fleet sessions, and a
fleet token signs for every player its session carries. Neither goes to a player's device or
into a browser page. CrowdyJS makes the same check in ``src/server-only.ts``; not exported.
"""

from __future__ import annotations

import sys

from crowdypy.errors import CrowdyError

__all__ = ["assert_organization_server", "organization_server_refusal"]

#: Where Python runs on a player's device or inside a browser page, by ``sys.platform``.
_PLAYER_PLATFORMS = {
    "emscripten": "this is Python running in a browser page or its worker",
    "android": "this is a player's device (Android)",
    "ios": "this is a player's device (iOS)",
}


def organization_server_refusal(platform: str | None = None) -> str | None:
    """Why a call that takes an organization's org token may not be made from this runtime, or
    ``None`` when it may.

    CPython on Linux, macOS or Windows is taken for a server. Python in a browser page
    (``sys.platform == "emscripten"``) and on a phone (``android``, ``ios``) is refused. This
    guards against a mistake, before the token is put on a request; the Application API
    refuses an org token sent with an ``Origin`` header whatever the runtime says.
    """
    return _PLAYER_PLATFORMS.get(_runtime_platform() if platform is None else platform)


def _runtime_platform() -> str:
    return sys.platform


def assert_organization_server(what: str) -> None:
    """Refuse ``what`` unless the runtime is a server's (:func:`organization_server_refusal`)."""
    why = organization_server_refusal()
    if why is None:
        return
    raise CrowdyError(
        f"{what} takes your organization's org token, which never goes into a browser page or "
        f"a game client, and {why}. Call it from a server your organization runs."
    )
