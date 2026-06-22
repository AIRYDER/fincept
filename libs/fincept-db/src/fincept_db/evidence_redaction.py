"""
fincept_db.evidence_redaction — redact secrets from provider evidence (TASK-0205).

The operator needs to see provider data freshness without the receipt
containing API keys, account identifiers, raw private URLs, or sensitive
payload fragments. This module provides conservative redaction that catches
token-shaped values, credential-bearing URLs, and known sensitive field names.

Design:
- ``redact_string(s)`` scans a string for known secret patterns and replaces
  them with ``[REDACTED:<pattern_name>]``.
- ``redact_dict(d)`` recursively walks a dict/list structure, redacting:
  1. Values of known sensitive keys (``api_key``, ``apiKey``, ``token``,
     ``secret``, ``password``, ``authorization``, etc.).
  2. String values that contain token-shaped substrings.
  3. URLs with embedded credentials (``https://user:pass@host/...``).
- Conservative by default: false positives (redacting non-sensitive data) are
  acceptable; false negatives (leaking a secret) are not.

File-disjoint from all active builders. New module in fincept-db.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Sensitive field names (case-insensitive match)                               #
# --------------------------------------------------------------------------- #

SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "apisecret",
        "apca_api_key_id",
        "apca_api_secret_key",
        "secret",
        "secret_key",
        "token",
        "access_token",
        "refresh_token",
        "auth_token",
        "authorization",
        "password",
        "passwd",
        "credentials",
        "private_key",
        "client_secret",
        "bearer",
    }
)


# --------------------------------------------------------------------------- #
# Token-shaped patterns (regex)                                                #
# --------------------------------------------------------------------------- #

# Bearer token: "Bearer <base64-ish string>"
_BEARER_PATTERN = re.compile(
    r"(?i)\bBearer\s+([A-Za-z0-9_\-=.+]{16,})",
)

# API key prefix patterns (common providers):
#   sk-... (OpenAI, Stripe)
#   AK... (Alpaca key ID)
#   pk_... (Stripe publishable)
_API_KEY_PREFIX_PATTERN = re.compile(
    r"\b(?:sk|pk|rk|AK)[A-Za-z0-9_\-]{12,}",
)

# Generic long alphanumeric token (32+ chars, no spaces):
# Catches JWTs, API secrets, etc.
_LONG_TOKEN_PATTERN = re.compile(
    r"\b[A-Za-z0-9_\-=.+]{32,}\b",
)

# URL with embedded credentials: https://user:pass@host/...
_CRED_URL_PATTERN = re.compile(
    r"(?i)(https?://)([^:/\s]+):([^@/\s]+)@",
)

# Query param with key/token/secret/password: ?apiKey=...&token=...
_QUERY_PARAM_PATTERN = re.compile(
    r"(?i)[?&](?:api[_-]?key|api[_-]?secret|token|secret|password|passwd|access[_-]?token)=[^&\s]+",
)

# key= / token= / password= / secret= in flat strings
_KV_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:key|token|password|passwd|secret|api[_-]?key|api[_-]?secret)\s*[:=]\s*[^\s,;\"'}\]]+",
)


# --------------------------------------------------------------------------- #
# RedactionResult                                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RedactionResult:
    """Result of a redaction operation.

    - ``redacted``: the redacted string or dict.
    - ``redaction_count``: number of redactions applied.
    - ``patterns_matched``: list of pattern names that matched (for audit).
    """

    redacted: Any
    redaction_count: int
    patterns_matched: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# String redaction                                                             #
# --------------------------------------------------------------------------- #


def redact_string(s: str) -> RedactionResult:
    """Redact token-shaped values from a string.

    Replaces matched secrets with ``[REDACTED:<pattern_name>]``.
    Conservative: may over-redact long alphanumeric strings that happen to
    look like tokens.
    """
    if not s:
        return RedactionResult(redacted=s, redaction_count=0, patterns_matched=[])

    count = 0
    patterns: list[str] = []
    result = s

    # 1. URLs with embedded credentials.
    def _cred_url_repl(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        patterns.append("credential_url")
        return f"{m.group(1)}[REDACTED:user]:[REDACTED:credential]@"

    result = _CRED_URL_PATTERN.sub(_cred_url_repl, result)

    # 2. Query params with secret names.
    def _query_param_repl(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        patterns.append("query_param_secret")
        # Preserve the param name but redact the value.
        eq_idx = m.group(0).find("=")
        if eq_idx == -1:
            return "[REDACTED:query_param]"
        return m.group(0)[: eq_idx + 1] + "[REDACTED:value]"

    result = _QUERY_PARAM_PATTERN.sub(_query_param_repl, result)

    # 3. Bearer tokens.
    def _bearer_repl(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        patterns.append("bearer_token")
        return "Bearer [REDACTED:token]"

    result = _BEARER_PATTERN.sub(_bearer_repl, result)

    # 4. API key prefixes (sk-, pk-, AK...).
    def _api_key_repl(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        patterns.append("api_key_prefix")
        return "[REDACTED:api_key]"

    result = _API_KEY_PREFIX_PATTERN.sub(_api_key_repl, result)

    # 5. key= / token= / password= / secret= in flat strings.
    def _kv_secret_repl(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        patterns.append("kv_secret")
        eq_idx = m.group(0).find("=")
        colon_idx = m.group(0).find(":")
        sep_idx = max(eq_idx, colon_idx)
        if sep_idx == -1:
            return "[REDACTED:kv_secret]"
        return m.group(0)[: sep_idx + 1] + "[REDACTED:value]"

    result = _KV_SECRET_PATTERN.sub(_kv_secret_repl, result)

    # 6. Generic long alphanumeric tokens (32+ chars).
    # This is the most aggressive pattern — it may false-positive on long
    # hashes or IDs. We apply it last so earlier, more specific patterns
    # have already replaced known secrets.
    def _long_token_repl(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        patterns.append("long_token")
        return "[REDACTED:token]"

    result = _LONG_TOKEN_PATTERN.sub(_long_token_repl, result)

    return RedactionResult(
        redacted=result,
        redaction_count=count,
        patterns_matched=patterns,
    )


# --------------------------------------------------------------------------- #
# Dict redaction                                                               #
# --------------------------------------------------------------------------- #


def redact_dict(d: Any) -> RedactionResult:
    """Recursively redact secrets from a dict/list/scalar structure.

    Redacts:
    1. Values of known sensitive keys (case-insensitive).
    2. String values containing token-shaped substrings.
    3. URLs with embedded credentials.
    """
    total_count = 0
    all_patterns: list[str] = []

    def _redact_value(key: str | None, value: Any) -> Any:
        nonlocal total_count, all_patterns

        # If the key is a sensitive field name, redact the entire value.
        if key is not None and key.lower() in SENSITIVE_FIELD_NAMES:
            total_count += 1
            all_patterns.append(f"sensitive_field:{key}")
            if isinstance(value, str):
                return "[REDACTED]"
            if isinstance(value, (int, float, bool)):
                return "[REDACTED]"
            if value is None:
                return None
            return "[REDACTED]"

        # Recurse into dicts.
        if isinstance(value, dict):
            return {k: _redact_value(k, v) for k, v in value.items()}

        # Recurse into lists.
        if isinstance(value, list):
            return [_redact_value(None, item) for item in value]

        # Redact strings.
        if isinstance(value, str):
            result = redact_string(value)
            total_count += result.redaction_count
            all_patterns.extend(result.patterns_matched)
            return result.redacted

        # Non-sensitive scalars (int, float, bool, None) pass through.
        return value

    redacted = _redact_value(None, d)
    return RedactionResult(
        redacted=redacted,
        redaction_count=total_count,
        patterns_matched=all_patterns,
    )
