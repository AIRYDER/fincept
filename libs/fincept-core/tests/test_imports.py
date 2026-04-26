from fincept_core import clock, config, errors, events, ids, logging, schemas, tracing


def test_import_surface():
    assert schemas.Venue.BINANCE.value == "binance"
    assert events.Event.__name__ == "Event"
    assert config.Settings.__name__ == "Settings"
    assert callable(clock.now_ns)
    assert callable(ids.new_id)
    assert errors.ContractError.__name__ == "ContractError"
    assert callable(logging.configure)
    assert callable(tracing.configure_tracing)
