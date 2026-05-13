from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from fincept_core.logging import get_logger

log = get_logger(__name__)

DEFAULT_HORIZON = "30m"
DEFAULT_DATASET_PATH = "data/news_alpha_candidate.parquet"
DEFAULT_OUT_DIR = "models/news_alpha_predictor_candidate"
DEFAULT_REPORT_PATH = "reports/news_alpha_candidate_report.json"
DEFAULT_MIN_ROWS = 200
DEFAULT_MIN_AUC = 0.52
DEFAULT_MIN_VAL_ROWS = 40

CommandRunner = Callable[[Sequence[str]], Awaitable[int]]


@dataclass(frozen=True)
class CandidateTrainingResult:
    status: str
    export_exit_code: int | None
    train_exit_code: int | None
    evaluate_exit_code: int | None
    dataset_path: str
    out_dir: str
    report_path: str
    horizon: str
    min_rows: int


def resolve_trainer_base_command() -> list[str]:
    override = os.environ.get("NEWS_ALPHA_TRAINER_CMD")
    if override:
        return override.split()
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("`uv` not on PATH; set NEWS_ALPHA_TRAINER_CMD")
    return [uv, "run", "--package", "agents", "python", "-m", "agents.news_alpha_predictor.train"]


def build_export_command(
    *,
    base_cmd: Sequence[str],
    dataset_path: str,
    horizon: str,
) -> list[str]:
    return [*base_cmd, "export", "--output", dataset_path, "--horizon", horizon]


def build_train_command(
    *,
    base_cmd: Sequence[str],
    dataset_path: str,
    out_dir: str,
    horizon: str,
    min_rows: int,
) -> list[str]:
    return [
        *base_cmd,
        "train",
        "--input",
        dataset_path,
        "--out-dir",
        out_dir,
        "--horizon",
        horizon,
        "--min-rows",
        str(min_rows),
    ]


def build_evaluate_command(
    *,
    base_cmd: Sequence[str],
    out_dir: str,
    report_path: str,
    min_rows: int,
    min_auc: float,
    min_val_rows: int,
) -> list[str]:
    return [
        *base_cmd,
        "evaluate",
        "--candidate-dir",
        out_dir,
        "--report",
        report_path,
        "--min-rows",
        str(min_rows),
        "--min-auc",
        str(min_auc),
        "--min-val-rows",
        str(min_val_rows),
    ]


async def run_command(cmd: Sequence[str]) -> int:
    proc = await asyncio.create_subprocess_exec(*cmd)
    return await proc.wait()


async def run_daily(
    *,
    horizon: str | None = None,
    dataset_path: str | None = None,
    out_dir: str | None = None,
    report_path: str | None = None,
    min_rows: int | None = None,
    min_auc: float | None = None,
    min_val_rows: int | None = None,
    base_cmd: Sequence[str] | None = None,
    runner: CommandRunner = run_command,
) -> CandidateTrainingResult:
    resolved_horizon = horizon or os.environ.get("NEWS_ALPHA_TRAIN_HORIZON", DEFAULT_HORIZON)
    resolved_dataset = dataset_path or os.environ.get(
        "NEWS_ALPHA_TRAIN_DATASET_PATH",
        DEFAULT_DATASET_PATH,
    )
    resolved_out_dir = out_dir or os.environ.get("NEWS_ALPHA_CANDIDATE_DIR", DEFAULT_OUT_DIR)
    resolved_report = report_path or os.environ.get(
        "NEWS_ALPHA_CANDIDATE_REPORT",
        DEFAULT_REPORT_PATH,
    )
    resolved_min_rows = min_rows or int(os.environ.get("NEWS_ALPHA_TRAIN_MIN_ROWS", str(DEFAULT_MIN_ROWS)))
    resolved_min_auc = min_auc or float(os.environ.get("NEWS_ALPHA_MIN_AUC", str(DEFAULT_MIN_AUC)))
    resolved_min_val_rows = min_val_rows or int(
        os.environ.get("NEWS_ALPHA_MIN_VAL_ROWS", str(DEFAULT_MIN_VAL_ROWS))
    )
    resolved_base_cmd = list(base_cmd or resolve_trainer_base_command())

    export_cmd = build_export_command(
        base_cmd=resolved_base_cmd,
        dataset_path=resolved_dataset,
        horizon=resolved_horizon,
    )
    export_exit = await runner(export_cmd)
    if export_exit != 0:
        log.warning("news_alpha_candidate.export_failed", exit_code=export_exit)
        return CandidateTrainingResult(
            status="export_failed",
            export_exit_code=export_exit,
            train_exit_code=None,
            evaluate_exit_code=None,
            dataset_path=resolved_dataset,
            out_dir=resolved_out_dir,
            report_path=resolved_report,
            horizon=resolved_horizon,
            min_rows=resolved_min_rows,
        )

    train_cmd = build_train_command(
        base_cmd=resolved_base_cmd,
        dataset_path=resolved_dataset,
        out_dir=resolved_out_dir,
        horizon=resolved_horizon,
        min_rows=resolved_min_rows,
    )
    train_exit = await runner(train_cmd)
    if train_exit != 0:
        log.warning("news_alpha_candidate.train_failed", exit_code=train_exit)
        return CandidateTrainingResult(
            status="train_failed",
            export_exit_code=export_exit,
            train_exit_code=train_exit,
            evaluate_exit_code=None,
            dataset_path=resolved_dataset,
            out_dir=resolved_out_dir,
            report_path=resolved_report,
            horizon=resolved_horizon,
            min_rows=resolved_min_rows,
        )

    evaluate_cmd = build_evaluate_command(
        base_cmd=resolved_base_cmd,
        out_dir=resolved_out_dir,
        report_path=resolved_report,
        min_rows=resolved_min_rows,
        min_auc=resolved_min_auc,
        min_val_rows=resolved_min_val_rows,
    )
    evaluate_exit = await runner(evaluate_cmd)
    if evaluate_exit != 0:
        log.warning("news_alpha_candidate.evaluate_failed", exit_code=evaluate_exit)
        return CandidateTrainingResult(
            status="evaluate_failed",
            export_exit_code=export_exit,
            train_exit_code=train_exit,
            evaluate_exit_code=evaluate_exit,
            dataset_path=resolved_dataset,
            out_dir=resolved_out_dir,
            report_path=resolved_report,
            horizon=resolved_horizon,
            min_rows=resolved_min_rows,
        )

    log.info(
        "news_alpha_candidate.complete",
        dataset_path=resolved_dataset,
        out_dir=resolved_out_dir,
        report_path=resolved_report,
        horizon=resolved_horizon,
        min_rows=resolved_min_rows,
    )
    return CandidateTrainingResult(
        status="completed",
        export_exit_code=export_exit,
        train_exit_code=train_exit,
        evaluate_exit_code=evaluate_exit,
        dataset_path=resolved_dataset,
        out_dir=resolved_out_dir,
        report_path=resolved_report,
        horizon=resolved_horizon,
        min_rows=resolved_min_rows,
    )


def main() -> None:
    asyncio.run(run_daily())


if __name__ == "__main__":
    main()
