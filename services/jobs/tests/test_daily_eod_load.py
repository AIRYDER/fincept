"""
Tests for jobs.daily_eod_load — orchestrator with injected loader + universe.

Plus a smoke test for jobs.main: the APScheduler cron expression compiles
to the expected weekday/22:30/America/New_York shape without actually
starting the scheduler loop.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest
from apscheduler.triggers.cron import CronTrigger

from fincept_core.schemas import Venue
from ingestor.eod_equity import Loader
from jobs.daily_eod_load import run_daily
from jobs.main import (
    EOD_CRON_DAY_OF_WEEK,
    EOD_CRON_HOUR,
    EOD_CRON_MINUTE,
    EOD_CRON_TZ,
    build_scheduler,
)


class _RecordingLoader(Loader):
    """A Loader fake that records every call so tests can assert on them."""

    venue: Venue = Venue.NASDAQ

    def __init__(self, return_rows: int = 5) -> None:
        self.return_rows = return_rows
        self.calls: list[tuple[Sequence[str], date, date]] = []

    async def load_for_date_range(self, symbols: Sequence[str], start: date, end: date) -> int:
        self.calls.append((list(symbols), start, end))
        return self.return_rows


def _const_universe(symbols: list[str]) -> Any:
    async def fn() -> list[str]:
        return symbols

    return fn


# ---------------------------------------------------------------------------
# run_daily orchestration
# ---------------------------------------------------------------------------


async def test_run_daily_invokes_loader_for_target_date() -> None:
    loader = _RecordingLoader(return_rows=10)
    rows = await run_daily(
        target=date(2024, 11, 5),
        loader_factory=lambda: loader,
        universe_fn=_const_universe(["AAPL", "MSFT"]),
    )
    assert rows == 10
    assert len(loader.calls) == 1
    symbols, start, end = loader.calls[0]
    assert list(symbols) == ["AAPL", "MSFT"]
    assert start == date(2024, 11, 5)
    assert end == date(2024, 11, 5)


async def test_run_daily_skips_on_weekend() -> None:
    loader = _RecordingLoader()
    rows = await run_daily(
        target=date(2024, 11, 9),  # Saturday
        loader_factory=lambda: loader,
        universe_fn=_const_universe(["AAPL"]),
    )
    assert rows == 0
    assert loader.calls == []


async def test_run_daily_skips_on_empty_universe() -> None:
    loader = _RecordingLoader()
    rows = await run_daily(
        target=date(2024, 11, 5),
        loader_factory=lambda: loader,
        universe_fn=_const_universe([]),
    )
    assert rows == 0
    assert loader.calls == []


async def test_run_daily_default_target_is_yesterday(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When *target* is None, use ``date.today() - 1`` so the 22:30 ET schedule
    captures the trading day that just closed."""
    loader = _RecordingLoader()

    class _FrozenDate(date):
        @classmethod
        def today(cls) -> date:  # type: ignore[override]
            return date(2024, 11, 5)  # Tuesday

    monkeypatch.setattr("jobs.daily_eod_load.date", _FrozenDate)

    await run_daily(
        loader_factory=lambda: loader,
        universe_fn=_const_universe(["AAPL"]),
    )
    assert len(loader.calls) == 1
    _, start, _ = loader.calls[0]
    assert start == date(2024, 11, 4)  # Monday — yesterday relative to frozen today


# ---------------------------------------------------------------------------
# Scheduler smoke
# ---------------------------------------------------------------------------


def test_eod_schedule_constants_match_spec() -> None:
    assert EOD_CRON_TZ == "America/New_York"
    assert (EOD_CRON_HOUR, EOD_CRON_MINUTE) == (22, 30)
    assert EOD_CRON_DAY_OF_WEEK == "mon-fri"


def test_build_scheduler_registers_eod_job() -> None:
    """Verify the job + trigger configuration without starting the scheduler.

    APScheduler raises ``SchedulerNotRunningError`` if shutdown is called
    before start, so we deliberately skip the cleanup — nothing was started.
    """
    scheduler = build_scheduler()
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == "daily_eod_load"
    assert isinstance(job.trigger, CronTrigger)
    # CronTrigger.fields is an ordered list of CronField subclasses; their
    # str() returns the cron expression they parse from.
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["day_of_week"] == "mon-fri"
    assert fields["hour"] == "22"
    assert fields["minute"] == "30"
