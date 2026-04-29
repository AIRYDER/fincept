"""Tests for ingestor.base — VenueAdapter ABC contract."""

from __future__ import annotations

import pytest

from fincept_core.schemas import Venue
from ingestor.base import VenueAdapter


def test_cannot_instantiate_abstract_base() -> None:
    """``VenueAdapter`` is an ABC and must reject direct instantiation."""
    with pytest.raises(TypeError):
        VenueAdapter(["BTC-USDT"])  # type: ignore[abstract]


def test_subclass_must_override_all_abstract_methods() -> None:
    """A subclass missing any abstract method should also fail to instantiate."""

    class Half(VenueAdapter):
        venue = Venue.BINANCE

        async def connect(self) -> None:
            return None

        # missing stream() and close()

    with pytest.raises(TypeError):
        Half(["BTC-USDT"])  # type: ignore[abstract]


def test_minimal_concrete_subclass_constructs() -> None:
    """A subclass that implements every abstract method instantiates cleanly."""
    from collections.abc import AsyncIterator

    from pydantic import BaseModel

    class Stub(VenueAdapter):
        venue = Venue.BINANCE

        async def connect(self) -> None:
            return None

        async def stream(self) -> AsyncIterator[BaseModel]:
            if False:  # pragma: no cover - never runs
                yield BaseModel()

        async def close(self) -> None:
            return None

    obj = Stub(["BTC-USDT"])
    assert obj.symbols == ["BTC-USDT"]
    assert obj.venue == Venue.BINANCE
