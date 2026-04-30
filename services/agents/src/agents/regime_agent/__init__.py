"""agents.regime_agent - macro regime classifier.

Polls a small set of FRED time series (VIX, yield curve, fed funds)
on a slow cadence (default 1 hour) and publishes a ``RegimeSignal``
to ``STREAM_SIG_REGIME`` whenever the classified regime changes.

Optional service: skips startup if ``FRED_API_KEY`` is unset.
"""
