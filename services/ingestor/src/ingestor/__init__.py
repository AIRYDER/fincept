"""ingestor — market-data ingestion service.

Pipeline: venue WebSocket -> VenueAdapter (per-venue normalizer) ->
canonical Pydantic event (TradeEvent / BookDeltaEvent) -> Writer
(fan-out to Redis Streams via fincept_bus.Producer + batch write to
Timescale via fincept_db.ticks).

Public surface:
  - VenueAdapter    — ABC every venue adapter implements
  - BinanceAdapter  — Binance spot WS adapter (TASK-010)
  - CoinbaseAdapter — Coinbase Advanced Trade WS adapter (TASK-012)
  - KrakenAdapter   — Kraken v2 WS adapter (TASK-013)
  - Writer          — fan-out + batched DB writer
  - QualityMonitor  — gap + latency observer
  - run_loop        — entrypoint coroutine (also wired in main.py)
"""

from ingestor.base import VenueAdapter
from ingestor.binance import BinanceAdapter
from ingestor.coinbase import CoinbaseAdapter
from ingestor.kraken import KrakenAdapter
from ingestor.main import run_loop
from ingestor.quality import QualityMonitor
from ingestor.writer import Writer

__all__ = [
    "BinanceAdapter",
    "CoinbaseAdapter",
    "KrakenAdapter",
    "QualityMonitor",
    "VenueAdapter",
    "Writer",
    "run_loop",
]
