"""Tests for orchestrator.allocator.target_notional."""

from __future__ import annotations

from decimal import Decimal

import pytest

from orchestrator.allocator import target_notional

# ---------------------------------------------------------------------------
# Sign + magnitude
# ---------------------------------------------------------------------------


def test_full_long_signal_yields_full_cap() -> None:
    out = target_notional(
        direction=1.0, confidence=1.0, cap_per_symbol=Decimal("10000")
    )
    assert out == Decimal("10000.00")


def test_full_short_signal_yields_negative_full_cap() -> None:
    out = target_notional(
        direction=-1.0, confidence=1.0, cap_per_symbol=Decimal("10000")
    )
    assert out == Decimal("-10000.00")


def test_half_signal_half_cap() -> None:
    out = target_notional(
        direction=0.5, confidence=1.0, cap_per_symbol=Decimal("10000")
    )
    assert out == Decimal("5000.00")


def test_low_confidence_dampens_magnitude() -> None:
    out = target_notional(
        direction=1.0, confidence=0.5, cap_per_symbol=Decimal("10000")
    )
    assert out == Decimal("5000.00")


# ---------------------------------------------------------------------------
# Deadband
# ---------------------------------------------------------------------------


def test_signal_below_threshold_returns_zero() -> None:
    """direction*confidence = 0.05; threshold 0.1 -> sit out."""
    out = target_notional(
        direction=0.05,
        confidence=1.0,
        cap_per_symbol=Decimal("10000"),
        confidence_threshold=0.1,
    )
    assert out == Decimal(0)


def test_signal_at_threshold_returns_nonzero() -> None:
    """At-threshold signals pass; only strictly below is filtered."""
    out = target_notional(
        direction=0.1,
        confidence=1.0,
        cap_per_symbol=Decimal("10000"),
        confidence_threshold=0.1,
    )
    # 0.1 * 10000 = 1000
    assert out == Decimal("1000.00")


def test_negative_signal_below_threshold_returns_zero() -> None:
    out = target_notional(
        direction=-0.05,
        confidence=1.0,
        cap_per_symbol=Decimal("10000"),
        confidence_threshold=0.1,
    )
    assert out == Decimal(0)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_direction_outside_unit_interval_raises() -> None:
    with pytest.raises(ValueError, match="direction"):
        target_notional(direction=1.5, confidence=0.5, cap_per_symbol=Decimal("10000"))
    with pytest.raises(ValueError, match="direction"):
        target_notional(direction=-1.5, confidence=0.5, cap_per_symbol=Decimal("10000"))


def test_confidence_outside_unit_interval_raises() -> None:
    with pytest.raises(ValueError, match="confidence"):
        target_notional(direction=0.5, confidence=1.5, cap_per_symbol=Decimal("10000"))
    with pytest.raises(ValueError, match="confidence"):
        target_notional(direction=0.5, confidence=-0.1, cap_per_symbol=Decimal("10000"))


def test_negative_cap_raises() -> None:
    with pytest.raises(ValueError, match="cap_per_symbol"):
        target_notional(direction=0.5, confidence=0.5, cap_per_symbol=Decimal("-1"))


def test_zero_cap_returns_zero() -> None:
    out = target_notional(direction=1.0, confidence=1.0, cap_per_symbol=Decimal(0))
    assert out == Decimal(0)


# ---------------------------------------------------------------------------
# Quantization
# ---------------------------------------------------------------------------


def test_result_quantized_to_cents() -> None:
    out = target_notional(
        direction=0.333, confidence=1.0, cap_per_symbol=Decimal("10000")
    )
    # 0.333 * 10000 = 3330; quantize -> 3330.00
    assert out == Decimal("3330.00")
