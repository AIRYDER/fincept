"""End-to-end test for ``GBMStrategy``: trains a tiny LightGBM model on
synthetic momentum data, points the strategy at it, and runs through the
backtester engine.  Asserts that fills happen and the model's bias shows
up in the trade log.

This is intentionally a *small* model (50 rounds, 3 features) trained on
toy data with a known directional signal.  We don't assert on absolute
PnL — just that the integration works, the feature contract holds, and
the strategy reacts to model predictions in the expected direction."""

from __future__ import annotations

import json
import pathlib
from collections import deque
from decimal import Decimal

import lightgbm as lgb
import numpy as np
import pytest

from backtester.blotter import Blotter
from backtester.broker import SimBroker
from backtester.costs import CostModel
from backtester.datasource import BarsDataSource
from backtester.engine import BacktestEngine
from backtester.report import compute_metrics
from backtester.runner import make_bar_reader
from backtester.strategies import STRATEGY_REGISTRY, GBMStrategy
from fincept_core.schemas import (
    AssetClass,
    BarEvent,
    Fill,
    Position,
    Side,
    Venue,
)

FEATURES = ["ret_1m", "ret_5m", "rv_5m"]
N_BARS = 600
SYMBOL = "TEST"


def _make_uptrend_bars(*, n: int = N_BARS, drift: float = 0.0005) -> list[BarEvent]:
    """Synthetic close path with a small positive drift + tiny noise.

    Drift is small enough that a momentum-style classifier can pick up
    "next bar is more likely to go up if recent returns were positive"
    without being able to perfectly separate.  Used both for training
    and for the backtest replay (they're disjoint windows below)."""
    rng = np.random.default_rng(42)
    log_path = np.cumsum(rng.normal(drift, 0.001, size=n))
    return [
        BarEvent(
            venue=Venue.PAPER,
            symbol=SYMBOL,
            asset_class=AssetClass.EQUITY,
            ts_event=i * 60_000_000_000,
            ts_recv=i * 60_000_000_000,
            freq="1m",
            open=Decimal(str(100.0 * float(np.exp(log_path[i])))),
            high=Decimal(str(100.0 * float(np.exp(log_path[i])))),
            low=Decimal(str(100.0 * float(np.exp(log_path[i])))),
            close=Decimal(str(100.0 * float(np.exp(log_path[i])))),
            volume=Decimal("100"),
            trades=1,
            vwap=None,
        )
        for i in range(n)
    ]


def _train_tiny_model(model_dir: pathlib.Path, bars: list[BarEvent]) -> None:
    """Train a 3-feature LightGBM Booster on the provided bars and write
    the model.txt + meta.json artifacts the strategy expects."""
    closes = np.array([float(b.close) for b in bars])
    log_returns = np.log(closes[1:] / closes[:-1])

    horizon = 5
    n_train = len(closes) - horizon - 1
    rows: list[list[float]] = []
    labels: list[int] = []
    for i in range(5, n_train):  # need 5 bars history for ret_5m / rv_5m
        ret_1m = log_returns[i - 1]
        ret_5m = float(np.log(closes[i] / closes[i - 5]))
        rv_5m = float(np.std(log_returns[i - 5 : i], ddof=0))
        future = float(np.log(closes[i + horizon] / closes[i]))
        rows.append([ret_1m, ret_5m, rv_5m])
        labels.append(1 if future > 0 else 0)

    x = np.array(rows, dtype=np.float64)
    y = np.array(labels, dtype=np.int32)
    train_ds = lgb.Dataset(x, label=y, feature_name=FEATURES)
    booster = lgb.train(
        params={
            "objective": "binary",
            "metric": "binary_logloss",
            "verbosity": -1,
            "num_leaves": 7,
            "learning_rate": 0.1,
        },
        train_set=train_ds,
        num_boost_round=50,
    )

    model_dir.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(model_dir / "model.txt"))
    (model_dir / "meta.json").write_text(
        json.dumps(
            {
                "features": FEATURES,
                "horizon_bars": horizon,
                "horizon_ns": horizon * 60_000_000_000,
                "trained_at": 0,
                "train_rows": len(rows),
                "best_iter": 50,
            }
        )
    )


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


