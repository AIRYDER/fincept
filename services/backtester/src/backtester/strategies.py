"""
backtester.strategies - reusable baseline strategies.

These are intentionally simple - each one is a "control group" you can
benchmark a real model-driven strategy against:

  - :class:`BuyAndHold`        Open a long target_notional at the first
                               bar per symbol and never trade again.
                               This is the floor any strategy should beat
                               net of costs.

  - :class:`MovingAverageCrossover`
                               Classic SMA(fast) crosses SMA(slow):
                               flip long when fast > slow, flat when
                               fast < slow.  Per-symbol; does not pyramid.

Both implement the full :class:`fincept_sdk.Strategy` ABC so the live
OMS could in principle execute them unchanged once TASK-044 lands.

Sizing rule: each opening trade targets ``per_symbol_notional`` USD.  The
broker fills market orders at the next bar's open, so position size is
derived as ``notional / bar.close`` at signal time.  This means in a
fast-moving market the realised position notional may differ slightly
from the target - that's the same behaviour the live OMS would exhibit.
"""

from __future__ import annotations

import json
import pathlib
from collections import deque
from collections.abc import Iterable
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel

from backtester.gbm_features import (
    compute_features,
    require_supported,
    required_window_bars,
)
from fincept_core.ids import new_id
from fincept_core.schemas import (
    BarEvent,
    Fill,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
    TradeEvent,
    Venue,
)
from fincept_sdk import Strategy, StrategyContext


def _market_order(
    *,
    strategy_id: str,
    symbol: str,
    side: Side,
    quantity: Decimal,
    venue: Venue,
    ts_ns: int,
) -> OrderIntent:
    """Helper: build a market OrderIntent in the canonical schema."""
    decision_id = new_id()
    order_id = new_id()
    return OrderIntent(
        order_id=order_id,
        decision_id=decision_id,
        ts_event=ts_ns,
        strategy_id=strategy_id,
        symbol=symbol,
        venue=venue,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        time_in_force=TimeInForce.GTC,
        tags={"source": "backtester"},
    )


# --------------------------------------------------------------------------- #
# BuyAndHold                                                                  #
# --------------------------------------------------------------------------- #


class BuyAndHold(Strategy):
    """Open a long position at the first bar per symbol; never trade again.

    Useful as the cost-of-doing-nothing floor: any strategy that loses
    to buy-and-hold over the same bar series isn't earning its cost
    drag.

    Constructor args:
      symbols              symbols to trade
      per_symbol_notional  USD notional to open per symbol
      venue                venue tag stamped on every order (default PAPER)
    """

    strategy_id: ClassVar[str] = "buy_and_hold.v1"
    symbols: ClassVar[list[str]] = []  # populated per-instance

    def __init__(
        self,
        symbols: Iterable[str],
        *,
        per_symbol_notional: Decimal = Decimal("10000"),
        venue: Venue = Venue.PAPER,
    ) -> None:
        # Strategy ABC declares ``symbols`` as a ClassVar; we shadow with
        # a per-instance list because each backtest may pick a different
        # universe.  mypy flags this as a ClassVar override; the runtime
        # behavior is correct and exactly what live OMS strategies will do.
        self.symbols = list(symbols)  # type: ignore[misc]
        self._per_symbol_notional = per_symbol_notional
        self._venue = venue
        self._opened: set[str] = set()

    def on_start(self, ctx: StrategyContext) -> None:
        self._opened.clear()

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
        if bar.symbol in self._opened:
            return
        if bar.symbol not in self.symbols:
            return
        if bar.close <= 0:
            return
        qty = self._per_symbol_notional / bar.close
        # Round to a reasonable precision; live venues will further
        # round to lot size.
        qty = qty.quantize(Decimal("0.000001"))
        if qty <= 0:
            return
        intent = _market_order(
            strategy_id=self.strategy_id,
            symbol=bar.symbol,
            side=Side.BUY,
            quantity=qty,
            venue=self._venue,
            ts_ns=bar.ts_event,
        )
        ctx.submit(intent)
        self._opened.add(bar.symbol)
        ctx.log("buy_and_hold.submit_open", symbol=bar.symbol, qty=str(qty))

    def on_tick(self, ctx: StrategyContext, trade: TradeEvent) -> None:
        return

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        return

    def on_signal(self, ctx: StrategyContext, signal: BaseModel) -> None:
        return

    def on_stop(self, ctx: StrategyContext) -> None:
        return


