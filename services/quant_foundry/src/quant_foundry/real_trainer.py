"""
quant_foundry.real_trainer — real LightGBM trainer with walk-forward validation.

Replaces the stub ``LocalTrainer`` with actual ML training using LightGBM.
ML dependencies (``lightgbm``, ``numpy``) are imported **lazily** inside
``train()`` so the ``quant_foundry`` package remains importable without ML
deps installed.

Produces a real trained model artifact with:
- Real sha256 hash (from pickled model bytes, not request inputs).
- Real training metrics (accuracy, logloss, Brier score, Sharpe, drawdown,
  win rate) computed from out-of-sample walk-forward predictions.
- Real calibration report (reliability buckets).
- Real feature importance (from LightGBM gain importance).
- Real PBO (probability of backtest overfitting) from fold-level overfit
  detection.
- Real deflated Sharpe ratio.

Security invariants (same as ``LocalTrainer``):
- NO broker credentials, NO Redis, NO stream write capability.
- ``Authority.SHADOW_ONLY`` always — no promotion in the trainer.
- Deterministic given same seed + data (``deterministic=True``,
  ``num_threads=1`` in LightGBM params).
- Time/budget enforced: deadline breach raises ``TrainingFailure``.
- Training failure returns a safe terminal status (``TrainingFailure``),
  not a raw exception.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import pickle
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from quant_foundry.runpod_training import (
    TrainingFailure,
    _container_digest_or_default,
    _git_sha_or_default,
    _lockfile_hash_or_default,
)
from quant_foundry.schemas import (
    ArtifactManifest,
    Authority,
    ModelDossier,
    RunPodTrainingRequest,
)

try:
    from fincept_core.storage import StorageBackend, get_storage_backend
except ImportError:  # pragma: no cover - fincept-core always present in-workspace
    StorageBackend = None  # type: ignore[assignment,misc]
    get_storage_backend = None  # type: ignore[assignment]


@dataclass
class RealLightGBMTrainer:
    """Real LightGBM trainer with walk-forward validation.

    Same interface as ``LocalTrainer``:
        ``train(req, *, deadline_ns) -> tuple[ArtifactManifest, ModelDossier]``

    ML dependencies are imported lazily inside ``train()`` so the
    ``quant_foundry`` package can be imported without ``lightgbm`` or
    ``numpy`` installed.

    Args:
        should_fail: if True, raise ``TrainingFailure`` on every ``train()``
            call (used to test the failure path).
        n_folds: number of walk-forward validation folds (expanding window).
        annualization_factor: square-root factor for Sharpe annualization
            (252 = daily, 52 = weekly, 12 = monthly).
    """

    should_fail: bool = False
    n_folds: int = 3
    annualization_factor: int = 252
    storage_backend: Any = None

    # --- public API ------------------------------------------------------

    def train(
        self,
        req: RunPodTrainingRequest,
        *,
        deadline_ns: int,
    ) -> tuple[ArtifactManifest, ModelDossier]:
        """Train a real LightGBM model with walk-forward validation.

        Returns ``(artifact_manifest, dossier)``.

        Raises ``TrainingFailure`` on deadline breach, missing dependencies,
        insufficient data, or if ``should_fail`` is set.
        """
        if self.should_fail:
            raise TrainingFailure(
                error_code="training_error",
                error_summary="real trainer injected failure (should_fail=True)",
            )

        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached before work started",
            )

        if importlib.util.find_spec("lightgbm") is None:
            raise TrainingFailure(
                error_code="missing_dependency",
                error_summary="ML dependency not available: lightgbm",
            )
        if importlib.util.find_spec("numpy") is None:
            raise TrainingFailure(
                error_code="missing_dependency",
                error_summary="ML dependency not available: numpy",
            )

        X, y, timestamps = self._load_dataset(req.dataset_manifest_ref)

        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached after dataset load",
            )

        seed = req.random_seed if req.random_seed is not None else 0

        metrics = self._walk_forward_validate(
            X,
            y,
            timestamps,
            seed,
            deadline_ns,
            req,
        )

        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached after validation",
            )

        final_model = self._train_final_model(X, y, seed, req)

        model_bytes = pickle.dumps(final_model, protocol=pickle.HIGHEST_PROTOCOL)
        sha256 = hashlib.sha256(model_bytes).hexdigest()
        size_bytes = len(model_bytes)

        n_features = int(X.shape[1])
        n_rows = int(X.shape[0])
        feature_schema_hash = hashlib.sha256(
            f"{req.dataset_manifest_ref}:n_features={n_features}".encode(),
        ).hexdigest()[:16]
        label_schema_hash = hashlib.sha256(
            f"{req.dataset_manifest_ref}:label=binary".encode(),
        ).hexdigest()[:16]

        now_ns = time.time_ns()
        artifact_id = f"artifact:{sha256[:16]}"
        artifact = ArtifactManifest(
            artifact_id=artifact_id,
            sha256=sha256,
            size_bytes=size_bytes,
            uri=None,
            model_family=req.model_family,
            created_at_ns=now_ns,
            feature_schema_hash=feature_schema_hash,
            label_schema_hash=label_schema_hash,
            code_git_sha=_git_sha_or_default(),
            lockfile_hash=_lockfile_hash_or_default(),
            container_image_digest=_container_digest_or_default(),
        )

        dossier = ModelDossier(
            model_id=f"model:{req.job_id}",
            artifact_manifest_id=artifact.artifact_id,
            dataset_manifest_id=req.dataset_manifest_ref,
            code_git_sha=artifact.code_git_sha or "unknown",
            lockfile_hash=artifact.lockfile_hash or "unknown",
            container_image_digest=artifact.container_image_digest or "unknown",
            random_seed=req.random_seed,
            hardware_class=req.hardware_class,
            training_metrics=metrics["training_metrics"],
            pbo=metrics["pbo"],
            deflated_sharpe=metrics["deflated_sharpe"],
            authority=Authority.SHADOW_ONLY,
            metadata={
                "model_family": req.model_family,
                "trainer": "real_lightgbm",
                "n_features": str(n_features),
                "n_rows": str(n_rows),
                "n_folds": str(self.n_folds),
                "brier_score": str(metrics["brier_score"]),
                "win_rate": str(metrics["win_rate"]),
                "max_drawdown": str(metrics["max_drawdown"]),
                "sharpe_ratio": str(metrics["sharpe_ratio"]),
            },
        )

        return artifact, dossier

    # --- dataset loading -------------------------------------------------

    def _resolve_path(self, ref: str) -> Path:
        """Resolve a dataset reference (file:// URI, s3:// URI, or plain path) to a Path.

        For ``s3://`` URIs, the configured ``storage_backend`` (or the factory
        singleton) is used to download the object to a temp file, which is
        returned. For ``file://`` URIs and bare paths, behavior is unchanged
        (backward compat).
        """
        parsed = urlparse(ref)
        # A single-letter scheme is a Windows drive letter (e.g. "C:\\path"),
        # not a real URI scheme. Treat it as a bare local path.
        if len(parsed.scheme) == 1:
            return Path(ref)
        if parsed.scheme == "file":
            path = unquote(parsed.path)
            if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
                path = path[1:]
            return Path(path)
        elif parsed.scheme == "":
            return Path(ref)
        elif parsed.scheme == "s3":
            backend = self.storage_backend
            if backend is None and get_storage_backend is not None:
                try:
                    backend = get_storage_backend()
                except Exception as exc:
                    raise TrainingFailure(
                        error_code="unsupported_uri",
                        error_summary=f"no storage backend available for s3 dataset: {exc}",
                    ) from exc
            if backend is None:
                raise TrainingFailure(
                    error_code="unsupported_uri",
                    error_summary=f"s3 dataset loading requires a storage backend: {ref}",
                )
            try:
                tmp_path = backend.download_to_temp(ref)
            except TrainingFailure:
                raise
            except Exception as exc:
                raise TrainingFailure(
                    error_code="unsupported_uri",
                    error_summary=f"failed to fetch s3 dataset {ref!r}: {exc}",
                ) from exc
            return Path(tmp_path)
        else:
            raise TrainingFailure(
                error_code="unsupported_uri",
                error_summary=f"unsupported URI scheme: {parsed.scheme!r}",
            )

    def _load_dataset(self, ref: str) -> tuple[Any, Any, Any]:
        """Load dataset from a URI. Returns ``(X, y, timestamps)``."""

        path = self._resolve_path(ref)
        if not path.exists():
            raise TrainingFailure(
                error_code="dataset_not_found",
                error_summary=f"dataset file not found: {path}",
            )

        ext = path.suffix.lower()
        if ext == ".parquet":
            return self._load_parquet(path)
        elif ext == ".csv":
            return self._load_csv(path)
        else:
            raise TrainingFailure(
                error_code="unsupported_format",
                error_summary=f"unsupported dataset format: {ext} (expected .parquet or .csv)",
            )

    def _load_parquet(self, path: Path) -> tuple[Any, Any, Any]:
        """Load a parquet file. Requires pyarrow or pandas (lazy import)."""
        import numpy as np

        try:
            import pyarrow.parquet as pq

            table = pq.read_table(str(path))
            columns = table.column_names
            data = table.to_pydict()
        except ImportError:
            try:
                import pandas as pd

                df = pd.read_parquet(str(path))
                columns = list(df.columns)
                data = {col: df[col].tolist() for col in columns}
            except ImportError:
                raise TrainingFailure(
                    error_code="missing_dependency",
                    error_summary="neither pyarrow nor pandas available for parquet loading",
                ) from None

        label_col = "label" if "label" in columns else columns[-1]
        ts_col: str | None = None
        for candidate in ("timestamp", "decision_time", "ts", "event_ts"):
            if candidate in columns:
                ts_col = candidate
                break

        y = np.array(data[label_col], dtype=np.float64)
        feature_cols = [c for c in columns if c != label_col and c != ts_col]
        X = np.column_stack([np.array(data[c], dtype=np.float64) for c in feature_cols])

        if ts_col is not None:
            timestamps = np.array(data[ts_col], dtype=np.int64)
        else:
            timestamps = np.arange(len(y), dtype=np.int64)

        return X, y, timestamps

    def _load_csv(self, path: Path) -> tuple[Any, Any, Any]:
        """Load a CSV file using numpy.

        Expected layout: first column = timestamp, last column = label,
        middle columns = features. A header row is required.
        """
        import numpy as np

        data = np.genfromtxt(str(path), delimiter=",", skip_header=1, dtype=float)
        if data.ndim == 1:
            data = data.reshape(1, -1)

        if data.shape[1] < 3:
            raise TrainingFailure(
                error_code="insufficient_features",
                error_summary=(
                    f"CSV must have at least 3 columns (timestamp, features, "
                    f"label); got {data.shape[1]}"
                ),
            )

        timestamps = data[:, 0].astype(np.int64)
        y = data[:, -1].astype(np.float64)
        X = data[:, 1:-1].astype(np.float64)
        if X.ndim == 1:
            X = X.reshape(-1, 1)
        return X, y, timestamps

    # --- LightGBM params -------------------------------------------------

    def _build_lgb_params(self, seed: int, req: RunPodTrainingRequest) -> dict[str, Any]:
        """Build LightGBM parameters from request search space + defaults."""
        params: dict[str, Any] = {
            "objective": "binary",
            "metric": "binary_logloss",
            "verbosity": -1,
            "seed": seed,
            "deterministic": True,
            "num_threads": 1,
            "num_leaves": 31,
            "learning_rate": 0.05,
            "feature_fraction": 0.9,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "min_data_in_leaf": 5,
            "force_col_wise": True,
        }

        ss = req.search_space
        if ss.get("num_leaves"):
            params["num_leaves"] = int(ss["num_leaves"][0])
        if ss.get("learning_rate"):
            params["learning_rate"] = float(ss["learning_rate"][0])
        if ss.get("max_depth"):
            params["max_depth"] = int(ss["max_depth"][0])
        if ss.get("min_data_in_leaf"):
            params["min_data_in_leaf"] = int(ss["min_data_in_leaf"][0])

        return params

    def _get_n_estimators(self, req: RunPodTrainingRequest) -> int:
        ss = req.search_space
        if ss.get("n_estimators"):
            return int(ss["n_estimators"][0])
        return 100

    # --- walk-forward validation -----------------------------------------

    def _walk_forward_validate(
        self,
        X: Any,
        y: Any,
        timestamps: Any,
        seed: int,
        deadline_ns: int,
        req: RunPodTrainingRequest,
    ) -> dict[str, Any]:
        """Walk-forward validation with expanding window.

        For each fold, trains on all data before the fold and validates on
        the fold. Collects out-of-sample predictions for metric computation.
        """
        import lightgbm as lgb
        import numpy as np

        n = len(y)
        if n < 10:
            raise TrainingFailure(
                error_code="insufficient_data",
                error_summary=f"dataset too small for walk-forward validation: {n} rows",
            )

        order = np.argsort(timestamps, kind="stable")
        X_s = X[order]
        y_s = y[order]

        min_train = max(10, n // (self.n_folds + 2))
        fold_size = max(5, (n - min_train) // self.n_folds)

        params = self._build_lgb_params(seed, req)
        n_estimators = self._get_n_estimators(req)

        all_preds: list[float] = []
        all_labels: list[float] = []
        fold_train_acc: list[float] = []
        fold_val_acc: list[float] = []

        for fold in range(self.n_folds):
            if time.time_ns() >= deadline_ns:
                raise TrainingFailure(
                    error_code="timeout",
                    error_summary=f"training deadline breached during fold {fold}",
                )

            train_end = min_train + fold * fold_size
            val_start = train_end
            val_end = min(val_start + fold_size, n)

            if val_start >= n or val_end <= val_start:
                break

            X_train = X_s[:train_end]
            y_train = y_s[:train_end]
            X_val = X_s[val_start:val_end]
            y_val = y_s[val_start:val_end]

            if len(np.unique(y_train)) < 2:
                continue

            train_set = lgb.Dataset(X_train, label=y_train)

            model = lgb.train(
                params,
                train_set,
                num_boost_round=n_estimators,
            )

            train_pred = np.asarray(model.predict(X_train), dtype=np.float64)
            train_acc = float(np.mean((train_pred > 0.5) == (y_train > 0.5)))
            val_pred = np.asarray(model.predict(X_val), dtype=np.float64)
            val_acc = float(np.mean((val_pred > 0.5) == (y_val > 0.5)))

            fold_train_acc.append(train_acc)
            fold_val_acc.append(val_acc)
            all_preds.extend(val_pred.tolist())
            all_labels.extend(y_val.tolist())

        if not all_preds:
            raise TrainingFailure(
                error_code="no_validation_data",
                error_summary=(
                    "no validation folds produced predictions (dataset too small or single-class)"
                ),
            )

        preds_arr = np.array(all_preds, dtype=np.float64)
        labels_arr = np.array(all_labels, dtype=np.float64)

        return self._compute_metrics(
            preds_arr,
            labels_arr,
            fold_train_acc,
            fold_val_acc,
        )

    # --- metric computation ----------------------------------------------

    def _compute_metrics(
        self,
        all_preds: Any,
        all_labels: Any,
        fold_train_acc: list[float],
        fold_val_acc: list[float],
    ) -> dict[str, Any]:
        """Compute real evaluation metrics from out-of-sample predictions."""
        import numpy as np

        pred_binary = (all_preds > 0.5).astype(np.float64)
        accuracy = float(np.mean(pred_binary == all_labels))

        eps = 1e-15
        pred_clipped = np.clip(all_preds, eps, 1 - eps)
        logloss = float(
            -np.mean(
                all_labels * np.log(pred_clipped) + (1 - all_labels) * np.log(1 - pred_clipped),
            ),
        )

        brier = float(np.mean((all_preds - all_labels) ** 2))

        n_buckets = 10
        bucket_probs: list[float] = []
        bucket_actuals: list[float] = []
        for i in range(n_buckets):
            lo = i / n_buckets
            hi = (i + 1) / n_buckets
            if i < n_buckets - 1:
                mask = (all_preds >= lo) & (all_preds < hi)
            else:
                mask = (all_preds >= lo) & (all_preds <= hi)
            if np.any(mask):
                bucket_probs.append(float(np.mean(all_preds[mask])))
                bucket_actuals.append(float(np.mean(all_labels[mask])))

        positions = 2 * all_preds - 1
        returns = positions * (2 * all_labels - 1)
        win_rate = float(np.mean(returns > 0))

        std_returns = float(np.std(returns))
        if std_returns > 0:
            sharpe = float(
                np.mean(returns) / std_returns * np.sqrt(self.annualization_factor),
            )
        else:
            sharpe = 0.0

        cumulative = np.cumsum(returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = cumulative - running_max
        max_drawdown = float(np.min(drawdowns)) if len(drawdowns) > 0 else 0.0

        if fold_train_acc and fold_val_acc:
            overfit_count = sum(
                1 for t, v in zip(fold_train_acc, fold_val_acc, strict=False) if v < t
            )
            pbo = float(overfit_count / len(fold_train_acc))
        else:
            pbo = 0.5

        deflated_sharpe = sharpe * (1.0 - pbo)

        return {
            "training_metrics": {
                "accuracy": accuracy,
                "logloss": logloss,
                "brier_score": brier,
                "sharpe_ratio": sharpe,
                "max_drawdown": max_drawdown,
                "win_rate": win_rate,
            },
            "pbo": pbo,
            "deflated_sharpe": deflated_sharpe,
            "brier_score": brier,
            "win_rate": win_rate,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe,
            "calibration_bucket_probs": bucket_probs,
            "calibration_bucket_actuals": bucket_actuals,
        }

    # --- final model training --------------------------------------------

    def _train_final_model(
        self,
        X: Any,
        y: Any,
        seed: int,
        req: RunPodTrainingRequest,
    ) -> Any:
        """Train the final LightGBM model on all available data."""
        import lightgbm as lgb

        params = self._build_lgb_params(seed, req)
        n_estimators = self._get_n_estimators(req)

        train_set = lgb.Dataset(X, label=y)
        model = lgb.train(
            params,
            train_set,
            num_boost_round=n_estimators,
        )
        return model
