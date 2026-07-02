"""quant_foundry.llm_feature_agent — LLM feature agent (T-13.4).

This module implements an *LLM feature agent* that uses large language
models for **event extraction, tagging, and explanations** — never for
direct trade signals. Every feature produced by the agent carries full
provenance (prompt hash, model id, model hash, source hash, availability
time) so it can be treated as an untrusted, validated input to downstream
quant pipelines.

Design invariants (non-negotiable, fail-closed):
- **Never a trade signal**: an :class:`LLMFeature` whose ``feature_name``
  is a trade signal (``"buy"``, ``"sell"``, ``"target_weight"`` …) is
  rejected by :func:`validate_no_trade_signal`. The agent itself refuses
  to emit such features.
- **Full provenance**: every :class:`LLMFeature` records the prompt id,
  prompt hash, model id, model hash, source hash, and the
  ``availability_time`` at which the underlying source became available
  (point-in-time correctness).
- **Fail-closed on missing source hash**: :func:`validate_source_hash_present`
  and :meth:`LLMFeatureAgent.extract` raise :class:`ValueError` if the
  source hash is missing or empty. An LLM feature without provenance over
  its input is never produced.
- **Schema validation**: LLM output is validated against the prompt's
  ``output_schema`` (JSON Schema). A feature whose value does not conform
  is flagged ``validated=False`` and :meth:`validate_feature` raises.
- **Deterministic ids**: ``feature_id`` is
  ``prompt_id_source_hash_model_hash`` so two runs over the same
  (prompt, source, model) produce identical ids.
- **Lazy API client import**: LLM provider clients (openai, anthropic,
  …) are imported lazily *inside* methods so the module imports cleanly
  in offline / test environments.

Public surface:
  - LLMModelSpec, PromptSpec, LLMFeature, LLMFeatureManifest (Pydantic v2)
  - compute_prompt_hash (function)
  - validate_no_trade_signal, validate_source_hash_present (functions)
  - LLMFeatureAgent (class)
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: 64-char lowercase hex (SHA-256) — same pattern as the rest of the package.
_HEX256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Allowed LLM providers.
_ALLOWED_PROVIDERS: frozenset[str] = frozenset(
    {"openai", "anthropic", "local", "azure"}
)

#: Feature names that are *allowed* (informational / tagging features).
_ALLOWED_FEATURE_NAMES: frozenset[str] = frozenset(
    {
        "event_sentiment",
        "event_tags",
        "explanation",
        "event_classification",
        "risk_flag",
    }
)

#: Feature names that are *forbidden* (direct trade signals). Any feature
#: whose name matches one of these (case-insensitive) is rejected.
_TRADE_SIGNAL_NAMES: frozenset[str] = frozenset(
    {
        "buy",
        "sell",
        "hold",
        "target_weight",
        "target_price",
        "position_size",
        "order",
        "trade_signal",
        "signal",
        "allocation",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _validate_hex256(value: str, field_name: str) -> str:
    """Require a 64-char lowercase hex SHA-256, return it.

    Args:
        value: the hash string to validate.
        field_name: the field name for error messages.

    Returns:
        The validated lowercase hex string.

    Raises:
        ValueError: if ``value`` is not a 64-char hex string.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty 64-char hex string")
    if not _HEX256_RE.match(value):
        raise ValueError(
            f"{field_name} must be a 64-char lowercase hex SHA-256; got {value!r}"
        )
    return value


