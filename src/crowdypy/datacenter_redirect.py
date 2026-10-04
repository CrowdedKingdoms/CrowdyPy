"""Read a ``WRONG_DATACENTER`` refusal into the endpoint the app actually lives at."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import WRONG_DATACENTER_CODE

__all__ = ["DatacenterMove", "move_from_error", "move_from_errors"]


@dataclass(frozen=True, slots=True)
class DatacenterMove:
    game_api_url: str
    game_api_ws_url: str | None = None
    app_id: str | None = None
    app_datacenter: str | None = None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def move_from_error(error: Mapping[str, Any] | None) -> DatacenterMove | None:
    """The move one GraphQL error asks for, or ``None``.

    Both a code and a non-empty ``gameApiUrl`` are required: a refusal that names no
    endpoint is not a redirect, whatever its code says.
    """
    extensions = error.get("extensions") if error else None
    if not isinstance(extensions, Mapping) or extensions.get("code") != WRONG_DATACENTER_CODE:
        return None
    url = _text(extensions.get("gameApiUrl"))
    if url is None:
        return None
    return DatacenterMove(
        game_api_url=url,
        game_api_ws_url=_text(extensions.get("gameApiWsUrl")),
        app_id=_text(extensions.get("appId")),
        app_datacenter=_text(extensions.get("appDatacenter")),
    )


def move_from_errors(errors: Iterable[Mapping[str, Any] | None] | None) -> DatacenterMove | None:
    for error in errors or ():
        move = move_from_error(error)
        if move is not None:
            return move
    return None
