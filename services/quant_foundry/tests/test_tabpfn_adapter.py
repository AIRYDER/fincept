"""Tests for quant_foundry.tabpfn_adapter (T-9.4).

Covers the TabPFN shadow adapter: config validation, dataset size checks,
in-context leakage detection, the shadow adapter run path (with TabPFN
mocked so the tests work without ``tabpfn`` installed), promotion-
eligibility policy, artifact save/load, and the family-registration
helper.

The test host does not have ``tabpfn`` installed, so the inference path is
exercised via ``sys.modules`` mocking of the ``tabpfn`` package.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from quant_foundry.tabpfn_adapter import (
    ALLOWED_DEVICES,
    ALLOWED_TASK_TYPES,
    TABPFN_HARD_MAX_FEATURES,
    TABPFN_HARD_MAX_TRAIN_SAMPLES,
    DatasetSizeCheck,
    TabPFNConfig,
    TabPFNShadowAdapter,
    TabPFNShadowResult,
    check_dataset_size,
    detect_in_context_leakage,
    register_tabpfn_family,
    validate_promotion_eligibility,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_tabpfn_module(
    proba_rows: list[list[float]] | None = None,
    pred_rows: list[float] | None = None,
) -> types.ModuleType:
    """Build a fake ``tabpfn`` module with stub classifier/regressor.

    The stub ``fit`` records the data and ``predict_proba`` / ``predict``
    return the canned rows. This lets the inference path run without the
    real ``tabpfn`` package installed.
    """
    mod = types.ModuleType("tabpfn")

    class _StubClassifier:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = dict(kwargs)
            self.fitted_x: object = None
            self.fitted_y: object = None

        def fit(self, x: object, y: object) -> "_StubClassifier":
            self.fitted_x = x
            self.fitted_y = y
            return self

        def predict_proba(self, x: object) -> list[list[float]]:
            if proba_rows is not None:
                return proba_rows
            # default: one row per test row, 2 classes, 0.5/0.5
            n = len(x) if hasattr(x, "__len__") else 1
            return [[0.5, 0.5] for _ in range(n)]

    class _StubRegressor:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = dict(kwargs)
            self.fitted_x: object = None
            self.fitted_y: object = None

        def fit(self, x: object, y: object) -> "_StubRegressor":
            self.fitted_x = x
            self.fitted_y = y
            return self

        def predict(self, x: object) -> list[float]:
            if pred_rows is not None:
                return pred_rows
            n = len(x) if hasattr(x, "__len__") else 1
            return [0.0 for _ in range(n)]

    mod.TabPFNClassifier = _StubClassifier  # type: ignore[attr-defined]
    mod.TabPFNRegressor = _StubRegressor  # type: ignore[attr-defined]
    return mod


@pytest.fixture
def fake_tabpfn(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a fake ``tabpfn`` module in ``sys.modules`` for the test."""
    mod = _make_fake_tabpfn_module()
    monkeypatch.setitem(sys.modules, "tabpfn", mod)
    return mod


# ---------------------------------------------------------------------------
# TabPFNConfig
# ---------------------------------------------------------------------------


