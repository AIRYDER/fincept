"""Tests for fincept_db.evidence_redaction — secret redaction from provider evidence.

This module is compliance-critical: it prevents API keys, tokens, and other
secrets from leaking into provider evidence receipts. These tests verify
each redaction pattern works correctly and that the dict walker recurses
properly.

No database fixtures needed — evidence_redaction is pure Python.
"""

from __future__ import annotations

from fincept_db.evidence_redaction import (
    SENSITIVE_FIELD_NAMES,
    RedactionResult,
    redact_dict,
    redact_string,
)


# ---------------------------------------------------------------------------
# redact_string — individual pattern tests
# ---------------------------------------------------------------------------


class TestRedactStringBearerToken:
    def test_bearer_token_redacted(self):
        result = redact_string("Authorization: Bearer abc123def456ghi789jkl012mno345")
        assert result.redaction_count >= 1
        assert "abc123def456ghi789jkl012mno345" not in result.redacted
        assert "[REDACTED:token]" in result.redacted
        assert "bearer_token" in result.patterns_matched

    def test_bearer_token_case_insensitive(self):
        result = redact_string("bearer abc123def456ghi789jkl012mno345")
        assert result.redaction_count >= 1
        assert "abc123def456ghi789jkl012mno345" not in result.redacted

    def test_short_bearer_not_redacted(self):
        """Bearer tokens shorter than 16 chars should not be redacted."""
        result = redact_string("Bearer short")
        # "short" is 5 chars, below the 16-char threshold
        assert "short" in result.redacted


class TestRedactStringApiKeyPrefix:
    def test_sk_prefix_redacted(self):
        result = redact_string("key=sk-1234567890abcdef")
        assert result.redaction_count >= 1
        assert "sk-1234567890abcdef" not in result.redacted
        assert "api_key_prefix" in result.patterns_matched or "kv_secret" in result.patterns_matched

    def test_pk_prefix_redacted(self):
        result = redact_string("key=pk_live_1234567890abcdef")
        assert result.redaction_count >= 1
        assert "pk_live_1234567890abcdef" not in result.redacted

    def test_AK_prefix_redacted(self):
        result = redact_string("id=AK1234567890ABCDEF")
        assert result.redaction_count >= 1
        assert "AK1234567890ABCDEF" not in result.redacted


class TestRedactStringCredentialUrl:
    def test_cred_url_redacted(self):
        result = redact_string("https://user:pass123@api.example.com/data")
        assert result.redaction_count >= 1
        assert "pass123" not in result.redacted
        assert "[REDACTED:credential]" in result.redacted
        assert "credential_url" in result.patterns_matched

    def test_cred_url_preserves_scheme_and_host(self):
        result = redact_string("https://user:pass123@api.example.com/data")
        assert "https://" in result.redacted
        assert "api.example.com" in result.redacted

    def test_url_without_creds_not_redacted(self):
        result = redact_string("https://api.example.com/data")
        assert result.redaction_count == 0 or "credential_url" not in result.patterns_matched


class TestRedactStringQueryParam:
    def test_api_key_query_param_redacted(self):
        result = redact_string("https://api.example.com/data?apiKey=secret123")
        assert result.redaction_count >= 1
        assert "secret123" not in result.redacted
        assert "query_param_secret" in result.patterns_matched

    def test_token_query_param_redacted(self):
        result = redact_string("https://api.example.com/data?token=abc123def456")
        assert result.redaction_count >= 1
        assert "abc123def456" not in result.redacted

    def test_password_query_param_redacted(self):
        result = redact_string("https://api.example.com/login?password=hunter2")
        assert result.redaction_count >= 1
        assert "hunter2" not in result.redacted


class TestRedactStringKvSecret:
    def test_key_equals_redacted(self):
        result = redact_string("key=sk_test_abc123def456ghi789")
        assert result.redaction_count >= 1
        assert "sk_test_abc123def456ghi789" not in result.redacted

    def test_token_colon_redacted(self):
        result = redact_string("token: abc123def456ghi789jkl012mno345pqr")
        assert result.redaction_count >= 1
        assert "abc123def456ghi789jkl012mno345pqr" not in result.redacted

    def test_password_equals_redacted(self):
        result = redact_string("password=mySecretPass123")
        assert result.redaction_count >= 1
        assert "mySecretPass123" not in result.redacted


class TestRedactStringLongToken:
    def test_32_char_token_redacted(self):
        token = "a" * 32
        result = redact_string(f"token={token}")
        assert result.redaction_count >= 1
        assert token not in result.redacted

    def test_short_string_not_redacted(self):
        result = redact_string("hello world")
        assert result.redaction_count == 0
        assert result.redacted == "hello world"

    def test_empty_string(self):
        result = redact_string("")
        assert result.redaction_count == 0
        assert result.redacted == ""


class TestRedactStringMultiplePatterns:
    def test_multiple_secrets_in_one_string(self):
        s = "https://user:pass@host.com/api?token=abc123def456ghi789jkl012mno345pqr&key=sk-1234567890abcdef"
        result = redact_string(s)
        assert result.redaction_count >= 2
        assert "pass" not in result.redacted
        assert "abc123def456ghi789jkl012mno345pqr" not in result.redacted
        assert "sk-1234567890abcdef" not in result.redacted


