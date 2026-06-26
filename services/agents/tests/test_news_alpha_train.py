from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import fakeredis.aioredis
import lightgbm as lgb
import numpy as np
import polars as pl
import pytest

from agents.news_alpha_predictor.features import DEFAULT_FEATURES
from agents.news_alpha_predictor.train import (
    build_dataset,
    export_labeled_examples,
    save_artifacts,
    train_booster,
)
from agents.news_outcome_labeler.store import NewsOutcomeStore
from fincept_core.schemas import FeatureFrame

NS_PER_MIN = 60 * 1_000_000_000


def _frame(*, idx: int, ret: float) -> FeatureFrame:
    values = {name: 0.0 for name in DEFAULT_FEATURES}
    values["sentiment_30m"] = ret
    values["sentiment_30m_confidence"] = abs(ret)
    values["sentiment_30m_article_count"] = 1.0
    return FeatureFrame(
        symbol="NVDA",
        ts_event=(100 + idx) * NS_PER_MIN,
        freq="sentiment",
        values=values,
        tags={"latest_event_category": "earnings"},
    )


async def test_export_labeled_examples_reads_matured_redis_records() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    store = NewsOutcomeStore(redis, horizons_ns={"30m": 30 * NS_PER_MIN})

    try:
        positive = _frame(idx=1, ret=0.8)
        negative = _frame(idx=2, ret=-0.8)
        pos_id = await store.capture_snapshot(positive, start_price=Decimal("100"))
        neg_id = await store.capture_snapshot(negative, start_price=Decimal("100"))
        assert pos_id is not None
        assert neg_id is not None

        async def lookup(symbol: str, ts_event: int) -> Decimal | None:
            return (
                Decimal("102")
                if ts_event == positive.ts_event + 30 * NS_PER_MIN
                else Decimal("98")
            )

        await store.label_due(
            now_ns=negative.ts_event + 30 * NS_PER_MIN, price_lookup=lookup
        )
        df = await export_labeled_examples(redis, horizon="30m")

        assert df.height == 2
        assert set(df["target_30m"].to_list()) == {0, 1}
        assert "sentiment_30m" in df.columns
        assert df["ts_event"].to_list() == sorted(df["ts_event"].to_list())
    finally:
        await redis.aclose()


def _training_frame(rows: int = 120) -> pl.DataFrame:
    rng = np.random.default_rng(0)
    y = np.array([i % 2 for i in range(rows)], dtype=int)
    data = {
        "symbol": ["NVDA"] * rows,
        "ts_event": list(range(rows)),
        "horizon": ["30m"] * rows,
        "forward_return": [0.01 if label else -0.01 for label in y],
        "target_30m": y,
    }
    for feature in DEFAULT_FEATURES:
        data[feature] = rng.normal(0, 0.1, rows)
    data["sentiment_30m"] = y * 2 - 1
    return pl.DataFrame(data)


def test_build_dataset_rejects_single_class_target() -> None:
    df = _training_frame(rows=20).with_columns(pl.lit(1).alias("target_30m"))

    with pytest.raises(ValueError, match="two classes"):
        build_dataset(df, horizon="30m")


def test_build_dataset_rejects_too_few_rows() -> None:
    df = _training_frame(rows=20)

    with pytest.raises(ValueError, match="need at least 21"):
        build_dataset(df, horizon="30m", min_rows=21)


def test_train_and_save_artifacts_are_loadable(tmp_path: pathlib.Path) -> None:
    df = _training_frame(rows=120)
    X, y = build_dataset(df, horizon="30m")
    model, meta = train_booster(
        X,
        y,
        num_boost_round=30,
        early_stopping_rounds=5,
        val_fraction=0.25,
    )
    out_dir = tmp_path / "news_alpha"
    save_artifacts(
        model,
        out_dir=out_dir,
        feature_names=DEFAULT_FEATURES,
        horizon="30m",
        extra_meta=meta,
    )

    assert (out_dir / "model.txt").is_file()
    assert (out_dir / "meta.json").is_file()
    saved_meta = json.loads((out_dir / "meta.json").read_text())
    assert saved_meta["features"] == DEFAULT_FEATURES
    assert saved_meta["horizon"] == "30m"
    assert saved_meta["horizon_ns"] == 30 * NS_PER_MIN
    reloaded = lgb.Booster(model_file=str(out_dir / "model.txt"))
    preds = reloaded.predict(X[:5])
    assert len(preds) == 5
    assert all(0.0 <= pred <= 1.0 for pred in preds)
