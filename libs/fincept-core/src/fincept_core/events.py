from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from . import schemas

_EVENT_SCHEMAS: dict[str, type[BaseModel]] = {
    "trade": schemas.TradeEvent,
    "book_delta": schemas.BookDeltaEvent,
    "book_snapshot": schemas.BookSnapshotEvent,
    "bar": schemas.BarEvent,
}


class Event(BaseModel):
    type: str
    payload: schemas.TradeEvent | schemas.BookDeltaEvent | schemas.BookSnapshotEvent | schemas.BarEvent


def make_event(type: str, payload: dict[str, Any], **kwargs: Any) -> Event:
    model_cls = _EVENT_SCHEMAS[type]
    model = model_cls.model_validate({**payload, **kwargs, "event_type": type})
    return Event(type=type, payload=model)


def parse_event(raw_dict: dict[str, Any]) -> Event:
    type_ = raw_dict["type"]
    payload = raw_dict["payload"]
    model_cls = _EVENT_SCHEMAS[type_]
    model = model_cls.model_validate(payload)
    return Event(type=type_, payload=model)
