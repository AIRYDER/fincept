from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import time
from typing import Any

import lightgbm as lgb
import numpy as np
import polars as pl
from fincept_core.config import get_settings
from fincept_core.schemas import FeatureFrame
from redis.asyncio import Redis

from agents.news_alpha_predictor.evaluate import (
    DEFAULT_REPORT_PATH,
    CandidateGatePolicy,
    evaluate_candidate,
    write_report,
)
from agents.news_alpha_predictor.features import DEFAULT_FEATURES, extract_sentiment_row
from agents.news_outcome_labeler.store import DEFAULT_HORIZONS_NS, EXAMPLE_KEY_TEMPLATE

LABEL_PREFIX = "label:{horizon}:return"
DEFAULT_DATASET_PATH = "data/news_alpha_training.parquet"
DEFAULT_MODEL_DIR = "models/news_alpha_predictor"


def _decode(raw: bytes | str | None) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode()
    return raw


def _target_column(horizon: str) -> str:
    return f"target_{horizon}"


def _label_field(horizon: str) -> str:
    return LABEL_PREFIX.format(horizon=horizon)


def _row_from_example(
    data: dict[str, str],
    *,
    horizon: str,
    feature_names: list[str],
) -> dict[str, Any] | None:
    raw_frame = data.get("frame")
    raw_return = data.get(_label_field(horizon))
    if raw_frame is None or raw_return is None:
        return None
    frame = FeatureFrame.model_validate_json(raw_frame)
    row = extract_sentiment_row(frame, feature_names=feature_names)
    if row is None:
        return None
    forward_return = float(raw_return)
    out: dict[str, Any] = {
        "symbol": frame.symbol,
        "ts_event": frame.ts_event,
        "horizon": horizon,
        "forward_return": forward_return,
        _target_column(horizon): int(forward_return > 0.0),
    }
    out.update(row)
    return out


async def export_labeled_examples(
    redis: Redis[Any],
    *,
    horizon: str = "30m",
    feature_names: list[str] | None = None,
    limit: int | None = None,
) -> pl.DataFrame:
    features = list(feature_names or DEFAULT_FEATURES)
    pattern = EXAMPLE_KEY_TEMPLATE.format(example_id="*")
    rows: list[dict[str, Any]] = []
    async for raw_key in redis.scan_iter(match=pattern):
        data = await redis.hgetall(raw_key)
        decoded = {
            key: value
            for key, value in ((_decode(k), _decode(v)) for k, v in data.items())
            if key is not None and value is not None
        }
        row = _row_from_example(decoded, horizon=horizon, feature_names=features)
        if row is not None:
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break
    if not rows:
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "ts_event": pl.Int64,
                "horizon": pl.String,
                "forward_return": pl.Float64,
                _target_column(horizon): pl.Int64,
                **{name: pl.Float64 for name in features},
            }
        )
    return pl.DataFrame(rows).sort(["ts_event", "symbol"])


def build_dataset(
    df: pl.DataFrame,
    *,
    horizon: str = "30m",
    feature_names: list[str] | None = None,
    min_rows: int = 1,
) -> tuple[np.ndarray, np.ndarray]:
    features = list(feature_names or DEFAULT_FEATURES)
    target = _target_column(horizon)
    missing = [name for name in [*features, target] if name not in df.columns]
    if missing:
        raise ValueError(f"dataset is missing required columns: {missing}")
    usable = df.drop_nulls([*features, target])
    if usable.is_empty():
        raise ValueError("dataset is empty after dropping nulls")
    if usable.height < min_rows:
        raise ValueError(f"dataset has {usable.height} rows; need at least {min_rows}")
    y = usable[target].to_numpy().astype(int)
    if len(np.unique(y)) < 2:
        raise ValueError("dataset target must contain at least two classes")
    X = usable.select(features).to_numpy()
    return X, y


def train_booster(
    X: np.ndarray,
    y: np.ndarray,
    *,
    num_boost_round: int = 200,
    early_stopping_rounds: int = 20,
    val_fraction: float = 0.2,
    params: dict[str, Any] | None = None,
) -> tuple[lgb.Booster, dict[str, Any]]:
    if not 0 < val_fraction < 1:
        raise ValueError(f"val_fraction must be in (0, 1); got {val_fraction}")
    split = int(len(X) * (1 - val_fraction))
    if split < 1 or split >= len(X):
        raise ValueError(f"insufficient rows for split: total={len(X)}, split={split}")
    if len(np.unique(y[:split])) < 2 or len(np.unique(y[split:])) < 2:
        raise ValueError("train and validation splits must each contain two classes")
    final_params: dict[str, Any] = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "num_threads": 1,
        "verbose": -1,
    }
    if params:
        final_params.update(params)
    dtrain = lgb.Dataset(X[:split], y[:split])
    dval = lgb.Dataset(X[split:], y[split:], reference=dtrain)
    model = lgb.train(
        final_params,
        dtrain,
        num_boost_round=num_boost_round,
        valid_sets=[dval],
        callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False)],
    )
    best_score = model.best_score.get("valid_0", {}).get("auc")
    return model, {
        "train_rows": int(split),
        "val_rows": int(len(X) - split),
        "best_iter": int(model.best_iteration or num_boost_round),
        "best_auc": float(best_score) if best_score is not None else None,
    }


