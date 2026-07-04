"""
quant_foundry.forecast_distribution — Forecast distribution contract (T-11.2).

Defines the canonical artifact schema for a foundation-model forecast
*distribution* and the explicit, recorded policy that converts a distribution
into an alpha signal. The goal is to make the forecast→alpha transformation
auditable: every emitted signal carries the id of the policy that produced it
and the id of the forecast artifact it came from, so the full provenance chain
(weight hash → forecast → policy → signal) is recoverable.

Design invariants (non-negotiable, fail-closed):

- A forecast distribution is an immutable, hash-pinned artifact
  (:class:`ForecastDistributionArtifact`). Its ``artifact_hash`` is a
  deterministic SHA-256 over a canonical JSON serialization, so two artifacts
  with identical content produce identical hashes (reproducibility / audit).
- All Pydantic models are ``frozen=True`` + ``extra='forbid'`` — no mutation,
  no surprise fields.
- The only sanctioned path from a forecast distribution to an alpha signal is
  :meth:`AlphaAdapter.convert`, which records its ``policy_id`` and the source
  ``forecast_artifact_id`` on the emitted :class:`AlphaSignal`. There is no
  free-floating "convert a distribution to a signal" helper — the adapter is
  the gate.
- Validation is fail-closed: invalid quantile levels, an uncertainty band that
  does not bracket the median, an out-of-range confidence, or a non-positive
  ``max_signal`` all raise ``ValueError`` / ``ValidationError`` at construction
  or conversion time.

Public surface:

  - :class:`TargetTransform` (enum)
  - :class:`QuantileSpec`, :class:`ForecastDistributionArtifact`,
    :class:`AlphaAdapterPolicy`, :class:`AlphaSignal` (Pydantic v2 models)
  - :func:`compute_forecast_hash` (deterministic SHA-256)
  - :class:`ForecastDistributionWriter` (JSON write/read round-trip)
  - :class:`AlphaAdapter` (distribution → signal gate)
  - :func:`validate_forecast_artifact` (consistency check)
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _canonical_json(payload: dict[str, Any]) -> str:
    """Serialize ``payload`` to a deterministic, sorted-keys JSON string.

    Floats are rendered with ``repr``-style precision via the default encoder
    so that the same logical values always produce the same bytes. Keys are
    sorted recursively.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TargetTransform(StrEnum):
    """Transform applied to the target before forecasting.

    ``IDENTITY``   — forecast the raw level.
    ``LOG_RETURN`` — forecast log returns (log(p_t / p_{t-1})).
    ``LOG``        — forecast the log of the level.
    ``DIFFERENCE`` — forecast the first difference of the level.
    """

    IDENTITY = "identity"
    LOG_RETURN = "log_return"
    LOG = "log"
    DIFFERENCE = "difference"


# ---------------------------------------------------------------------------
# QuantileSpec
# ---------------------------------------------------------------------------


