"""
TDD tests for provider evidence redaction + freshness receipts (TASK-0205).

The operator needs to see "Binance data is 2 seconds stale, Polygon is 30
seconds stale" without the receipt containing API keys or raw private URLs.

Acceptance criteria:
- Provider evidence proves freshness without leaking secrets.
- Redaction tests catch token-shaped values.
- Dashboard can show data freshness and provider degradation.

Scope: redaction logic + receipt schema + tests. OMS integration and
dashboard components are follow-up work.
"""

from __future__ import annotations

import json
import time

from fincept_db.evidence_redaction import (
    RedactionResult,
    redact_dict,
    redact_string,
)
from fincept_db.provider_receipts import (
    build_evidence_receipt,
    freshness_from_age_sec,
)

# --------------------------------------------------------------------------- #
# Redaction — string level                                                     #
# --------------------------------------------------------------------------- #


class TestRedactString:
    def test_api_key_is_redacted(self):
        s = "Authorization: Bearer sk-abc123def456ghi789"
        result = redact_string(s)
        assert "sk-abc123def456ghi789" not in result.redacted
        assert result.redaction_count >= 1

    def test_bearer_token_is_redacted(self):
        s = "Bearer dXNlcjpwYXNzMTIzNDU2Nzg5"
        result = redact_string(s)
        assert "dXNlcjpwYXNzMTIzNDU2Nzg5" not in result.redacted
        assert result.redaction_count >= 1

    def test_private_url_is_redacted(self):
        s = "https://user:pass@api.example.com/v1/data?key=secret123"
        result = redact_string(s)
        assert "pass" not in result.redacted
        assert "secret123" not in result.redacted
        assert result.redaction_count >= 1

    def test_query_param_api_key_is_redacted(self):
        s = "https://api.polygon.io/v2/aggs?apiKey=abc123XYZdef456"
        result = redact_string(s)
        assert "abc123XYZdef456" not in result.redacted
        assert result.redaction_count >= 1

    def test_non_sensitive_string_is_unchanged(self):
        s = "provider=binance dataset=bars symbol=BTCUSDT row_count=100"
        result = redact_string(s)
        assert result.redacted == s
        assert result.redaction_count == 0

    def test_empty_string(self):
        result = redact_string("")
        assert result.redacted == ""
        assert result.redaction_count == 0

    def test_multiple_secrets_in_one_string(self):
        s = "key=abc123def456ghi789 token=Bearer xyz789abc012def345 password=secret456ghi789"
        result = redact_string(s)
        assert "abc123def456ghi789" not in result.redacted
        assert "xyz789abc012def345" not in result.redacted
        assert "secret456ghi789" not in result.redacted
        assert result.redaction_count >= 3


# --------------------------------------------------------------------------- #
# Redaction — dict level                                                       #
# --------------------------------------------------------------------------- #


class TestRedactDict:
    def test_api_key_field_is_redacted(self):
        d = {"provider": "binance", "api_key": "sk-abc123def456", "rows": 100}
        result = redact_dict(d)
        assert result.redacted["provider"] == "binance"
        assert result.redacted["rows"] == 100
        assert "sk-abc123def456" not in str(result.redacted)
        assert result.redaction_count >= 1

    def test_authorization_header_is_redacted(self):
        d = {"headers": {"Authorization": "Bearer dXNlcjpwYXNz"}, "data": [1, 2, 3]}
        result = redact_dict(d)
        assert "dXNlcjpwYXNz" not in str(result.redacted)
        assert result.redacted["data"] == [1, 2, 3]
        assert result.redaction_count >= 1

    def test_nested_dict_is_redacted(self):
        d = {
            "request": {
                "url": "https://api.example.com/data",
                "params": {"token": "secret789", "limit": 100},
            },
            "response": {"rows": 50},
        }
        result = redact_dict(d)
        assert "secret789" not in str(result.redacted)
        assert result.redacted["response"]["rows"] == 50
        assert result.redacted["request"]["params"]["limit"] == 100
        assert result.redaction_count >= 1

    def test_non_sensitive_dict_is_unchanged(self):
        d = {
            "provider": "binance",
            "dataset": "bars",
            "symbol": "BTCUSDT",
            "row_count": 100,
        }
        result = redact_dict(d)
        assert result.redacted == d
        assert result.redaction_count == 0

    def test_list_values_are_redacted(self):
        d = {"items": [{"api_key": "sk-secret123"}, {"name": "ok"}]}
        result = redact_dict(d)
        assert "sk-secret123" not in str(result.redacted)
        assert result.redacted["items"][1]["name"] == "ok"
        assert result.redaction_count >= 1

    def test_password_field_is_redacted(self):
        d = {"username": "operator", "password": "hunter2pass"}
        result = redact_dict(d)
        assert "hunter2pass" not in str(result.redacted)
        assert result.redacted["username"] == "operator"
        assert result.redaction_count >= 1

    def test_secret_field_is_redacted(self):
        d = {"provider": "polygon", "secret": "abc123XYZdef456ghi789"}
        result = redact_dict(d)
        assert "abc123XYZdef456ghi789" not in str(result.redacted)
        assert result.redaction_count >= 1

    def test_token_field_is_redacted(self):
        d = {"provider": "alpaca", "token": "xyz789uvw012"}
        result = redact_dict(d)
        assert "xyz789uvw012" not in str(result.redacted)
        assert result.redaction_count >= 1


# --------------------------------------------------------------------------- #
# Freshness status                                                             #
# --------------------------------------------------------------------------- #


