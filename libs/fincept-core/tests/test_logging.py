from contextvars import Token

from fincept_core import logging as fincept_logging


def test_add_correlation_id_injects_context_value():
    token: Token[str | None] = fincept_logging.correlation_id.set("cid-123")
    try:
        event = fincept_logging.add_correlation_id(None, "info", {})
        assert event["correlation_id"] == "cid-123"
    finally:
        fincept_logging.correlation_id.reset(token)