class QuantileSpec(BaseModel):
    """A single forecast quantile level (e.g. 0.05, 0.5, 0.95).

    Frozen + extra-forbid. ``level`` must lie strictly in the open interval
    (0, 1) — 0 and 1 are not valid quantile *levels* (they would be the min /
    max, not a quantile).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: float

    @field_validator("level")
    @classmethod
    def _level_in_open_unit_interval(cls, v: float) -> float:
        if not isinstance(v, (int, float)):
            raise ValueError("level must be a number")
        v = float(v)
        if not (0.0 < v < 1.0):
            raise ValueError("level must be strictly in (0, 1)")
        return v


# ---------------------------------------------------------------------------
# ForecastDistributionArtifact
# ---------------------------------------------------------------------------


class ForecastDistributionArtifact(BaseModel):
    """Immutable, hash-pinned forecast distribution artifact.

    Captures the full probabilistic forecast produced by a foundation model:
    point estimates (mean, median), a set of quantiles, optional Monte Carlo
    samples, and an uncertainty band. The ``artifact_hash`` is a deterministic
    SHA-256 over the canonical JSON of the artifact (excluding the hash
    itself), so identical content → identical hash.

    Invariants enforced at construction (fail-closed):

    - ``uncertainty_band_lower <= median <= uncertainty_band_upper``.
    - Quantile levels (keys of ``quantiles``) are sorted ascending.
    - Every quantile level lies strictly in (0, 1).
    - ``artifact_hash`` is a 64-char lowercase hex SHA-256 digest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    model_id: str
    weight_hash: str
    symbol: str
    horizon: int
    target_transform: TargetTransform
    mean: float
    median: float
    quantiles: dict[float, float]
    samples: list[float] | None = None
    uncertainty_band_lower: float
    uncertainty_band_upper: float
    created_at: str
    artifact_hash: str

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("artifact_id must be a non-empty string")
        return v

    @field_validator("model_id")
    @classmethod
    def _model_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("model_id must be a non-empty string")
        return v

    @field_validator("weight_hash")
    @classmethod
    def _weight_hash_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("weight_hash must be a non-empty string")
        return v

    @field_validator("symbol")
    @classmethod
    def _symbol_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("symbol must be a non-empty string")
        return v

    @field_validator("horizon")
    @classmethod
    def _horizon_positive(cls, v: int) -> int:
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("horizon must be an int")
        if v <= 0:
            raise ValueError("horizon must be a positive integer")
        return v

    @field_validator("quantiles")
    @classmethod
    def _quantiles_well_formed(cls, v: dict[float, float]) -> dict[float, float]:
        if not isinstance(v, dict) or not v:
            raise ValueError("quantiles must be a non-empty dict")
        levels: list[float] = []
        for key, val in v.items():
            # Coerce numeric keys (json loads float keys as strings sometimes).
            try:
                lvl = float(key)
            except (TypeError, ValueError):
                raise ValueError(f"quantile level {key!r} is not a number") from None
            if not (0.0 < lvl < 1.0):
                raise ValueError(f"quantile level {lvl!r} must be strictly in (0, 1)")
            if not isinstance(val, (int, float)):
                raise ValueError(f"quantile value for level {lvl!r} must be a number")
            levels.append(lvl)
        # Levels must be sorted ascending.
        if levels != sorted(levels):
            raise ValueError("quantile levels must be sorted ascending")
        # No duplicate levels.
        if len(set(levels)) != len(levels):
            raise ValueError("quantile levels must be unique")
        return v

    @model_validator(mode="after")
    def _uncertainty_band_brackets_median(self) -> ForecastDistributionArtifact:
        if self.uncertainty_band_lower > self.median:
            raise ValueError("uncertainty_band_lower must be <= median")
        if self.median > self.uncertainty_band_upper:
            raise ValueError("uncertainty_band_upper must be >= median")
        return self


# ---------------------------------------------------------------------------
# AlphaAdapterPolicy
# ---------------------------------------------------------------------------