class PositionTracker(Strategy):
    strategy_id: ClassVar[str] = "position_tracker.v1"
    symbols: ClassVar[list[str]] = []

    def __init__(self, symbols: Iterable[str]) -> None:
        self.symbols = list(symbols)  # type: ignore[misc]

    def on_start(self, ctx: StrategyContext) -> None:
        return

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
        return

    def on_tick(self, ctx: StrategyContext, trade: TradeEvent) -> None:
        return

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        return

    def on_signal(self, ctx: StrategyContext, signal: BaseModel) -> None:
        return

    def on_stop(self, ctx: StrategyContext) -> None:
        return


# --------------------------------------------------------------------------- #
# MovingAverageCrossover                                                      #
# --------------------------------------------------------------------------- #


class MovingAverageCrossover(Strategy):
    """Classic SMA(fast) vs SMA(slow) crossover, per-symbol, no pyramiding.

    Goes long ``per_symbol_notional`` when fast crosses ABOVE slow.
    Closes the position (sell to flat) when fast crosses BELOW slow.
    Does NOT short - keeps the strategy long-only so risk gates that
    forbid shorting don't reject orders.

    Constructor args:
      symbols              symbols to trade
      fast                 short SMA window (number of bars)
      slow                 long SMA window (number of bars; > fast)
      per_symbol_notional  USD notional opened on each long signal
      venue                venue tag stamped on every order
    """

    strategy_id: ClassVar[str] = "ma_crossover.v1"
    symbols: ClassVar[list[str]] = []

    def __init__(
        self,
        symbols: Iterable[str],
        *,
        fast: int = 5,
        slow: int = 20,
        per_symbol_notional: Decimal = Decimal("10000"),
        venue: Venue = Venue.PAPER,
    ) -> None:
        if fast >= slow:
            raise ValueError(f"fast ({fast}) must be < slow ({slow})")
        if fast < 1 or slow < 2:
            raise ValueError("fast and slow must be positive")
        # See note in BuyAndHold.__init__ on the ClassVar override.
        self.symbols = list(symbols)  # type: ignore[misc]
        self._fast = fast
        self._slow = slow
        self._per_symbol_notional = per_symbol_notional
        self._venue = venue
        # Per-symbol close window for SMA calc.
        self._closes: dict[str, deque[Decimal]] = {}
        # Last classified state per symbol: True=long, False=flat.
        self._is_long: dict[str, bool] = {}

    def on_start(self, ctx: StrategyContext) -> None:
        self._closes.clear()
        self._is_long.clear()
        for sym in self.symbols:
            self._closes[sym] = deque(maxlen=self._slow)
            self._is_long[sym] = False

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
        if bar.symbol not in self._closes:
            return
        window = self._closes[bar.symbol]
        window.append(bar.close)
        if len(window) < self._slow:
            return  # not enough history; skip until SMA(slow) is defined.

        closes = list(window)
        slow_avg = sum(closes) / Decimal(len(closes))
        fast_window = closes[-self._fast :]
        fast_avg = sum(fast_window) / Decimal(len(fast_window))

        # Decide target state.
        want_long = fast_avg > slow_avg
        is_long = self._is_long[bar.symbol]
        if want_long == is_long:
            return

        # State transition: open a long or close to flat.
        if want_long:
            qty = self._per_symbol_notional / bar.close
            qty = qty.quantize(Decimal("0.000001"))
            if qty <= 0:
                return
            ctx.submit(
                _market_order(
                    strategy_id=self.strategy_id,
                    symbol=bar.symbol,
                    side=Side.BUY,
                    quantity=qty,
                    venue=self._venue,
                    ts_ns=bar.ts_event,
                )
            )
            self._is_long[bar.symbol] = True
            ctx.log("ma_crossover.go_long", symbol=bar.symbol, qty=str(qty))
        else:
            position = ctx.positions.get(bar.symbol)
            if position is None or position.quantity <= 0:
                # No long to close (we never opened, or already flat).
                self._is_long[bar.symbol] = False
                return
            ctx.submit(
                _market_order(
                    strategy_id=self.strategy_id,
                    symbol=bar.symbol,
                    side=Side.SELL,
                    quantity=position.quantity,
                    venue=self._venue,
                    ts_ns=bar.ts_event,
                )
            )
            self._is_long[bar.symbol] = False
            ctx.log(
                "ma_crossover.go_flat",
                symbol=bar.symbol,
                qty=str(position.quantity),
            )

    def on_tick(self, ctx: StrategyContext, trade: TradeEvent) -> None:
        return

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        return

    def on_signal(self, ctx: StrategyContext, signal: BaseModel) -> None:
        return

    def on_stop(self, ctx: StrategyContext) -> None:
        return


