from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import time
from typing import Any

from agents.news_alpha_predictor.infer import AGENT_ID

DEFAULT_MIN_AUC = 0.52
DEFAULT_MIN_ROWS = 200
DEFAULT_MIN_VAL_ROWS = 40
DEFAULT_MIN_AUC_DELTA = 0.0
DEFAULT_MAX_AGE_HOURS = 168.0
DEFAULT_REPORT_PATH = "reports/news_alpha_candidate_report.json"


@dataclasses.dataclass(frozen=True)
class CandidateGatePolicy:
    min_auc: float = DEFAULT_MIN_AUC
    min_rows: int = DEFAULT_MIN_ROWS
    min_val_rows: int = DEFAULT_MIN_VAL_ROWS
    min_auc_delta: float = DEFAULT_MIN_AUC_DELTA
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS


@dataclasses.dataclass(frozen=True)
class CandidateGateReport:
    approved: bool
    reasons: list[str]
    candidate_model_name: str
    candidate_dir: str
    candidate_meta: dict[str, Any]
    active_model_name: str | None
    active_meta: dict[str, Any] | None
    policy: dict[str, Any]
    generated_at: float
    promotion_hint: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _load_json(path: pathlib.Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _float_meta(meta: dict[str, Any] | None, key: str) -> float | None:
    if meta is None:
        return None
    value = meta.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_meta(meta: dict[str, Any] | None, key: str) -> int | None:
    value = _float_meta(meta, key)
    return int(value) if value is not None else None


def _model_name_for(candidate_dir: pathlib.Path, models_dir: pathlib.Path) -> str:
    try:
        return str(candidate_dir.resolve().relative_to(models_dir.resolve())).replace("\\", "/")
    except ValueError:
        return candidate_dir.name


def _active_dir(models_dir: pathlib.Path, active_dir: pathlib.Path | None) -> pathlib.Path:
    return active_dir or (models_dir / "active")


def _active_binding(
    *,
    models_dir: pathlib.Path,
    active_dir: pathlib.Path | None,
    agent_id: str,
) -> tuple[str | None, dict[str, Any] | None]:
    binding = _load_json(_active_dir(models_dir, active_dir) / f"{agent_id}.json")
    if binding is None:
        return None, None
    model_name = binding.get("model_name")
    if not isinstance(model_name, str) or not model_name:
        return None, None
    return model_name, _load_json(models_dir / model_name / "meta.json")


def evaluate_candidate(
    *,
    candidate_dir: pathlib.Path,
    models_dir: pathlib.Path = pathlib.Path("models"),
    active_dir: pathlib.Path | None = None,
    agent_id: str = AGENT_ID,
    policy: CandidateGatePolicy = CandidateGatePolicy(),
) -> CandidateGateReport:
    reasons: list[str] = []
    candidate_model_name = _model_name_for(candidate_dir, models_dir)
    meta = _load_json(candidate_dir / "meta.json") or {}
    active_model_name, active_meta = _active_binding(
        models_dir=models_dir,
        active_dir=active_dir,
        agent_id=agent_id,
    )

    if not (candidate_dir / "model.txt").is_file():
        reasons.append("candidate model.txt missing")
    if not (candidate_dir / "meta.json").is_file():
        reasons.append("candidate meta.json missing")

    rows = _int_meta(meta, "rows")
    if rows is None or rows < policy.min_rows:
        reasons.append(f"rows {rows} below minimum {policy.min_rows}")

    val_rows = _int_meta(meta, "val_rows")
    if val_rows is None or val_rows < policy.min_val_rows:
        reasons.append(f"val_rows {val_rows} below minimum {policy.min_val_rows}")

    candidate_auc = _float_meta(meta, "best_auc")
    if candidate_auc is None:
        reasons.append("best_auc missing")
    elif candidate_auc < policy.min_auc:
        reasons.append(f"best_auc {candidate_auc:.6f} below minimum {policy.min_auc:.6f}")

    trained_at = _float_meta(meta, "trained_at")
    now = time.time()
    if trained_at is None:
        reasons.append("trained_at missing")
    elif now - trained_at > policy.max_age_hours * 3600:
        reasons.append(f"candidate older than {policy.max_age_hours:g}h")

    active_auc = _float_meta(active_meta, "best_auc")
    if active_auc is not None and candidate_auc is not None:
        required = active_auc + policy.min_auc_delta
        if candidate_auc < required:
            reasons.append(
                f"best_auc {candidate_auc:.6f} below active threshold {required:.6f}"
            )

    promotion_hint = {
        "shadow": {
            "method": "POST",
            "path": f"/models/{candidate_model_name}/shadow",
            "body": {"agent_id": agent_id, "promoted_by": "operator"},
        },
        "active": {
            "method": "POST",
            "path": f"/models/{candidate_model_name}/promote",
            "body": {"agent_id": agent_id, "promoted_by": "operator"},
        },
    }
    return CandidateGateReport(
        approved=not reasons,
        reasons=reasons,
        candidate_model_name=candidate_model_name,
        candidate_dir=str(candidate_dir),
        candidate_meta=meta,
        active_model_name=active_model_name,
        active_meta=active_meta,
        policy=dataclasses.asdict(policy),
        generated_at=now,
        promotion_hint=promotion_hint,
    )


def write_report(report: CandidateGateReport, path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="news_alpha_predictor.evaluate")
    parser.add_argument("--candidate-dir", default="models/news_alpha_predictor_candidate")
    parser.add_argument("--models-dir", default="models")
    parser.add_argument("--active-dir", default=None)
    parser.add_argument("--report", default=DEFAULT_REPORT_PATH)
    parser.add_argument("--agent-id", default=AGENT_ID)
    parser.add_argument("--min-auc", type=float, default=DEFAULT_MIN_AUC)
    parser.add_argument("--min-rows", type=int, default=DEFAULT_MIN_ROWS)
    parser.add_argument("--min-val-rows", type=int, default=DEFAULT_MIN_VAL_ROWS)
    parser.add_argument("--min-auc-delta", type=float, default=DEFAULT_MIN_AUC_DELTA)
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    args = parser.parse_args(argv)
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
        agent_id=args.agent_id,
        policy=policy,
    )
    write_report(report, pathlib.Path(args.report))
    print(json.dumps(report.to_dict()))


if __name__ == "__main__":
    main()
