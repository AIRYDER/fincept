"""DB-gated round-trip tests for fincept_db.provider_data."""

from __future__ import annotations

import pytest

from fincept_db.provider_data import (
    build_openbb_quote_record,
    read_provider_data,
    write_provider_data,
)


@pytest.mark.asyncio
async def test_write_and_read_provider_data_roundtrip() -> None:
    record = build_openbb_quote_record(
        request={"symbol": "NVDA", "provider": "yfinance"},
        response={
            "ok": True,
            "provider": "yfinance",
            "results": [{"symbol": "NVDA", "last_price": 900.12}],
        },
        ts_event=1_000,
    )

    written = await write_provider_data([record])
    assert written == 1

    rows = await read_provider_data(provider="openbb", dataset="equity.price.quote", symbol="NVDA")
    assert len(rows) == 1
    assert rows[0].record_id == record.record_id
    assert rows[0].normalized["rows"][0]["last_price"] == 900.12


@pytest.mark.asyncio
async def test_write_provider_data_upserts_on_record_id_and_ts_event() -> None:
    record = build_openbb_quote_record(
        request={"symbol": "NVDA", "provider": "yfinance"},
        response={
            "ok": True,
            "provider": "yfinance",
            "results": [{"symbol": "NVDA", "last_price": 900.12}],
        },
        ts_event=1_000,
    )

    await write_provider_data([record])
    await write_provider_data([record])

    rows = await read_provider_data(provider="openbb", dataset="equity.price.quote", symbol="NVDA")
    assert len(rows) == 1
