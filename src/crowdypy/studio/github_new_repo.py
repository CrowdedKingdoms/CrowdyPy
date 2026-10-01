"""GitHub's "new repository" page, prefilled for a Studio project (CrowdyJS's
``githubNewRepositoryUrl``). Nothing is created for you: the page is GitHub's."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import quote_plus

__all__ = ["github_new_repository_url", "github_repository_slug"]

_EDGES = re.compile(r"^[-._]+|[-._]+$")


def github_repository_slug(value: str) -> str:
    """A repository name GitHub accepts: lowercase ``[a-z0-9._-]``, runs of anything else
    as one dash, at most 100 characters (``crowdy-mod`` when nothing is left)."""
    out = ""
    dash = False
    for ch in value.strip().lower():
        if "a" <= ch <= "z" or "0" <= ch <= "9" or ch in "._-":
            out += ch
            dash = ch == "-"
        elif not dash and out:
            out += "-"
            dash = True
    return _EDGES.sub("", out)[:100] or "crowdy-mod"


def _utf16_prefix(value: str, units: int) -> str:
    out, used = [], 0
    for ch in value:
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > units:
            break
        out.append(ch)
    return "".join(out)


def _form(value: str) -> str:
    # URLSearchParams: alphanumerics and *-._ stay, a space is +, everything else escapes.
    return quote_plus(value, safe="*").replace("~", "%7E")


def github_new_repository_url(
    *,
    name: str,
    owner: str | None = None,
    description: str | None = None,
    visibility: Literal["private", "public"] = "private",
) -> str:
    params: list[tuple[str, str]] = []
    if owner:
        params.append(("owner", owner))
    params.append(("name", github_repository_slug(name)))
    if description and description.strip():
        params.append(("description", _utf16_prefix(description.strip(), 350)))
    params.append(("visibility", visibility))
    return "https://github.com/new?" + "&".join(f"{_form(k)}={_form(v)}" for k, v in params)
