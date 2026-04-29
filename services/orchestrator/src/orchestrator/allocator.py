"""
orchestrator.allocator - (direction, confidence) -> target notional.

Pure function with two knobs:

  - ``confidence_threshold``  Below this absolute signal strength
                              (|direction * confidence|), return 0.
                              Below-threshold signals are noise; the
                              orchestrator should sit out of the
                              market rather than churn through round
                              trips.
  - ``cap_per_symbol``        Maximum signed target notional in USD.
                              Linearly scaled by signal strength:
                              full signal (|d * c| = 1) -> full cap.

The output is signed: positive for long, negative for short.  Real
Kelly-optimal sizing arrives in TASK-042; v1 uses linear scaling
because:

  1. We don't yet have correlations + covariance estimates that Kelly
     needs.
  2. Linear sizing is conservative against an over-confident signal.
  3. The risk gate (TASK-041) enforces the same per-symbol cap as a
     hard ceiling, so over-allocation is impossible regardless.
"""

from __future__ import annotations

from decimal import Decimal


def target_notional(
    *,
    direction: float,
    confidence: float,
    cap_per_symbol: Decimal,
    confidence_threshold: float = 0.1,
) -> Decimal:
    """Map signal strength to a signed target USD notional.

    >>> target_notional(direction=1.0, confidence=1.0, cap_per_symbol=Decimal(10000))
    Decimal('10000')
    >>> target_notional(direction=-0.5, confidence=0.5, cap_per_symbol=Decimal(10000))
    Decimal('-2500')
    >>> target_notional(direction=0.05, confidence=1.0, cap_per_symbol=Decimal(10000))
    Decimal('0')
    """
    if not -1.0 <= direction <= 1.0:
        raise ValueError(f"direction must be in [-1, 1]; got {direction}")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be in [0, 1]; got {confidence}")
    if cap_per_symbol < 0:
        raise ValueError(f"cap_per_symbol must be non-negative; got {cap_per_symbol}")

    signal = direction * confidence
    if abs(signal) < confidence_threshold:
        return Decimal(0)

    magnitude = cap_per_symbol * Decimal(str(abs(signal)))
    # Quantize to cents to keep the wire compact and avoid floating
    # representations like Decimal('2500.0000000000004').
    magnitude = magnitude.quantize(Decimal("0.01"))
    return magnitude if signal > 0 else -magnitude
