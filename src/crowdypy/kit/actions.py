"""Optimistic actions: apply locally, ask the server, roll back if it says no."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["OptimisticActionOutcome", "run_optimistic_action"]


@dataclass(frozen=True, slots=True)
class OptimisticActionOutcome:
    ok: bool
    action_id: str
    result: Any = None
    error_message: str | None = None
    error: BaseException | None = None


def _accepted(result: Any) -> bool:
    return not (isinstance(result, dict) and result.get("success") is False)


def _denial(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    for key in ("errorMessage", "reason"):
        if isinstance(result.get(key), str):
            return str(result[key])
    return None


async def run_optimistic_action(
    apply: Callable[[], Callable[[], Any] | None],
    invoke: Callable[[str], Awaitable[Any]],
    *,
    rollback: Callable[[], Any] | None = None,
    validate: Callable[[Any], bool] | None = None,
    denial_message: Callable[[Any], str | None] | None = None,
    confirm: Callable[[Any], Any] | None = None,
    action_id: str | None = None,
) -> OptimisticActionOutcome:
    """Run ``apply()`` (it may return its own rollback), then ``await invoke(action_id)``.

    A result ``validate`` refuses (by default ``{"success": False}``) or a raised error rolls
    the local change back; otherwise ``confirm(result)`` runs. ``action_id`` correlates the
    attempt end to end (a fresh UUID by default). Never raises for a refusal.
    """
    action = action_id or str(uuid.uuid4())
    check = validate or _accepted
    explain = denial_message or _denial
    undo = rollback
    try:
        returned = apply()
        if callable(returned):
            undo = returned
    except Exception as exc:
        return OptimisticActionOutcome(
            False, action, error_message=str(exc) or "optimistic apply failed", error=exc
        )
    try:
        result = await invoke(action)
        if not check(result):
            if undo is not None:
                undo()
            return OptimisticActionOutcome(
                False, action, result, explain(result) or "action rejected"
            )
        if confirm is not None:
            confirmed = confirm(result)
            if hasattr(confirmed, "__await__"):
                await confirmed
        return OptimisticActionOutcome(True, action, result)
    except Exception as exc:
        if undo is not None:
            undo()
        return OptimisticActionOutcome(
            False, action, error_message=str(exc) or "action failed", error=exc
        )
