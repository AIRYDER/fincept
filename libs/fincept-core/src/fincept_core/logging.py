from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, MutableMapping
from contextvars import ContextVar
from typing import Any, cast

import structlog
from structlog.stdlib import BoundLogger

from .config import get_settings

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_configured = False


def add_correlation_id(
    logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> Mapping[str, Any]:
    value = correlation_id.get()
    if value is not None:
        event_dict["correlation_id"] = value
    return event_dict


def configure() -> None:
    global _configured
    if _configured:
        return
    level = getattr(logging, get_settings().LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            add_correlation_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
    _configured = True


configure_logging = configure


def get_logger(name: str) -> BoundLogger:
    return cast(BoundLogger, structlog.get_logger(name))