# ---------------------------------------------------------------------------
# redact_dict — recursive dict walking
# ---------------------------------------------------------------------------


class TestRedactDictSensitiveFields:
    def test_api_key_field_redacted(self):
        d = {"api_key": "sk-1234567890abcdef", "name": "my_provider"}
        result = redact_dict(d)
        assert result.redacted["api_key"] == "[REDACTED]"
        assert result.redacted["name"] == "my_provider"
        assert result.redaction_count >= 1

    def test_token_field_redacted(self):
        d = {"token": "abc123def456ghi789jkl012mno345pqr", "data": "ok"}
        result = redact_dict(d)
        assert result.redacted["token"] == "[REDACTED]"
        assert result.redacted["data"] == "ok"

    def test_password_field_redacted(self):
        d = {"password": "hunter2", "user": "admin"}
        result = redact_dict(d)
        assert result.redacted["password"] == "[REDACTED]"
        assert result.redacted["user"] == "admin"

    def test_authorization_field_redacted(self):
        d = {"authorization": "Bearer abc123def456ghi789jkl"}
        result = redact_dict(d)
        assert result.redacted["authorization"] == "[REDACTED]"

    def test_case_insensitive_field_name(self):
        d = {"API_KEY": "secret123", "ApiKey": "secret456"}
        result = redact_dict(d)
        assert result.redacted["API_KEY"] == "[REDACTED]"
        assert result.redacted["ApiKey"] == "[REDACTED]"

    def test_all_sensitive_field_names_covered(self):
        """Every name in SENSITIVE_FIELD_NAMES should be redacted."""
        for name in SENSITIVE_FIELD_NAMES:
            d = {name: "some_secret_value"}
            result = redact_dict(d)
            assert result.redacted[name] == "[REDACTED]", f"Field '{name}' was not redacted!"


class TestRedactDictRecursion:
    def test_nested_dict_redacted(self):
        d = {"outer": {"inner": {"api_key": "sk-1234567890abcdef"}}}
        result = redact_dict(d)
        assert result.redacted["outer"]["inner"]["api_key"] == "[REDACTED]"
        assert result.redaction_count >= 1

    def test_list_of_dicts_redacted(self):
        d = {"providers": [{"token": "abc123"}, {"api_key": "sk-1234567890abcdef"}]}
        result = redact_dict(d)
        assert result.redacted["providers"][0]["token"] == "[REDACTED]"
        assert result.redacted["providers"][1]["api_key"] == "[REDACTED]"

    def test_deeply_nested_structure(self):
        d = {
            "level1": {
                "level2": [
                    {"level3": {"secret": "abc123def456ghi789jkl012mno345pqr"}},
                ],
            },
        }
        result = redact_dict(d)
        assert result.redacted["level1"]["level2"][0]["level3"]["secret"] == "[REDACTED]"

    def test_list_of_strings_redacted(self):
        d = {"urls": ["https://user:pass@host.com/api", "https://safe.com/api"]}
        result = redact_dict(d)
        assert "pass" not in str(result.redacted)
        assert "safe.com" in result.redacted["urls"][1]


class TestRedactDictNonSensitive:
    def test_non_sensitive_data_passes_through(self):
        d = {"name": "binance", "latency_ms": 42, "active": True}
        result = redact_dict(d)
        assert result.redaction_count == 0
        assert result.redacted == d

    def test_none_value_passes_through(self):
        d = {"name": "binance", "error": None}
        result = redact_dict(d)
        assert result.redacted["error"] is None

    def test_int_and_float_pass_through(self):
        d = {"count": 42, "rate": 3.14}
        result = redact_dict(d)
        assert result.redacted["count"] == 42
        assert result.redacted["rate"] == 3.14

    def test_bool_passes_through(self):
        d = {"active": True, "disabled": False}
        result = redact_dict(d)
        assert result.redacted["active"] is True
        assert result.redacted["disabled"] is False


class TestRedactDictStringValues:
    def test_string_with_bearer_token_redacted(self):
        d = {"header": "Authorization: Bearer abc123def456ghi789jkl012mno"}
        result = redact_dict(d)
        assert "abc123def456ghi789jkl012mno" not in result.redacted["header"]

    def test_string_with_cred_url_redacted(self):
        d = {"url": "https://user:pass123@api.example.com/data"}
        result = redact_dict(d)
        assert "pass123" not in result.redacted["url"]


# ---------------------------------------------------------------------------
# RedactionResult structure
# ---------------------------------------------------------------------------


class TestRedactionResult:
    def test_result_has_redacted_field(self):
        result = redact_string("hello")
        assert hasattr(result, "redacted")
        assert hasattr(result, "redaction_count")
        assert hasattr(result, "patterns_matched")

    def test_result_is_frozen(self):
        result = redact_string("hello")
        # RedactionResult is a frozen dataclass
        try:
            result.redaction_count = 999  # type: ignore[misc]
            raise AssertionError("RedactionResult should be frozen")
        except AttributeError:
            pass  # Expected — frozen dataclass

    def test_patterns_matched_tracks_pattern_names(self):
        result = redact_string("Bearer abc123def456ghi789jkl012mno345")
        assert "bearer_token" in result.patterns_matched

    def test_dict_result_patterns_track_field_names(self):
        result = redact_dict({"api_key": "secret"})
        assert any("sensitive_field" in p for p in result.patterns_matched)