def test_gbm_strategy_registered() -> None:
    assert "gbm" in STRATEGY_REGISTRY
    assert STRATEGY_REGISTRY["gbm"] is GBMStrategy


def test_gbm_strategy_rejects_unsupported_features(
    tmp_path: pathlib.Path,
) -> None:
    """meta.json with order-book features must fail at on_start."""
    bars = _make_uptrend_bars(n=100)
    _train_tiny_model(tmp_path, bars)
    # Overwrite meta with bad features
    meta = json.loads((tmp_path / "meta.json").read_text())
    meta["features"] = ["book_imbalance_1", "spread_bps"]
    (tmp_path / "meta.json").write_text(json.dumps(meta))

    strategy = GBMStrategy(symbols=[SYMBOL], model_dir=tmp_path, bar_minutes=1)

    class _Ctx:
        def log(self, *_a: object, **_kw: object) -> None:
            return

    with pytest.raises(ValueError, match="cannot compute"):
        strategy.on_start(_Ctx())  # type: ignore[arg-type]


def test_gbm_strategy_missing_artifacts_raises(
    tmp_path: pathlib.Path,
) -> None:
    strategy = GBMStrategy(symbols=[SYMBOL], model_dir=tmp_path, bar_minutes=1)

    class _Ctx:
        def log(self, *_a: object, **_kw: object) -> None:
            return

    with pytest.raises(FileNotFoundError):
        strategy.on_start(_Ctx())  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Partial-fill state machine (no full engine; hand-rolled stub context)        #
# --------------------------------------------------------------------------- #


class _StubCtx:
    """Minimal StrategyContext stand-in for state-machine tests.

    Records every ``submit`` call so tests can assert on submission
    sequences without needing the full engine + broker stack.
    """

    def __init__(self) -> None:
        self.now_ns = 0
        self.positions: dict[str, Position] = {}
        self.submitted: list[object] = []

    def submit(self, intent: object) -> str:
        self.submitted.append(intent)
        return getattr(intent, "order_id", "stub")

    def cancel(self, order_id: str) -> None:
        return

    def get_feature(self, name: str, symbol: str) -> float | None:
        return None

    def log(self, *args: object, **kwargs: object) -> None:
        return


class _StubBooster:
    """LightGBM Booster stand-in returning a scripted prob_up sequence.

    Once the script is exhausted, returns 0.5 (neutral) so trailing bars
    don't accidentally trigger entries.
    """

    def __init__(self, prob_ups: list[float]) -> None:
        self._scripted = list(prob_ups)
        self._idx = 0

    def predict(self, x: object) -> list[float]:
        if self._idx >= len(self._scripted):
            return [0.5]
        value = self._scripted[self._idx]
        self._idx += 1
        return [value]


def _set_position(ctx: _StubCtx, *, symbol: str, qty: str) -> None:
    """Inject/update a position on the stub ctx (mimics what the engine
    would do after a fill via ``apply_fill_to_position``)."""
    ctx.positions[symbol] = Position(
        strategy_id="gbm.v1",
        symbol=symbol,
        quantity=Decimal(qty),
        avg_cost=Decimal("100"),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        updated_at=0,
    )


def _make_strategy_for_state_machine(*, prob_ups: list[float]) -> tuple[GBMStrategy, _StubCtx]:
    """Build a GBMStrategy with internals primed for state-machine tests
    (single feature, no model load, scripted booster).

    The ``prob_ups`` script is consumed one entry per *post-warmup* bar
    (the first bar fills the window but doesn't invoke predict).
    """
    strategy = GBMStrategy(symbols=[SYMBOL], model_dir=pathlib.Path("/nonexistent"))
    strategy._features = ["ret_1m"]
    strategy._window_bars = 2  # 1 lookback + 1 buffer
    strategy._windows = {SYMBOL: deque(maxlen=2)}
    strategy._pending_buys = {SYMBOL: Decimal(0)}
    strategy._pending_sells = {SYMBOL: Decimal(0)}
    strategy._booster = _StubBooster(prob_ups)
    return strategy, _StubCtx()


