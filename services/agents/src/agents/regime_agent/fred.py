"""
agents.regime_agent.fred - tiny FRED API client.

We hit ``https://api.stlouisfed.org/fred/series/observations`` directly
with httpx.  Same rationale as sentiment_agent.llm: no SDK dependency,
the API is stable and dead-simple.

Free FRED tier: 120 requests/minute, no daily cap.  We're nowhere near
the limit.

The default poll cadence is ``--interval-sec 3600`` (1h); FRED data is
mostly daily (some weekly), so polling more frequently is wasted.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

import httpx

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"


@dataclass(frozen=True)
class Observation:
    """One FRED observation, normalized to (date, float) pairs.

    ``date`` is the date the observation describes (not when it was
    reported - FRED carries vintage data).  ``value`` is None for
    "." records (FRED's missing-data sentinel).
    """

    date: _dt.date
    value: float | None


async def fetch_latest(
    client: httpx.AsyncClient,
    *,
    series_id: str,
    api_key: str,
    lookback_days: int = 14,
    limit: int = 5,
) -> list[Observation]:
    """Fetch the latest ``limit`` observations for ``series_id``.

    Returned in DESCENDING date order so ``observations[0]`` is the
    most recent value.  ``lookback_days`` clips at the request layer
    so a stale or rarely-updated series doesn't return ancient prints.
    """
    cutoff = _dt.date.today() - _dt.timedelta(days=lookback_days)
    resp = await client.get(
        FRED_OBSERVATIONS_URL,
        params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": cutoff.isoformat(),
            "sort_order": "desc",
            "limit": limit,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    body = resp.json()
    raws = body.get("observations") or []
    out: list[Observation] = []
    for raw in raws:
        date_str = raw.get("date")
        value_str = raw.get("value")
        if not date_str:
            continue
        try:
            date = _dt.date.fromisoformat(date_str)
        except ValueError:
            continue
        try:
            value: float | None = float(value_str) if value_str not in (None, ".", "") else None
        except (TypeError, ValueError):
            value = None
        out.append(Observation(date=date, value=value))
    return out


async def latest_value(
    client: httpx.AsyncClient,
    *,
    series_id: str,
    api_key: str,
    lookback_days: int = 14,
) -> float | None:
    """Convenience: return the most recent non-null value or None.

    Skips '.' / null observations.  Returns None if every observation
    in the lookback window is missing - lets the rule classifier
    bail cleanly when a series is stale.
    """
    obs = await fetch_latest(
        client,
        series_id=series_id,
        api_key=api_key,
        lookback_days=lookback_days,
        limit=10,
    )
    for o in obs:
        if o.value is not None:
            return o.value
    return None
