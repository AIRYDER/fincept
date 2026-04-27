from __future__ import annotations

import pytest

from fincept_db import audit


@pytest.mark.asyncio
async def test_append_returns_ulid() -> None:
    event_id = await audit.append(
        "orchestrator",
        "decision",
        {"strategy_id": "s1", "symbol": "BTC-USD"},
        correlation_id="d-1",
    )
    assert isinstance(event_id, str)
    assert len(event_id) == 26


@pytest.mark.asyncio
async def test_read_by_correlation_returns_chronological_chain() -> None:
    correlation_id = "decision-42"
    await audit.append("orchestrator", "decision", {"step": 1}, correlation_id=correlation_id)
    await audit.append("risk", "risk_check", {"step": 2}, correlation_id=correlation_id)
    await audit.append("oms", "order", {"step": 3}, correlation_id=correlation_id)
    await audit.append("orchestrator", "decision", {"step": "other"}, correlation_id="other")

    chain = await audit.read_by_correlation(correlation_id)
    assert [entry["actor"] for entry in chain] == ["orchestrator", "risk", "oms"]
    assert [entry["payload"]["step"] for entry in chain] == [1, 2, 3]


@pytest.mark.asyncio
async def test_append_is_append_only_idempotent_on_collision() -> None:
    event_id = await audit.append("orchestrator", "decision", {"v": 1}, correlation_id="c-1")
    again = await audit.append("orchestrator", "decision", {"v": 1}, correlation_id="c-1")
    assert event_id != again

    chain = await audit.read_by_correlation("c-1")
    assert len(chain) == 2
