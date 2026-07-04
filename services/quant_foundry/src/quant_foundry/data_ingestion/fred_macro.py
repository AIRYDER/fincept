"""
quant_foundry.data_ingestion.fred_macro — fetch macro economic indicators
from the FRED API and ingest them through the macro pipeline.

This module adds a vendor API adapter for FRED
(https://api.stlouisfed.org) that fetches economic time series via the
``/series/observations`` endpoint and feeds them into the same
leakage-safe :func:`ingest_macro_indicators` pipeline used by local-file
ingestion.

We hit FRED directly with ``httpx`` (no SDK), matching the pattern in
``services/agents/src/agents/regime_agent/fred.py``.  The FRED API is
stable and dead-simple: one GET per series returns JSON observations.

Env vars (from ``.env.example``):

- ``FRED_API_KEY`` — FRED API key (also used by ``regime_agent.fred``).

Heavy dependencies (httpx) are imported lazily inside functions so this
module is importable without them.
"""

from __future__ import annotations

import csv
import os
import pathlib
import tempfile

from quant_foundry.data_ingestion.equities import IngestionResult
from quant_foundry.data_ingestion.macro import ingest_macro_indicators

#: FRED observations endpoint base URL.
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

#: Env var name for the FRED API key.
_FRED_KEY_ENV = "FRED_API_KEY"


def _resolve_api_key(api_key: str | None) -> str:
    """Resolve the FRED API key, falling back to the env var.

    Raises ``ValueError`` with a clear message if the key is missing.
    """
    key = api_key or os.environ.get(_FRED_KEY_ENV, "")
    if not key:
        raise ValueError(
            f"FRED API key not provided; pass api_key= or set {_FRED_KEY_ENV}",
        )
    return key


async def fetch_fred_series(
    *,
    series_ids: list[str],
    start: str,
    end: str,
    api_key: str | None = None,
) -> pathlib.Path:
    """Fetch macro series from FRED and write to a temp CSV file.

    Parameters
    ----------
    series_ids
        List of FRED series IDs (e.g. ``["FEDFUNDS", "CPIAUCSL"]``).
    start
        ISO date string for the observation start (e.g. ``"2020-01-01"``).
    end
        ISO date string for the observation end (e.g. ``"2024-01-01"``).
    api_key
        FRED API key.  Falls back to ``FRED_API_KEY``.

    Returns
    -------
    pathlib.Path
        Path to the temp CSV file with columns ``date, indicator, value``
        (one row per observation per series).

    Raises
    ------
    ValueError
        If the API key is missing or no observations are returned.
    """
    import httpx

    key = _resolve_api_key(api_key)

    rows: list[dict[str, str]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for series_id in series_ids:
            params = {
                "series_id": series_id,
                "api_key": key,
                "file_type": "json",
                "observation_start": start,
                "observation_end": end,
            }
            resp = await client.get(FRED_BASE_URL, params=params)
            resp.raise_for_status()
            body = resp.json()
            observations = body.get("observations") or []
            for obs in observations:
                date_str = obs.get("date")
                value_str = obs.get("value")
                if not date_str:
                    continue
                # FRED uses "." as its missing-data sentinel; skip those.
                if value_str in (None, ".", ""):
                    continue
                rows.append(
                    {
                        "date": str(date_str),
                        "indicator": str(series_id),
                        "value": str(value_str),
                    },
                )

    if not rows:
        raise ValueError(
            f"no observations returned from FRED for series {series_ids} in [{start}, {end}]",
        )

    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="fred_macro_"))
    out_path = tmp_dir / "fred_macro.csv"
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["date", "indicator", "value"],
        )
        writer.writeheader()
        writer.writerows(rows)
    return out_path


async def ingest_fred_macro(
    *,
    series_ids: list[str],
    start: str,
    end: str,
    output_dir: pathlib.Path,
    dataset_id: str,
    n_folds: int = 3,
    api_key: str | None = None,
) -> IngestionResult:
    """Fetch macro data from FRED and ingest into a leakage-safe dataset.

    Fetches observations via :func:`fetch_fred_series`, then runs the full
    :func:`ingest_macro_indicators` pipeline (features + labels + manifest +
    receipt + quality report).

    Parameters
    ----------
    series_ids
        List of FRED series IDs.
    start
        ISO date string for the observation start.
    end
        ISO date string for the observation end.
    output_dir
        Directory to write the dataset artifacts.  Created if needed.
    dataset_id
        Unique dataset identifier.
    n_folds
        Number of purged-k-fold validation windows (default 3).
    api_key
        FRED API key; falls back to env var.

    Returns
    -------
    IngestionResult
        Paths to all emitted artifacts plus the manifest and quality report.
    """
    csv_path = await fetch_fred_series(
        series_ids=series_ids,
        start=start,
        end=end,
        api_key=api_key,
    )
    return ingest_macro_indicators(
        csv_path,
        output_dir=pathlib.Path(output_dir),
        dataset_id=dataset_id,
        n_folds=n_folds,
    )


__all__ = [
    "FRED_BASE_URL",
    "fetch_fred_series",
    "ingest_fred_macro",
]