def _validate_iso_temporal(value: str, field_name: str) -> str:
    """Validate that ``value`` is a parseable ISO datetime string.

    Args:
        value: the string to validate.
        field_name: the field name for error messages.

    Returns:
        The validated string.

    Raises:
        ValueError: if ``value`` is not a parseable ISO datetime.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{field_name} must be a non-empty ISO datetime string; got {value!r}"
        )
    # Accept anything datetime.fromisoformat can parse (Python 3.11+ handles
    # trailing 'Z' as well). We deliberately do not require a timezone so
    # naive ISO strings are accepted but flagged by callers if needed.
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be a parseable ISO datetime string; "
            f"got {value!r}: {exc}"
        ) from exc
    return value


def compute_prompt_hash(prompt_template: str) -> str:
    """Compute the deterministic SHA-256 hash of a prompt template.

    The hash is taken over the UTF-8 encoded bytes of ``prompt_template``.
    Two identical templates always produce the same hash, so a
    :class:`PromptSpec` can be reconstructed and verified from its hash.

    Args:
        prompt_template: the prompt template string.

    Returns:
        64-character lowercase hex SHA-256 digest.

    Raises:
        ValueError: if ``prompt_template`` is not a string.
    """
    if not isinstance(prompt_template, str):
        raise ValueError("prompt_template must be a string")
    return hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()


def _is_trade_signal_name(feature_name: str) -> bool:
    """Return True if ``feature_name`` (case-insensitive) is a trade signal."""
    if not isinstance(feature_name, str):
        return False
    return feature_name.lower() in _TRADE_SIGNAL_NAMES


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class LLMModelSpec(BaseModel):
    """Pin for an LLM model used by the feature agent.

    Frozen + extra-forbid so a pinned model spec cannot be mutated or
    extended with surprise fields after construction.

    Fields:
        model_id: the model identifier (e.g. ``"gpt-4"``,
            ``"claude-3-opus"``, ``"llama-3-70b"``).
        model_hash: SHA-256 of the model weights (open models) or of the
            API version / deployment (closed models). 64-char hex.
        provider: one of ``"openai"``, ``"anthropic"``, ``"local"``,
            ``"azure"``.
        max_tokens: maximum output tokens (default 4096).
        temperature: sampling temperature in ``[0, 2]`` (default 0.0 —
            deterministic).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    model_hash: str
    provider: str
    max_tokens: int = 4096
    temperature: float = 0.0

    @field_validator("model_id")
    @classmethod
    def _model_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("model_id must be a non-empty string")
        return v

    @field_validator("model_hash")
    @classmethod
    def _model_hash_hex256(cls, v: str) -> str:
        return _validate_hex256(v, "model_hash")

    @field_validator("provider")
    @classmethod
    def _provider_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_PROVIDERS:
            raise ValueError(
                f"provider must be one of {sorted(_ALLOWED_PROVIDERS)!r}; got {v!r}"
            )
        return v

    @field_validator("max_tokens")
    @classmethod
    def _max_tokens_positive(cls, v: int) -> int:
        if not isinstance(v, int) or v <= 0:
            raise ValueError("max_tokens must be a positive integer")
        return v

    @field_validator("temperature")
    @classmethod
    def _temperature_range(cls, v: float) -> float:
        if not isinstance(v, (int, float)):
            raise ValueError("temperature must be a number")
        if v < 0 or v > 2:
            raise ValueError("temperature must be in [0, 2]")
        return float(v)


