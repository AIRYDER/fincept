"""Tests for the /models endpoint + list_models helper.

Uses tmp_path to seed a fake models/ directory with various meta.json
shapes (walk_forward, holdout_80_20, malformed, missing model.txt) and
verifies the route reports them correctly.

Auth and route mounting are exercised via the existing ``client`` +
``auth_headers`` fixtures from conftest.py.
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any

import pytest
from httpx import AsyncClient


def _write_meta(model_dir: pathlib.Path, meta: dict[str, Any]) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "meta.json").write_text(json.dumps(meta))


def _walk_forward_meta(*, trained_at: int) -> dict[str, Any]:
    return {
        "features": ["ret_1m", "ret_5m"],
        "horizon_bars": 15,
        "horizon_ns": 900_000_000_000,
        "trained_at": trained_at,
        "eval_mode": "walk_forward",
        "cv_summary": {
            "n_folds": 5,
            "n_scored": 5,
            "n_skipped": 0,
            "mean_auc": 0.55,
            "std_auc": 0.02,
            "min_auc": 0.52,
            "max_auc": 0.58,
            "median_best_iter": 100,
        },
        "final_train_rows": 10000,
        "final_num_boost_round": 100,
    }


def _training_request(*, model_name: str = "gbm_predictor") -> dict[str, Any]:
    return {
        "model_name": model_name,
        "input_path": "data/synth_bars.parquet",
        "horizon_bars": 15,
        "bar_seconds": 60,
        "cv_folds": 5,
        "purge_bars": -1,
        "embargo_bars": 0,
        "num_boost_round": 500,
        "early_stopping_rounds": 30,
    }


def _holdout_meta(*, trained_at: int) -> dict[str, Any]:
    return {
        "features": ["ret_1m"],
        "horizon_bars": 10,
        "horizon_ns": 600_000_000_000,
        "trained_at": trained_at,
        "eval_mode": "holdout_80_20",
        "train_rows": 800,
        "val_rows": 200,
        "best_iter": 73,
        "best_auc": 0.51,
    }


@pytest.fixture(autouse=True)
def _patch_models_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> pathlib.Path:
    """Redirect the models endpoint at a fresh tmp dir for each test."""
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    monkeypatch.setenv("TRAINING_RUNS_DIR", str(tmp_path / "training_runs"))
    monkeypatch.setattr("api.routes.models._MODELS_DIR", models_dir)
    monkeypatch.setattr(
        "api.routes.models._NEWS_ALPHA_CANDIDATE_REPORT",
        tmp_path / "reports" / "news_alpha_candidate_report.json",
    )
    return models_dir


# --------------------------------------------------------------------------- #
# list_models() helper                                                        #
# --------------------------------------------------------------------------- #


class TestListModels:
    def test_empty_dir_returns_empty_list(
        self, _patch_models_dir: pathlib.Path
    ) -> None:
        from api.routes.models import list_models

        assert list_models() == []

    def test_walk_forward_record(self, _patch_models_dir: pathlib.Path) -> None:
        from api.routes.models import list_models

        gbm = _patch_models_dir / "gbm_predictor"
        _write_meta(gbm, _walk_forward_meta(trained_at=int(time.time()) - 60))
        (gbm / "model.txt").write_text("fake lightgbm bytes")

        records = list_models()
        assert len(records) == 1
        rec = records[0]
        assert rec["name"] == "gbm_predictor"
        assert rec["model_file_exists"] is True
        assert rec["eval_mode"] == "walk_forward"
        assert rec["horizon_bars"] == 15
        assert rec["features"] == ["ret_1m", "ret_5m"]
        assert rec["feature_count"] == 2
        assert 0 <= rec["age_seconds"] < 120
        assert rec["cv_summary"] is not None
        assert rec["cv_summary"]["mean_auc"] == pytest.approx(0.55)
        assert rec["cv_summary"]["std_auc"] == pytest.approx(0.02)
        assert rec["cv_summary"]["median_best_iter"] == 100
        # walk_forward never populates legacy holdout fields.
        assert rec["holdout_auc"] is None
        assert rec["warnings"] == []

    def test_holdout_record(self, _patch_models_dir: pathlib.Path) -> None:
        from api.routes.models import list_models

        gbm = _patch_models_dir / "gbm_predictor"
        _write_meta(gbm, _holdout_meta(trained_at=int(time.time())))
        (gbm / "model.txt").write_text("fake lightgbm bytes")

        records = list_models()
        rec = records[0]
        assert rec["eval_mode"] == "holdout_80_20"
        assert rec["holdout_auc"] == pytest.approx(0.51)
        assert rec["holdout_rows"] == 200
        # holdout never populates cv_summary.
        assert rec["cv_summary"] is None
        assert rec["warnings"] == []

    def test_training_request_attached_from_latest_completed_run(
        self, _patch_models_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        from api.routes.models import list_models

        gbm = _patch_models_dir / "gbm_predictor"
        _write_meta(gbm, _walk_forward_meta(trained_at=int(time.time())))
        (gbm / "model.txt").write_text("fake lightgbm bytes")

        runs_dir = tmp_path / "training_runs"
        runs_dir.mkdir()
        request = _training_request()
        (runs_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": "run",
                    "status": "completed",
                    "created_at": time.time(),
                    "request": request,
                }
            )
        )

        records = list_models()
        rec = records[0]
        assert rec["training_input_path"] == "data/synth_bars.parquet"
        assert rec["training_request"] == request

    def test_missing_model_file_warns_but_returns_record(
        self, _patch_models_dir: pathlib.Path
    ) -> None:
        from api.routes.models import list_models

        gbm = _patch_models_dir / "gbm_predictor"
        _write_meta(gbm, _walk_forward_meta(trained_at=1_000))
        # no model.txt

        records = list_models()
        rec = records[0]
        assert rec["model_file_exists"] is False
        assert "model.txt missing" in rec["warnings"]

    def test_malformed_meta_warns_but_returns_record(
        self, _patch_models_dir: pathlib.Path
    ) -> None:
        from api.routes.models import list_models

        gbm = _patch_models_dir / "gbm_predictor"
        gbm.mkdir()
        (gbm / "meta.json").write_text("{bad json")
        (gbm / "model.txt").write_text("fake")

        records = list_models()
        rec = records[0]
        assert any("malformed" in w for w in rec["warnings"])
        # Without a meta we can't classify - all the eval/feature fields
        # should be at their default empty values, not crash.
        assert rec["eval_mode"] is None
        assert rec["features"] == []
        assert rec["cv_summary"] is None

    def test_subdirs_without_meta_are_skipped(
        self, _patch_models_dir: pathlib.Path
    ) -> None:
        """An unrelated subdir (e.g. cache/) shouldn't show up at all."""
        from api.routes.models import list_models

        (_patch_models_dir / "cache").mkdir()
        (_patch_models_dir / "cache" / "stats.txt").write_text("...")
        # Plus a real model:
        gbm = _patch_models_dir / "gbm_predictor"
        _write_meta(gbm, _walk_forward_meta(trained_at=int(time.time())))

        records = list_models()
        assert len(records) == 1
        assert records[0]["name"] == "gbm_predictor"

    def test_non_existent_root_returns_empty(self, tmp_path: pathlib.Path) -> None:
        from api.routes.models import list_models

        missing = tmp_path / "definitely_not_here"
        assert list_models(root=missing) == []

    def test_multiple_models_sorted_by_name(
        self, _patch_models_dir: pathlib.Path
    ) -> None:
        from api.routes.models import list_models

        for name in ("zeta_model", "alpha_model", "gbm_predictor"):
            sub = _patch_models_dir / name
            _write_meta(sub, _walk_forward_meta(trained_at=int(time.time())))

        records = list_models()
        assert [r["name"] for r in records] == [
            "alpha_model",
            "gbm_predictor",
            "zeta_model",
        ]


