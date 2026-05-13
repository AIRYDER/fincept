"""
strategy_host.runtime — LiveStrategyContext: the per-strategy
``StrategyContext`` implementation the live runner injects into
strategy hooks.

Why this exists separately from the runner
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The strategy ABC defines a small protocol (``submit``, ``cancel``,
``get_feature``, ``log``, ``positions``, ``now_ns``).  The backtester
already has a similar context internally; the live host needs its
own implementation that publishes to the bus instead of an in-memory
broker.

Keeping ``LiveStrategyContext`` in a separate module lets us:

  * unit-test it without spinning up the full runner / consumer loop;
  * compose it from the runner (which owns the redis connection)
    without circular imports;
  * swap it out per strategy class if a future class needs a richer
    context (e.g., a feature-cache reader).

Submit semantics
~~~~~~~~~~~~~~~~

The ``StrategyContext.submit`` protocol is **synchronous**: a
strategy hook (``on_bar``, ``on_fill``) is sync code and calls
``ctx.submit(intent)`` inline.  The bus ``Producer.publish`` is
**async**.  We can't fire-and-forget from sync code without losing
ordering and crash-safety guarantees.

Resolution: ``submit`` enqueues the intent into a per-context list.
The runner drains the list immediately after each hook returns and
publishes in submission order.  This means:

  * Hooks remain sync (matches ABC).
  * Order-of-submission is preserved end-to-end.
  * If the host crashes between hook and drain, no order is on the
    wire -- the strategy can re-submit on restart from a fresh
    bar.

Cancel semantics
~~~~~~~~~~~~~~~~

The protocol exposes ``cancel(order_id)``.  The OMS doesn't yet
expose a cancel stream (TASK-066 territory) so the F3 implementation
just logs the request.  Strategies that depend on cancel (none of
the three current ones do) will find this stub when they're
introduced.

Feature reads
~~~~~~~~~~~~~

``get_feature`` is a no-op in F3.  All current strategies compute
features themselves from their bar window (``MovingAverageCrossover``
SMAs, ``GBMStrategy`` GBM features).  When a future strategy needs
the cached online feature frames published by the features service,
this implementation gets a Redis hash reader.
"""

from __future__ import annotations

from typing import Any

from fincept_core.schemas import OrderIntent, Position


class LiveStrategyContext:
    """Implements the ``StrategyContext`` runtime protocol for the host.

    Constructed once per strategy runner; mutated as events flow:

      * ``now_ns`` is set to the event timestamp before each hook call.
      * ``positions`` is updated as Position events for this
        ``strategy_id`` are observed on the bus.
      * ``_pending_submits`` collects OrderIntents from ``submit``
        calls inside a hook; the runner drains it after the hook
        returns.

    The runner is the only writer to ``positions`` and the only
    reader of ``_pending_submits``.  Strategy code only calls the
    public protocol methods.
    """

    def __init__(
        self,
        *,
        strategy_id: str,
        log: Any,
    ) -> None:
        self.strategy_id = strategy_id
        # Mutable per-event state; the runner sets these before each
        # hook call.
        self.now_ns: int = 0
        self.positions: dict[str, Position] = {}
        # Pending submit queue (drained by the runner after each hook).
        self._pending_submits: list[OrderIntent] = []
        # Bound logger -- structlog-compatible.  We keep the duck-type
        # rather than a hard import on structlog so unit tests can
        # pass a stdlib Logger.
        self._log = log

    # ------ StrategyContext protocol ------------------------------------ #

    def submit(self, intent: OrderIntent) -> str:
        """Enqueue an OrderIntent for the runner to publish.

        Returns the intent's ``order_id`` so the strategy can track
        it locally (e.g., for cancellation in a future phase).
        """
        self._pending_submits.append(intent)
        return intent.order_id

    def cancel(self, order_id: str) -> None:
        """Stub: cancel-by-id isn't yet plumbed through the OMS.

        Logs at INFO so the operator sees the intent without a
        crash.  When TASK-066 (cancel stream) lands, this becomes
        a publish to ``ord.cancels``.
        """
        self._log.info(
            "strategy.cancel_unsupported",
            strategy_id=self.strategy_id,
            order_id=order_id,
        )

    def get_feature(self, name: str, symbol: str) -> float | None:
        """No-op in F3.

        All current strategies derive features from bars internally;
        a future strategy that wants the features-service cache will
        get a Redis hash reader here.
        """
        del name, symbol  # silence unused-arg until implemented
        return None

    def log(self, msg: str, **kwargs: Any) -> None:
        """Strategy-scoped log line.  Stamps ``strategy_id`` so a
        single log stream from the host can be filtered per
        strategy without round-tripping through the message text.
        """
        self._log.info(msg, strategy_id=self.strategy_id, **kwargs)

    # ------ runner-only helpers ----------------------------------------- #

    def drain_submits(self) -> list[OrderIntent]:
        """Pop and return all pending OrderIntents.

        Used by the runner immediately after each strategy hook
        returns.  Returning a list (not the live deque) so the
        caller can iterate without worrying about concurrent
        modification if a future hook becomes async.
        """
        out = list(self._pending_submits)
        self._pending_submits.clear()
        return out

    def update_position(self, position: Position) -> None:
        """Runner-only: install a Position observed on the bus.

        We don't expose this on the StrategyContext protocol
        because strategies aren't supposed to mutate positions
        themselves -- the engine does it from fills (in backtest)
        and the bus does it from the portfolio service (live).
        """
        if position.strategy_id != self.strategy_id:
            return
        self.positions[position.symbol] = position
