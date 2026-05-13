from __future__ import annotations

from collections.abc import Sequence

from jobs.news_alpha_candidate_train import (
    CandidateTrainingResult,
    build_evaluate_command,
    build_export_command,
    build_train_command,
    run_daily,
)


def test_build_commands_include_horizon_paths_and_min_rows() -> None:
    base = ["python", "-m", "agents.news_alpha_predictor.train"]

    assert build_export_command(
        base_cmd=base,
        dataset_path="data/out.csv",
        horizon="30m",
    ) == [
        *base,
        "export",
        "--output",
        "data/out.csv",
        "--horizon",
        "30m",
    ]
    assert build_train_command(
        base_cmd=base,
        dataset_path="data/out.csv",
        out_dir="models/candidate",
        horizon="30m",
        min_rows=200,
    ) == [
        *base,
        "train",
        "--input",
        "data/out.csv",
        "--out-dir",
        "models/candidate",
        "--horizon",
        "30m",
        "--min-rows",
        "200",
    ]
    assert build_evaluate_command(
        base_cmd=base,
        out_dir="models/candidate",
        report_path="reports/candidate.json",
        min_rows=200,
        min_auc=0.55,
        min_val_rows=40,
    ) == [
        *base,
        "evaluate",
        "--candidate-dir",
        "models/candidate",
        "--report",
        "reports/candidate.json",
        "--min-rows",
        "200",
        "--min-auc",
        "0.55",
        "--min-val-rows",
        "40",
    ]


async def test_run_daily_completes_when_export_and_train_succeed() -> None:
    calls: list[list[str]] = []

    async def runner(cmd: Sequence[str]) -> int:
        calls.append(list(cmd))
        return 0

    result = await run_daily(
        horizon="5m",
        dataset_path="data/test.csv",
        out_dir="models/news_candidate",
        report_path="reports/news_candidate.json",
        min_rows=12,
        min_auc=0.6,
        min_val_rows=4,
        base_cmd=["trainer"],
        runner=runner,
    )

    assert isinstance(result, CandidateTrainingResult)
    assert result.status == "completed"
    assert len(calls) == 3
    assert calls[0] == ["trainer", "export", "--output", "data/test.csv", "--horizon", "5m"]
    assert calls[1][-2:] == ["--min-rows", "12"]
    assert calls[2] == [
        "trainer",
        "evaluate",
        "--candidate-dir",
        "models/news_candidate",
        "--report",
        "reports/news_candidate.json",
        "--min-rows",
        "12",
        "--min-auc",
        "0.6",
        "--min-val-rows",
        "4",
    ]


async def test_run_daily_skips_train_when_export_fails() -> None:
    calls: list[list[str]] = []

    async def runner(cmd: Sequence[str]) -> int:
        calls.append(list(cmd))
        return 7

    result = await run_daily(base_cmd=["trainer"], runner=runner)

    assert result.status == "export_failed"
    assert result.export_exit_code == 7
    assert result.train_exit_code is None
    assert result.evaluate_exit_code is None
    assert len(calls) == 1


async def test_run_daily_reports_train_failure() -> None:
    exits = iter([0, 9])

    async def runner(_cmd: Sequence[str]) -> int:
        return next(exits)

    result = await run_daily(base_cmd=["trainer"], runner=runner)

    assert result.status == "train_failed"
    assert result.export_exit_code == 0
    assert result.train_exit_code == 9
    assert result.evaluate_exit_code is None


async def test_run_daily_reports_evaluate_failure() -> None:
    exits = iter([0, 0, 3])

    async def runner(_cmd: Sequence[str]) -> int:
        return next(exits)

    result = await run_daily(base_cmd=["trainer"], runner=runner)

    assert result.status == "evaluate_failed"
    assert result.export_exit_code == 0
    assert result.train_exit_code == 0
    assert result.evaluate_exit_code == 3