class TestFreshnessStatus:
    def test_fresh_age(self):
        status = freshness_from_age_sec(age_sec=2, provider="binance")
        assert status.status == "fresh"
        assert status.age_sec == 2
        assert status.provider == "binance"

    def test_stale_age(self):
        status = freshness_from_age_sec(age_sec=30, provider="polygon")
        assert status.status == "stale"
        assert status.age_sec == 30

    def test_degraded_age(self):
        status = freshness_from_age_sec(age_sec=120, provider="openbb")
        assert status.status == "degraded"
        assert status.age_sec == 120

    def test_unknown_age(self):
        status = freshness_from_age_sec(age_sec=None, provider="binance")
        assert status.status == "unknown"
        assert status.age_sec is None

    def test_custom_thresholds(self):
        status = freshness_from_age_sec(
            age_sec=45,
            provider="custom",
            fresh_threshold_sec=10,
            stale_threshold_sec=20,
            degraded_threshold_sec=40,
        )
        assert status.status == "degraded"


# --------------------------------------------------------------------------- #
# Evidence receipt                                                             #
# --------------------------------------------------------------------------- #


class TestEvidenceReceipt:
    def test_build_receipt_with_fresh_data(self):
        now = int(time.time())
        receipt = build_evidence_receipt(
            provider="binance",
            source="websocket",
            dataset="bars",
            symbol="BTCUSDT",
            ts_event=now - 2,
            ts_received=now,
            row_count=100,
            request_hash="abc123",
            request={"endpoint": "/ws/btcusdt@trade"},
            ok=True,
        )
        assert receipt.provider == "binance"
        assert receipt.freshness.status == "fresh"
        assert receipt.row_count == 100
        assert receipt.ok is True
        assert receipt.error_type is None

    def test_build_receipt_with_stale_data(self):
        now = int(time.time())
        receipt = build_evidence_receipt(
            provider="polygon",
            source="rest",
            dataset="aggs",
            symbol="AAPL",
            ts_event=now - 30,
            ts_received=now,
            row_count=50,
            request_hash="def456",
            request={"endpoint": "/v2/aggs"},
            ok=True,
        )
        assert receipt.freshness.status == "stale"
        assert receipt.freshness.age_sec == 30

    def test_build_receipt_redacts_sensitive_request(self):
        now = int(time.time())
        receipt = build_evidence_receipt(
            provider="polygon",
            source="rest",
            dataset="aggs",
            symbol="AAPL",
            ts_event=now,
            ts_received=now,
            row_count=10,
            request_hash="ghi789",
            request={
                "endpoint": "/v2/aggs",
                "apiKey": "abc123XYZdef456",
                "Authorization": "Bearer sk-secret789",
            },
            ok=True,
        )
        receipt_dict = receipt.to_dict()
        request_json = json.dumps(receipt_dict["request"])
        assert "abc123XYZdef456" not in request_json
        assert "sk-secret789" not in request_json
        assert receipt.redaction_count >= 2

    def test_build_receipt_with_error(self):
        now = int(time.time())
        receipt = build_evidence_receipt(
            provider="binance",
            source="websocket",
            dataset="bars",
            symbol="BTCUSDT",
            ts_event=now,
            ts_received=now,
            row_count=0,
            request_hash="err123",
            request={},
            ok=False,
            error_type="ConnectionError",
        )
        assert receipt.ok is False
        assert receipt.error_type == "ConnectionError"
        assert receipt.row_count == 0

    def test_receipt_to_dict_is_json_serializable(self):
        now = int(time.time())
        receipt = build_evidence_receipt(
            provider="binance",
            source="websocket",
            dataset="bars",
            symbol="BTCUSDT",
            ts_event=now,
            ts_received=now,
            row_count=100,
            request_hash="abc123",
            request={"endpoint": "/ws/btcusdt@trade"},
            ok=True,
        )
        d = receipt.to_dict()
        # Must be JSON serializable (for API responses + storage).
        json.dumps(d)

    def test_receipt_to_dict_does_not_contain_secrets(self):
        now = int(time.time())
        receipt = build_evidence_receipt(
            provider="alpaca",
            source="rest",
            dataset="bars",
            symbol="AAPL",
            ts_event=now,
            ts_received=now,
            row_count=50,
            request_hash="jkl012",
            request={
                "endpoint": "/v2/bars",
                "APCA_API_KEY_ID": "AKABC123DEF",
                "APCA_API_SECRET_KEY": "xyz789abc456def012",
            },
            ok=True,
        )
        d = receipt.to_dict()
        full_json = json.dumps(d)
        assert "AKABC123DEF" not in full_json
        assert "xyz789abc456def012" not in full_json

    def test_receipt_with_none_symbol(self):
        now = int(time.time())
        receipt = build_evidence_receipt(
            provider="exa",
            source="rest",
            dataset="research_brief",
            symbol=None,
            ts_event=now,
            ts_received=now,
            row_count=5,
            request_hash="mno345",
            request={"query": "AAPL news"},
            ok=True,
        )
        assert receipt.symbol is None
        assert receipt.to_dict()["symbol"] is None


# --------------------------------------------------------------------------- #
# RedactionResult                                                              #
# --------------------------------------------------------------------------- #


class TestRedactionResult:
    def test_result_has_redacted_and_count(self):
        result = redact_string("key=secret123abc")
        assert isinstance(result, RedactionResult)
        assert hasattr(result, "redacted")
        assert hasattr(result, "redaction_count")
        assert hasattr(result, "patterns_matched")

    def test_patterns_matched_lists_what_was_redacted(self):
        result = redact_dict({"api_key": "sk-abc123def456"})
        assert len(result.patterns_matched) >= 1
        # Each pattern matched should be a descriptive string.
        for pattern in result.patterns_matched:
            assert isinstance(pattern, str)
            assert len(pattern) > 0
