from decimal import Decimal

from fincept_core.clock import FrozenClock, MonotonicClock
from fincept_core.ids import new_id


def test_monotonic_clock_returns_int_ns():
    clock = MonotonicClock()
    assert isinstance(clock.now_ns(), int)


def test_frozen_clock_returns_fixed_value():
    clock = FrozenClock(123)
    assert clock.now_ns() == 123
    assert clock.now_ns() == 123


def test_new_id_looks_like_ulid():
    value = new_id()
    assert isinstance(value, str)
    assert len(value) == 26
    assert value == value.upper()