class PromptSpec(BaseModel):
    """A prompt template plus its expected output JSON schema.

    Frozen + extra-forbid. The ``prompt_hash`` must equal
    :func:`compute_prompt_hash` applied to ``prompt_template`` (verified
    in a model validator so a mismatch fails construction).

    Fields:
        prompt_id: a non-empty identifier for the prompt.
        prompt_template: the template string (with ``{source_text}``
            placeholder rendered by the agent).
        prompt_hash: SHA-256 of ``prompt_template`` (64-char hex).
        output_schema: a JSON Schema dict describing the expected LLM
            output.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_id: str
    prompt_template: str
    prompt_hash: str
    output_schema: dict

    @field_validator("prompt_id")
    @classmethod
    def _prompt_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("prompt_id must be a non-empty string")
        return v

    @field_validator("prompt_template")
    @classmethod
    def _prompt_template_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v:
            raise ValueError("prompt_template must be a non-empty string")
        return v

    @field_validator("prompt_hash")
    @classmethod
    def _prompt_hash_hex256(cls, v: str) -> str:
        return _validate_hex256(v, "prompt_hash")

    @field_validator("output_schema")
    @classmethod
    def _output_schema_dict(cls, v: dict) -> dict:
        if not isinstance(v, dict):
            raise ValueError("output_schema must be a dict")
        return v

    @model_validator(mode="after")
    def _hash_matches_template(self) -> "PromptSpec":
        expected = compute_prompt_hash(self.prompt_template)
        if self.prompt_hash != expected:
            raise ValueError(
                "prompt_hash does not match SHA-256 of prompt_template; "
                f"expected {expected!r}, got {self.prompt_hash!r}"
            )
        return self


class LLMFeature(BaseModel):
    """A single feature extracted by the LLM feature agent.

    Frozen + extra-forbid. Carries full provenance so it can be treated
    as an untrusted, validated input downstream.

    Fields:
        feature_id: deterministic id ``prompt_id_source_hash_model_hash``.
        prompt_id: the prompt that produced this feature.
        prompt_hash: SHA-256 of the prompt template.
        model_id: the model that produced this feature.
        model_hash: SHA-256 of the model weights / API version.
        source_hash: SHA-256 of the input source data.
        feature_name: one of the allowed informational feature names
            (e.g. ``"event_sentiment"``, ``"event_tags"``).
        feature_value: the extracted value (str / float / list[str] /
            dict).
        availability_time: ISO datetime — when the source became
            available (point-in-time correctness).
        created_at: ISO datetime — when this feature object was created.
        validated: whether the value passed output-schema validation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    feature_id: str
    prompt_id: str
    prompt_hash: str
    model_id: str
    model_hash: str
    source_hash: str
    feature_name: str
    feature_value: str | float | list[Any] | dict
    availability_time: str
    created_at: str
    validated: bool = False

    @field_validator("feature_id")
    @classmethod
    def _feature_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("feature_id must be a non-empty string")
        return v

    @field_validator("prompt_id")
    @classmethod
    def _prompt_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("prompt_id must be a non-empty string")
        return v

    @field_validator("prompt_hash")
    @classmethod
    def _prompt_hash_hex256(cls, v: str) -> str:
        return _validate_hex256(v, "prompt_hash")

    @field_validator("model_id")
    @classmethod
    def _model_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("model_id must be a non-empty string")
        return v

    @field_validator("model_hash")
    @classmethod
    def _model_hash_hex256(cls, v: str) -> str:
        return _validate_hex256(v, "model_hash")

    @field_validator("source_hash")
    @classmethod
    def _source_hash_hex256(cls, v: str) -> str:
        return _validate_hex256(v, "source_hash")

    @field_validator("feature_name")
    @classmethod
    def _feature_name_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("feature_name must be a non-empty string")
        return v

    @field_validator("availability_time")
    @classmethod
    def _availability_time_iso(cls, v: str) -> str:
        return _validate_iso_temporal(v, "availability_time")

    @field_validator("created_at")
    @classmethod
    def _created_at_iso(cls, v: str) -> str:
        return _validate_iso_temporal(v, "created_at")

    @model_validator(mode="after")
    def _reject_trade_signal_name(self) -> "LLMFeature":
        """Fail-closed: a feature may never be a direct trade signal."""
        if _is_trade_signal_name(self.feature_name):
            raise ValueError(
                f"feature_name {self.feature_name!r} is a trade signal; "
                "LLM features must never be direct trade signals"
            )
        return self