class TestTabPFNConfig:
    def test_defaults(self) -> None:
        cfg = TabPFNConfig()
        assert cfg.max_train_samples == 1000
        assert cfg.max_features == 100
        assert cfg.device == "auto"
        assert cfg.shadow_only is True
        assert cfg.task_type == "binary"
        assert cfg.n_ensemble_configurations == 4
        assert cfg.seed == 42

    def test_frozen(self) -> None:
        cfg = TabPFNConfig()
        with pytest.raises(Exception):
            cfg.max_train_samples = 2000  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            TabPFNConfig(unexpected="x")  # type: ignore[call-arg]

    def test_custom_values(self) -> None:
        cfg = TabPFNConfig(
            max_train_samples=500,
            max_features=50,
            device="cuda",
            shadow_only=False,
            task_type="regression",
            n_ensemble_configurations=8,
            seed=7,
        )
        assert cfg.max_train_samples == 500
        assert cfg.max_features == 50
        assert cfg.device == "cuda"
        assert cfg.shadow_only is False
        assert cfg.task_type == "regression"
        assert cfg.n_ensemble_configurations == 8
        assert cfg.seed == 7

    def test_max_train_samples_at_hard_limit(self) -> None:
        cfg = TabPFNConfig(max_train_samples=TABPFN_HARD_MAX_TRAIN_SAMPLES)
        assert cfg.max_train_samples == TABPFN_HARD_MAX_TRAIN_SAMPLES

    def test_max_train_samples_above_hard_limit_rejected(self) -> None:
        with pytest.raises(Exception):
            TabPFNConfig(max_train_samples=TABPFN_HARD_MAX_TRAIN_SAMPLES + 1)

    def test_max_train_samples_zero_rejected(self) -> None:
        with pytest.raises(Exception):
            TabPFNConfig(max_train_samples=0)

    def test_max_train_samples_negative_rejected(self) -> None:
        with pytest.raises(Exception):
            TabPFNConfig(max_train_samples=-5)

    def test_max_features_at_hard_limit(self) -> None:
        cfg = TabPFNConfig(max_features=TABPFN_HARD_MAX_FEATURES)
        assert cfg.max_features == TABPFN_HARD_MAX_FEATURES

    def test_max_features_above_hard_limit_rejected(self) -> None:
        with pytest.raises(Exception):
            TabPFNConfig(max_features=TABPFN_HARD_MAX_FEATURES + 1)

    def test_max_features_zero_rejected(self) -> None:
        with pytest.raises(Exception):
            TabPFNConfig(max_features=0)

    def test_invalid_device_rejected(self) -> None:
        with pytest.raises(Exception):
            TabPFNConfig(device="tpu")

    def test_valid_devices_accepted(self) -> None:
        for d in ALLOWED_DEVICES:
            cfg = TabPFNConfig(device=d)
            assert cfg.device == d

    def test_invalid_task_type_rejected(self) -> None:
        with pytest.raises(Exception):
            TabPFNConfig(task_type="quantile")

    def test_valid_task_types_accepted(self) -> None:
        for t in ALLOWED_TASK_TYPES:
            cfg = TabPFNConfig(task_type=t)
            assert cfg.task_type == t

    def test_n_ensemble_zero_rejected(self) -> None:
        with pytest.raises(Exception):
            TabPFNConfig(n_ensemble_configurations=0)

    def test_seed_zero_accepted(self) -> None:
        cfg = TabPFNConfig(seed=0)
        assert cfg.seed == 0

    def test_seed_negative_rejected(self) -> None:
        with pytest.raises(Exception):
            TabPFNConfig(seed=-1)


# ---------------------------------------------------------------------------
# DatasetSizeCheck
# ---------------------------------------------------------------------------


class TestDatasetSizeCheck:
    def test_within_limit_construction(self) -> None:
        chk = DatasetSizeCheck(
            n_samples=100, n_features=10, within_limit=True, reason=None
        )
        assert chk.n_samples == 100
        assert chk.n_features == 10
        assert chk.within_limit is True
        assert chk.reason is None

    def test_over_limit_construction(self) -> None:
        chk = DatasetSizeCheck(
            n_samples=2000,
            n_features=10,
            within_limit=False,
            reason="too many samples",
        )
        assert chk.within_limit is False
        assert chk.reason == "too many samples"

    def test_frozen(self) -> None:
        chk = DatasetSizeCheck(n_samples=1, n_features=1, within_limit=True)
        with pytest.raises(Exception):
            chk.n_samples = 2  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            DatasetSizeCheck(  # type: ignore[call-arg]
                n_samples=1, n_features=1, within_limit=True, extra="x"
            )


# ---------------------------------------------------------------------------
# TabPFNShadowResult
# ---------------------------------------------------------------------------


