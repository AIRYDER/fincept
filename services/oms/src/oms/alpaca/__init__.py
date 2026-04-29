"""
oms.alpaca - real Alpaca paper-broker integration.

Public surface:

  - ``AlpacaClient``       Thin httpx wrapper for the REST API we use:
                           submit, get, cancel.  Authenticated via
                           ``APCA-API-KEY-ID`` + ``APCA-API-SECRET-KEY``
                           headers from Settings.
  - ``submit_intent``      Async function: takes an OrderIntent, submits
                           to Alpaca, polls briefly for instant fills,
                           returns ``IntentResult`` matching the sim
                           processor's shape (so main.py is symmetric).
  - ``poll_pending_orders`` Background task that periodically queries
                           Alpaca for any orders we submitted but didn't
                           see fill within the synchronous poll window.
                           Emits Fill + state events when they finally
                           land.
  - ``to_alpaca_symbol`` /
    ``from_alpaca_symbol``  Symbol-format mapping.  Equities are
                           identical (AAPL); crypto swaps separator
                           (BTC-USD <-> BTC/USD).

Reading order if you're unfamiliar: ``symbols`` -> ``client`` ->
``runtime``.  Tests are in ``services/oms/tests/test_alpaca_*.py``.
"""

from oms.alpaca.client import AlpacaClient, AlpacaError
from oms.alpaca.runtime import poll_pending_orders, submit_intent
from oms.alpaca.symbols import from_alpaca_symbol, is_crypto_symbol, to_alpaca_symbol

__all__ = [
    "AlpacaClient",
    "AlpacaError",
    "from_alpaca_symbol",
    "is_crypto_symbol",
    "poll_pending_orders",
    "submit_intent",
    "to_alpaca_symbol",
]