def _bar(*, close: float, ts_ns: int) -> BarEvent:
    return BarEvent(
        venue=Venue.PAPER,
        symbol=SYMBOL,
        asset_class=AssetClass.EQUITY,
        ts_event=ts_ns,
        ts_recv=ts_ns,
        freq="1m",
        open=Decimal(str(close)),
        high=Decimal(str(close)),
        low=Decimal(str(close)),
        close=Decimal(str(close)),
        volume=Decimal("100"),
        trades=1,
        vwap=None,
    )


def _fill(*, side: str, qty: str, price: str = "100") -> Fill:
    """Build a Fill the strategy's on_fill can decrement against."""
    side_enum = Side.BUY if side == "BUY" else Side.SELL
    return Fill(
        fill_id="f",
        order_id="o",
        ts_event=0,
        symbol=SYMBOL,
        side=side_enum,
        price=Decimal(price),
        quantity=Decimal(qty),
        fee=Decimal(0),
    )


def test_state_machine_does_not_pyramid_during_partial_buy() -> None:
    """Bullish signal repeats; first BUY submitted, subsequent bars wait
    until the BUY drains via on_fill."""
    # Two post-warmup bars; both bullish.
    strategy, ctx = _make_strategy_for_state_machine(prob_ups=[0.99, 0.99])
    # Warm the window so compute_features can produce a value.
    strategy.on_bar(ctx, _bar(close=100.0, ts_ns=0))
    # Bar 2: signal bullish, currently flat -> submit BUY.
    strategy.on_bar(ctx, _bar(close=101.0, ts_ns=60_000_000_000))
    assert len(ctx.submitted) == 1
    assert ctx.submitted[0].side.value == "buy"
    # pending_buys is non-zero now.
    assert strategy._pending_buys[SYMBOL] > 0

    # Bar 3: still bullish, BUY hasn't filled yet -> NO new submission.
    strategy.on_bar(ctx, _bar(close=102.0, ts_ns=120_000_000_000))
    assert len(ctx.submitted) == 1, "must not pyramid while pending BUY in flight"


def test_state_machine_re_enters_only_after_full_unwind() -> None:
    """After a full BUY+SELL round-trip, a fresh bullish signal can re-open."""
    # Four post-warmup bars: bullish, bearish, bearish, bullish.
    strategy, ctx = _make_strategy_for_state_machine(prob_ups=[0.99, 0.01, 0.01, 0.99])
    # Warmup.
    strategy.on_bar(ctx, _bar(close=100.0, ts_ns=0))
    # Bar 2 (bullish): submit BUY.
    strategy.on_bar(ctx, _bar(close=101.0, ts_ns=60_000_000_000))
    assert len(ctx.submitted) == 1
    qty_buy = ctx.submitted[0].quantity
    # Drain the BUY in one fill (engine path) and update the stub position.
    strategy.on_fill(ctx, _fill(side="BUY", qty=str(qty_buy)))
    _set_position(ctx, symbol=SYMBOL, qty=str(qty_buy))
    assert strategy._pending_buys[SYMBOL] == Decimal(0)

    # Bar 3 (bearish): submit SELL.
    strategy.on_bar(ctx, _bar(close=102.0, ts_ns=120_000_000_000))
    assert len(ctx.submitted) == 2
    assert ctx.submitted[1].side.value == "sell"
    qty_sell = ctx.submitted[1].quantity

    # Bar 4 (still bearish, SELL pending): no new SELL.
    strategy.on_bar(ctx, _bar(close=103.0, ts_ns=180_000_000_000))
    assert len(ctx.submitted) == 2

    # Drain the SELL and zero the position.
    strategy.on_fill(ctx, _fill(side="SELL", qty=str(qty_sell)))
    _set_position(ctx, symbol=SYMBOL, qty="0")
    assert strategy._pending_sells[SYMBOL] == Decimal(0)

    # Bar 5 (bullish again): now eligible to re-open.
    strategy.on_bar(ctx, _bar(close=104.0, ts_ns=240_000_000_000))
    assert len(ctx.submitted) == 3
    assert ctx.submitted[2].side.value == "buy"