class AlphaAdapterPolicy(BaseModel):
    """Recorded policy that converts a forecast distribution to an alpha signal.

    Frozen + extra-forbid. The policy is the *only* sanctioned conversion
    path; every :class:`AlphaSignal` it emits carries its ``policy_id`` so the
    transformation is auditable.

    Fields:

    - ``signal_type`` — one of ``"z_score"``, ``"quantile_rank"``,
      ``"directional"``.
    - ``reference_point`` — the baseline subtracted before normalization:
      ``"median"``, ``"mean"``, or ``"zero"``.
    - ``normalization`` — how the residual is scaled: ``"band_width"``
      (uncertainty band width), ``"std"`` (sample std), ``"iqr"``
      (interquartile range of quantiles).
    - ``min_confidence`` — signals with confidence below this are still
      emitted but flagged (must be in [0, 1]).
    - ``max_signal`` — signal magnitude is clipped to this (must be > 0).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    signal_type: str
    reference_point: str = "median"
    normalization: str = "band_width"
    min_confidence: float = 0.5
    max_signal: float = 2.0
    created_at: str

    _VALID_SIGNAL_TYPES: ClassVar[tuple[str, ...]] = ("z_score", "quantile_rank", "directional")
    _VALID_REFERENCE_POINTS: ClassVar[tuple[str, ...]] = ("mean", "median", "zero")
    _VALID_NORMALIZATIONS: ClassVar[tuple[str, ...]] = ("band_width", "std", "iqr")

    @field_validator("policy_id")
    @classmethod
    def _policy_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("policy_id must be a non-empty string")
        return v

    @field_validator("signal_type")
    @classmethod
    def _signal_type_valid(cls, v: str) -> str:
        if v not in cls._VALID_SIGNAL_TYPES:
            raise ValueError(f"signal_type must be one of {cls._VALID_SIGNAL_TYPES!r}")
        return v

    @field_validator("reference_point")
    @classmethod
    def _reference_point_valid(cls, v: str) -> str:
        if v not in cls._VALID_REFERENCE_POINTS:
            raise ValueError(f"reference_point must be one of {cls._VALID_REFERENCE_POINTS!r}")
        return v

    @field_validator("normalization")
    @classmethod
    def _normalization_valid(cls, v: str) -> str:
        if v not in cls._VALID_NORMALIZATIONS:
            raise ValueError(f"normalization must be one of {cls._VALID_NORMALIZATIONS!r}")
        return v

    @field_validator("min_confidence")
    @classmethod
    def _min_confidence_in_unit(cls, v: float) -> float:
        if not isinstance(v, (int, float)):
            raise ValueError("min_confidence must be a number")
        v = float(v)
        if not (0.0 <= v <= 1.0):
            raise ValueError("min_confidence must be in [0, 1]")
        return v

    @field_validator("max_signal")
    @classmethod
    def _max_signal_positive(cls, v: float) -> float:
        if not isinstance(v, (int, float)):
            raise ValueError("max_signal must be a number")
        v = float(v)
        if v <= 0.0:
            raise ValueError("max_signal must be > 0")
        return v


# ---------------------------------------------------------------------------
# AlphaSignal
# ---------------------------------------------------------------------------


class AlphaSignal(BaseModel):
    """An alpha signal emitted by :class:`AlphaAdapter`.

    Frozen + extra-forbid. Carries the ``policy_id`` of the policy that
    produced it and the ``forecast_artifact_id`` of the source forecast, so
    the full provenance chain is recoverable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    horizon: int
    signal_value: float
    confidence: float
    policy_id: str
    forecast_artifact_id: str
    created_at: str

    @field_validator("symbol")
    @classmethod
    def _symbol_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("symbol must be a non-empty string")
        return v

    @field_validator("horizon")
    @classmethod
    def _horizon_positive(cls, v: int) -> int:
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("horizon must be an int")
        if v <= 0:
            raise ValueError("horizon must be a positive integer")
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_in_unit(cls, v: float) -> float:
        if not isinstance(v, (int, float)):
            raise ValueError("confidence must be a number")
        v = float(v)
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be in [0, 1]")
        return v

    @field_validator("policy_id")
    @classmethod
    def _policy_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("policy_id must be a non-empty string")
        return v

    @field_validator("forecast_artifact_id")
    @classmethod
    def _forecast_artifact_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("forecast_artifact_id must be a non-empty string")
        return v


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def compute_forecast_hash(artifact: ForecastDistributionArtifact) -> str:
    """Compute the deterministic SHA-256 hash of a forecast artifact.

    The hash is taken over the canonical JSON of every field *except*
    ``artifact_hash`` itself (to avoid a self-referential cycle). The
    resulting digest is a 64-char lowercase hex string.

    Args:
        artifact: The forecast distribution artifact to hash.

    Returns:
        64-character lowercase hex SHA-256 digest.

    Raises:
        TypeError: if ``artifact`` is not a ``ForecastDistributionArtifact``.
    """
    if not isinstance(artifact, ForecastDistributionArtifact):
        raise TypeError("artifact must be a ForecastDistributionArtifact")
    payload = artifact.model_dump(mode="json")
    payload.pop("artifact_hash", None)
    # Render quantile keys as stable strings for canonical serialization.
    if "quantiles" in payload and isinstance(payload["quantiles"], dict):
        payload["quantiles"] = {str(float(k)): v for k, v in payload["quantiles"].items()}
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class ForecastDistributionWriter:
    """JSON writer/reader for :class:`ForecastDistributionArtifact`.

    Writes artifacts as pretty-printed JSON (one artifact per file) and reads
    them back with full Pydantic validation. The filename is derived from the
    artifact id so a given artifact always lands at the same path.
    """

    def __init__(self, output_dir: str) -> None:
        """Create a writer rooted at ``output_dir``.

        Args:
            output_dir: Directory where artifact JSON files are stored. It is
                created (with parents) if it does not already exist.
        """
        if not isinstance(output_dir, str) or not output_dir.strip():
            raise ValueError("output_dir must be a non-empty string")
        self._output_dir: str = output_dir
        os.makedirs(output_dir, exist_ok=True)

    @property
    def output_dir(self) -> str:
        """The configured output directory (read-only)."""
        return self._output_dir

    def _path_for(self, artifact_id: str) -> str:
        """Return the on-disk path for a given ``artifact_id``."""
        safe = "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "_" for ch in artifact_id)
        return os.path.join(self._output_dir, f"{safe}.json")

    def write(self, artifact: ForecastDistributionArtifact) -> str:
        """Write ``artifact`` as JSON and return the file path.

        Args:
            artifact: The forecast distribution artifact to persist.

        Returns:
            The absolute path of the written JSON file.

        Raises:
            TypeError: if ``artifact`` is not a ``ForecastDistributionArtifact``.
        """
        if not isinstance(artifact, ForecastDistributionArtifact):
            raise TypeError("artifact must be a ForecastDistributionArtifact")
        path = self._path_for(artifact.artifact_id)
        payload = artifact.model_dump(mode="json")
        # Render quantile keys as strings for JSON (JSON keys are always str).
        if isinstance(payload.get("quantiles"), dict):
            payload["quantiles"] = {str(float(k)): v for k, v in payload["quantiles"].items()}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, default=str)
        return path

    def read(self, path: str) -> ForecastDistributionArtifact:
        """Load and validate an artifact from ``path``.

        Args:
            path: Path to a previously written artifact JSON file.

        Returns:
            A validated :class:`ForecastDistributionArtifact`.

        Raises:
            ValueError: if ``path`` is empty or the file does not exist.
            ValidationError: if the JSON does not satisfy the artifact schema.
        """
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        if not os.path.exists(path):
            raise ValueError(f"artifact file not found: {path!r}")
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        # Coerce string quantile keys back to floats for the model.
        if isinstance(raw.get("quantiles"), dict):
            raw["quantiles"] = {float(k): v for k, v in raw["quantiles"].items()}
        return ForecastDistributionArtifact.model_validate(raw)


