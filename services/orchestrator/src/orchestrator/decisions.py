"""
orchestrator.decisions - target rebalance -> (Decision, OrderIntent).

Two pieces:

  ``TargetState``               Dict of last-emitted signed
                                target notional per symbol.  The
                                router asks for ``delta(symbol, new)``
                                to get the rebalance amount; if it
                                exceeds the deadband, the router emits
                                an order and calls ``update`` to record
                                the new high-water mark.

                                When a Redis client is provided, the
                                state is persisted to Redis key
                                ``orchestrator:target_state`` (a hash
                                with symbol -> target_str).  On
                                construction, the state is hydrated
                                from Redis so the orchestrator doesn't
                                re-emit a burst of order intents after
                                restart.

  ``build_decision_and_intent`` Pure function: given a delta in USD
                                plus a reference price, builds the
                                canonical Decision (audit) + OrderIntent
                                (routable) pair.  Both share the same
                                ``decision_id`` so audit / blotter /
                                attribution can join across streams.

Decimal precision: quantity is computed as ``|delta| / price`` and
quantized to 8 decimals - finer than any spot/perp tick we care
about, coarser than Decimal's full precision (which would make Redis
payloads huge).  Real per-symbol tick sizes are a Phase H concern
(TASK-074 venue catalog).

Side determination: ``side = BUY`` when delta > 0 (we want to ADD
long exposure or REDUCE short), ``SELL`` when delta < 0.  Combined
with the OMS net-position math (TASK-044), this naturally handles
position flips: a target of -5000 from a current of +3000 yields a
SELL of 8000-worth, taking us from +3000 to -5000.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis

from fincept_core.ids import new_id
from fincept_core.logging import get_logger
from fincept_core.schemas import (
    Decision,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
    Venue,
)

log = get_logger(__name__)

DEFAULT_QUANTITY_QUANTUM = Decimal("0.00000001")  # 1 satoshi-equivalent

TARGET_STATE_KEY = "orchestrator:target_state"


@dataclass
class TargetState:
    """Last-emitted target notional per symbol, optionally persisted to Redis.

    When ``redis`` is provided:
      - On construction, call ``hydrate()`` to load from Redis.
      - On every ``update()`` and ``clear()``, the change is persisted.
      - If Redis fails, the in-memory state is still correct.

    When ``redis`` is None (e.g. in tests):
      - Behaves as the old in-memory-only implementation.
    """

    targets: dict[str, Decimal] = field(default_factory=dict)
    redis: Redis[Any] | None = None

    async def hydrate(self) -> None:
        """Load targets from Redis.  Call once after construction.

        If Redis fails, targets stays empty (same as old in-memory
        behavior — the orchestrator will re-emit intents for all
        symbols on the next prediction, which is the safe direction).
        """
        if self.redis is None:
            return
        try:
            raw = await self.redis.hgetall(TARGET_STATE_KEY)
            if not raw:
                return
            for symbol_bytes, target_str in raw.items():
                symbol = (
                    symbol_bytes.decode()
                    if isinstance(symbol_bytes, bytes)
                    else str(symbol_bytes)
                )
                if isinstance(target_str, bytes):
                    target_str = target_str.decode()
                try:
                    self.targets[symbol] = Decimal(target_str)
                except Exception:
                    log.warning(
                        "orchestrator.target_state.hydrate_skip",
                        symbol=symbol,
                        raw=target_str,
                    )
            if self.targets:
                log.info(
                    "orchestrator.target_state.hydrated",
                    count=len(self.targets),
                    symbols=list(self.targets),
                )
        except Exception as exc:
            log.warning(
                "orchestrator.target_state.hydrate_failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def delta(self, symbol: str, new_target: Decimal) -> Decimal:
        return new_target - self.targets.get(symbol, Decimal(0))

    def update(self, symbol: str, new_target: Decimal) -> None:
        self.targets[symbol] = new_target
        if self.redis is not None:
            self._persist(symbol, str(new_target))

    def clear(self, symbol: str) -> None:
        self.targets.pop(symbol, None)
        if self.redis is not None:
            self._persist_delete(symbol)

    def known_symbols(self) -> set[str]:
        return set(self.targets)

    def _persist(self, symbol: str, target_str: str) -> None:
        """Best-effort async persist — fire and forget.

        We create a task instead of awaiting because ``update`` is
        sync.  If the task fails, a warning is logged.
        """
        import asyncio

        async def _do_persist() -> None:
            try:
                await self.redis.hset(TARGET_STATE_KEY, symbol, target_str)  # type: ignore[union-attr]
            except Exception as exc:
                log.warning(
                    "orchestrator.target_state.persist_failed",
                    symbol=symbol,
                    error=f"{type(exc).__name__}: {exc}",
                )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do_persist())
        except RuntimeError:
            pass  # No running loop (e.g. in tests) — skip persistence.

    def _persist_delete(self, symbol: str) -> None:
        import asyncio

        async def _do_delete() -> None:
            try:
                await self.redis.hdel(TARGET_STATE_KEY, symbol)  # type: ignore[union-attr]
            except Exception as exc:
                log.warning(
                    "orchestrator.target_state.delete_failed",
                    symbol=symbol,
                    error=f"{type(exc).__name__}: {exc}",
                )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do_delete())
        except RuntimeError:
            pass


def build_decision_and_intent(
    *,
    symbol: str,
    delta_notional: Decimal,
    last_price: Decimal,
    strategy_id: str,
    ts_event: int,
    rationale: str,
    source_signals: Iterable[str],
    venue: Venue = Venue.ALPACA,
    urgency: float = 0.5,
    quantity_quantum: Decimal = DEFAULT_QUANTITY_QUANTUM,
) -> tuple[Decision, OrderIntent]:
    """Build (Decision, OrderIntent) pair sharing a fresh decision_id.

    ``delta_notional`` is signed: positive for net BUY, negative for
    net SELL.  Returns a market-order intent with GTC TIF; refining
    to limit + smart routing is the job of a future execution agent.
    """
    if last_price <= 0:
        raise ValueError(f"last_price must be positive; got {last_price}")
    if delta_notional == 0:
        raise ValueError("delta_notional must be non-zero")

    side = Side.BUY if delta_notional > 0 else Side.SELL
    quantity_raw = delta_notional.copy_abs() / last_price
    quantity = quantity_raw.quantize(quantity_quantum)
    if quantity <= 0:
        raise ValueError(
            f"computed quantity {quantity} <= 0 for delta={delta_notional} @ {last_price}"
        )

    decision_id = new_id()
    order_id = new_id()
    sources = list(source_signals)
    decision = Decision(
        decision_id=decision_id,
        ts_event=ts_event,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        target_notional_usd=delta_notional.copy_abs(),
        urgency=urgency,
        rationale=rationale,
        source_signals=sources,
    )
    intent = OrderIntent(
        order_id=order_id,
        decision_id=decision_id,
        ts_event=ts_event,
        strategy_id=strategy_id,
        symbol=symbol,
        venue=venue,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        time_in_force=TimeInForce.GTC,
        tags={"orchestrator": strategy_id},
    )
    return decision, intent