def test_state_machine_close_until_flat_under_partial_sells() -> None:
    """Bearish signal + partial SELL fills => no re-submit while pending,
    but the existing SELL drains to flat over multiple ``on_fill`` calls."""
    # Six post-warmup bars: bullish (open BUY), bearish (submit SELL),
    # then four bearish bars while the SELL drains via partial fills.
    strategy, ctx = _make_strategy_for_state_machine(prob_ups=[0.99, 0.01, 0.01, 0.01, 0.01, 0.01])
    strategy.on_bar(ctx, _bar(close=100.0, ts_ns=0))
    strategy.on_bar(ctx, _bar(close=101.0, ts_ns=60_000_000_000))
    qty_buy = ctx.submitted[-1].quantity
    strategy.on_fill(ctx, _fill(side="BUY", qty=str(qty_buy)))
    _set_position(ctx, symbol=SYMBOL, qty=str(qty_buy))

    # Bearish signal -> submit a SELL for current_qty.
    strategy.on_bar(ctx, _bar(close=102.0, ts_ns=120_000_000_000))
    sell_total = ctx.submitted[-1].quantity
    assert ctx.submitted[-1].side.value == "sell"
    n_after_first_sell = len(ctx.submitted)

    # Drain the SELL in 4 partials; on each partial bar the strategy
    # must NOT submit a duplicate SELL.  Position shrinks per fill.
    partials = [
        sell_total / Decimal(4),
        sell_total / Decimal(4),
        sell_total / Decimal(4),
        sell_total / Decimal(4),
    ]
    remaining_qty = sell_total
    for i, pf in enumerate(partials):
        strategy.on_fill(ctx, _fill(side="SELL", qty=str(pf)))
        remaining_qty -= pf
        _set_position(ctx, symbol=SYMBOL, qty=str(remaining_qty))
        # Strategy sees bearish signal each remaining bar.
        strategy.on_bar(
            ctx,
            _bar(close=102.0 + i, ts_ns=180_000_000_000 + i * 60_000_000_000),
        )
        assert len(ctx.submitted) == n_after_first_sell, (
            f"unexpected new submission on partial-fill bar {i}"
        )
    # After the last fill the position is flat and pending_sells is 0.
    assert strategy._pending_sells[SYMBOL] == Decimal(0)
    assert ctx.positions[SYMBOL].quantity == Decimal(0)


def test_on_fill_clamps_pending_counter_at_zero() -> None:
    """A spurious extra fill must NOT push the counter negative."""
    # One post-warmup bar (bullish enough to submit a BUY).
    strategy, ctx = _make_strategy_for_state_machine(prob_ups=[0.99])
    strategy.on_bar(ctx, _bar(close=100.0, ts_ns=0))
    strategy.on_bar(ctx, _bar(close=101.0, ts_ns=60_000_000_000))
    qty = ctx.submitted[0].quantity
    # Two BUY fills exceed the order qty (impossible in practice; defends
    # against accumulator drift).
    strategy.on_fill(ctx, _fill(side="BUY", qty=str(qty)))
    strategy.on_fill(ctx, _fill(side="BUY", qty="1"))
    assert strategy._pending_buys[SYMBOL] == Decimal(0)


def test_on_fill_ignores_unknown_symbol() -> None:
    """Fills for a symbol the strategy doesn't track are no-ops."""
    strategy, ctx = _make_strategy_for_state_machine(prob_ups=[])
    other = Fill(
        fill_id="f",
        order_id="o",
        ts_event=0,
        symbol="UNKNOWN",
        side=Side.BUY,
        price=Decimal("100"),
        quantity=Decimal("1"),
        fee=Decimal(0),
    )
    # Should not raise even though UNKNOWN isn't in _pending_buys.
    strategy.on_fill(ctx, other)


# --------------------------------------------------------------------------- #
# End-to-end                                                                  #
# --------------------------------------------------------------------------- #