# --------------------------------------------------------------------------- #
# /models route                                                               #
# --------------------------------------------------------------------------- #


class TestModelsRoute:
    @pytest.mark.asyncio
    async def test_requires_auth(self, client: AsyncClient) -> None:
        response = await client.get("/models")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_models_dir(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.get("/models", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["models"] == []
        assert body["summary"]["count"] == 0
        assert body["summary"]["with_cv"] == 0
        assert body["summary"]["with_holdout"] == 0
        assert body["summary"]["with_warnings"] == 0

    @pytest.mark.asyncio
    async def test_returns_walk_forward_record(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        _patch_models_dir: pathlib.Path,
    ) -> None:
        gbm = _patch_models_dir / "gbm_predictor"
        _write_meta(gbm, _walk_forward_meta(trained_at=int(time.time())))
        (gbm / "model.txt").write_text("fake")

        response = await client.get("/models", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["summary"]["count"] == 1
        assert body["summary"]["with_cv"] == 1
        assert body["summary"]["with_holdout"] == 0
        assert body["summary"]["with_warnings"] == 0
        rec = body["models"][0]
        assert rec["name"] == "gbm_predictor"
        assert rec["eval_mode"] == "walk_forward"
        assert rec["cv_summary"]["mean_auc"] == pytest.approx(0.55)

    @pytest.mark.asyncio
    async def test_summary_counts_warnings_and_modes(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        _patch_models_dir: pathlib.Path,
    ) -> None:
        # walk-forward, healthy
        wf = _patch_models_dir / "wf_model"
        _write_meta(wf, _walk_forward_meta(trained_at=int(time.time())))
        (wf / "model.txt").write_text("fake")
        # holdout, healthy
        ho = _patch_models_dir / "ho_model"
        _write_meta(ho, _holdout_meta(trained_at=int(time.time())))
        (ho / "model.txt").write_text("fake")
        # walk-forward, missing model.txt -> warning
        broken = _patch_models_dir / "broken_model"
        _write_meta(broken, _walk_forward_meta(trained_at=int(time.time())))

        response = await client.get("/models", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["summary"]["count"] == 3
        assert body["summary"]["with_cv"] == 2
        assert body["summary"]["with_holdout"] == 1
        assert body["summary"]["with_warnings"] == 1

    @pytest.mark.asyncio
    async def test_news_alpha_candidate_report_missing_returns_empty_state(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        response = await client.get(
            "/models/news-alpha/candidate-report",
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["exists"] is False
        assert body["report"] is None
        assert body["report_path"].endswith("news_alpha_candidate_report.json")

    @pytest.mark.asyncio
    async def test_news_alpha_candidate_report_returns_json(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report_path = tmp_path / "reports" / "news_alpha_candidate_report.json"
        monkeypatch.setattr(
            "api.routes.models._NEWS_ALPHA_CANDIDATE_REPORT",
            report_path,
        )
        report_path.parent.mkdir(parents=True)
        report_path.write_text(
            json.dumps(
                {
                    "approved": True,
                    "reasons": [],
                    "candidate_model_name": "news_alpha_predictor_candidate",
                    "candidate_dir": "models/news_alpha_predictor_candidate",
                    "candidate_meta": {"best_auc": 0.61},
                    "active_model_name": None,
                    "active_meta": None,
                    "policy": {"min_auc": 0.52},
                    "generated_at": 1_700_000_000.0,
                    "promotion_hint": {},
                }
            )
        )

        response = await client.get(
            "/models/news-alpha/candidate-report",
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["exists"] is True
        assert body["report"]["approved"] is True
        assert (
            body["report"]["candidate_model_name"] == "news_alpha_predictor_candidate"
        )


# --------------------------------------------------------------------------- #
# /models/{name} detail + feature-importance routes                           #
# --------------------------------------------------------------------------- #


def _walk_forward_meta_with_folds(*, trained_at: int) -> dict[str, Any]:
    """Same shape the trainer writes in walk-forward mode: full per-fold
    detail in addition to the aggregate cv_summary.  The detail endpoint
    should surface both."""
    base = _walk_forward_meta(trained_at=trained_at)
    base["cv_folds"] = [
        {
            "fold": 0,
            "train_rows": 1000,
            "val_rows": 200,
            "best_iter": 50,
            "best_auc": 0.55,
        },
        {
            "fold": 1,
            "train_rows": 1200,
            "val_rows": 200,
            "best_iter": 80,
            "best_auc": 0.58,
        },
        {
            "fold": 2,
            "train_rows": 1400,
            "val_rows": 200,
            "best_iter": 30,
            "best_auc": None,
            "reason_skipped": "single-class fold",
        },
    ]
    base["purge_bars"] = 15
    base["embargo_bars"] = 0
    return base


class TestModelDetailRoute:
    @pytest.mark.asyncio
    async def test_requires_auth(self, client: AsyncClient) -> None:
        response = await client.get("/models/anything")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_404_when_missing(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.get("/models/no_such_model", headers=auth_headers)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_record_with_cv_folds(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        _patch_models_dir: pathlib.Path,
    ) -> None:
        gbm = _patch_models_dir / "gbm_predictor"
        _write_meta(gbm, _walk_forward_meta_with_folds(trained_at=int(time.time())))
        (gbm / "model.txt").write_text("fake")

        response = await client.get("/models/gbm_predictor", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "gbm_predictor"
        assert body["eval_mode"] == "walk_forward"
        # The summary keeps working alongside the detail.
        assert body["cv_summary"]["mean_auc"] == pytest.approx(0.55)
        # New detail field — full per-fold breakdown.
        assert isinstance(body["cv_folds"], list)
        assert len(body["cv_folds"]) == 3
        f0 = body["cv_folds"][0]
        assert f0 == {
            "fold": 0,
            "train_rows": 1000,
            "val_rows": 200,
            "best_iter": 50,
            "best_auc": pytest.approx(0.55),
            "reason_skipped": None,
        }
        # Skipped fold preserves the reason and a None AUC.
        f2 = body["cv_folds"][2]
        assert f2["best_auc"] is None
        assert f2["reason_skipped"] == "single-class fold"
        # Training-config passthrough.
        assert body["purge_bars"] == 15
        assert body["embargo_bars"] == 0

    @pytest.mark.asyncio
    async def test_holdout_model_has_null_cv_folds(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        _patch_models_dir: pathlib.Path,
    ) -> None:
        gbm = _patch_models_dir / "ho_model"
        _write_meta(gbm, _holdout_meta(trained_at=int(time.time())))
        (gbm / "model.txt").write_text("fake")

        response = await client.get("/models/ho_model", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["eval_mode"] == "holdout_80_20"
        assert body["cv_folds"] is None
        assert body["holdout_auc"] == pytest.approx(0.51)

    @pytest.mark.asyncio
    async def test_rejects_path_with_slashes(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """A URL-decoded name containing a path separator never matches the
        ``{name}`` route — FastAPI returns 404 before our handler runs.

        That's defence-in-depth: even if a future router config widened
        the matcher, our validator rejects slashes/backslashes too (see
        the direct unit test below)."""
        response = await client.get("/models/a/b", headers=auth_headers)
        assert response.status_code in (400, 404)

    def test_resolve_model_dir_rejects_traversal(
        self, _patch_models_dir: pathlib.Path
    ) -> None:
        """Direct test of the validator: any name FastAPI does pass through
        as-is must be rejected if it contains separators or starts with a
        dot.  This is the layer that defends against future routing changes
        and bypassed URL-normalizers."""
        from fastapi import HTTPException

        from api.routes.models import _resolve_model_dir

        for bad in ("..", ".", "..\\foo", "../etc/passwd", "a/b", "a\\b", ""):
            with pytest.raises(HTTPException) as exc:
                _resolve_model_dir(bad)
            assert exc.value.status_code in (400, 404)


class TestFeatureImportanceRoute:
    @pytest.mark.asyncio
    async def test_requires_auth(self, client: AsyncClient) -> None:
        response = await client.get("/models/anything/feature-importance")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_404_when_missing(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.get(
            "/models/no_such_model/feature-importance", headers=auth_headers
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_text_parser_returns_split_counts(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        _patch_models_dir: pathlib.Path,
    ) -> None:
        gbm = _patch_models_dir / "gbm_predictor"
        meta = _walk_forward_meta(trained_at=int(time.time()))
        # Match the splits below: 3 features, idx 1 most-used.
        meta["features"] = ["alpha", "beta", "gamma"]
        _write_meta(gbm, meta)
        (gbm / "model.txt").write_text("split_feature=0 1 2 1\nsplit_feature=1 0\n")

        response = await client.get(
            "/models/gbm_predictor/feature-importance", headers=auth_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["model"] == "gbm_predictor"
        assert body["importance_type"] == "split_count"
        assert body["source"] == "model_text"
        # idx 1 ("beta") used 3 times; idx 0 ("alpha") used 2; idx 2 ("gamma") used 1.
        names = [r["feature"] for r in body["importances"]]
        assert names == ["beta", "alpha", "gamma"]
        assert body["importances"][0]["split_count"] == 3
        assert all(r["gain"] is None for r in body["importances"])

    @pytest.mark.asyncio
    async def test_sidecar_takes_precedence(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        _patch_models_dir: pathlib.Path,
    ) -> None:
        gbm = _patch_models_dir / "gbm_predictor"
        meta = _walk_forward_meta(trained_at=int(time.time()))
        meta["features"] = ["alpha", "beta"]
        _write_meta(gbm, meta)
        (gbm / "model.txt").write_text("split_feature=0 0 0\n")  # would say alpha=3
        (gbm / "feature_importance.json").write_text(
            json.dumps(
                {
                    "gain": {"alpha": 1.0, "beta": 4.0},
                    "split": {"alpha": 10, "beta": 40},
                }
            )
        )

        response = await client.get(
            "/models/gbm_predictor/feature-importance", headers=auth_headers
        )
        body = response.json()
        assert body["importance_type"] == "gain_and_split"
        assert body["source"] == "sidecar"
        # Sorted by gain desc => beta first.
        names = [r["feature"] for r in body["importances"]]
        assert names == ["beta", "alpha"]
        assert body["importances"][0]["gain"] == pytest.approx(4.0)
        assert body["importances"][0]["split_count"] == 40
