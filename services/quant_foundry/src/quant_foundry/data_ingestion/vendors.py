"""
quant_foundry.data_ingestion.vendors — vendor registry mapping vendor names to
their ingestion functions.

This module provides a single :data:`VENDOR_INGESTERS` registry and a
:func:`get_ingester` lookup so callers can dispatch to the correct ingestion
function by vendor name without importing every module at the call site.
"""

from __future__ import annotations

from collections.abc import Callable

from quant_foundry.data_ingestion.equities import IngestionResult, ingest_equity_bars
from quant_foundry.data_ingestion.macro import ingest_macro_indicators
from quant_foundry.data_ingestion.news import ingest_news_events

#: Registry of vendor name -> ingestion function.  Every function returns an
#: :class:`IngestionResult`.
VENDOR_INGESTERS: dict[str, Callable[..., IngestionResult]] = {
    "equity_bars": ingest_equity_bars,
    "news_events": ingest_news_events,
    "macro_indicators": ingest_macro_indicators,
}


def get_ingester(vendor: str) -> Callable[..., IngestionResult]:
    """Return the ingestion function for a vendor name.

    Parameters
    ----------
    vendor
        Vendor name key into :data:`VENDOR_INGESTERS`.

    Returns
    -------
    Callable[..., IngestionResult]
        The ingestion function for *vendor*.

    Raises
    ------
    ValueError
        If *vendor* is not a known vendor name.
    """
    if vendor not in VENDOR_INGESTERS:
        raise ValueError(
            f"unknown vendor: {vendor!r}; available: {list(VENDOR_INGESTERS)}",
        )
    return VENDOR_INGESTERS[vendor]


__all__ = [
    "VENDOR_INGESTERS",
    "get_ingester",
]
