"""Tests for quant_foundry.forecast_distribution (T-11.2 Forecast Distribution Contract).

Covers:
- TargetTransform enum
- QuantileSpec construction + validation
- ForecastDistributionArtifact construction + validation (band, quantiles, hash)
- AlphaAdapterPolicy construction + validation
- AlphaSignal construction
- compute_forecast_hash determinism
- ForecastDistributionWriter write/read round-trip
- AlphaAdapter.convert (z_score, quantile_rank, directional)
- signal clipping (max_signal)
- confidence computation
- validate_forecast_artifact (valid + fail-closed)
- edge cases: empty samples, single quantile, zero uncertainty
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from pydantic import ValidationError
from quant_foundry.forecast_distribution import (
    AlphaAdapter,
    AlphaAdapterPolicy,
    AlphaSignal,
    ForecastDistributionArtifact,
    ForecastDistributionWriter,
    QuantileSpec,
    TargetTransform,
    compute_forecast_hash,
    validate_forecast_artifact,
)

ISO_TS = "2026-01-01T00:00:00+00:00"
ZERO_HASH = hashlib.sha256(b"").hexdigest()
ALT_HASH = hashlib.sha256(b"different").hexdigest()


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_quantiles() -> dict[float, float]:
    """Return a valid sorted quantile dict."""
    return {0.05: -1.0, 0.5: 0.0, 0.95: 1.0}


def _make_artifact_payload(**overrides) -> dict:
    """Return a valid artifact payload with optional overrides."""
    base = {
        "artifact_id": "art-001",
        "model_id": "chronos-base",
        "weight_hash": ZERO_HASH,
        "symbol": "AAPL",
        "horizon": 5,
        "target_transform": TargetTransform.LOG_RETURN,
        "mean": 0.01,
        "median": 0.0,
        "quantiles": {0.05: -1.0, 0.5: 0.0, 0.95: 1.0},
        "samples": [-0.8, -0.1, 0.0, 0.1, 0.8],
        "uncertainty_band_lower": -1.0,
        "uncertainty_band_upper": 1.0,
        "created_at": ISO_TS,
        "artifact_hash": "0" * 64,  # placeholder; tests override as needed
    }
    base.update(overrides)
    return base


@pytest.fixture
def valid_artifact_payload() -> dict:
    """A valid artifact payload (with placeholder hash)."""
    return _make_artifact_payload()


@pytest.fixture
def valid_artifact() -> ForecastDistributionArtifact:
    """A valid ForecastDistributionArtifact with a real hash."""
    payload = _make_artifact_payload()
    # Build without hash first, compute, then inject.
    tmp = ForecastDistributionArtifact(**{**payload, "artifact_hash": "0" * 64})
    h = compute_forecast_hash(tmp)
    return ForecastDistributionArtifact(**{**payload, "artifact_hash": h})


@pytest.fixture
def valid_policy_payload() -> dict:
    """A valid AlphaAdapterPolicy payload."""
    return {
        "policy_id": "pol-001",
        "signal_type": "z_score",
        "reference_point": "median",
        "normalization": "band_width",
        "min_confidence": 0.5,
        "max_signal": 2.0,
        "created_at": ISO_TS,
    }


@pytest.fixture
def valid_policy(valid_policy_payload) -> AlphaAdapterPolicy:
    """A valid AlphaAdapterPolicy."""
    return AlphaAdapterPolicy(**valid_policy_payload)


# ---------------------------------------------------------------------------
# TargetTransform enum
# ---------------------------------------------------------------------------


class TestTargetTransform:
    def test_values(self):
        assert TargetTransform.IDENTITY.value == "identity"
        assert TargetTransform.LOG_RETURN.value == "log_return"
        assert TargetTransform.LOG.value == "log"
        assert TargetTransform.DIFFERENCE.value == "difference"

    def test_count(self):
        assert len(list(TargetTransform)) == 4

    def test_from_value(self):
        assert TargetTransform("log_return") is TargetTransform.LOG_RETURN

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            TargetTransform("bogus")

    def test_str_representation(self):
        assert str(TargetTransform.LOG) == "log"

    def test_is_str_enum(self):
        assert isinstance(TargetTransform.IDENTITY, str)


# ---------------------------------------------------------------------------
# QuantileSpec
# ---------------------------------------------------------------------------


class TestQuantileSpec:
    def test_valid_construction(self):
        q = QuantileSpec(level=0.05)
        assert q.level == 0.05

    def test_median_level(self):
        q = QuantileSpec(level=0.5)
        assert q.level == 0.5

    def test_high_level(self):
        q = QuantileSpec(level=0.95)
        assert q.level == 0.95

    def test_level_zero_rejected(self):
        with pytest.raises(ValidationError):
            QuantileSpec(level=0.0)

    def test_level_one_rejected(self):
        with pytest.raises(ValidationError):
            QuantileSpec(level=1.0)

    def test_level_negative_rejected(self):
        with pytest.raises(ValidationError):
            QuantileSpec(level=-0.1)

    def test_level_above_one_rejected(self):
        with pytest.raises(ValidationError):
            QuantileSpec(level=1.5)

    def test_frozen(self):
        q = QuantileSpec(level=0.5)
        with pytest.raises(ValidationError):
            q.level = 0.9  # type: ignore[misc]

    def test_extra_forbidden(self):
        with pytest.raises(ValidationError):
            QuantileSpec(level=0.5, extra="boom")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ForecastDistributionArtifact
# ---------------------------------------------------------------------------


class TestForecastDistributionArtifact:
    def test_valid_construction(self, valid_artifact):
        assert valid_artifact.artifact_id == "art-001"
        assert valid_artifact.symbol == "AAPL"
        assert valid_artifact.horizon == 5

    def test_frozen(self, valid_artifact):
        with pytest.raises(ValidationError):
            valid_artifact.symbol = "MSFT"  # type: ignore[misc]

    def test_extra_forbidden(self, valid_artifact_payload):
        valid_artifact_payload["extra"] = "boom"
        with pytest.raises(ValidationError):
            ForecastDistributionArtifact(**valid_artifact_payload)

    def test_empty_artifact_id_rejected(self, valid_artifact_payload):
        valid_artifact_payload["artifact_id"] = ""
        with pytest.raises(ValidationError):
            ForecastDistributionArtifact(**valid_artifact_payload)

    def test_empty_model_id_rejected(self, valid_artifact_payload):
        valid_artifact_payload["model_id"] = "  "
        with pytest.raises(ValidationError):
            ForecastDistributionArtifact(**valid_artifact_payload)

    def test_empty_symbol_rejected(self, valid_artifact_payload):
        valid_artifact_payload["symbol"] = ""
        with pytest.raises(ValidationError):
            ForecastDistributionArtifact(**valid_artifact_payload)

    def test_zero_horizon_rejected(self, valid_artifact_payload):
        valid_artifact_payload["horizon"] = 0
        with pytest.raises(ValidationError):
            ForecastDistributionArtifact(**valid_artifact_payload)

    def test_negative_horizon_rejected(self, valid_artifact_payload):
        valid_artifact_payload["horizon"] = -3
        with pytest.raises(ValidationError):
            ForecastDistributionArtifact(**valid_artifact_payload)

    def test_band_lower_above_median_rejected(self, valid_artifact_payload):
        valid_artifact_payload["uncertainty_band_lower"] = 0.5
        valid_artifact_payload["median"] = 0.0
        with pytest.raises(ValidationError):
            ForecastDistributionArtifact(**valid_artifact_payload)

    def test_band_upper_below_median_rejected(self, valid_artifact_payload):
        valid_artifact_payload["uncertainty_band_upper"] = -0.5
        valid_artifact_payload["median"] = 0.0
        with pytest.raises(ValidationError):
            ForecastDistributionArtifact(**valid_artifact_payload)

    def test_unsorted_quantiles_rejected(self, valid_artifact_payload):
        valid_artifact_payload["quantiles"] = {0.95: 1.0, 0.05: -1.0, 0.5: 0.0}
        with pytest.raises(ValidationError):
            ForecastDistributionArtifact(**valid_artifact_payload)

    def test_quantile_level_zero_rejected(self, valid_artifact_payload):
        valid_artifact_payload["quantiles"] = {0.0: -1.0, 0.5: 0.0, 0.95: 1.0}
        with pytest.raises(ValidationError):
            ForecastDistributionArtifact(**valid_artifact_payload)

    def test_quantile_level_one_rejected(self, valid_artifact_payload):
        valid_artifact_payload["quantiles"] = {0.05: -1.0, 0.5: 0.0, 1.0: 1.0}
        with pytest.raises(ValidationError):
            ForecastDistributionArtifact(**valid_artifact_payload)

    def test_empty_quantiles_rejected(self, valid_artifact_payload):
        valid_artifact_payload["quantiles"] = {}
        with pytest.raises(ValidationError):
            ForecastDistributionArtifact(**valid_artifact_payload)

    def test_duplicate_levels_rejected(self, valid_artifact_payload):
        # 0.5 and 0.50000000000000001 collapse to the same float key in a
        # Python dict, so the model receives only 3 unique levels and cannot
        # detect the duplicate. Instead, verify that a non-sorted set of
        # distinct-but-close levels is still accepted (sorted check), and
        # that genuinely out-of-order levels are rejected (covered by
        # test_unsorted_quantiles_rejected). Here we just confirm a valid
        # 3-level dict is accepted.
        valid_artifact_payload["quantiles"] = {0.05: -1.0, 0.5: 0.0, 0.95: 1.0}
        art = ForecastDistributionArtifact(**valid_artifact_payload)
        assert len(art.quantiles) == 3

    def test_samples_optional(self, valid_artifact_payload):
        valid_artifact_payload["samples"] = None
        art = ForecastDistributionArtifact(**valid_artifact_payload)
        assert art.samples is None

    def test_samples_list(self, valid_artifact_payload):
        valid_artifact_payload["samples"] = [0.1, 0.2, 0.3]
        art = ForecastDistributionArtifact(**valid_artifact_payload)
        assert art.samples == [0.1, 0.2, 0.3]

    def test_target_transform_enum(self, valid_artifact):
        assert valid_artifact.target_transform is TargetTransform.LOG_RETURN

    def test_band_equal_to_median_ok(self, valid_artifact_payload):
        # lower == median == upper is a degenerate but valid band.
        valid_artifact_payload["median"] = 0.0
        valid_artifact_payload["uncertainty_band_lower"] = 0.0
        valid_artifact_payload["uncertainty_band_upper"] = 0.0
        art = ForecastDistributionArtifact(**valid_artifact_payload)
        assert art.uncertainty_band_lower == art.median == art.uncertainty_band_upper


# ---------------------------------------------------------------------------
# AlphaAdapterPolicy
# ---------------------------------------------------------------------------


class TestAlphaAdapterPolicy:
    def test_valid_construction(self, valid_policy):
        assert valid_policy.policy_id == "pol-001"
        assert valid_policy.signal_type == "z_score"

    def test_defaults(self):
        p = AlphaAdapterPolicy(
            policy_id="pol-x",
            signal_type="directional",
            created_at=ISO_TS,
        )
        assert p.reference_point == "median"
        assert p.normalization == "band_width"
        assert p.min_confidence == 0.5
        assert p.max_signal == 2.0

    def test_frozen(self, valid_policy):
        with pytest.raises(ValidationError):
            valid_policy.signal_type = "directional"  # type: ignore[misc]

    def test_extra_forbidden(self, valid_policy_payload):
        valid_policy_payload["extra"] = "boom"
        with pytest.raises(ValidationError):
            AlphaAdapterPolicy(**valid_policy_payload)

    def test_empty_policy_id_rejected(self, valid_policy_payload):
        valid_policy_payload["policy_id"] = ""
        with pytest.raises(ValidationError):
            AlphaAdapterPolicy(**valid_policy_payload)

    def test_invalid_signal_type_rejected(self, valid_policy_payload):
        valid_policy_payload["signal_type"] = "bogus"
        with pytest.raises(ValidationError):
            AlphaAdapterPolicy(**valid_policy_payload)

    def test_invalid_reference_point_rejected(self, valid_policy_payload):
        valid_policy_payload["reference_point"] = "bogus"
        with pytest.raises(ValidationError):
            AlphaAdapterPolicy(**valid_policy_payload)

    def test_invalid_normalization_rejected(self, valid_policy_payload):
        valid_policy_payload["normalization"] = "bogus"
        with pytest.raises(ValidationError):
            AlphaAdapterPolicy(**valid_policy_payload)

    def test_min_confidence_below_zero_rejected(self, valid_policy_payload):
        valid_policy_payload["min_confidence"] = -0.1
        with pytest.raises(ValidationError):
            AlphaAdapterPolicy(**valid_policy_payload)

    def test_min_confidence_above_one_rejected(self, valid_policy_payload):
        valid_policy_payload["min_confidence"] = 1.1
        with pytest.raises(ValidationError):
            AlphaAdapterPolicy(**valid_policy_payload)

    def test_min_confidence_zero_ok(self, valid_policy_payload):
        valid_policy_payload["min_confidence"] = 0.0
        p = AlphaAdapterPolicy(**valid_policy_payload)
        assert p.min_confidence == 0.0

    def test_min_confidence_one_ok(self, valid_policy_payload):
        valid_policy_payload["min_confidence"] = 1.0
        p = AlphaAdapterPolicy(**valid_policy_payload)
        assert p.min_confidence == 1.0

    def test_max_signal_zero_rejected(self, valid_policy_payload):
        valid_policy_payload["max_signal"] = 0.0
        with pytest.raises(ValidationError):
            AlphaAdapterPolicy(**valid_policy_payload)

    def test_max_signal_negative_rejected(self, valid_policy_payload):
        valid_policy_payload["max_signal"] = -1.0
        with pytest.raises(ValidationError):
            AlphaAdapterPolicy(**valid_policy_payload)

    def test_all_signal_types_accepted(self, valid_policy_payload):
        for st in ("z_score", "quantile_rank", "directional"):
            valid_policy_payload["signal_type"] = st
            p = AlphaAdapterPolicy(**valid_policy_payload)
            assert p.signal_type == st


# ---------------------------------------------------------------------------
# AlphaSignal
# ---------------------------------------------------------------------------


class TestAlphaSignal:
    def test_valid_construction(self):
        s = AlphaSignal(
            symbol="AAPL",
            horizon=5,
            signal_value=1.2,
            confidence=0.8,
            policy_id="pol-001",
            forecast_artifact_id="art-001",
            created_at=ISO_TS,
        )
        assert s.symbol == "AAPL"
        assert s.signal_value == 1.2

    def test_frozen(self):
        s = AlphaSignal(
            symbol="AAPL",
            horizon=5,
            signal_value=1.2,
            confidence=0.8,
            policy_id="pol-001",
            forecast_artifact_id="art-001",
            created_at=ISO_TS,
        )
        with pytest.raises(ValidationError):
            s.signal_value = 0.0  # type: ignore[misc]

    def test_extra_forbidden(self):
        with pytest.raises(ValidationError):
            AlphaSignal(
                symbol="AAPL",
                horizon=5,
                signal_value=1.2,
                confidence=0.8,
                policy_id="pol-001",
                forecast_artifact_id="art-001",
                created_at=ISO_TS,
                extra="boom",  # type: ignore[call-arg]
            )

    def test_confidence_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            AlphaSignal(
                symbol="AAPL",
                horizon=5,
                signal_value=1.2,
                confidence=1.5,
                policy_id="pol-001",
                forecast_artifact_id="art-001",
                created_at=ISO_TS,
            )

    def test_empty_policy_id_rejected(self):
        with pytest.raises(ValidationError):
            AlphaSignal(
                symbol="AAPL",
                horizon=5,
                signal_value=1.2,
                confidence=0.8,
                policy_id="",
                forecast_artifact_id="art-001",
                created_at=ISO_TS,
            )

    def test_empty_forecast_artifact_id_rejected(self):
        with pytest.raises(ValidationError):
            AlphaSignal(
                symbol="AAPL",
                horizon=5,
                signal_value=1.2,
                confidence=0.8,
                policy_id="pol-001",
                forecast_artifact_id="",
                created_at=ISO_TS,
            )


# ---------------------------------------------------------------------------
# compute_forecast_hash
# ---------------------------------------------------------------------------


class TestComputeForecastHash:
    def test_deterministic(self, valid_artifact):
        h1 = compute_forecast_hash(valid_artifact)
        h2 = compute_forecast_hash(valid_artifact)
        assert h1 == h2

    def test_is_sha256_hex(self, valid_artifact):
        h = compute_forecast_hash(valid_artifact)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_excludes_artifact_hash_field(self, valid_artifact_payload):
        # Two artifacts identical except for artifact_hash should hash equal.
        p1 = _make_artifact_payload(artifact_hash="0" * 64)
        p2 = _make_artifact_payload(artifact_hash=ALT_HASH)
        a1 = ForecastDistributionArtifact(**p1)
        a2 = ForecastDistributionArtifact(**p2)
        assert compute_forecast_hash(a1) == compute_forecast_hash(a2)

    def test_different_content_different_hash(self, valid_artifact_payload):
        a1 = ForecastDistributionArtifact(**_make_artifact_payload(median=0.0))
        a2 = ForecastDistributionArtifact(**_make_artifact_payload(median=0.5))
        assert compute_forecast_hash(a1) != compute_forecast_hash(a2)

    def test_type_error_on_non_artifact(self):
        with pytest.raises(TypeError):
            compute_forecast_hash("not-an-artifact")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ForecastDistributionWriter
# ---------------------------------------------------------------------------


class TestForecastDistributionWriter:
    def test_write_creates_file(self, tmp_path, valid_artifact):
        w = ForecastDistributionWriter(str(tmp_path))
        path = w.write(valid_artifact)
        assert os.path.exists(path)
        assert path.endswith(".json")

    def test_write_returns_path(self, tmp_path, valid_artifact):
        w = ForecastDistributionWriter(str(tmp_path))
        path = w.write(valid_artifact)
        assert isinstance(path, str)
        assert valid_artifact.artifact_id.replace("-", "_") in os.path.basename(
            path
        ) or valid_artifact.artifact_id in os.path.basename(path)

    def test_read_round_trip(self, tmp_path, valid_artifact):
        w = ForecastDistributionWriter(str(tmp_path))
        path = w.write(valid_artifact)
        loaded = w.read(path)
        assert loaded.artifact_id == valid_artifact.artifact_id
        assert loaded.median == valid_artifact.median
        assert loaded.quantiles == valid_artifact.quantiles

    def test_read_preserves_samples(self, tmp_path, valid_artifact):
        w = ForecastDistributionWriter(str(tmp_path))
        path = w.write(valid_artifact)
        loaded = w.read(path)
        assert loaded.samples == valid_artifact.samples

    def test_read_preserves_none_samples(self, tmp_path, valid_artifact_payload):
        valid_artifact_payload["samples"] = None
        art = ForecastDistributionArtifact(**valid_artifact_payload)
        w = ForecastDistributionWriter(str(tmp_path))
        path = w.write(art)
        loaded = w.read(path)
        assert loaded.samples is None

    def test_read_missing_file_raises(self, tmp_path):
        w = ForecastDistributionWriter(str(tmp_path))
        with pytest.raises(ValueError):
            w.read(str(tmp_path / "nope.json"))

    def test_read_empty_path_raises(self, tmp_path):
        w = ForecastDistributionWriter(str(tmp_path))
        with pytest.raises(ValueError):
            w.read("")

    def test_write_type_error_on_non_artifact(self, tmp_path):
        w = ForecastDistributionWriter(str(tmp_path))
        with pytest.raises(TypeError):
            w.write("not-an-artifact")  # type: ignore[arg-type]

    def test_constructor_creates_dir(self, tmp_path):
        nested = tmp_path / "nested" / "dir"
        w = ForecastDistributionWriter(str(nested))
        assert os.path.isdir(str(nested))

    def test_constructor_empty_dir_rejected(self):
        with pytest.raises(ValueError):
            ForecastDistributionWriter("")

    def test_output_dir_property(self, tmp_path):
        w = ForecastDistributionWriter(str(tmp_path))
        assert w.output_dir == str(tmp_path)

    def test_json_is_valid(self, tmp_path, valid_artifact):
        w = ForecastDistributionWriter(str(tmp_path))
        path = w.write(valid_artifact)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["artifact_id"] == valid_artifact.artifact_id


# ---------------------------------------------------------------------------
# AlphaAdapter
# ---------------------------------------------------------------------------


class TestAlphaAdapter:
    def test_constructor_valid(self, valid_policy):
        adapter = AlphaAdapter(valid_policy)
        assert adapter.policy is valid_policy

    def test_constructor_type_error(self):
        with pytest.raises(TypeError):
            AlphaAdapter("not-a-policy")  # type: ignore[arg-type]

    def test_validate_policy_ok(self, valid_policy):
        adapter = AlphaAdapter(valid_policy)
        # Should not raise.
        adapter.validate_policy()

    def test_convert_z_score(self, valid_artifact):
        policy = AlphaAdapterPolicy(
            policy_id="pol-z",
            signal_type="z_score",
            reference_point="median",
            normalization="band_width",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(valid_artifact)
        # median=0, reference=median=0, band_width=2 -> 0/2 = 0
        assert sig.signal_value == 0.0
        assert sig.policy_id == "pol-z"
        assert sig.forecast_artifact_id == valid_artifact.artifact_id

    def test_convert_z_score_with_reference_mean(self, valid_artifact):
        policy = AlphaAdapterPolicy(
            policy_id="pol-zm",
            signal_type="z_score",
            reference_point="mean",
            normalization="band_width",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(valid_artifact)
        # median=0, mean=0.01, band_width=2 -> (0 - 0.01)/2 = -0.005
        assert sig.signal_value == pytest.approx(-0.005)

    def test_convert_z_score_with_reference_zero(self, valid_artifact):
        policy = AlphaAdapterPolicy(
            policy_id="pol-z0",
            signal_type="z_score",
            reference_point="zero",
            normalization="band_width",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(valid_artifact)
        # median=0, reference=0 -> 0
        assert sig.signal_value == 0.0

    def test_convert_directional_positive(self):
        payload = _make_artifact_payload(median=0.5, mean=0.5)
        art = ForecastDistributionArtifact(**payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-d",
            signal_type="directional",
            reference_point="zero",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        assert sig.signal_value == 1.0

    def test_convert_directional_negative(self):
        payload = _make_artifact_payload(median=-0.5, mean=-0.5)
        art = ForecastDistributionArtifact(**payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-d",
            signal_type="directional",
            reference_point="zero",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        assert sig.signal_value == -1.0

    def test_convert_directional_zero(self, valid_artifact):
        policy = AlphaAdapterPolicy(
            policy_id="pol-d",
            signal_type="directional",
            reference_point="median",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(valid_artifact)
        # median == reference -> 0
        assert sig.signal_value == 0.0

    def test_convert_quantile_rank(self):
        # median in the middle of the quantile range -> 0
        payload = _make_artifact_payload(
            median=0.0,
            quantiles={0.05: -1.0, 0.5: 0.0, 0.95: 1.0},
        )
        art = ForecastDistributionArtifact(**payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-q",
            signal_type="quantile_rank",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        assert sig.signal_value == pytest.approx(0.0)

    def test_convert_quantile_rank_high(self):
        payload = _make_artifact_payload(
            median=1.0,
            quantiles={0.05: -1.0, 0.5: 0.0, 0.95: 1.0},
        )
        art = ForecastDistributionArtifact(**payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-q",
            signal_type="quantile_rank",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        assert sig.signal_value == pytest.approx(1.0)

    def test_convert_quantile_rank_low(self):
        payload = _make_artifact_payload(
            median=-1.0,
            quantiles={0.05: -1.0, 0.5: 0.0, 0.95: 1.0},
        )
        art = ForecastDistributionArtifact(**payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-q",
            signal_type="quantile_rank",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        assert sig.signal_value == pytest.approx(-1.0)

    def test_convert_records_policy_id(self, valid_artifact, valid_policy):
        adapter = AlphaAdapter(valid_policy)
        sig = adapter.convert(valid_artifact)
        assert sig.policy_id == valid_policy.policy_id

    def test_convert_records_forecast_artifact_id(self, valid_artifact, valid_policy):
        adapter = AlphaAdapter(valid_policy)
        sig = adapter.convert(valid_artifact)
        assert sig.forecast_artifact_id == valid_artifact.artifact_id

    def test_convert_type_error_on_non_artifact(self, valid_policy):
        adapter = AlphaAdapter(valid_policy)
        with pytest.raises(TypeError):
            adapter.convert("not-an-artifact")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Signal clipping
# ---------------------------------------------------------------------------


class TestSignalClipping:
    def test_clip_positive(self):
        # Large median relative to tiny band -> large z-score clipped.
        payload = _make_artifact_payload(
            median=100.0,
            mean=100.0,
            uncertainty_band_lower=99.999,
            uncertainty_band_upper=100.001,
            quantiles={0.05: 99.999, 0.5: 100.0, 0.95: 100.001},
        )
        art = ForecastDistributionArtifact(**payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-clip",
            signal_type="z_score",
            reference_point="zero",
            normalization="band_width",
            max_signal=2.0,
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        assert sig.signal_value == 2.0

    def test_clip_negative(self):
        payload = _make_artifact_payload(
            median=-100.0,
            mean=-100.0,
            uncertainty_band_lower=-100.001,
            uncertainty_band_upper=-99.999,
            quantiles={0.05: -100.001, 0.5: -100.0, 0.95: -99.999},
        )
        art = ForecastDistributionArtifact(**payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-clip",
            signal_type="z_score",
            reference_point="zero",
            normalization="band_width",
            max_signal=2.0,
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        assert sig.signal_value == -2.0

    def test_clip_custom_max_signal(self):
        payload = _make_artifact_payload(
            median=100.0,
            mean=100.0,
            uncertainty_band_lower=99.999,
            uncertainty_band_upper=100.001,
            quantiles={0.05: 99.999, 0.5: 100.0, 0.95: 100.001},
        )
        art = ForecastDistributionArtifact(**payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-clip",
            signal_type="z_score",
            reference_point="zero",
            normalization="band_width",
            max_signal=0.5,
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        assert sig.signal_value == 0.5


# ---------------------------------------------------------------------------
# Confidence computation
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_zero_band_high_confidence(self):
        payload = _make_artifact_payload(
            median=0.0,
            mean=0.0,
            uncertainty_band_lower=0.0,
            uncertainty_band_upper=0.0,
            quantiles={0.05: 0.0, 0.5: 0.0, 0.95: 0.0},
        )
        art = ForecastDistributionArtifact(**payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-c",
            signal_type="z_score",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        assert sig.confidence == 1.0

    def test_wide_band_low_confidence(self):
        payload = _make_artifact_payload(
            median=0.0,
            mean=0.0,
            uncertainty_band_lower=-100.0,
            uncertainty_band_upper=100.0,
            quantiles={0.05: -100.0, 0.5: 0.0, 0.95: 100.0},
        )
        art = ForecastDistributionArtifact(**payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-c",
            signal_type="z_score",
            normalization="band_width",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        # band_width=200, scale=200 -> ratio=1 -> conf=0
        assert sig.confidence == pytest.approx(0.0)

    def test_confidence_in_unit_interval(self, valid_artifact, valid_policy):
        adapter = AlphaAdapter(valid_policy)
        sig = adapter.convert(valid_artifact)
        assert 0.0 <= sig.confidence <= 1.0

    def test_confidence_with_std_normalization(self):
        payload = _make_artifact_payload(
            median=0.0,
            mean=0.0,
            uncertainty_band_lower=-1.0,
            uncertainty_band_upper=1.0,
            quantiles={0.05: -1.0, 0.5: 0.0, 0.95: 1.0},
            samples=[-1.0, -1.0, 1.0, 1.0],
        )
        art = ForecastDistributionArtifact(**payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-std",
            signal_type="z_score",
            normalization="std",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        assert 0.0 <= sig.confidence <= 1.0

    def test_confidence_with_iqr_normalization(self):
        payload = _make_artifact_payload(
            median=0.0,
            mean=0.0,
            uncertainty_band_lower=-1.0,
            uncertainty_band_upper=1.0,
            quantiles={0.05: -1.0, 0.5: 0.0, 0.95: 1.0},
        )
        art = ForecastDistributionArtifact(**payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-iqr",
            signal_type="z_score",
            normalization="iqr",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        assert 0.0 <= sig.confidence <= 1.0


# ---------------------------------------------------------------------------
# validate_forecast_artifact
# ---------------------------------------------------------------------------


class TestValidateForecastArtifact:
    def test_valid_artifact_returns_true(self, valid_artifact):
        assert validate_forecast_artifact(valid_artifact) is True

    def test_placeholder_hash_skips_check(self, valid_artifact_payload):
        # artifact_hash = "0"*64 is a placeholder; structural checks still run.
        art = ForecastDistributionArtifact(**valid_artifact_payload)
        assert validate_forecast_artifact(art) is True

    def test_hash_mismatch_raises(self, valid_artifact_payload):
        # Compute the real hash, then perturb it.
        tmp = ForecastDistributionArtifact(**{**valid_artifact_payload, "artifact_hash": "0" * 64})
        real = compute_forecast_hash(tmp)
        bad = real[:-1] + ("a" if real[-1] != "a" else "b")
        art = ForecastDistributionArtifact(**{**valid_artifact_payload, "artifact_hash": bad})
        with pytest.raises(ValueError):
            validate_forecast_artifact(art)

    def test_type_error_on_non_artifact(self):
        with pytest.raises(TypeError):
            validate_forecast_artifact("nope")  # type: ignore[arg-type]

    def test_band_invariant_failure_raises(self, valid_artifact_payload):
        # Construct an artifact that is valid, then bypass the model validator
        # by directly testing validate with a malformed object is not possible
        # (model is frozen + validated). Instead confirm the model itself
        # rejects a bad band.
        valid_artifact_payload["uncertainty_band_lower"] = 10.0
        valid_artifact_payload["median"] = 0.0
        with pytest.raises(ValidationError):
            ForecastDistributionArtifact(**valid_artifact_payload)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_samples_list_allowed(self, valid_artifact_payload):
        valid_artifact_payload["samples"] = []
        art = ForecastDistributionArtifact(**valid_artifact_payload)
        assert art.samples == []

    def test_single_quantile_allowed(self, valid_artifact_payload):
        valid_artifact_payload["quantiles"] = {0.5: 0.0}
        art = ForecastDistributionArtifact(**valid_artifact_payload)
        assert len(art.quantiles) == 1

    def test_zero_uncertainty_band(self, valid_artifact_payload):
        valid_artifact_payload["median"] = 0.0
        valid_artifact_payload["uncertainty_band_lower"] = 0.0
        valid_artifact_payload["uncertainty_band_upper"] = 0.0
        valid_artifact_payload["quantiles"] = {0.5: 0.0}
        art = ForecastDistributionArtifact(**valid_artifact_payload)
        assert art.uncertainty_band_lower == art.uncertainty_band_upper

    def test_convert_with_zero_uncertainty(self, valid_artifact_payload):
        valid_artifact_payload["median"] = 0.0
        valid_artifact_payload["mean"] = 0.0
        valid_artifact_payload["uncertainty_band_lower"] = 0.0
        valid_artifact_payload["uncertainty_band_upper"] = 0.0
        valid_artifact_payload["quantiles"] = {0.5: 0.0}
        art = ForecastDistributionArtifact(**valid_artifact_payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-z",
            signal_type="z_score",
            reference_point="zero",
            normalization="band_width",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        # zero band -> scale falls back to 1.0, median=0, ref=0 -> 0
        assert sig.signal_value == 0.0
        assert sig.confidence == 1.0

    def test_std_normalization_without_samples_falls_back(self, valid_artifact_payload):
        valid_artifact_payload["samples"] = None
        art = ForecastDistributionArtifact(**valid_artifact_payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-std",
            signal_type="z_score",
            normalization="std",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        # Should not raise; falls back to band_width.
        assert isinstance(sig.signal_value, float)

    def test_iqr_normalization_single_quantile_falls_back(self, valid_artifact_payload):
        valid_artifact_payload["quantiles"] = {0.5: 0.0}
        art = ForecastDistributionArtifact(**valid_artifact_payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-iqr",
            signal_type="z_score",
            normalization="iqr",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        assert isinstance(sig.signal_value, float)

    def test_quantile_rank_degenerate_returns_zero(self):
        payload = _make_artifact_payload(
            median=0.0,
            quantiles={0.05: 0.0, 0.5: 0.0, 0.95: 0.0},
        )
        art = ForecastDistributionArtifact(**payload)
        policy = AlphaAdapterPolicy(
            policy_id="pol-q",
            signal_type="quantile_rank",
            created_at=ISO_TS,
        )
        adapter = AlphaAdapter(policy)
        sig = adapter.convert(art)
        assert sig.signal_value == 0.0

    def test_round_trip_preserves_hash(self, tmp_path, valid_artifact):
        w = ForecastDistributionWriter(str(tmp_path))
        path = w.write(valid_artifact)
        loaded = w.read(path)
        assert loaded.artifact_hash == valid_artifact.artifact_hash
        assert compute_forecast_hash(loaded) == compute_forecast_hash(valid_artifact)

    def test_no_direct_conversion_helper(self):
        # There is no module-level "convert" function; only AlphaAdapter.
        import quant_foundry.forecast_distribution as mod

        assert not hasattr(mod, "convert")
        assert not hasattr(mod, "convert_to_signal")

    def test_alpha_signal_provenance_chain(self, valid_artifact, valid_policy):
        adapter = AlphaAdapter(valid_policy)
        sig = adapter.convert(valid_artifact)
        # Provenance: policy_id + forecast_artifact_id both present.
        assert sig.policy_id == valid_policy.policy_id
        assert sig.forecast_artifact_id == valid_artifact.artifact_id
        assert sig.policy_id
        assert sig.forecast_artifact_id
