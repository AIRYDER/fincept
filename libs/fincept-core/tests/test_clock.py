from fincept_core.clock import FrozenClock, MonotonicClock, iso_to_ns, ns_to_iso


def test_monotonic_clock_returns_int_ns():
    clock = MonotonicClock()
    assert isinstance(clock.now_ns(), int)


def test_frozen_clock_returns_fixed_value():
    clock = FrozenClock(now_ns=123)
    assert clock.now_ns() == 123
    assert clock.now_ns() == 123


def test_iso_round_trip():
    value = 1_700_000_000_123_456_789
    assert abs(iso_to_ns(ns_to_iso(value)) - value) < 1_000


def test_monotonic_clock_returns_epoch_ns_per_contracts_section_1():
    import time

    before = time.time_ns()
    got = MonotonicClock().now_ns()
    after = time.time_ns()
    assert before <= got <= after, (
        f"MonotonicClock returned {got}, outside epoch window [{before}, {after}]. "
        "CONTRACTS section 1 requires nanoseconds since UNIX epoch."
    )
