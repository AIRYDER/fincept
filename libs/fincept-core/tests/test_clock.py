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


def test_iso_to_ns_uses_integer_arithmetic_no_float_loss():
    """Verify iso_to_ns doesn't lose precision via float multiplication.

    For timestamps far from epoch, float64 mantissa (~15-16 significant digits)
    is too narrow for nanosecond precision (~19 digits).  The integer-arithmetic
    implementation should be exact for any whole-microsecond ISO string.
    """
    # 2030-01-01T00:00:00.000000+00:00 — far enough out that float * 1e9
    # would lose low-order digits.
    iso = "2030-01-01T00:00:00.123456+00:00"
    result = iso_to_ns(iso)
    # Expected: days since epoch * 86400 * 1e9 + 123456 * 1000
    # 2030-01-01 is 21915 days after 1970-01-01.
    expected = 21915 * 86_400 * 1_000_000_000 + 123_456 * 1_000
    assert result == expected, f"iso_to_ns({iso!r}) = {result}, expected {expected}"


def test_iso_to_ns_naive_datetime_treated_as_utc():
    """Naive datetimes (no tzinfo) should be treated as UTC."""
    iso_naive = "2023-11-14T22:13:21.123456"
    iso_utc = "2023-11-14T22:13:21.123456+00:00"
    assert iso_to_ns(iso_naive) == iso_to_ns(iso_utc)


def test_monotonic_clock_returns_epoch_ns_per_contracts_section_1():
    import time

    before = time.time_ns()
    got = MonotonicClock().now_ns()
    after = time.time_ns()
    assert before <= got <= after, (
        f"MonotonicClock returned {got}, outside epoch window [{before}, {after}]. "
        "CONTRACTS section 1 requires nanoseconds since UNIX epoch."
    )
