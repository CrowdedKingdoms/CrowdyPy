"""run_soon outside an event loop: the async client's tick() may run where no loop is running."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from crowdypy import _concurrency


def test_run_soon_without_a_loop_skips_the_job_and_warns_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_concurrency, "_warned_no_loop", False)
    ran: list[int] = []

    async def job() -> None:
        ran.append(1)

    with caplog.at_level(logging.WARNING, logger="crowdypy"):
        _concurrency.run_soon(job)
        _concurrency.run_soon(job)
    assert ran == []
    warnings = [r for r in caplog.records if "no event loop is running" in r.getMessage()]
    assert len(warnings) == 1


def test_run_soon_on_a_loop_runs_the_job() -> None:
    ran: list[Any] = []

    async def job() -> None:
        ran.append(1)

    async def main() -> None:
        _concurrency.run_soon(job)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(main())
    assert ran == [1]