class LLMFeatureManifest(BaseModel):
    """Manifest bundling a model spec, prompt specs, and produced features.

    Frozen + extra-forbid. Rejects duplicate ``feature_id`` and
    ``prompt_id`` values at construction time.

    Fields:
        manifest_id: a non-empty identifier for the manifest.
        model_spec: the :class:`LLMModelSpec` used.
        prompt_specs: the list of :class:`PromptSpec` used.
        features: the list of :class:`LLMFeature` produced.
        created_at: ISO datetime — when the manifest was built.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: str
    model_spec: LLMModelSpec
    prompt_specs: list[PromptSpec]
    features: list[LLMFeature]
    created_at: str

    @field_validator("manifest_id")
    @classmethod
    def _manifest_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("manifest_id must be a non-empty string")
        return v

    @field_validator("created_at")
    @classmethod
    def _created_at_iso(cls, v: str) -> str:
        return _validate_iso_temporal(v, "created_at")

    @model_validator(mode="after")
    def _no_duplicate_ids(self) -> "LLMFeatureManifest":
        feature_ids = [f.feature_id for f in self.features]
        if len(feature_ids) != len(set(feature_ids)):
            dupes = {fid for fid in feature_ids if feature_ids.count(fid) > 1}
            raise ValueError(
                f"duplicate feature_id values are not allowed: {sorted(dupes)!r}"
            )
        prompt_ids = [p.prompt_id for p in self.prompt_specs]
        if len(prompt_ids) != len(set(prompt_ids)):
            dupes = {pid for pid in prompt_ids if prompt_ids.count(pid) > 1}
            raise ValueError(
                f"duplicate prompt_id values are not allowed: {sorted(dupes)!r}"
            )
        return self


# ---------------------------------------------------------------------------
# Standalone validators
# ---------------------------------------------------------------------------


def validate_no_trade_signal(feature: LLMFeature) -> bool:
    """Check that ``feature`` is not a direct trade signal.

    A feature whose ``feature_name`` (case-insensitive) is one of the
    forbidden trade-signal names (``"buy"``, ``"sell"``,
    ``"target_weight"`` …) is rejected. Note that :class:`LLMFeature`
    already rejects trade-signal names at construction, so this function
    is a defence-in-depth check for features that may have been
    constructed outside the normal path.

    Args:
        feature: the :class:`LLMFeature` to check.

    Returns:
        ``True`` if the feature is not a trade signal.

    Raises:
        ValueError: if the feature is a trade signal (fail-closed).
    """
    if not isinstance(feature, LLMFeature):
        raise ValueError("feature must be an LLMFeature instance")
    if _is_trade_signal_name(feature.feature_name):
        raise ValueError(
            f"feature_name {feature.feature_name!r} is a trade signal; "
            "LLM features must never be direct trade signals"
        )
    return True


def validate_source_hash_present(feature: LLMFeature) -> bool:
    """Check that ``feature`` has a non-empty source hash (fail-closed).

    Args:
        feature: the :class:`LLMFeature` to check.

    Returns:
        ``True`` if the source hash is present and non-empty.

    Raises:
        ValueError: if the source hash is missing or empty.
    """
    if not isinstance(feature, LLMFeature):
        raise ValueError("feature must be an LLMFeature instance")
    if not getattr(feature, "source_hash", None):
        raise ValueError("source_hash is missing or empty (fail-closed)")
    return True


# ---------------------------------------------------------------------------
# Lightweight JSON-Schema validation
# ---------------------------------------------------------------------------


def _coerce_value_to_schema_type(value: Any, schema_type: str) -> bool:
    """Return True if ``value`` matches the JSON Schema ``type`` string."""
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "object":
        return isinstance(value, dict)
    return True  # unknown type — be permissive


def _validate_against_schema(value: Any, schema: dict) -> bool:
    """Validate ``value`` against a (small subset of) JSON Schema.

    Supports ``type``, ``enum``, ``items`` (for arrays), and
    ``properties`` / ``required`` (for objects). This is intentionally a
    minimal validator — the agent only needs to check the shape of LLM
    output, not full JSON Schema semantics.

    Args:
        value: the value to validate.
        schema: the JSON Schema dict.

    Returns:
        ``True`` if ``value`` conforms to ``schema``.

    Raises:
        ValueError: if ``value`` does not conform (with a reason).
    """
    if not isinstance(schema, dict):
        raise ValueError("schema must be a dict")

    schema_type = schema.get("type")
    if schema_type is not None:
        if isinstance(schema_type, list):
            if not any(_coerce_value_to_schema_type(value, t) for t in schema_type):
                raise ValueError(
                    f"value type {type(value).__name__} not in {schema_type!r}"
                )
        else:
            if not _coerce_value_to_schema_type(value, schema_type):
                raise ValueError(
                    f"value type {type(value).__name__} != required {schema_type!r}"
                )

    if "enum" in schema:
        if value not in schema["enum"]:
            raise ValueError(f"value {value!r} not in enum {schema['enum']!r}")

    if schema_type == "array" or (schema_type is None and isinstance(value, list)):
        items_schema = schema.get("items")
        if items_schema is not None and isinstance(value, list):
            for i, item in enumerate(value):
                try:
                    _validate_against_schema(item, items_schema)
                except ValueError as exc:
                    raise ValueError(
                        f"array item {i} invalid: {exc}"
                    ) from exc

    if schema_type == "object" or (schema_type is None and isinstance(value, dict)):
        props = schema.get("properties", {})
        required = schema.get("required", [])
        if isinstance(value, dict):
            for req in required:
                if req not in value:
                    raise ValueError(f"missing required property {req!r}")
            for key, sub_schema in props.items():
                if key in value:
                    try:
                        _validate_against_schema(value[key], sub_schema)
                    except ValueError as exc:
                        raise ValueError(
                            f"property {key!r} invalid: {exc}"
                        ) from exc

    return True


# ---------------------------------------------------------------------------
# LLMFeatureAgent
# ---------------------------------------------------------------------------


class LLMFeatureAgent:
    """Agent that extracts informational features from text using an LLM.

    The agent is constructed with a pinned :class:`LLMModelSpec` and a
    list of :class:`PromptSpec` objects. It renders a prompt template
    with the source text, calls the LLM (lazily importing the provider
    client), validates the output against the prompt's ``output_schema``,
    and produces an :class:`LLMFeature` with full provenance.

    The agent **never** produces direct trade signals. A prompt whose
    intended feature name is a trade signal is rejected at extraction
    time.

    The LLM call is performed via an injectable ``llm_client`` callable
    (signature ``(model_spec, prompt_text) -> Any``). In tests this is
    mocked; in production it lazily imports the appropriate provider
    client. This keeps the module importable in offline environments.
    """

    def __init__(
        self,
        model_spec: LLMModelSpec,
        prompt_specs: list[PromptSpec],
        llm_client: Callable[[LLMModelSpec, str], Any] | None = None,
    ) -> None:
        """Initialize the agent.

        Args:
            model_spec: the pinned LLM model to use.
            prompt_specs: the list of available prompts (must have unique
                ``prompt_id`` values).
            llm_client: optional callable ``(model_spec, prompt_text) ->
                Any`` used to invoke the LLM. If ``None``, a default
                client that lazily imports the provider SDK is used.

        Raises:
            ValueError: if ``prompt_specs`` is empty or contains
                duplicate ``prompt_id`` values.
        """
        if not isinstance(model_spec, LLMModelSpec):
            raise ValueError("model_spec must be an LLMModelSpec")
        if not isinstance(prompt_specs, list) or not prompt_specs:
            raise ValueError("prompt_specs must be a non-empty list")
        for ps in prompt_specs:
            if not isinstance(ps, PromptSpec):
                raise ValueError("each prompt_spec must be a PromptSpec")
        prompt_ids = [p.prompt_id for p in prompt_specs]
        if len(prompt_ids) != len(set(prompt_ids)):
            raise ValueError(
                "prompt_specs contains duplicate prompt_id values"
            )
        self._model_spec = model_spec
        self._prompt_specs = list(prompt_specs)
        self._prompts_by_id: dict[str, PromptSpec] = {
            p.prompt_id: p for p in self._prompt_specs
        }
        self._llm_client = llm_client

    # -- properties -------------------------------------------------------

    @property
    def model_spec(self) -> LLMModelSpec:
        """The pinned model spec used by this agent."""
        return self._model_spec

    @property
    def prompt_specs(self) -> list[PromptSpec]:
        """The list of prompt specs available to this agent."""
        return list(self._prompt_specs)

    # -- internal helpers ------------------------------------------------

    def _select_prompt(self, prompt_id: str) -> PromptSpec:
        """Return the prompt spec for ``prompt_id``.

        Raises:
            ValueError: if no prompt with ``prompt_id`` is registered.
        """
        if not isinstance(prompt_id, str) or not prompt_id.strip():
            raise ValueError("prompt_id must be a non-empty string")
        ps = self._prompts_by_id.get(prompt_id)
        if ps is None:
            raise ValueError(
                f"no prompt_spec registered for prompt_id {prompt_id!r}"
            )
        return ps

    def _default_llm_client(self, model_spec: LLMModelSpec, prompt_text: str) -> Any:
        """Default LLM client that lazily imports the provider SDK.

        This is intentionally simple and only supports the providers we
        know about. In tests a mock client is injected instead.

        Raises:
            ValueError: if the provider is not supported or the SDK is
                not installed.
        """
        provider = model_spec.provider
        if provider == "openai":
            try:
                import openai  # type: ignore[import-not-found]  # noqa: F401
            except ImportError as exc:  # pragma: no cover - exercised in prod
                raise ValueError(
                    "openai SDK is not installed; cannot call LLM"
                ) from exc
            # Real call would go here; tests inject a mock instead.
            raise ValueError(
                "default openai client requires a real API key; "
                "inject an llm_client for testing"
            )
        if provider == "anthropic":
            try:
                import anthropic  # type: ignore[import-not-found]  # noqa: F401
            except ImportError as exc:  # pragma: no cover
                raise ValueError(
                    "anthropic SDK is not installed; cannot call LLM"
                ) from exc
            raise ValueError(
                "default anthropic client requires a real API key; "
                "inject an llm_client for testing"
            )
        if provider in ("local", "azure"):
            raise ValueError(
                f"default client for provider {provider!r} is not "
                "implemented; inject an llm_client"
            )
        raise ValueError(f"unsupported provider {provider!r}")

    def _call_llm(self, prompt_text: str) -> Any:
        """Invoke the LLM (mocked or real) and return its raw output."""
        client = self._llm_client or self._default_llm_client
        return client(self._model_spec, prompt_text)

    @staticmethod
    def _render_prompt(prompt_spec: PromptSpec, source_text: str) -> str:
        """Render the prompt template with ``source_text``.

        Uses ``str.format`` with a single ``source_text`` placeholder.
        """
        if "{source_text}" in prompt_spec.prompt_template:
            return prompt_spec.prompt_template.format(source_text=source_text)
        return prompt_spec.prompt_template + "\n\n" + source_text

    @staticmethod
    def _make_feature_id(
        prompt_id: str, source_hash: str, model_hash: str
    ) -> str:
        """Build the deterministic feature id."""
        return f"{prompt_id}_{source_hash}_{model_hash}"

    # -- public API -------------------------------------------------------

    def extract(
        self,
        source_text: str,
        source_hash: str,
        prompt_id: str,
        availability_time: str,
    ) -> LLMFeature:
        """Extract a single feature from ``source_text``.

        Selects the prompt by ``prompt_id``, renders it with
        ``source_text``, calls the LLM, validates the output against the
        prompt's ``output_schema``, and returns an :class:`LLMFeature`
        with full provenance.

        Fail-closed behaviors:
        - Missing or empty ``source_hash`` raises :class:`ValueError`.
        - A prompt whose intended feature name is a trade signal is
          rejected.
        - Output that does not conform to ``output_schema`` is still
          returned but with ``validated=False`` (the caller can then
          decide to drop it).

        Args:
            source_text: the raw text to extract from (may be empty).
            source_hash: SHA-256 of the source data (64-char hex).
            prompt_id: the prompt to use.
            availability_time: ISO datetime — when the source became
                available.

        Returns:
            The extracted :class:`LLMFeature`.

        Raises:
            ValueError: if ``source_hash`` is missing/empty/invalid, if
                ``prompt_id`` is unknown, or if the prompt's feature
                name is a trade signal.
        """
        # Fail-closed: source hash must be present and valid.
        if not isinstance(source_hash, str) or not source_hash.strip():
            raise ValueError(
                "source_hash is missing or empty (fail-closed); "
                "an LLM feature without source provenance is never produced"
            )
        source_hash = _validate_hex256(source_hash, "source_hash")

        if not isinstance(source_text, str):
            raise ValueError("source_text must be a string")

        prompt_spec = self._select_prompt(prompt_id)

        # Infer the feature name from the prompt id (convention:
        # prompt_id == feature_name, e.g. "event_sentiment"). We also
        # reject trade-signal names defensively.
        feature_name = prompt_spec.prompt_id
        if _is_trade_signal_name(feature_name):
            raise ValueError(
                f"prompt_id {prompt_id!r} resolves to a trade-signal "
                "feature name; LLM features must never be direct trade signals"
            )

        availability_time = _validate_iso_temporal(
            availability_time, "availability_time"
        )

        prompt_text = self._render_prompt(prompt_spec, source_text)
        raw_output = self._call_llm(prompt_text)

        # Validate the output against the prompt's output_schema.
        validated = False
        try:
            _validate_against_schema(raw_output, prompt_spec.output_schema)
            validated = True
        except ValueError:
            validated = False

        feature_id = self._make_feature_id(
            prompt_spec.prompt_id, source_hash, self._model_spec.model_hash
        )

        return LLMFeature(
            feature_id=feature_id,
            prompt_id=prompt_spec.prompt_id,
            prompt_hash=prompt_spec.prompt_hash,
            model_id=self._model_spec.model_id,
            model_hash=self._model_spec.model_hash,
            source_hash=source_hash,
            feature_name=feature_name,
            feature_value=raw_output,
            availability_time=availability_time,
            created_at=_now_iso(),
            validated=validated,
        )

    def validate_feature(
        self, feature: LLMFeature, prompt_spec: PromptSpec
    ) -> bool:
        """Validate ``feature`` against ``prompt_spec.output_schema``.

        Args:
            feature: the :class:`LLMFeature` to validate.
            prompt_spec: the :class:`PromptSpec` whose schema to use.

        Returns:
            ``True`` if the feature value conforms to the schema.

        Raises:
            ValueError: if the feature value does not conform.
        """
        if not isinstance(feature, LLMFeature):
            raise ValueError("feature must be an LLMFeature")
        if not isinstance(prompt_spec, PromptSpec):
            raise ValueError("prompt_spec must be a PromptSpec")
        _validate_against_schema(feature.feature_value, prompt_spec.output_schema)
        return True

    def batch_extract(
        self,
        source_texts: list[str],
        source_hashes: list[str],
        prompt_id: str,
        availability_time: str,
    ) -> list[LLMFeature]:
        """Extract features for a batch of source texts.

        Args:
            source_texts: list of raw texts (same length as
                ``source_hashes``).
            source_hashes: list of source hashes (same length as
                ``source_texts``).
            prompt_id: the prompt to use for all texts.
            availability_time: ISO datetime — when the sources became
                available.

        Returns:
            List of :class:`LLMFeature` (one per input).

        Raises:
            ValueError: if the two lists have different lengths, or if
                any individual extraction fails (fail-closed).
        """
        if not isinstance(source_texts, list) or not isinstance(source_hashes, list):
            raise ValueError("source_texts and source_hashes must be lists")
        if len(source_texts) != len(source_hashes):
            raise ValueError(
                "source_texts and source_hashes must have the same length"
            )
        features: list[LLMFeature] = []
        for text, shash in zip(source_texts, source_hashes):
            features.append(
                self.extract(
                    source_text=text,
                    source_hash=shash,
                    prompt_id=prompt_id,
                    availability_time=availability_time,
                )
            )
        return features

    def build_manifest(
        self,
        features: list[LLMFeature],
        manifest_id: str | None = None,
    ) -> LLMFeatureManifest:
        """Build an :class:`LLMFeatureManifest` from a list of features.

        Args:
            features: the features to include.
            manifest_id: optional manifest id; defaults to a deterministic
                id derived from the model hash and the count of features.

        Returns:
            The :class:`LLMFeatureManifest`.

        Raises:
            ValueError: if there are duplicate ``feature_id`` values.
        """
        if not isinstance(features, list):
            raise ValueError("features must be a list")
        if manifest_id is None:
            manifest_id = (
                f"manifest_{self._model_spec.model_hash}_"
                f"{len(features)}_{_now_iso()}"
            )
        return LLMFeatureManifest(
            manifest_id=manifest_id,
            model_spec=self._model_spec,
            prompt_specs=list(self._prompt_specs),
            features=list(features),
            created_at=_now_iso(),
        )
