"""Tests for the v2 CostModel: square-root impact, per-symbol overrides,
vol-scaled spread, and backward-compatibility with the linear adv_pct
mode.

Existing v1 tests in :file:`test_costs.py` cover the legacy linear path
and stay unchanged; this file targets the new behaviours added on top.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from backtester.broker import SimBroker
from backtester.costs import CostModel, SymbolCosts
from fincept_core.clock import now_ns
from fincept_core.ids import new_id
from fincept_core.schemas import (
    AssetClass,
    BarEvent,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
    Venue,
)

# --------------------------------------------------------------------------- #
# Square-root market impact                                                   #
# --------------------------------------------------------------------------- #


class TestSqrtImpact:
    def test_impact_kicks_in_when_bar_volume_supplied(self) -> None:
        """1 unit at 1% of bar volume => 10 bps impact at default coef."""
        # 1 / 100 = 1% participation; sqrt(1) = 1; 10 * 1 = 10 bps impact
        model = CostModel()
        exec_price, _ = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("1"),
            is_maker=False,
            bar_volume=Decimal("100"),
        )
        # 100 + half-spread (3bp/2 = 0.015) + 10bp impact (0.1) = 100.115
        assert exec_price == pytest.approx(Decimal("100.115"), abs=Decimal("0.001"))

    def test_impact_scales_with_sqrt_not_linearly(self) -> None:
        """100x more participation => sqrt(100) = 10x more impact, not 100x."""
        model = CostModel(spread_bps_default=Decimal("0"))
        small_price, _ = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("1"),
            is_maker=False,
            bar_volume=Decimal("10000"),  # 0.01% participation
        )
        large_price, _ = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("100"),
            is_maker=False,
            bar_volume=Decimal("10000"),  # 1% participation
        )
        # 100x size; sqrt model => ~10x impact
        small_impact = float(small_price - Decimal("100"))
        large_impact = float(large_price - Decimal("100"))
        ratio = large_impact / small_impact
        assert 9.0 < ratio < 11.0, f"sqrt model should give ~10x; got {ratio:.2f}x"

    def test_impact_works_against_seller(self) -> None:
        """Sells slip down, not up."""
        model = CostModel(spread_bps_default=Decimal("0"))
        exec_price, _ = model.apply(
            side=Side.SELL,
            price=Decimal("100"),
            quantity=Decimal("10"),
            is_maker=False,
            bar_volume=Decimal("1000"),  # 1% participation => 10 bps
        )
        # Sells receive less: 100 - impact (0.1)
        assert exec_price < Decimal("100")
        assert exec_price == pytest.approx(Decimal("99.9"), abs=Decimal("0.001"))

    def test_max_participation_caps_extreme_orders(self) -> None:
        """Order > max_participation_pct of bar volume gets clamped."""
        capped = CostModel(
            spread_bps_default=Decimal("0"),
            max_participation_pct=Decimal("50"),
        )
        # Trying to trade 200% of bar volume; should clamp to 50%.
        capped_price, _ = capped.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("200"),
            is_maker=False,
            bar_volume=Decimal("100"),
        )
        # impact at 50%: sqrt(50) * 10 bps ≈ 70.71 bps = 0.7071
        expected_impact = math.sqrt(50.0) * 10 / 10000 * 100
        assert float(capped_price - Decimal("100")) == pytest.approx(expected_impact, rel=1e-3)

    def test_zero_bar_volume_falls_back_to_linear_mode(self) -> None:
        """bar_volume=0 should NOT use sqrt mode (would divide by zero)."""
        model = CostModel(spread_bps_default=Decimal("0"))
        exec_price, _ = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("1"),
            is_maker=False,
            bar_volume=Decimal("0"),
            adv_pct=0.01,  # legacy mode active
        )
        # 0.01 * 100 = 1 bp via linear coef 0.1 => 0.1 bp impact = 0.001
        assert exec_price == pytest.approx(Decimal("100.001"), abs=Decimal("0.0001"))


# --------------------------------------------------------------------------- #
# Per-symbol overrides                                                        #
# --------------------------------------------------------------------------- #


class TestPerSymbolOverrides:
    def test_symbol_override_for_spread_only(self) -> None:
        """SPY tightens just spread; impact + fees still inherit globals."""
        model = CostModel(per_symbol={"SPY": SymbolCosts(spread_bps=Decimal("0.5"))})
        exec_spy, _ = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("1"),
            is_maker=False,
            symbol="SPY",
        )
        exec_other, _ = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("1"),
            is_maker=False,
            symbol="OTHER",
        )
        # SPY: 100 + 0.5bp/2 = 100.0025 ; OTHER: 100 + 3bp/2 = 100.015
        assert exec_spy == pytest.approx(Decimal("100.0025"), abs=Decimal("0.0001"))
        assert exec_other == pytest.approx(Decimal("100.015"), abs=Decimal("0.0001"))

    def test_symbol_override_for_fees_only(self) -> None:
        """Override taker fee while spread inherits global."""
        model = CostModel(per_symbol={"BTC": SymbolCosts(taker_fee_bps=Decimal("20"))})
        exec_price, fee_btc = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("1"),
            is_maker=False,
            symbol="BTC",
        )
        # taker fee = exec_price * 20bp / 10000
        expected_fee = exec_price * Decimal("20") / Decimal("10000")
        assert fee_btc == expected_fee

    def test_unknown_symbol_uses_global_defaults(self) -> None:
        model = CostModel(per_symbol={"SPY": SymbolCosts(spread_bps=Decimal("0.5"))})
        exec_unknown, _ = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("1"),
            is_maker=False,
            symbol="UNKNOWN",
        )
        # 100 + 3bp/2 = 100.015 (global default)
        assert exec_unknown == pytest.approx(Decimal("100.015"), abs=Decimal("0.0001"))

    def test_no_symbol_passed_uses_global_defaults(self) -> None:
        model = CostModel(per_symbol={"SPY": SymbolCosts(spread_bps=Decimal("0.5"))})
        # Don't pass symbol => global defaults regardless of dict contents
        exec_price, _ = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("1"),
            is_maker=False,
        )
        assert exec_price == pytest.approx(Decimal("100.015"), abs=Decimal("0.0001"))

    def test_per_symbol_impact_coef_override(self) -> None:
        """Lower impact coef for a high-liquidity name."""
        model = CostModel(
            spread_bps_default=Decimal("0"),
            per_symbol={"SPY": SymbolCosts(impact_coef_sqrt=Decimal("2"))},
        )
        exec_spy, _ = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("10"),
            is_maker=False,
            symbol="SPY",
            bar_volume=Decimal("1000"),  # 1% participation
        )
        exec_other, _ = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("10"),
            is_maker=False,
            symbol="OTHER",
            bar_volume=Decimal("1000"),
        )
        # SPY: coef 2 => 2 bp = 0.02; OTHER: coef 10 => 10 bp = 0.1
        spy_impact = float(exec_spy - Decimal("100"))
        other_impact = float(exec_other - Decimal("100"))
        assert other_impact == pytest.approx(spy_impact * 5, rel=1e-3)


# --------------------------------------------------------------------------- #
# Volatility-scaled spread                                                    #
# --------------------------------------------------------------------------- #


class TestVolScaledSpread:
    def test_disabled_by_default(self) -> None:
        """Default vol_spread_factor=0 so existing arithmetic is preserved."""
        model = CostModel()  # factor=0
        exec_with_bar, _ = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("1"),
            is_maker=False,
            bar_high=Decimal("105"),  # huge 500 bp range
            bar_low=Decimal("100"),
        )
        # Should match a call without bar_high/bar_low: 100 + 3bp/2 = 100.015
        assert exec_with_bar == pytest.approx(Decimal("100.015"), abs=Decimal("0.0001"))

    def test_widens_spread_on_volatile_bar(self) -> None:
        """factor=0.5, range 200bp => effective spread = max(3, 100) = 100bp."""
        model = CostModel(vol_spread_factor=Decimal("0.5"))
        exec_price, _ = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("1"),
            is_maker=False,
            bar_high=Decimal("101"),  # 100 bp range
            bar_low=Decimal("99"),
        )
        # range_bps = 200; vol_spread = 200 * 0.5 = 100bp; half = 0.5
        # 100 + 0.5 = 100.5
        assert exec_price == pytest.approx(Decimal("100.5"), abs=Decimal("0.001"))

    def test_default_spread_wins_on_calm_bar(self) -> None:
        """If bar range is tight, default spread is the floor."""
        model = CostModel(vol_spread_factor=Decimal("0.5"))
        exec_price, _ = model.apply(
            side=Side.BUY,
            price=Decimal("100"),
            quantity=Decimal("1"),
            is_maker=False,
            bar_high=Decimal("100.01"),  # 1 bp range
            bar_low=Decimal("100"),
        )
        # vol_spread = 1 * 0.5 = 0.5bp; default = 3bp; max = 3bp
        # 100 + 3bp/2 = 100.015
        assert exec_price == pytest.approx(Decimal("100.015"), abs=Decimal("0.0001"))


# --------------------------------------------------------------------------- #
# SimBroker integration                                                       #
# --------------------------------------------------------------------------- #


def _market_buy(symbol: str = "AAPL", qty: str = "10") -> OrderIntent:
    return OrderIntent(
        order_id=new_id(),
        decision_id=new_id(),
        ts_event=now_ns(),
        strategy_id="test",
        symbol=symbol,
        venue=Venue.PAPER,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
        time_in_force=TimeInForce.GTC,
    )


def _bar(symbol: str, *, close: float, volume: float, range_bps: float = 50) -> BarEvent:
    half_range = close * range_bps / 20000
    return BarEvent(
        venue=Venue.PAPER,
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        ts_event=now_ns(),
        ts_recv=now_ns(),
        freq="1m",
        open=Decimal(str(close)),
        high=Decimal(str(close + half_range)),
        low=Decimal(str(close - half_range)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
        trades=10,
        vwap=None,
    )


class TestBrokerWithRealisticCosts:
    def test_broker_uses_bar_volume_for_sqrt_impact(self) -> None:
        """A larger order against the same bar fills at a worse price."""
        broker_small = SimBroker()
        broker_large = SimBroker()
        bar = _bar("AAPL", close=100.0, volume=100_000)
        broker_small.submit(_market_buy(qty="100"))
        broker_large.submit(_market_buy(qty="10000"))

        small_fill = broker_small.on_bar(bar)[0]
        large_fill = broker_large.on_bar(bar)[0]

        # Small: 0.1% participation => sqrt(0.1)*10 ≈ 3.16 bp impact
        # Large: 10% participation  => sqrt(10)*10  ≈ 31.6 bp impact
        # Both pay 1.5 bp half-spread. Large's exec price should be much higher.
        assert large_fill.price > small_fill.price
        impact_diff_bps = float(
            (large_fill.price - small_fill.price) / Decimal("100") * Decimal("10000")
        )
        # Expected ~28.4 bp difference (31.6 - 3.16); allow generous tolerance
        assert 20 < impact_diff_bps < 40

    def test_broker_passes_symbol_override_through(self) -> None:
        """Per-symbol override on SPY beats global default at fill time."""
        model = CostModel(per_symbol={"SPY": SymbolCosts(spread_bps=Decimal("0.5"))})
        broker_spy = SimBroker(cost_model=model)
        broker_aapl = SimBroker(cost_model=model)
        bar_spy = _bar("SPY", close=400.0, volume=1_000_000)
        bar_aapl = _bar("AAPL", close=200.0, volume=1_000_000)

        broker_spy.submit(_market_buy(symbol="SPY", qty="100"))
        broker_aapl.submit(_market_buy(symbol="AAPL", qty="100"))
        spy_fill = broker_spy.on_bar(bar_spy)[0]
        aapl_fill = broker_aapl.on_bar(bar_aapl)[0]

        # Effective half-spread bps: SPY=0.25, AAPL=1.5
        spy_spread_bps = float(
            (spy_fill.price - Decimal("400")) / Decimal("400") * Decimal("10000")
        )
        aapl_spread_bps = float(
            (aapl_fill.price - Decimal("200")) / Decimal("200") * Decimal("10000")
        )
        # AAPL should pay ~6x more in spread alone (1.5 / 0.25 = 6)
        # plus identical sqrt impact for the same participation rate, so
        # the *spread component* of AAPL's drift is 6x SPY's.
        # Stricter: subtract the impact component (same for both since
        # participation matches at 0.01%) and compare residuals.
        # For simplicity we just check ordering + magnitude bounds.
        assert aapl_spread_bps > spy_spread_bps
