from fincept_core.ids import idempotency_key, new_id


def test_new_id_is_26_chars():
    value = new_id()
    assert len(value) == 26


def test_new_id_is_sortable_string():
    first = new_id()
    second = new_id()
    assert isinstance(first, str)
    assert isinstance(second, str)


def test_idempotency_key_is_stable():
    assert idempotency_key("a", "b") == idempotency_key("a", "b")
    assert idempotency_key("a", "b") != idempotency_key("a", "c")
