from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from . import schemas
from .errors import ContractError

EventPayload = (
    schemas.TradeEvent
    | schemas.BookDeltaEvent
    | schemas.BookSnapshotEvent
    | schemas.BarEvent
    | schemas.AlertEvent
    | schemas.FeatureFrame
    | schemas.OrderIntent
    | schemas.Order
    | schemas.Fill
    | schemas.Position
    | schemas.Prediction
    | schemas.RegimeSignal
    | schemas.SentimentSignal
    | schemas.Decision
)

_EVENT_SCHEMAS: dict[str, type[EventPayload]] = {
    "trade": schemas.TradeEvent,
    "book_delta": schemas.BookDeltaEvent,
    "book_snapshot": schemas.BookSnapshotEvent,
    "bar": schemas.BarEvent,
    "alert": schemas.AlertEvent,
    "feature_frame": schemas.FeatureFrame,
    "order_intent": schemas.OrderIntent,
    "order": schemas.Order,
    "fill": schemas.Fill,
    "position": schemas.Position,
    "prediction": schemas.Prediction,
    "regime": schemas.RegimeSignal,
    "sentiment": schemas.SentimentSignal,
    "decision": schemas.Decision,
}


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: str
    payload: EventPayload


def _payload_model(type_: str) -> type[EventPayload]:
    model_cls = _EVENT_SCHEMAS.get(type_)
    if model_cls is None:
        raise ContractError(f"unknown event type: {type_}")
    return model_cls


def make_event(type: str, payload: dict[str, Any] | BaseModel, **kwargs: Any) -> Event:
    model_cls = _payload_model(type)
    payload_dict = payload.model_dump() if isinstance(payload, BaseModel) else dict(payload)
    payload_dict.update(kwargs)
    # Only set event_type for schemas that declare it (market events, alerts,
    # feature frames).  Order / Fill / Position etc. don't define event_type
    # and would reject it under ``extra="forbid"``.
    if "event_type" in model_cls.model_fields:
        payload_dict["event_type"] = type
    model = model_cls.model_validate(payload_dict)
    return Event(type=type, payload=model)


def parse_event(raw_dict: dict[str, Any]) -> Event:
    type_ = str(raw_dict["type"])
    payload = raw_dict["payload"]
    model_cls = _payload_model(type_)
    if isinstance(payload, bytes):
        model = model_cls.model_validate_json(payload.decode())
    elif isinstance(payload, str):
        model = model_cls.model_validate_json(payload)
    else:
        model = model_cls.model_validate(payload)
    return Event(type=type_, payload=model)


def serialize(event: Event, event_id: str, published_at: int) -> dict[str, str]:
    return {
        "event_id": event_id,
        "published_at": str(published_at),
        "type": event.type,
        "payload": event.payload.model_dump_json(),
    }


def deserialize(fields: Mapping[str | bytes, str | bytes]) -> Event:
    decoded = {
        key.decode() if isinstance(key, bytes) else key: value.decode()
        if isinstance(value, bytes)
        else value
        for key, value in fields.items()
    }
    return parse_event({"type": decoded["type"], "payload": decoded["payload"]})
    return parse_event({"type": decoded["type"], "payload": decoded["payload"]})