# --------------------------------------------------------------------------- #
# GBMStrategy                                                                 #
# --------------------------------------------------------------------------- #


class GBMStrategy(Strategy):
    """Backtest adapter for the live ``gbm_predictor`` agent.

    Loads a trained LightGBM Booster + ``meta.json`` from ``model_dir``
    and runs inference each bar against features computed from a
    rolling OHLCV window.  Mirrors the live agent's calibration:

      prob_up    = booster.predict(X)[0]                    in [0, 1]
      direction  = 2 * prob_up - 1                          in [-1, +1]
      confidence = abs(direction)                           in [0, +1]

    Trading rule (long-only, no pyramiding):
      - direction > entry_threshold AND **flat with no open BUY** -> open
        long ``per_symbol_notional``
      - direction < -exit_threshold AND **long with no open SELL** ->
        close the entire current position
      - otherwise hold (an order is still working, or already at target)

    State machine (partial-fill safe):
      The strategy never flips a static "am I long?" flag at submit
      time — doing so left residual positions stranded when a SELL
      partially-filled.  Instead, two per-symbol counters track
      outstanding-but-unfilled quantity:

        - ``_pending_buys[sym]``   total qty still working on open BUYs
        - ``_pending_sells[sym]``  total qty still working on open SELLs

      On every fill, ``on_fill`` decrements the matching counter by the
      fill quantity so multi-bar partial fills decrement the counter
      gradually until the order is complete.  The decision logic in
      ``on_bar`` reads ``ctx.positions[sym].quantity`` for actual
      position state and uses the pending counters to suppress fresh
      submissions while a previous order is still working — same
      no-pyramiding intent as before, now correct under partial fills.

    Constructor args:
      symbols              symbols to trade
      model_dir            directory containing ``model.txt`` + ``meta.json``
      bar_minutes          length of one bar in minutes (must match the
                           parquet's freq).  Used to convert feature
                           windows like ``ret_5m`` to a number of bars.
      entry_threshold      direction must exceed this to open long
                           (default 0.0 => any positive lean opens)
      exit_threshold       direction below ``-exit_threshold`` triggers
                           an exit (default 0.0 symmetric)
      per_symbol_notional  USD notional per opening trade
      venue                venue tag stamped on every order

    Restrictions:
      - The model's ``meta.json`` ``features`` list must be entirely
        OHLCV-derivable.  Supported names are
        ``<ret|rv|mom_z>_<N><m|h|d>`` where ``m`` = minutes, ``h`` =
        hours, ``d`` = calendar days (e.g., ``ret_5m``, ``rv_2h``,
        ``mom_z_20d``).  Use ``m`` for minute-bar parquets and ``d``
        for daily-bar parquets so feature lookback windows aren't
        truncated to a single bar.  A model trained with order-book
        features (e.g., ``book_imbalance_1``) raises ``ValueError`` at
        ``on_start`` — re-train on OHLCV-only features to backtest it
        here.
    """

    strategy_id: ClassVar[str] = "gbm.v1"
    symbols: ClassVar[list[str]] = []  # populated per-instance

    def __init__(
        self,
        symbols: Iterable[str],
        *,
        model_dir: pathlib.Path | str,
        bar_minutes: int = 1,
        entry_threshold: float = 0.0,
        exit_threshold: float = 0.0,
        per_symbol_notional: Decimal = Decimal("10000"),
        venue: Venue = Venue.PAPER,
    ) -> None:
        # See note in BuyAndHold.__init__ on the ClassVar override.
        self.symbols = list(symbols)  # type: ignore[misc]
        self._model_dir = pathlib.Path(model_dir)
        self._bar_minutes = int(bar_minutes)
        self._entry_threshold = float(entry_threshold)
        self._exit_threshold = float(exit_threshold)
        self._per_symbol_notional = per_symbol_notional
        self._venue = venue
        # Lazy-loaded in on_start so construction doesn't require the
        # heavy lightgbm import or a real model dir.
        self._booster: Any | None = None
        self._features: list[str] = []
        self._window_bars: int = 0
        self._windows: dict[str, deque[BarEvent]] = {}
        # Outstanding-but-unfilled qty per symbol per side.  See class
        # docstring "State machine" for the rationale.
        self._pending_buys: dict[str, Decimal] = {}
        self._pending_sells: dict[str, Decimal] = {}

    def on_start(self, ctx: StrategyContext) -> None:
        # Lazy import keeps the rest of the backtester importable on
        # systems that don't yet have lightgbm installed.
        import lightgbm as lgb

        meta_path = self._model_dir / "meta.json"
        model_path = self._model_dir / "model.txt"
        if not meta_path.is_file() or not model_path.is_file():
            raise FileNotFoundError(
                f"GBMStrategy needs model.txt + meta.json in {self._model_dir!s}"
            )
        meta = json.loads(meta_path.read_text())
        features = list(meta.get("features") or [])
        if not features:
            raise ValueError(
                f"meta.json at {meta_path} is missing the 'features' list"
            )
        # Fail fast if any feature isn't OHLCV-derivable.
        require_supported(features)
        self._features = features
        self._window_bars = required_window_bars(
            features, bar_minutes=self._bar_minutes
        )
        self._booster = lgb.Booster(model_file=str(model_path))

        self._windows = {
            sym: deque(maxlen=self._window_bars) for sym in self.symbols
        }
        self._pending_buys = {sym: Decimal(0) for sym in self.symbols}
        self._pending_sells = {sym: Decimal(0) for sym in self.symbols}
        ctx.log(
            "gbm.loaded",
            model_dir=str(self._model_dir),
            features=self._features,
            window_bars=self._window_bars,
            bar_minutes=self._bar_minutes,
        )

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
        if self._booster is None:
            return
        if bar.symbol not in self._windows:
            return
        window = self._windows[bar.symbol]
        window.append(bar)
        if len(window) < self._window_bars:
            return  # warming up
        feats = compute_features(
            list(window),
            feature_names=self._features,
            bar_minutes=self._bar_minutes,
        )
        if feats is None:
            return  # not enough history for some feature
        # Order matters: must match training order (which is what
        # meta.json's `features` list records).
        x = [[feats[name] for name in self._features]]
        prob_up = float(self._booster.predict(x)[0])
        # Defensive clamp — lightgbm may produce ~1e-9 violations.
        prob_up = max(0.0, min(1.0, prob_up))
        direction = 2.0 * prob_up - 1.0

        position = ctx.positions.get(bar.symbol)
        current_qty = position.quantity if position is not None else Decimal(0)
        pending_buy = self._pending_buys.get(bar.symbol, Decimal(0))
        pending_sell = self._pending_sells.get(bar.symbol, Decimal(0))

        # Open: signal is bullish, we currently hold nothing, and no
        # BUY or SELL is in flight.  Blocking on pending_sell prevents
        # a flapping signal from re-opening before the unwind completes.
        is_inactive_flat = (
            current_qty == 0 and pending_buy == 0 and pending_sell == 0
        )
        # Close: signal is bearish, we are actually long, and no SELL
        # is already working (so we don't double up the unwind).
        # ``pending_buy == 0`` ensures we don't start unwinding while a
        # BUY leg is still partially filling — wait for it to complete
        # rather than getting flat-then-long-again loops.
        is_inactive_long = (
            current_qty > 0 and pending_buy == 0 and pending_sell == 0
        )

        if is_inactive_flat and direction > self._entry_threshold:
            self._open_long(ctx, bar, direction)
        elif is_inactive_long and direction < -self._exit_threshold:
            self._close_long(ctx, bar, direction, current_qty)

    def _open_long(
        self, ctx: StrategyContext, bar: BarEvent, direction: float
    ) -> None:
        if bar.close <= 0:
            return
        qty = self._per_symbol_notional / bar.close
        qty = qty.quantize(Decimal("0.000001"))
        if qty <= 0:
            return
        ctx.submit(
            _market_order(
                strategy_id=self.strategy_id,
                symbol=bar.symbol,
                side=Side.BUY,
                quantity=qty,
                venue=self._venue,
                ts_ns=bar.ts_event,
            )
        )
        # Record outstanding qty so on_bar suppresses re-submission until
        # this BUY drains via on_fill (one or many partial fills).
        self._pending_buys[bar.symbol] = (
            self._pending_buys.get(bar.symbol, Decimal(0)) + qty
        )
        ctx.log(
            "gbm.go_long",
            symbol=bar.symbol,
            qty=str(qty),
            direction=f"{direction:+.4f}",
        )

    def _close_long(
        self,
        ctx: StrategyContext,
        bar: BarEvent,
        direction: float,
        current_qty: Decimal,
    ) -> None:
        # ``current_qty`` is supplied by ``on_bar`` (already non-zero).
        # Re-deriving from ctx.positions here is redundant but cheap;
        # keep it as a defensive check in case a caller skips on_bar.
        position = ctx.positions.get(bar.symbol)
        if position is None or position.quantity <= 0:
            return
        sell_qty = position.quantity
        ctx.submit(
            _market_order(
                strategy_id=self.strategy_id,
                symbol=bar.symbol,
                side=Side.SELL,
                quantity=sell_qty,
                venue=self._venue,
                ts_ns=bar.ts_event,
            )
        )
        self._pending_sells[bar.symbol] = (
            self._pending_sells.get(bar.symbol, Decimal(0)) + sell_qty
        )
        ctx.log(
            "gbm.go_flat",
            symbol=bar.symbol,
            qty=str(sell_qty),
            direction=f"{direction:+.4f}",
        )
        # Reference current_qty so callers that pass it from on_bar
        # don't trigger an unused-arg lint; behaviour is unchanged.
        del current_qty

    def on_tick(self, ctx: StrategyContext, trade: TradeEvent) -> None:
        return

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        # Decrement the pending counter that matches this fill.  Under
        # partial fills this fires once per partial, draining the
        # counter to zero exactly when the original order is complete
        # (assuming no FOK rejects — this strategy uses GTC, so no FOK).
        if fill.symbol not in self._pending_buys:
            return
        if fill.side == Side.BUY:
            remaining = self._pending_buys[fill.symbol] - fill.quantity
            self._pending_buys[fill.symbol] = max(remaining, Decimal(0))
        elif fill.side == Side.SELL:
            remaining = self._pending_sells[fill.symbol] - fill.quantity
            self._pending_sells[fill.symbol] = max(remaining, Decimal(0))

    def on_signal(self, ctx: StrategyContext, signal: BaseModel) -> None:
        return

    def on_stop(self, ctx: StrategyContext) -> None:
        return

    # ------ hot-reload protocol ----------------------------------------- #
    #
    # Called by the live strategy host (services/strategy_host) when an
    # operator promotes a new model under the same ``model_binding``.
    # Atomic: either the new artifacts load cleanly and replace the old
    # ones in one step, or the call raises and the strategy keeps using
    # its previous model.  In-flight order accounting
    # (``_pending_buys`` / ``_pending_sells``) is intentionally
    # preserved across the swap -- a model reload is NOT a position
    # reset.

    def reload_from_dir(self, model_dir: pathlib.Path | str) -> None:
        """Hot-reload booster + meta from ``model_dir`` atomically.

        Validates the new artifacts BEFORE touching live strategy
        state so a corrupt promotion never silently disables an
        active strategy.  Specifically:

          1. Read & parse meta.json (raises on missing / invalid).
          2. Validate the feature list is non-empty and that every
             feature is OHLCV-derivable (the backtester-only contract).
          3. Compute the new ``window_bars`` from the feature set.
          4. Load the new Booster.
          5. Only after all four succeed, swap into ``self``.

        Window resize semantics:

          * If ``window_bars`` is unchanged, existing bar windows
            keep their data and trading can resume on the next bar
            without a re-warm gap.
          * If ``window_bars`` changes (a feature with a longer or
            shorter lookback was added / removed), windows are
            reset to fresh empty deques.  ``on_bar`` then naturally
            no-ops until each window refills, the same as cold-start.

        Pending-order state is preserved unconditionally:

          * ``_pending_buys`` and ``_pending_sells`` are NOT touched.
            An order that was working at reload time will still
            decrement the right counter when its fill arrives, so
            the no-pyramiding state machine remains correct.

        Raises
        ------
        FileNotFoundError
            If ``model.txt`` or ``meta.json`` is missing.
        ValueError
            If meta.json has no ``features`` list or contains a
            feature the backtester can't derive from OHLCV alone.
        """
        # Lazy import keeps the rest of the backtester importable on
        # systems that don't yet have lightgbm installed -- same as
        # ``on_start``.
        import lightgbm as lgb

        new_dir = pathlib.Path(model_dir)
        meta_path = new_dir / "meta.json"
        model_path = new_dir / "model.txt"
        if not meta_path.is_file() or not model_path.is_file():
            raise FileNotFoundError(
                f"GBMStrategy.reload: missing artifacts in {new_dir!s}"
            )
        meta = json.loads(meta_path.read_text())
        new_features = list(meta.get("features") or [])
        if not new_features:
            raise ValueError(
                f"meta.json at {meta_path} is missing the 'features' list"
            )
        require_supported(new_features)
        new_window_bars = required_window_bars(
            new_features, bar_minutes=self._bar_minutes
        )
        # Load the booster LAST so a parse / validation failure
        # above doesn't leave us with an orphaned booster object.
        new_booster = lgb.Booster(model_file=str(model_path))

        # ---- atomic swap: every assignment below is to an attribute
        # already initialised in ``__init__`` / ``on_start``, so a
        # caller mid-on_bar that reads any one of these never sees a
        # mismatch between (e.g.) ``_features`` and ``_booster``.
        # The single-threaded cooperative scheduling of asyncio means
        # the host never interleaves an on_bar call with a reload call;
        # this swap block runs to completion without yielding.
        self._booster = new_booster
        self._features = new_features
        self._model_dir = new_dir
        if new_window_bars != self._window_bars:
            self._window_bars = new_window_bars
            self._windows = {
                sym: deque(maxlen=new_window_bars) for sym in self.symbols
            }


# --------------------------------------------------------------------------- #
# Registry                                                                    #
# --------------------------------------------------------------------------- #


STRATEGY_REGISTRY: dict[str, Any] = {
    "buy_and_hold": BuyAndHold,
    "position_tracker": PositionTracker,
    "ma_crossover": MovingAverageCrossover,
    "gbm": GBMStrategy,
}
"""Map of CLI / API string keys to Strategy classes for runner-driven runs."""