async def test_gbm_strategy_runs_through_engine_and_trades(
    tmp_path: pathlib.Path,
) -> None:
    """Train on the first 400 bars, replay the next 200; assert the
    strategy actually generates fills and ends with sensible metrics."""
    train_bars = _make_uptrend_bars(n=400)
    replay_bars = _make_uptrend_bars(n=600)[400:]  # disjoint suffix
    _train_tiny_model(tmp_path, train_bars)

    bars_by_symbol = {SYMBOL: replay_bars}
    start_ns = replay_bars[0].ts_event
    end_ns = replay_bars[-1].ts_event + 1

    datasource = BarsDataSource(
        symbols=[SYMBOL],
        freq="1m",
        start_ns=start_ns,
        end_ns=end_ns,
        bar_reader=make_bar_reader(bars_by_symbol),
    )
    strategy = GBMStrategy(
        symbols=[SYMBOL],
        model_dir=tmp_path,
        bar_minutes=1,
        per_symbol_notional=Decimal("10000"),
    )
    broker = SimBroker(cost_model=CostModel())
    blotter = Blotter(starting_cash=Decimal("100000"))
    engine = BacktestEngine(
        strategy=strategy,
        datasource=datasource,
        broker=broker,
        blotter=blotter,
    )
    await engine.run()

    report = compute_metrics(blotter, bars_per_year=525_600)
    # The model trained on uptrend data should generate at least one
    # long entry on the (also-uptrend) replay window.
    assert report.n_fills >= 1, (
        f"expected at least one fill from gbm strategy, got {report.n_fills}"
    )
    # Equity stayed within sane bounds (no negative cash blowup).
    assert report.final_equity > 50_000
    assert report.final_equity < 200_000


# --------------------------------------------------------------------------- #
# Hot-reload (reload_from_dir)                                                #
# --------------------------------------------------------------------------- #
#
# These tests cover the contract used by the live strategy host's
# model-binding watcher.  They verify atomicity (a corrupt promotion
# leaves the running model intact) and the preservation of in-flight
# order accounting (a reload is NOT a position reset).


def _build_loaded_strategy(
    model_dir: pathlib.Path,
    *,
    bars: list[BarEvent] | None = None,
) -> GBMStrategy:
    """Train a tiny model into ``model_dir``, then construct + on_start
    a strategy.  Returns the strategy ready for reload tests."""
    if bars is None:
        bars = _make_uptrend_bars(n=200)
    _train_tiny_model(model_dir, bars)
    strategy = GBMStrategy(symbols=[SYMBOL], model_dir=model_dir, bar_minutes=1)
    strategy.on_start(_StubCtx())  # type: ignore[arg-type]
    return strategy


