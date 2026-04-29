"""
jobs — scheduled batch jobs.

Currently exposes:

  - ``daily_eod_load.run_daily(target=None)``  — TASK-015; pulls one day's
    OHLCV bars for the active equity universe via ``ingestor.eod_equity``.
  - ``main.build_scheduler``                   — APScheduler factory that
    cron-fires ``run_daily`` weekdays at 22:30 ET.
"""

from jobs.daily_eod_load import run_daily

__all__ = ["run_daily"]