class TestTabPFNShadowResult:
    def test_construction_defaults(self) -> None:
        cfg = TabPFNConfig()
        chk = DatasetSizeCheck(
            n_samples=10, n_features=5, within_limit=True
        )
        res = TabPFNShadowResult(config=cfg, size_check=chk)
        assert res.config is cfg
        assert res.size_check is chk
        assert res.predictions is None
        assert res.artifact_path is None
        assert res.is_shadow is True
        assert res.promotion_eligible is False
        assert res.leakage_check_passed is True
        assert res.metrics == {}

    def test_frozen(self) -> None:
        cfg = TabPFNConfig()
        chk = DatasetSizeCheck(
            n_samples=10, n_features=5, within_limit=True
        )
        res = TabPFNShadowResult(config=cfg, size_check=chk)
        with pytest.raises(Exception):
            res.predictions = [0.1]  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        cfg = TabPFNConfig()
        chk = DatasetSizeCheck(
            n_samples=10, n_features=5, within_limit=True
        )
        with pytest.raises(Exception):
            TabPFNShadowResult(  # type: ignore[call-arg]
                config=cfg, size_check=chk, unexpected="x"
            )

    def test_full_construction(self) -> None:
        cfg = TabPFNConfig()
        chk = DatasetSizeCheck(
            n_samples=10, n_features=5, within_limit=True
        )
        res = TabPFNShadowResult(
            config=cfg,
            size_check=chk,
            predictions=[0.1, 0.9],
            artifact_path="/tmp/x.json",
            is_shadow=False,
            promotion_eligible=True,
            leakage_check_passed=True,
            metrics={"accuracy": 0.95},
        )
        assert res.predictions == [0.1, 0.9]
        assert res.artifact_path == "/tmp/x.json"
        assert res.is_shadow is False
        assert res.promotion_eligible is True
        assert res.metrics == {"accuracy": 0.95}


# ---------------------------------------------------------------------------
# check_dataset_size
# ---------------------------------------------------------------------------


class TestCheckDatasetSize:
    def test_within_limit(self) -> None:
        cfg = TabPFNConfig()
        chk = check_dataset_size(500, 50, cfg)
        assert chk.within_limit is True
        assert chk.reason is None
        assert chk.n_samples == 500
        assert chk.n_features == 50

    def test_over_sample_limit(self) -> None:
        cfg = TabPFNConfig(max_train_samples=1000, max_features=100)
        chk = check_dataset_size(1500, 50, cfg)
        assert chk.within_limit is False
        assert chk.reason is not None
        assert "n_samples=1500" in chk.reason
        assert "max_train_samples=1000" in chk.reason

    def test_over_feature_limit(self) -> None:
        cfg = TabPFNConfig(max_train_samples=1000, max_features=100)
        chk = check_dataset_size(500, 150, cfg)
        assert chk.within_limit is False
        assert chk.reason is not None
        assert "n_features=150" in chk.reason
        assert "max_features=100" in chk.reason

    def test_over_both_limits(self) -> None:
        cfg = TabPFNConfig(max_train_samples=1000, max_features=100)
        chk = check_dataset_size(2000, 200, cfg)
        assert chk.within_limit is False
        assert chk.reason is not None
        assert "n_samples=2000" in chk.reason
        assert "n_features=200" in chk.reason

    def test_exactly_at_limit(self) -> None:
        cfg = TabPFNConfig(max_train_samples=1000, max_features=100)
        chk = check_dataset_size(1000, 100, cfg)
        assert chk.within_limit is True
        assert chk.reason is None

    def test_zero_samples_within_limit(self) -> None:
        cfg = TabPFNConfig()
        chk = check_dataset_size(0, 10, cfg)
        assert chk.within_limit is True


# ---------------------------------------------------------------------------
# detect_in_context_leakage
# ---------------------------------------------------------------------------


class TestDetectInContextLeakage:
    def test_no_leakage(self) -> None:
        train = [[1.0, 2.0], [3.0, 4.0]]
        labels = [0, 1]
        test = [[5.0, 6.0], [7.0, 8.0]]
        assert detect_in_context_leakage(train, labels, test) is True

    def test_exact_row_match(self) -> None:
        train = [[1.0, 2.0], [3.0, 4.0]]
        labels = [0, 1]
        test = [[1.0, 2.0], [7.0, 8.0]]  # first row duplicates train row
        assert detect_in_context_leakage(train, labels, test) is False

    def test_label_embedding_in_test_feature(self) -> None:
        # train label value 1.0 appears as a feature in the test row.
        train = [[1.0, 2.0], [3.0, 4.0]]
        labels = [0, 1]
        test = [[5.0, 1.0]]  # 1.0 is a train label
        assert detect_in_context_leakage(train, labels, test) is False

    def test_empty_test_data(self) -> None:
        train = [[1.0, 2.0]]
        labels = [0]
        assert detect_in_context_leakage(train, labels, []) is True

    def test_none_test_data(self) -> None:
        train = [[1.0, 2.0]]
        labels = [0]
        assert detect_in_context_leakage(train, labels, None) is True

    def test_no_labels_no_leakage(self) -> None:
        train = [[1.0, 2.0], [3.0, 4.0]]
        test = [[5.0, 6.0]]
        assert detect_in_context_leakage(train, None, test) is True

    def test_all_test_rows_duplicate_train(self) -> None:
        train = [[1.0, 2.0], [3.0, 4.0]]
        labels = [0, 1]
        test = [[1.0, 2.0], [3.0, 4.0]]
        assert detect_in_context_leakage(train, labels, test) is False

    def test_label_value_not_in_features_no_leakage(self) -> None:
        # label value 99.0 does not appear in any test feature.
        train = [[1.0, 2.0], [3.0, 4.0]]
        labels = [0, 99.0]
        test = [[5.0, 6.0], [7.0, 8.0]]
        assert detect_in_context_leakage(train, labels, test) is True