class TestReloadFromDir:
    def test_reload_swaps_booster(self, tmp_path: pathlib.Path) -> None:
        # Two models trained on different drift directions so their
        # boosters predict different probabilities for the same input.
        # We don't assert on the prediction values (that's lightgbm's
        # contract), only that the booster object identity changed.
        dir_a = tmp_path / "model_a"
        dir_b = tmp_path / "model_b"
        _train_tiny_model(dir_a, _make_uptrend_bars(n=200, drift=0.001))
        _train_tiny_model(dir_b, _make_uptrend_bars(n=200, drift=-0.001))

        strategy = GBMStrategy(symbols=[SYMBOL], model_dir=dir_a, bar_minutes=1)
        strategy.on_start(_StubCtx())  # type: ignore[arg-type]
        old_booster = strategy._booster
        assert old_booster is not None

        strategy.reload_from_dir(dir_b)

        assert strategy._booster is not None
        assert strategy._booster is not old_booster
        assert strategy._model_dir == dir_b

    def test_reload_preserves_pending_order_state(self, tmp_path: pathlib.Path) -> None:
        # Pre-load a strategy and seed its pending-buys counter
        # (simulating an in-flight BUY mid-fill).  After reload, the
        # counter must be intact -- otherwise the no-pyramiding logic
        # would resubmit the open as soon as the next bar arrives.
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        _train_tiny_model(dir_a, _make_uptrend_bars(n=200))
        _train_tiny_model(dir_b, _make_uptrend_bars(n=200))

        strategy = _build_loaded_strategy(dir_a)
        strategy._pending_buys[SYMBOL] = Decimal("1.5")
        strategy._pending_sells[SYMBOL] = Decimal("0.7")

        strategy.reload_from_dir(dir_b)

        assert strategy._pending_buys[SYMBOL] == Decimal("1.5")
        assert strategy._pending_sells[SYMBOL] == Decimal("0.7")

    def test_reload_keeps_windows_when_window_bars_unchanged(self, tmp_path: pathlib.Path) -> None:
        # Same FEATURES list -> same window_bars -> windows preserved.
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        _train_tiny_model(dir_a, _make_uptrend_bars(n=200))
        _train_tiny_model(dir_b, _make_uptrend_bars(n=200))

        strategy = _build_loaded_strategy(dir_a)
        # Seed the window with N bars so we can assert post-reload it
        # still has the same N entries.
        seeded_bars = _make_uptrend_bars(n=10)
        for bar in seeded_bars:
            strategy._windows[SYMBOL].append(bar)
        old_len = len(strategy._windows[SYMBOL])

        strategy.reload_from_dir(dir_b)

        assert len(strategy._windows[SYMBOL]) == old_len

    def test_reload_resets_windows_when_window_bars_changes(self, tmp_path: pathlib.Path) -> None:
        # Train model A with default FEATURES (ret_1m, ret_5m, rv_5m
        # -> max lookback 5 bars).  For B, write a meta with a
        # longer-lookback feature so window_bars increases.  The
        # actual booster doesn't need to match meta features for
        # this test (we never call on_bar after reload); we only
        # need the reload code path to detect the size change.
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        _train_tiny_model(dir_a, _make_uptrend_bars(n=200))
        _train_tiny_model(dir_b, _make_uptrend_bars(n=200))
        # Patch B's meta.json so window_bars grows.
        meta_b = json.loads((dir_b / "meta.json").read_text())
        meta_b["features"] = ["ret_30m", "rv_30m"]
        (dir_b / "meta.json").write_text(json.dumps(meta_b))

        strategy = _build_loaded_strategy(dir_a)
        # Seed window with stale data; reload should drop it.
        seeded_bars = _make_uptrend_bars(n=10)
        for bar in seeded_bars:
            strategy._windows[SYMBOL].append(bar)
        old_window_bars = strategy._window_bars

        strategy.reload_from_dir(dir_b)

        assert strategy._window_bars > old_window_bars
        # Reset deque is empty: the strategy will re-warm naturally.
        assert len(strategy._windows[SYMBOL]) == 0

    def test_reload_missing_artifacts_raises_and_keeps_old(self, tmp_path: pathlib.Path) -> None:
        # Reload pointing at an empty dir must raise BEFORE touching
        # any state; old booster + features still in place.
        dir_a = tmp_path / "a"
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        _train_tiny_model(dir_a, _make_uptrend_bars(n=200))

        strategy = _build_loaded_strategy(dir_a)
        old_booster = strategy._booster
        old_features = list(strategy._features)
        old_dir = strategy._model_dir

        with pytest.raises(FileNotFoundError):
            strategy.reload_from_dir(empty_dir)

        assert strategy._booster is old_booster
        assert strategy._features == old_features
        assert strategy._model_dir == old_dir

    def test_reload_invalid_features_raises_and_keeps_old(self, tmp_path: pathlib.Path) -> None:
        # An order-book feature in meta.json must be rejected by
        # ``require_supported`` BEFORE the booster swap happens.
        dir_a = tmp_path / "a"
        dir_bad = tmp_path / "bad"
        _train_tiny_model(dir_a, _make_uptrend_bars(n=200))
        _train_tiny_model(dir_bad, _make_uptrend_bars(n=200))
        meta_bad = json.loads((dir_bad / "meta.json").read_text())
        meta_bad["features"] = ["book_imbalance_1"]
        (dir_bad / "meta.json").write_text(json.dumps(meta_bad))

        strategy = _build_loaded_strategy(dir_a)
        old_booster = strategy._booster

        with pytest.raises(ValueError, match="cannot compute"):
            strategy.reload_from_dir(dir_bad)

        # Old booster still installed despite the failed reload.
        assert strategy._booster is old_booster

    def test_reload_empty_features_list_raises(self, tmp_path: pathlib.Path) -> None:
        dir_a = tmp_path / "a"
        dir_empty_meta = tmp_path / "empty_meta"
        _train_tiny_model(dir_a, _make_uptrend_bars(n=200))
        _train_tiny_model(dir_empty_meta, _make_uptrend_bars(n=200))
        meta = json.loads((dir_empty_meta / "meta.json").read_text())
        meta["features"] = []
        (dir_empty_meta / "meta.json").write_text(json.dumps(meta))

        strategy = _build_loaded_strategy(dir_a)

        with pytest.raises(ValueError, match="missing the 'features' list"):
            strategy.reload_from_dir(dir_empty_meta)