def save_artifacts(
    model: lgb.Booster,
    *,
    out_dir: pathlib.Path,
    feature_names: list[str],
    horizon: str,
    extra_meta: dict[str, Any] | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_dir / "model.txt"))
    horizon_ns = DEFAULT_HORIZONS_NS[horizon]
    meta: dict[str, Any] = {
        "features": feature_names,
        "horizon": horizon,
        "horizon_ns": horizon_ns,
        "trained_at": int(time.time()),
        "target": _target_column(horizon),
        "label_source": "news_alpha:example:*",
    }
    if extra_meta:
        meta.update(extra_meta)
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))


async def _export_command(args: argparse.Namespace) -> None:
    redis: Redis[Any] = Redis.from_url(get_settings().REDIS_URL)
    try:
        df = await export_labeled_examples(
            redis,
            horizon=args.horizon,
            feature_names=DEFAULT_FEATURES,
            limit=args.limit,
        )
    finally:
        await redis.aclose()  # type: ignore[attr-defined]
    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".csv":
        df.write_csv(out)
    else:
        df.write_parquet(out)
    print(f"Exported {df.height} rows to {out}")


def _train_command(args: argparse.Namespace) -> None:
    feature_names = list(DEFAULT_FEATURES)
    input_path = pathlib.Path(args.input)
    df = (
        pl.read_csv(input_path)
        if input_path.suffix.lower() == ".csv"
        else pl.read_parquet(input_path)
    )
    X, y = build_dataset(
        df,
        horizon=args.horizon,
        feature_names=feature_names,
        min_rows=args.min_rows,
    )
    model, meta = train_booster(
        X,
        y,
        num_boost_round=args.num_boost_round,
        early_stopping_rounds=args.early_stopping_rounds,
        val_fraction=args.val_fraction,
    )
    meta.update(
        {
            "dataset_path": str(args.input),
            "rows": len(X),
            "eval_mode": "time_ordered_holdout",
        }
    )
    save_artifacts(
        model,
        out_dir=pathlib.Path(args.out_dir),
        feature_names=feature_names,
        horizon=args.horizon,
        extra_meta=meta,
    )
    print(
        f"Saved {args.out_dir} "
        f"(rows={len(X)}, best_auc={meta.get('best_auc')}, best_iter={meta.get('best_iter')})"
    )


async def _main(args: argparse.Namespace) -> None:
    if args.command == "export":
        await _export_command(args)
    elif args.command == "train":
        _train_command(args)
    elif args.command == "evaluate":
        _evaluate_command(args)
    else:
        raise ValueError(f"unknown command: {args.command}")


def _evaluate_command(args: argparse.Namespace) -> None:
    policy = CandidateGatePolicy(
        min_auc=args.min_auc,
        min_rows=args.min_rows,
        min_val_rows=args.min_val_rows,
        min_auc_delta=args.min_auc_delta,
        max_age_hours=args.max_age_hours,
    )
    report = evaluate_candidate(
        candidate_dir=pathlib.Path(args.candidate_dir),
        models_dir=pathlib.Path(args.models_dir),
        active_dir=pathlib.Path(args.active_dir) if args.active_dir else None,
        policy=policy,
    )
    write_report(report, pathlib.Path(args.report))
    print(json.dumps(report.to_dict()))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--output", default=DEFAULT_DATASET_PATH)
    export.add_argument("--horizon", default="30m", choices=sorted(DEFAULT_HORIZONS_NS))
    export.add_argument("--limit", type=int, default=None)
    train = sub.add_parser("train")
    train.add_argument("--input", default=DEFAULT_DATASET_PATH)
    train.add_argument("--out-dir", default=DEFAULT_MODEL_DIR)
    train.add_argument("--horizon", default="30m", choices=sorted(DEFAULT_HORIZONS_NS))
    train.add_argument("--num-boost-round", type=int, default=200)
    train.add_argument("--early-stopping-rounds", type=int, default=20)
    train.add_argument("--val-fraction", type=float, default=0.2)
    train.add_argument("--min-rows", type=int, default=1)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--candidate-dir", default=DEFAULT_MODEL_DIR)
    evaluate.add_argument("--models-dir", default="models")
    evaluate.add_argument("--active-dir", default=None)
    evaluate.add_argument("--report", default=DEFAULT_REPORT_PATH)
    evaluate.add_argument("--min-auc", type=float, default=0.52)
    evaluate.add_argument("--min-rows", type=int, default=200)
    evaluate.add_argument("--min-val-rows", type=int, default=40)
    evaluate.add_argument("--min-auc-delta", type=float, default=0.0)
    evaluate.add_argument("--max-age-hours", type=float, default=168.0)
    args = parser.parse_args(argv)
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
