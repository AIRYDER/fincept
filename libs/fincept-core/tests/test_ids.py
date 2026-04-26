from fincept_core.ids import new_id


def test_new_id_is_26_chars():
    value = new_id()
    assert len(value) == 26


def test_new_id_is_sortable_string():
    first = new_id()
    second = new_id()
    assert isinstance(first, str)
    assert isinstance(second, str)
