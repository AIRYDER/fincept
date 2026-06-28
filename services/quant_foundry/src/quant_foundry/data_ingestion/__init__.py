"""
quant_foundry.data_ingestion — real dataset ingestion + quality reports.

This package provides vendor-specific ingestion functions that turn raw data
(OHLCV bars, news events, macro indicators) into leakage-safe point-in-time
datasets with a :class:`FeatureLakeManifest`, an export receipt, and a
:class:`DatasetQualityReport`.

Public surface:
  - :class:`DatasetQualityReport`, :func:`compute_quality_report`
  - :func:`ingest_equity_bars`
  - :func:`ingest_news_events`
  - :func:`ingest_macro_indicators`
  - :data:`VENDOR_INGESTERS`, :func:`get_ingester`
"""

from __future__ import annotations

from quant_foundry.data_ingestion.equities import IngestionResult, ingest_equity_bars
from quant_foundry.data_ingestion.macro import ingest_macro_indicators
from quant_foundry.data_ingestion.news import ingest_news_events
from quant_foundry.data_ingestion.quality_report import (
    DatasetQualityReport,
    compute_quality_report,
)
from quant_foundry.data_ingestion.vendors import VENDOR_INGESTERS, get_ingester

__all__ = [
    "VENDOR_INGESTERS",
    "DatasetQualityReport",
    "IngestionResult",
    "compute_quality_report",
    "get_ingester",
    "ingest_equity_bars",
    "ingest_macro_indicators",
    "ingest_news_events",
]