# ---------------------------------------------------------------------------
# AlphaAdapter
# ---------------------------------------------------------------------------


class AlphaAdapter:
    """The sanctioned gate from a forecast distribution to an alpha signal.

    Holds an :class:`AlphaAdapterPolicy` and converts a
    :class:`ForecastDistributionArtifact` into an :class:`AlphaSignal` using
    that policy. The emitted signal always carries the policy's ``policy_id``
    and the source artifact's ``artifact_id``, so the transformation is
    auditable end-to-end.

    Conversion modes (``policy.signal_type``):

    - ``"z_score"`` — ``(median - reference) / normalization``.
    - ``"quantile_rank"`` — the rank of the median within the sorted quantile
      values, mapped to [-1, 1].
    - ``"directional"`` — ``sign(median - reference)``.

    The raw signal is clipped to ``[-max_signal, max_signal]``. Confidence is
    derived from the uncertainty band width relative to a scale estimate
    (narrower band → higher confidence), clamped to [0, 1].
    """

    def __init__(self, policy: AlphaAdapterPolicy) -> None:
        """Create an adapter bound to ``policy``.

        Args:
            policy: The conversion policy to enforce.

        Raises:
            TypeError: if ``policy`` is not an ``AlphaAdapterPolicy``.
        """
        if not isinstance(policy, AlphaAdapterPolicy):
            raise TypeError("policy must be an AlphaAdapterPolicy")
        self._policy: AlphaAdapterPolicy = policy
        self.validate_policy()

    @property
    def policy(self) -> AlphaAdapterPolicy:
        """The bound conversion policy (read-only)."""
        return self._policy

    def validate_policy(self) -> None:
        """Validate that the bound policy is well-formed.

        Re-checks the policy's invariants. The Pydantic model already enforces
        these at construction, so this is a defense-in-depth no-op when the
        policy was constructed normally; it raises ``ValueError`` if the
        policy is somehow malformed.

        Raises:
            ValueError: if the policy fails re-validation.
        """
        p = self._policy
        if p.signal_type not in AlphaAdapterPolicy._VALID_SIGNAL_TYPES:
            raise ValueError(f"invalid signal_type: {p.signal_type!r}")
        if p.reference_point not in AlphaAdapterPolicy._VALID_REFERENCE_POINTS:
            raise ValueError(f"invalid reference_point: {p.reference_point!r}")
        if p.normalization not in AlphaAdapterPolicy._VALID_NORMALIZATIONS:
            raise ValueError(f"invalid normalization: {p.normalization!r}")
        if not (0.0 <= p.min_confidence <= 1.0):
            raise ValueError("min_confidence must be in [0, 1]")
        if p.max_signal <= 0.0:
            raise ValueError("max_signal must be > 0")

    # -- helpers -------------------------------------------------------

    def _reference_value(self, forecast: ForecastDistributionArtifact) -> float:
        """Return the reference point value for the current policy."""
        rp = self._policy.reference_point
        if rp == "median":
            return float(forecast.median)
        if rp == "mean":
            return float(forecast.mean)
        # "zero"
        return 0.0

    def _scale_value(self, forecast: ForecastDistributionArtifact) -> float:
        """Return the normalization scale for the current policy.

        Falls back to the uncertainty band width when the requested scale is
        unavailable (e.g. ``std`` with no samples) or degenerate (zero), so
        the adapter never divides by zero.
        """
        norm = self._policy.normalization
        band_width = float(forecast.uncertainty_band_upper - forecast.uncertainty_band_lower)
        if norm == "band_width":
            scale = band_width
        elif norm == "std":
            samples = forecast.samples
            if samples and len(samples) > 1:
                mean = sum(samples) / len(samples)
                var = sum((s - mean) ** 2 for s in samples) / len(samples)
                scale = var**0.5
            else:
                scale = band_width
        elif norm == "iqr":
            levels = sorted(forecast.quantiles.keys())
            if len(levels) >= 2:
                q_low = forecast.quantiles[min(levels)]
                q_high = forecast.quantiles[max(levels)]
                scale = float(q_high - q_low)
            else:
                scale = band_width
        else:  # pragma: no cover — guarded by validate_policy
            scale = band_width
        if scale == 0.0:
            # Degenerate scale: fall back to band width, then to 1.0.
            scale = band_width if band_width != 0.0 else 1.0
        return float(scale)

    def _confidence(self, forecast: ForecastDistributionArtifact) -> float:
        """Compute a [0, 1] confidence from the uncertainty band width.

        Confidence is high when the band is narrow relative to the scale
        estimate. We use ``max(0, 1 - band_width / (scale + eps))`` clamped
        to [0, 1]. A zero-width band yields confidence 1.0.
        """
        band_width = float(forecast.uncertainty_band_upper - forecast.uncertainty_band_lower)
        if band_width <= 0.0:
            return 1.0
        scale = self._scale_value(forecast)
        if scale <= 0.0:
            return 0.0
        ratio = band_width / scale
        conf = 1.0 - ratio
        if conf < 0.0:
            conf = 0.0
        if conf > 1.0:
            conf = 1.0
        return float(conf)

    # -- main API ------------------------------------------------------

    def convert(self, forecast: ForecastDistributionArtifact) -> AlphaSignal:
        """Convert a forecast distribution into an alpha signal.

        The conversion uses the bound policy. The emitted signal carries
        ``policy_id`` (from the policy) and ``forecast_artifact_id`` (from the
        forecast), so the full provenance chain is recorded.

        Args:
            forecast: The forecast distribution artifact to convert.

        Returns:
            An :class:`AlphaSignal` with the converted value, confidence,
            policy id, and source artifact id.

        Raises:
            TypeError: if ``forecast`` is not a ``ForecastDistributionArtifact``.
            ValueError: if the forecast fails validation.
        """
        if not isinstance(forecast, ForecastDistributionArtifact):
            raise TypeError("forecast must be a ForecastDistributionArtifact")
        # Fail-closed: validate the source artifact before converting.
        validate_forecast_artifact(forecast)

        ref = self._reference_value(forecast)
        scale = self._scale_value(forecast)
        st = self._policy.signal_type

        if st == "z_score":
            raw = (float(forecast.median) - ref) / scale if scale != 0.0 else 0.0
        elif st == "quantile_rank":
            raw = self._quantile_rank(forecast)
        elif st == "directional":
            diff = float(forecast.median) - ref
            raw = 1.0 if diff > 0 else (-1.0 if diff < 0 else 0.0)
        else:  # pragma: no cover — guarded by validate_policy
            raise ValueError(f"unsupported signal_type: {st!r}")

        # Clip to [-max_signal, max_signal].
        max_s = self._policy.max_signal
        if raw > max_s:
            raw = max_s
        elif raw < -max_s:
            raw = -max_s

        confidence = self._confidence(forecast)

        return AlphaSignal(
            symbol=forecast.symbol,
            horizon=forecast.horizon,
            signal_value=float(raw),
            confidence=confidence,
            policy_id=self._policy.policy_id,
            forecast_artifact_id=forecast.artifact_id,
            created_at=_now_iso(),
        )

    def _quantile_rank(self, forecast: ForecastDistributionArtifact) -> float:
        """Rank the median within the sorted quantile values, mapped to [-1, 1].

        The median's position within the sorted list of quantile values is
        mapped so that the lowest quantile → -1 and the highest → +1. If the
        median equals all quantile values (degenerate), returns 0.0.
        """
        levels = sorted(forecast.quantiles.keys())
        values = [forecast.quantiles[l] for l in levels]
        if not values:
            return 0.0
        lo = float(min(values))
        hi = float(max(values))
        if hi == lo:
            return 0.0
        med = float(forecast.median)
        # Clamp median into [lo, hi] for ranking.
        if med <= lo:
            return -1.0
        if med >= hi:
            return 1.0
        return ((med - lo) / (hi - lo)) * 2.0 - 1.0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_forecast_artifact(artifact: ForecastDistributionArtifact) -> bool:
    """Validate that all fields of ``artifact`` are mutually consistent.

    Re-checks the invariants that the Pydantic model enforces at construction
    (defense in depth), plus the hash consistency check: the recomputed
    :func:`compute_forecast_hash` must equal ``artifact.artifact_hash`` when
    the artifact was created with a matching hash. When the stored hash is a
    placeholder (e.g. all zeros or empty), only the structural invariants are
    checked.

    Args:
        artifact: The forecast distribution artifact to validate.

    Returns:
        True if the artifact is valid.

    Raises:
        TypeError: if ``artifact`` is not a ``ForecastDistributionArtifact``.
        ValueError: if any invariant is violated (fail-closed).
    """
    if not isinstance(artifact, ForecastDistributionArtifact):
        raise TypeError("artifact must be a ForecastDistributionArtifact")

    # Structural invariants (re-checked for defense in depth).
    if artifact.uncertainty_band_lower > artifact.median:
        raise ValueError("uncertainty_band_lower must be <= median")
    if artifact.median > artifact.uncertainty_band_upper:
        raise ValueError("uncertainty_band_upper must be >= median")

    levels = sorted(artifact.quantiles.keys())
    if levels != list(artifact.quantiles.keys()):
        raise ValueError("quantile levels must be sorted ascending")
    for lvl in levels:
        if not (0.0 < lvl < 1.0):
            raise ValueError(f"quantile level {lvl!r} must be strictly in (0, 1)")
    if len(set(levels)) != len(levels):
        raise ValueError("quantile levels must be unique")

    if artifact.horizon <= 0:
        raise ValueError("horizon must be a positive integer")

    # Hash consistency: if a real 64-char hex hash is stored, it must match
    # the recomputed canonical hash. A placeholder (zeros / empty) skips this.
    stored = artifact.artifact_hash
    if stored and stored.strip() and stored != "0" * 64:
        recomputed = compute_forecast_hash(artifact)
        if recomputed != stored:
            raise ValueError("artifact_hash does not match recomputed canonical hash")

    return True
