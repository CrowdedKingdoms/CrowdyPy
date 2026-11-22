"""Read a ``WRONG_DATACENTER`` refusal into the endpoint the app actually lives at."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import WRONG_DATACENTER_CODE

__all__ = ["DatacenterMove", "move_from_error", "move_from_errors"]


@dataclass(frozen=True, slots=True)
class DatacenterMove:
    application_api_url: str | None = None
    application_api_ws_url: str | None = None
    app_id: str | None = None
    app_datacenter: str | None = None
    #: Deprecated: use ``application_api_url`` (the Game API is now the Application API); removed on January 1, 2027.
    game_api_url: str | None = None
    #: Deprecated: use ``application_api_ws_url`` (the Game API is now the Application API); removed on January 1, 2027.
    game_api_ws_url: str | None = None

    def __post_init__(self) -> None:
        # Either name fills the other, so a move built with the old names still works.
        if self.application_api_url is None:
            object.__setattr__(self, "application_api_url", self.game_api_url)
        if self.application_api_ws_url is None:
            object.__setattr__(self, "application_api_ws_url", self.game_api_ws_url)
        if self.game_api_url is None:
            object.__setattr__(self, "game_api_url", self.application_api_url)
        if self.game_api_ws_url is None:
            object.__setattr__(self, "game_api_ws_url", self.application_api_ws_url)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def move_from_error(error: Mapping[str, Any] | None) -> DatacenterMove | None:
    """The move one GraphQL error asks for, or ``None``.

    Both a code and a non-empty ``applicationApiUrl`` (``gameApiUrl`` from an Application API
    older than v2.85.0) are required: a refusal that names no endpoint is not a redirect,
    whatever its code says.
    """
    extensions = error.get("extensions") if error else None
    if not isinstance(extensions, Mapping) or extensions.get("code") != WRONG_DATACENTER_CODE:
        return None
    url = _text(extensions.get("applicationApiUrl")) or _text(extensions.get("gameApiUrl"))
    if url is None:
        return None
    ws_url = _text(extensions.get("applicationApiWsUrl")) or _text(extensions.get("gameApiWsUrl"))
    return DatacenterMove(
        application_api_url=url,
        application_api_ws_url=ws_url,
        app_id=_text(extensions.get("appId")),
        app_datacenter=_text(extensions.get("appDatacenter")),
    )


def move_from_errors(errors: Iterable[Mapping[str, Any] | None] | None) -> DatacenterMove | None:
    for error in errors or ():
        move = move_from_error(error)
        if move is not None:
            return move
    return None
