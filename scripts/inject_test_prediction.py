"""
scripts/inject_test_prediction.py - publish a synthetic Prediction event.

Used to smoke-test the full trading pipeline without a trained agent::

  uv run python scripts/inject_test_prediction.py --symbol BTC-USD \
      --direction 1.0 --confidence 0.5

Writes a single ``Prediction`` event to ``STREAM_SIG_PREDICT``.  The
orchestrator should consume it, the consensus + allocator should
produce an OrderIntent, the OMS should fill it (sim or alpaca depending
on FINCEPT_OMS_ROUTER), and the portfolio service should update the
PositionStore.

Tail the corresponding service windows to watch each hop fire.  After
a successful run the new position appears under strategy_id
``orchestrator.v1`` in the /positions endpoint and on the dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from redis.asyncio import Redis

from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_SIG_PREDICT
from fincept_core.clock import now_ns
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.schemas import Prediction


async def inject(
    *,
    symbol: str,
    direction: float,
    confidence: float,
    horizon_ns: int,
    agent_id: str,
) -> None:
    settings = get_settings()
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    try:
        prediction = Prediction(
            agent_id=agent_id,
            symbol=symbol,
            direction=direction,
            confidence=confidence,
            horizon_ns=horizon_ns,
            ts_event=now_ns(),
        )
        event = Event(type="prediction", payload=prediction)
        await producer.publish(STREAM_SIG_PREDICT, event)
        print("published prediction:")
        print(f"  agent_id      : {prediction.agent_id}")
        print(f"  symbol        : {prediction.symbol}")
        print(f"  direction     : {prediction.direction:+.3f}")
        print(f"  confidence    : {prediction.confidence:.3f}")
        print(f"  horizon_ns    : {prediction.horizon_ns}")
        print()
        print("Watch:")
        print("  - fincept-orchestrator window for 'orchestrator.emitted'")
        print("  - fincept-oms window for 'oms.intent.accepted' + 'oms.fill'")
        print("  - fincept-portfolio window for 'portfolio.fill'")
    finally:
        await redis.aclose()  # type: ignore[attr-defined]


def main() -> int:
    parser = argparse.ArgumentParser(prog="inject_test_prediction")
    parser.add_argument("--symbol", default="BTC-USD")
    parser.add_argument(
        "--direction",
        type=float,
        default=1.0,
        help="Signed direction in [-1, 1]; +1 = full long, -1 = full short.",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.5,
        help="Confidence in [0, 1]; only signals above the orchestrator's "
        "threshold (default 0.1) generate orders.",
    )
    parser.add_argument(
        "--horizon-min",
        type=int,
        default=15,
        help="Prediction horizon in minutes (converted to ns).",
    )
    parser.add_argument(
        "--agent-id",
        default="test_injector.v1",
        help="agent_id label to attach (kept distinct from real agents).",
    )
    args = parser.parse_args()

    horizon_ns = args.horizon_min * 60 * 1_000_000_000
    asyncio.run(
        inject(
            symbol=args.symbol,
            direction=args.direction,
            confidence=args.confidence,
            horizon_ns=horizon_ns,
            agent_id=args.agent_id,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