# ---------------------------------------------------------------------------
# TabPFNShadowAdapter
# ---------------------------------------------------------------------------


class TestTabPFNShadowAdapter:
    def test_init_rejects_non_config(self) -> None:
        with pytest.raises(TypeError):
            TabPFNShadowAdapter("not a config")  # type: ignore[arg-type]

    def test_shadow_only_default_promotion_ineligible(
        self, fake_tabpfn: types.ModuleType
    ) -> None:
        cfg = TabPFNConfig()  # shadow_only=True by default
        adapter = TabPFNShadowAdapter(cfg)
        train = [[float(i), float(i + 1)] for i in range(10)]
        labels = [float(i % 2) for i in range(10)]
        test = [[100.0, 101.0], [102.0, 103.0]]
        result = adapter.run_shadow(train, labels, test, [0.0, 1.0])
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert result.leakage_check_passed is True
        assert result.predictions is not None

    def test_shadow_only_false_promotion_eligible(
        self, fake_tabpfn: types.ModuleType
    ) -> None:
        cfg = TabPFNConfig(shadow_only=False)
        adapter = TabPFNShadowAdapter(cfg)
        train = [[float(i), float(i + 1)] for i in range(10)]
        labels = [float(i % 2) for i in range(10)]
        test = [[100.0, 101.0], [102.0, 103.0]]
        result = adapter.run_shadow(train, labels, test, [0.0, 1.0])
        assert result.is_shadow is False
        assert result.promotion_eligible is True

    def test_fail_closed_oversized_dataset(
        self, fake_tabpfn: types.ModuleType
    ) -> None:
        cfg = TabPFNConfig(max_train_samples=100, max_features=100)
        adapter = TabPFNShadowAdapter(cfg)
        # 200 samples exceeds the 100 limit.
        train = [[float(i), float(i + 1)] for i in range(200)]
        labels = [float(i % 2) for i in range(200)]
        test = [[100.0, 101.0]]
        result = adapter.run_shadow(train, labels, test, [0.0])
        assert result.predictions is None
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert result.size_check.within_limit is False
        assert result.size_check.reason is not None
        assert "status_oversized" in result.metrics

    def test_fail_closed_over_feature_limit(
        self, fake_tabpfn: types.ModuleType
    ) -> None:
        cfg = TabPFNConfig(max_train_samples=1000, max_features=5)
        adapter = TabPFNShadowAdapter(cfg)
        # 10 features exceeds the 5 limit.
        train = [[float(i + j) for j in range(10)] for i in range(10)]
        labels = [float(i % 2) for i in range(10)]
        test = [[float(100 + j) for j in range(10)]]
        result = adapter.run_shadow(train, labels, test, [0.0])
        assert result.predictions is None
        assert result.size_check.within_limit is False
        assert "n_features" in (result.size_check.reason or "")

    def test_fail_closed_leakage_exact_row(
        self, fake_tabpfn: types.ModuleType
    ) -> None:
        cfg = TabPFNConfig()
        adapter = TabPFNShadowAdapter(cfg)
        train = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
        labels = [0.0, 1.0, 0.0]
        # test row [1.0, 2.0] duplicates a train row.
        test = [[1.0, 2.0], [7.0, 8.0]]
        result = adapter.run_shadow(train, labels, test, [0.0, 1.0])
        assert result.predictions is None
        assert result.leakage_check_passed is False
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert "status_leakage_detected" in result.metrics

    def test_fail_closed_leakage_label_embedding(
        self, fake_tabpfn: types.ModuleType
    ) -> None:
        cfg = TabPFNConfig()
        adapter = TabPFNShadowAdapter(cfg)
        train = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
        labels = [0.0, 1.0, 0.0]
        # test feature contains 1.0 which is a train label.
        test = [[7.0, 1.0]]
        result = adapter.run_shadow(train, labels, test, [1.0])
        assert result.predictions is None
        assert result.leakage_check_passed is False

    def test_run_with_artifact_save(
        self,
        fake_tabpfn: types.ModuleType,
        tmp_path: Path,
    ) -> None:
        cfg = TabPFNConfig()
        adapter = TabPFNShadowAdapter(cfg)
        train = [[float(i), float(i + 1)] for i in range(10)]
        labels = [float(i % 2) for i in range(10)]
        test = [[100.0, 101.0], [102.0, 103.0]]
        art = tmp_path / "result.json"
        result = adapter.run_shadow(
            train, labels, test, [0.0, 1.0], artifact_path=str(art)
        )
        assert result.artifact_path == str(art)
        assert art.exists()

    def test_regression_task_metrics(
        self,
        fake_tabpfn: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Provide canned regression predictions.
        mod = _make_fake_tabpfn_module(pred_rows=[1.0, 2.0, 3.0])
        monkeypatch.setitem(sys.modules, "tabpfn", mod)
        cfg = TabPFNConfig(task_type="regression")
        adapter = TabPFNShadowAdapter(cfg)
        train = [[float(i), float(i + 1)] for i in range(10)]
        labels = [float(i) for i in range(10)]
        test = [[100.0, 101.0], [102.0, 103.0], [104.0, 105.0]]
        result = adapter.run_shadow(train, labels, test, [1.0, 2.0, 3.0])
        assert result.predictions == [1.0, 2.0, 3.0]
        assert "rmse" in result.metrics
        assert "mae" in result.metrics
        # predictions exactly match labels -> rmse and mae are 0.
        assert result.metrics["rmse"] == 0.0
        assert result.metrics["mae"] == 0.0

    def test_binary_task_accuracy(
        self,
        fake_tabpfn: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Canned probabilities: [0.2, 0.8] -> positive prob 0.8 -> round=1.
        mod = _make_fake_tabpfn_module(
            proba_rows=[[0.2, 0.8], [0.9, 0.1]]
        )
        monkeypatch.setitem(sys.modules, "tabpfn", mod)
        cfg = TabPFNConfig(task_type="binary")
        adapter = TabPFNShadowAdapter(cfg)
        train = [[float(i), float(i + 1)] for i in range(10)]
        labels = [float(i % 2) for i in range(10)]
        test = [[100.0, 101.0], [102.0, 103.0]]
        # test_labels: [1, 0] — predictions [0.8, 0.1] round to [1, 0] -> 100%.
        result = adapter.run_shadow(train, labels, test, [1.0, 0.0])
        assert result.predictions == [0.8, 0.1]
        assert result.metrics["accuracy"] == 1.0

    def test_single_sample_train(
        self, fake_tabpfn: types.ModuleType
    ) -> None:
        cfg = TabPFNConfig()
        adapter = TabPFNShadowAdapter(cfg)
        train = [[1.0, 2.0]]
        labels = [0.0]
        test = [[3.0, 4.0]]
        result = adapter.run_shadow(train, labels, test, [0.0])
        assert result.size_check.within_limit is True
        assert result.predictions is not None

    def test_exactly_at_sample_limit(
        self, fake_tabpfn: types.ModuleType
    ) -> None:
        cfg = TabPFNConfig(max_train_samples=10, max_features=10)
        adapter = TabPFNShadowAdapter(cfg)
        train = [[float(i), float(i + 1)] for i in range(10)]
        labels = [float(i % 2) for i in range(10)]
        test = [[100.0, 101.0]]
        result = adapter.run_shadow(train, labels, test, [0.0])
        assert result.size_check.within_limit is True
        assert result.predictions is not None

    def test_empty_train_data(
        self, fake_tabpfn: types.ModuleType
    ) -> None:
        cfg = TabPFNConfig()
        adapter = TabPFNShadowAdapter(cfg)
        result = adapter.run_shadow([], [], [[1.0, 2.0]], [0.0])
        # 0 samples is within limit; inference runs on empty train.
        assert result.size_check.within_limit is True
        assert result.size_check.n_samples == 0


# ---------------------------------------------------------------------------
# validate_promotion_eligibility
# ---------------------------------------------------------------------------


class TestValidatePromotionEligibility:
    def _make_result(
        self, is_shadow: bool, promotion_eligible: bool
    ) -> TabPFNShadowResult:
        cfg = TabPFNConfig()
        chk = DatasetSizeCheck(
            n_samples=10, n_features=5, within_limit=True
        )
        return TabPFNShadowResult(
            config=cfg,
            size_check=chk,
            is_shadow=is_shadow,
            promotion_eligible=promotion_eligible,
        )

    def test_shadow_not_eligible_without_override(self) -> None:
        res = self._make_result(is_shadow=True, promotion_eligible=False)
        assert validate_promotion_eligibility(res) is False

    def test_shadow_eligible_with_override(self) -> None:
        res = self._make_result(is_shadow=True, promotion_eligible=False)
        assert validate_promotion_eligibility(res, manual_override=True) is True

    def test_non_shadow_eligible(self) -> None:
        res = self._make_result(is_shadow=False, promotion_eligible=True)
        assert validate_promotion_eligibility(res) is True

    def test_non_shadow_override_keeps_eligible(self) -> None:
        res = self._make_result(is_shadow=False, promotion_eligible=True)
        assert validate_promotion_eligibility(res, manual_override=True) is True

    def test_fail_closed_result_not_eligible(self) -> None:
        # A fail-closed result is shadow with no predictions.
        cfg = TabPFNConfig()
        chk = DatasetSizeCheck(
            n_samples=2000, n_features=10, within_limit=False, reason="too big"
        )
        res = TabPFNShadowResult(
            config=cfg,
            size_check=chk,
            predictions=None,
            is_shadow=True,
            promotion_eligible=False,
            leakage_check_passed=True,
            metrics={"status_oversized": 1.0},
        )
        assert validate_promotion_eligibility(res) is False
        # Even with override, a fail-closed oversized run should require
        # the explicit override — which authorises it.
        assert (
            validate_promotion_eligibility(res, manual_override=True) is True
        )


# ---------------------------------------------------------------------------
# Artifact save / load
# ---------------------------------------------------------------------------


class TestArtifactSaveLoad:
    def test_save_and_load_roundtrip(
        self, tmp_path: Path, fake_tabpfn: types.ModuleType
    ) -> None:
        cfg = TabPFNConfig()
        adapter = TabPFNShadowAdapter(cfg)
        train = [[float(i), float(i + 1)] for i in range(10)]
        labels = [float(i % 2) for i in range(10)]
        test = [[100.0, 101.0], [102.0, 103.0]]
        art = tmp_path / "art.json"
        result = adapter.run_shadow(
            train, labels, test, [0.0, 1.0], artifact_path=str(art)
        )
        assert art.exists()
        loaded = adapter.load_artifact(str(art))
        assert loaded == result

    def test_save_creates_parent_dirs(
        self, tmp_path: Path
    ) -> None:
        cfg = TabPFNConfig()
        chk = DatasetSizeCheck(
            n_samples=10, n_features=5, within_limit=True
        )
        res = TabPFNShadowResult(config=cfg, size_check=chk)
        adapter = TabPFNShadowAdapter(cfg)
        nested = tmp_path / "nested" / "dir" / "art.json"
        adapter.save_artifact(res, str(nested))
        assert nested.exists()
        loaded = adapter.load_artifact(str(nested))
        assert loaded == res

    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        cfg = TabPFNConfig()
        adapter = TabPFNShadowAdapter(cfg)
        with pytest.raises(FileNotFoundError):
            adapter.load_artifact(str(tmp_path / "nope.json"))

    def test_save_fail_closed_result_roundtrip(
        self, tmp_path: Path, fake_tabpfn: types.ModuleType
    ) -> None:
        cfg = TabPFNConfig(max_train_samples=5)
        adapter = TabPFNShadowAdapter(cfg)
        train = [[float(i), float(i + 1)] for i in range(20)]
        labels = [float(i % 2) for i in range(20)]
        test = [[100.0, 101.0]]
        art = tmp_path / "fail.json"
        result = adapter.run_shadow(
            train, labels, test, [0.0], artifact_path=str(art)
        )
        assert result.predictions is None
        assert art.exists()
        loaded = adapter.load_artifact(str(art))
        assert loaded.predictions is None
        assert loaded.size_check.within_limit is False


# ---------------------------------------------------------------------------
# register_tabpfn_family
# ---------------------------------------------------------------------------


class TestRegisterTabPFNFamily:
    def test_returns_dict_with_required_fields(self) -> None:
        spec = register_tabpfn_family()
        assert isinstance(spec, dict)
        for key in (
            "family_id",
            "display_name",
            "version",
            "dataset_shape",
            "objectives",
            "artifact_format",
            "artifact_loader",
            "required_metrics",
            "runpod_image",
            "requires_gpu",
            "max_budget_cents",
            "promotion_eligibility_class",
            "is_baseline_exception",
            "created_at_ns",
        ):
            assert key in spec, f"missing key: {key}"

    def test_family_id_is_tabpfn(self) -> None:
        spec = register_tabpfn_family()
        assert spec["family_id"] == "tabpfn"

    def test_shadow_only_flag(self) -> None:
        spec = register_tabpfn_family()
        assert spec["shadow_only"] is True

    def test_objectives_include_all_task_types(self) -> None:
        spec = register_tabpfn_family()
        objectives = set(spec["objectives"])
        assert {"binary", "multiclass", "regression"} <= objectives

    def test_hard_limits_recorded(self) -> None:
        spec = register_tabpfn_family()
        assert spec["max_train_samples"] == TABPFN_HARD_MAX_TRAIN_SAMPLES
        assert spec["max_features"] == TABPFN_HARD_MAX_FEATURES

    def test_not_baseline_exception(self) -> None:
        spec = register_tabpfn_family()
        assert spec["is_baseline_exception"] is False

    def test_created_at_ns_is_int(self) -> None:
        spec = register_tabpfn_family()
        assert isinstance(spec["created_at_ns"], int)
        assert spec["created_at_ns"] > 0

    def test_artifact_format_json(self) -> None:
        spec = register_tabpfn_family()
        assert spec["artifact_format"] == "json"

    def test_artifact_loader_name(self) -> None:
        spec = register_tabpfn_family()
        assert spec["artifact_loader"] == "tabpfn_shadow_result"

    def test_promotion_class_challenger(self) -> None:
        spec = register_tabpfn_family()
        assert spec["promotion_eligibility_class"] == "challenger"

    def test_deterministic_except_timestamp(self) -> None:
        spec1 = register_tabpfn_family()
        spec2 = register_tabpfn_family()
        # created_at_ns may differ; everything else should be identical.
        spec1.pop("created_at_ns")
        spec2.pop("created_at_ns")
        assert spec1 == spec2


# ---------------------------------------------------------------------------
# Integration: full shadow run end-to-end (mocked TabPFN)
# ---------------------------------------------------------------------------


class TestShadowRunIntegration:
    def test_full_shadow_run(
        self,
        fake_tabpfn: types.ModuleType,
        tmp_path: Path,
    ) -> None:
        cfg = TabPFNConfig()
        adapter = TabPFNShadowAdapter(cfg)
        train = [[float(i), float(i + 1)] for i in range(50)]
        labels = [float(i % 2) for i in range(50)]
        test = [[100.0, 101.0], [102.0, 103.0]]
        art = tmp_path / "shadow.json"
        result = adapter.run_shadow(
            train, labels, test, [0.0, 1.0], artifact_path=str(art)
        )
        # Shadow-only -> not promotion eligible.
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert validate_promotion_eligibility(result) is False
        # Manual override authorises promotion.
        assert (
            validate_promotion_eligibility(result, manual_override=True)
            is True
        )
        # Artifact saved and reloadable.
        assert art.exists()
        loaded = adapter.load_artifact(str(art))
        assert loaded == result

    def test_oversized_then_small_run(
        self,
        fake_tabpfn: types.ModuleType,
    ) -> None:
        """Adapter can be reused: oversized run fails, small run succeeds."""
        cfg = TabPFNConfig(max_train_samples=20)
        adapter = TabPFNShadowAdapter(cfg)
        # Oversized.
        big = [[float(i), float(i + 1)] for i in range(50)]
        big_labels = [float(i % 2) for i in range(50)]
        test = [[100.0, 101.0]]
        r1 = adapter.run_shadow(big, big_labels, test, [0.0])
        assert r1.predictions is None
        # Small.
        small = [[float(i), float(i + 1)] for i in range(10)]
        small_labels = [float(i % 2) for i in range(10)]
        r2 = adapter.run_shadow(small, small_labels, test, [0.0])
        assert r2.predictions is not None
        assert r2.size_check.within_limit is True
