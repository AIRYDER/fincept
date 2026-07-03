"""
TDD tests for quant_foundry.cli.runpod_cli (T-OP.1: Unified RunPod CLI Surface).

Acceptance:
- CLIConfig, CommandSpec, CommandResult are frozen Pydantic v2 models
  (extra=forbid) with the specified fields and validators.
- RunPodCLI.dispatch routes to the correct handler.
- cmd_dataset_register / cmd_dataset_upload work (local).
- cmd_train_canary / cmd_train_production succeed with a registered
  dataset and fail-closed when a raw CSV path is provided.
- cmd_train_status shows dispatch, queue, worker, callback, artifact
  verification, and final eligibility states.
- cmd_train_verify / cmd_train_cost work (local).
- validate_preflight passes, fails on raw CSV, fails on missing args.
- list_commands / render_help / format_status_report work.
- No command trains locally (all training is remote / mocked).
- Edge cases: unknown command, empty args.
"""

from __future__ import annotations

import pathlib
import time
from typing import Any

import pytest
from quant_foundry.cli.runpod_cli import (
    CLIConfig,
    CommandResult,
    CommandSpec,
    RunPodCLI,
    format_status_report,
    list_commands,
    render_help,
    validate_preflight,
)
from quant_foundry.dataset_manifest import (
    DatasetRegistry,
    ReadinessLevel,
    TrainingMode,
)
from quant_foundry.job_ledger import JobLedgerState, TrainingJobLedger


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_mock_dispatch(
    *, cost_cents: int = 25, duration: float = 1.0
) -> "Any":
    """Return a deterministic mock dispatch function."""

    def _dispatch(
        *,
        job_id: str,
        request_payload: dict[str, Any],
        budget_cents: int,
    ) -> dict[str, Any]:
        return {
            "runpod_job_id": f"rp-{job_id}",
            "status": "dispatched",
            "cost_cents": cost_cents,
            "duration_seconds": duration,
        }

    return _dispatch


def _make_cli(
    tmp_path: pathlib.Path,
    *,
    config: CLIConfig | None = None,
    registry: DatasetRegistry | None = None,
    ledger: TrainingJobLedger | None = None,
    dispatch_fn: Any | None = None,
) -> RunPodCLI:
    """Build a RunPodCLI with isolated temp-backed dependencies."""
    cfg = config or CLIConfig()
    reg = registry if registry is not None else DatasetRegistry()
    led = ledger if ledger is not None else TrainingJobLedger(base_dir=tmp_path / "ledger")
    disp = dispatch_fn if dispatch_fn is not None else _make_mock_dispatch()
    return RunPodCLI(cfg, registry=reg, ledger=led, dispatch_fn=disp)


def _register_dataset(
    registry: DatasetRegistry,
    dataset_id: str = "ds-test-001",
    *,
    readiness: ReadinessLevel = ReadinessLevel.L3_QUALITY_GATED,
) -> None:
    """Register a dataset at L3 (production-eligible)."""
    registry.register(
        dataset_id=dataset_id,
        manifest_uri="https://example.com/manifests/ds-test-001.manifest.json",
        data_uri="https://example.com/data/ds-test-001.parquet",
        manifest_sha256="a" * 64,
        data_sha256="b" * 64,
        quality_report_uri="https://example.com/quality/ds-test-001.json",
        quality_report_sha256="c" * 64,
        readiness_level=readiness,
    )


# ---------------------------------------------------------------------------
# CLIConfig
# ---------------------------------------------------------------------------


class TestCLIConfig:
    def test_defaults(self) -> None:
        cfg = CLIConfig()
        assert cfg.runpod_api_key_env == "RUNPOD_API_KEY"
        assert cfg.callback_secret_env == "CALLBACK_SECRET"
        assert cfg.default_gpu_type == "RTX_4090"
        assert cfg.default_budget_usd == 10.0
        assert cfg.canary_budget_usd == 1.0
        assert cfg.receipt_dir == "reports/runpod-training"
        assert cfg.require_registered_dataset is True

    def test_frozen(self) -> None:
        cfg = CLIConfig()
        with pytest.raises(Exception):
            cfg.default_budget_usd = 5.0  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            CLIConfig(unknown_field=123)  # type: ignore[call-arg]

    def test_default_budget_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            CLIConfig(default_budget_usd=0)
        with pytest.raises(Exception):
            CLIConfig(default_budget_usd=-1.0)

    def test_canary_budget_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            CLIConfig(canary_budget_usd=0)
        with pytest.raises(Exception):
            CLIConfig(canary_budget_usd=-5.0)

    def test_custom_values(self) -> None:
        cfg = CLIConfig(
            runpod_api_key_env="MY_KEY",
            default_budget_usd=50.0,
            canary_budget_usd=5.0,
            default_gpu_type="A100",
        )
        assert cfg.runpod_api_key_env == "MY_KEY"
        assert cfg.default_budget_usd == 50.0
        assert cfg.canary_budget_usd == 5.0
        assert cfg.default_gpu_type == "A100"


# ---------------------------------------------------------------------------
# CommandSpec
# ---------------------------------------------------------------------------


class TestCommandSpec:
    def test_construction(self) -> None:
        spec = CommandSpec(
            command="train production",
            description="dispatch production training",
            requires_registered_dataset=True,
            trains_remotely=True,
            budget_cap=10.0,
        )
        assert spec.command == "train production"
        assert spec.requires_registered_dataset is True
        assert spec.trains_remotely is True
        assert spec.budget_cap == 10.0

    def test_frozen(self) -> None:
        spec = CommandSpec(
            command="x", description="d", requires_registered_dataset=False, trains_remotely=False
        )
        with pytest.raises(Exception):
            spec.command = "y"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            CommandSpec(  # type: ignore[call-arg]
                command="x",
                description="d",
                requires_registered_dataset=False,
                trains_remotely=False,
                oops=1,
            )

    def test_budget_cap_optional(self) -> None:
        spec = CommandSpec(
            command="dataset register",
            description="d",
            requires_registered_dataset=False,
            trains_remotely=False,
        )
        assert spec.budget_cap is None


# ---------------------------------------------------------------------------
# CommandResult
# ---------------------------------------------------------------------------


class TestCommandResult:
    def test_construction_success(self) -> None:
        r = CommandResult(
            command="train canary",
            success=True,
            message="ok",
            job_id="job-1",
        )
        assert r.success is True
        assert r.job_id == "job-1"
        assert r.error is None
        assert r.receipt_path is None
        assert r.duration_seconds == 0.0

    def test_construction_failure(self) -> None:
        r = CommandResult(
            command="train production",
            success=False,
            message="failed",
            error="boom",
        )
        assert r.success is False
        assert r.error == "boom"

    def test_frozen(self) -> None:
        r = CommandResult(command="x", success=True, message="m")
        with pytest.raises(Exception):
            r.success = False  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            CommandResult(command="x", success=True, message="m", extra=1)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# list_commands / render_help
# ---------------------------------------------------------------------------


class TestListCommands:
    def test_returns_all_commands(self) -> None:
        cmds = list_commands()
        names = [c.command for c in cmds]
        assert "dataset register" in names
        assert "dataset upload" in names
        assert "train canary" in names
        assert "train production" in names
        assert "train status" in names
        assert "train verify" in names
        assert "train cost" in names
        assert len(cmds) == 7

    def test_train_commands_trains_remotely(self) -> None:
        cmds = {c.command: c for c in list_commands()}
        assert cmds["train canary"].trains_remotely is True
        assert cmds["train production"].trains_remotely is True
        assert cmds["dataset register"].trains_remotely is False
        assert cmds["train status"].trains_remotely is False

    def test_train_commands_require_registered_dataset(self) -> None:
        cmds = {c.command: c for c in list_commands()}
        assert cmds["train canary"].requires_registered_dataset is True
        assert cmds["train production"].requires_registered_dataset is True
        assert cmds["dataset register"].requires_registered_dataset is False

    def test_budget_cap_with_config(self) -> None:
        cfg = CLIConfig(canary_budget_usd=2.0, default_budget_usd=20.0)
        cmds = {c.command: c for c in list_commands(cfg)}
        assert cmds["train canary"].budget_cap == 2.0
        assert cmds["train production"].budget_cap == 20.0
        assert cmds["dataset register"].budget_cap is None

    def test_budget_cap_none_without_config(self) -> None:
        cmds = {c.command: c for c in list_commands()}
        assert cmds["train canary"].budget_cap is None


class TestRenderHelp:
    def test_contains_all_commands(self) -> None:
        help_text = render_help()
        for name in [
            "dataset register",
            "dataset upload",
            "train canary",
            "train production",
            "train status",
            "train verify",
            "train cost",
        ]:
            assert name in help_text

    def test_contains_safety_invariants(self) -> None:
        help_text = render_help()
        assert "No command trains locally" in help_text
        assert "registered dataset" in help_text

    def test_with_config_shows_budget(self) -> None:
        cfg = CLIConfig(canary_budget_usd=3.0)
        help_text = render_help(cfg)
        assert "$3.00" in help_text


# ---------------------------------------------------------------------------
# validate_preflight
# ---------------------------------------------------------------------------


class TestValidatePreflight:
    def test_pass_for_dataset_register(self) -> None:
        cfg = CLIConfig()
        validate_preflight(
            "dataset register",
            {"dataset_id": "ds-1", "manifest_uri": "m.json", "data_uri": "d.parquet"},
            cfg,
        )  # no raise

    def test_pass_for_train_canary_with_registered_id(self) -> None:
        cfg = CLIConfig()
        validate_preflight("train canary", {"dataset_id": "ds-001"}, cfg)  # no raise

    def test_fail_on_raw_csv_for_production(self) -> None:
        cfg = CLIConfig()
        with pytest.raises(ValueError, match="raw CSV"):
            validate_preflight("train production", {"dataset_id": "data/raw.csv"}, cfg)

    def test_fail_on_raw_csv_for_canary(self) -> None:
        cfg = CLIConfig()
        with pytest.raises(ValueError, match="raw CSV"):
            validate_preflight("train canary", {"dataset_id": "file.csv"}, cfg)

    def test_fail_on_inline_csv_for_canary(self) -> None:
        cfg = CLIConfig()
        with pytest.raises(ValueError, match="raw CSV"):
            validate_preflight("train canary", {"dataset_id": "inline://a,b,c"}, cfg)

    def test_fail_on_missing_dataset_id_for_production(self) -> None:
        cfg = CLIConfig()
        with pytest.raises(ValueError, match="missing required args"):
            validate_preflight("train production", {}, cfg)

    def test_fail_on_empty_dataset_id(self) -> None:
        cfg = CLIConfig()
        with pytest.raises(ValueError, match="dataset_id"):
            validate_preflight("train canary", {"dataset_id": "  "}, cfg)

    def test_fail_on_missing_args_for_dataset_register(self) -> None:
        cfg = CLIConfig()
        with pytest.raises(ValueError, match="missing required args"):
            validate_preflight("dataset register", {"dataset_id": "ds-1"}, cfg)

    def test_fail_on_missing_job_id_for_status(self) -> None:
        cfg = CLIConfig()
        with pytest.raises(ValueError, match="missing required args"):
            validate_preflight("train status", {}, cfg)

    def test_unknown_command_raises(self) -> None:
        cfg = CLIConfig()
        with pytest.raises(ValueError, match="unknown command"):
            validate_preflight("frobnicate", {}, cfg)

    def test_require_registered_dataset_disabled_allows_raw(self) -> None:
        cfg = CLIConfig(require_registered_dataset=False)
        # When the fail-closed gate is disabled, preflight does not
        # reject raw CSV (the registry gate still enforces at dispatch).
        validate_preflight("train canary", {"dataset_id": "raw.csv"}, cfg)  # no raise


# ---------------------------------------------------------------------------
# RunPodCLI.dispatch routing
# ---------------------------------------------------------------------------


class TestDispatchRouting:
    def test_routes_to_dataset_register(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch(
            "dataset register",
            {
                "dataset_id": "ds-route-001",
                "manifest_uri": "https://x/m.json",
                "data_uri": "https://x/d.parquet",
            },
        )
        assert result.command == "dataset register"
        assert result.success is True

    def test_routes_to_train_canary(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        _register_dataset(cli.registry)
        result = cli.dispatch("train canary", {"dataset_id": "ds-test-001"})
        assert result.command == "train canary"
        assert result.success is True
        assert result.job_id is not None

    def test_unknown_command_returns_failure(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch("bogus", {})
        assert result.success is False
        assert "unknown command" in result.error

    def test_empty_args_returns_failure(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch("train production", {})
        assert result.success is False
        assert "preflight" in result.message


# ---------------------------------------------------------------------------
# cmd_dataset_register
# ---------------------------------------------------------------------------


class TestCmdDatasetRegister:
    def test_registers_dataset(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch(
            "dataset register",
            {
                "dataset_id": "ds-reg-001",
                "manifest_uri": "https://x/m.json",
                "data_uri": "https://x/d.parquet",
            },
        )
        assert result.success is True
        assert cli.registry.is_registered("ds-reg-001")

    def test_register_with_hashes(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch(
            "dataset register",
            {
                "dataset_id": "ds-reg-002",
                "manifest_uri": "https://x/m.json",
                "data_uri": "https://x/d.parquet",
                "manifest_sha256": "a" * 64,
                "data_sha256": "b" * 64,
            },
        )
        assert result.success is True
        entry = cli.registry.inspect("ds-reg-002")
        assert entry.manifest_sha256 == "a" * 64

    def test_register_invalid_dataset_id_fails(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch(
            "dataset register",
            {
                "dataset_id": "data/raw.csv",
                "manifest_uri": "https://x/m.json",
                "data_uri": "https://x/d.parquet",
            },
        )
        assert result.success is False


# ---------------------------------------------------------------------------
# cmd_dataset_upload
# ---------------------------------------------------------------------------


class TestCmdDatasetUpload:
    def test_upload_registered_dataset(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        _register_dataset(cli.registry, "ds-up-001")
        result = cli.dispatch(
            "dataset upload",
            {
                "dataset_id": "ds-up-001",
                "manifest_uri": "https://example.com/manifests/ds-test-001.manifest.json",
                "data_uri": "https://example.com/data/ds-test-001.parquet",
            },
        )
        assert result.success is True
        assert "upload request" in result.message

    def test_upload_unregistered_fails(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch(
            "dataset upload",
            {
                "dataset_id": "ds-not-registered",
                "manifest_uri": "https://x/m.json",
                "data_uri": "https://x/d.parquet",
            },
        )
        assert result.success is False
        assert "not registered" in result.error

    def test_upload_manifest_mismatch_fails(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        _register_dataset(cli.registry, "ds-up-002")
        result = cli.dispatch(
            "dataset upload",
            {
                "dataset_id": "ds-up-002",
                "manifest_uri": "https://wrong/m.json",
                "data_uri": "https://example.com/data/ds-test-001.parquet",
            },
        )
        assert result.success is False


# ---------------------------------------------------------------------------
# cmd_train_canary
# ---------------------------------------------------------------------------


class TestCmdTrainCanary:
    def test_canary_with_registered_dataset_succeeds(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        _register_dataset(cli.registry, "ds-canary-001")
        result = cli.dispatch("train canary", {"dataset_id": "ds-canary-001"})
        assert result.success is True
        assert result.job_id is not None
        assert "canary" in result.message

    def test_canary_with_raw_csv_fails_closed(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch("train canary", {"dataset_id": "data/raw.csv"})
        assert result.success is False
        assert "raw CSV" in result.error
        # No job was dispatched (mock dispatch not called for raw CSV).
        assert result.job_id is None

    def test_canary_budget_capped(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path, config=CLIConfig(canary_budget_usd=2.0))
        _register_dataset(cli.registry, "ds-canary-002")
        result = cli.dispatch("train canary", {"dataset_id": "ds-canary-002"})
        assert result.success is True
        assert "$2.00" in result.message

    def test_canary_records_in_ledger(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        _register_dataset(cli.registry, "ds-canary-003")
        result = cli.dispatch("train canary", {"dataset_id": "ds-canary-003"})
        rec = cli.ledger.get(result.job_id)
        assert rec is not None
        assert rec.state == JobLedgerState.DISPATCHED
        assert rec.dataset_id == "ds-canary-003"


# ---------------------------------------------------------------------------
# cmd_train_production
# ---------------------------------------------------------------------------


class TestCmdTrainProduction:
    def test_production_with_registered_dataset_succeeds(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        _register_dataset(cli.registry, "ds-prod-001")
        result = cli.dispatch("train production", {"dataset_id": "ds-prod-001"})
        assert result.success is True
        assert result.job_id is not None
        assert "production" in result.message

    def test_production_with_raw_csv_fails_closed(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch("train production", {"dataset_id": "raw/data.csv"})
        assert result.success is False
        assert "raw CSV" in result.error
        assert result.job_id is None

    def test_production_with_unregistered_id_fails_at_registry(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        # Preflight passes (valid slug), but registry gate rejects.
        result = cli.dispatch("train production", {"dataset_id": "ds-not-in-registry"})
        assert result.success is False
        assert "unregistered" in result.error.lower() or "not" in result.error.lower()

    def test_production_budget_capped(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path, config=CLIConfig(default_budget_usd=25.0))
        _register_dataset(cli.registry, "ds-prod-002")
        result = cli.dispatch("train production", {"dataset_id": "ds-prod-002"})
        assert result.success is True
        assert "$25.00" in result.message

    def test_production_records_in_ledger(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        _register_dataset(cli.registry, "ds-prod-003")
        result = cli.dispatch("train production", {"dataset_id": "ds-prod-003"})
        rec = cli.ledger.get(result.job_id)
        assert rec is not None
        assert rec.state == JobLedgerState.DISPATCHED


# ---------------------------------------------------------------------------
# cmd_train_status
# ---------------------------------------------------------------------------


class TestCmdTrainStatus:
    def test_status_shows_all_states(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        _register_dataset(cli.registry, "ds-status-001")
        # Dispatch a job so it exists in the ledger.
        disp = cli.dispatch("train canary", {"dataset_id": "ds-status-001"})
        job_id = disp.job_id
        # Simulate callback + artifact verification.
        cli.ledger.record_callback(job_id, "cb-1")
        cli.ledger.record_artifact(job_id, "art-1")
        result = cli.dispatch("train status", {"job_id": job_id})
        assert result.success is True
        report = result.message
        # Dispatch state.
        assert "dispatched" in report
        # Queue / outbox.
        assert "outbox_id" in report
        # Worker (runpod_job_id).
        assert "runpod_job_id" in report
        # Callback.
        assert "cb-1" in report
        # Artifact verification.
        assert "art-1" in report
        assert "artifact_verified" in report
        # Final eligibility.
        assert "ELIGIBLE" in report

    def test_status_unknown_job_fails(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch("train status", {"job_id": "no-such-job"})
        assert result.success is False
        assert "unknown job_id" in result.error

    def test_status_pending_job(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        _register_dataset(cli.registry, "ds-status-002")
        disp = cli.dispatch("train canary", {"dataset_id": "ds-status-002"})
        result = cli.dispatch("train status", {"job_id": disp.job_id})
        assert result.success is True
        assert "PENDING" in result.message


# ---------------------------------------------------------------------------
# cmd_train_verify
# ---------------------------------------------------------------------------


class TestCmdTrainVerify:
    def test_verify_with_artifact_id(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        _register_dataset(cli.registry, "ds-verify-001")
        disp = cli.dispatch("train canary", {"dataset_id": "ds-verify-001"})
        result = cli.dispatch(
            "train verify",
            {"job_id": disp.job_id, "artifact_id": "art-verify-1"},
        )
        assert result.success is True
        assert "art-verify-1" in result.message

    def test_verify_already_verified(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        _register_dataset(cli.registry, "ds-verify-002")
        disp = cli.dispatch("train canary", {"dataset_id": "ds-verify-002"})
        cli.dispatch("train verify", {"job_id": disp.job_id, "artifact_id": "art-2"})
        result = cli.dispatch("train verify", {"job_id": disp.job_id})
        assert result.success is True

    def test_verify_not_verified_fails(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        _register_dataset(cli.registry, "ds-verify-003")
        disp = cli.dispatch("train canary", {"dataset_id": "ds-verify-003"})
        result = cli.dispatch("train verify", {"job_id": disp.job_id})
        assert result.success is False
        assert "not artifact-verified" in result.message

    def test_verify_unknown_job_fails(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch("train verify", {"job_id": "nope"})
        assert result.success is False


# ---------------------------------------------------------------------------
# cmd_train_cost
# ---------------------------------------------------------------------------


class TestCmdTrainCost:
    def test_cost_report(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        _register_dataset(cli.registry, "ds-cost-001")
        disp = cli.dispatch("train canary", {"dataset_id": "ds-cost-001"})
        result = cli.dispatch("train cost", {"job_id": disp.job_id})
        assert result.success is True
        assert "$" in result.message
        assert "duration" in result.message

    def test_cost_unknown_job_fails(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch("train cost", {"job_id": "nope"})
        assert result.success is False


# ---------------------------------------------------------------------------
# format_status_report
# ---------------------------------------------------------------------------


class TestFormatStatusReport:
    def test_formats_all_sections(self) -> None:
        status = {
            "ledger_id": "L1",
            "state": "artifact_verified",
            "outbox_id": "O1",
            "runpod_job_id": "RP1",
            "dataset_id": "DS1",
            "artifact_id": "ART1",
            "callbacks": ("cb-1", "cb-2"),
            "failures": ({"error_code": "E1", "error_message": "boom"},),
            "cost_cents": 50,
            "duration_seconds": 12.5,
            "retries": 1,
            "terminal": True,
            "trajectory": [
                {"state": "queued", "ts_ns": 1},
                {"state": "dispatched", "ts_ns": 2},
                {"state": "artifact_verified", "ts_ns": 3},
            ],
        }
        report = format_status_report(status)
        assert "L1" in report
        assert "artifact_verified" in report
        assert "O1" in report
        assert "RP1" in report
        assert "DS1" in report
        assert "ART1" in report
        assert "cb-1" in report
        assert "E1" in report
        assert "ELIGIBLE" in report
        assert "queued" in report
        assert "dispatched" in report

    def test_pending_non_terminal(self) -> None:
        status = {
            "ledger_id": "L2",
            "state": "dispatched",
            "outbox_id": "O2",
            "callbacks": (),
            "failures": (),
            "cost_cents": 0,
            "duration_seconds": 0.0,
            "retries": 0,
            "terminal": False,
            "trajectory": [{"state": "dispatched", "ts_ns": 1}],
        }
        report = format_status_report(status)
        assert "PENDING" in report

    def test_rejected_terminal(self) -> None:
        status = {
            "ledger_id": "L3",
            "state": "rejected",
            "outbox_id": "O3",
            "callbacks": (),
            "failures": (),
            "cost_cents": 0,
            "duration_seconds": 0.0,
            "retries": 0,
            "terminal": True,
            "trajectory": [],
        }
        report = format_status_report(status)
        assert "REJECTED" in report

    def test_empty_trajectory(self) -> None:
        status = {
            "ledger_id": "L4",
            "state": "queued",
            "outbox_id": "O4",
            "callbacks": (),
            "failures": (),
            "cost_cents": 0,
            "duration_seconds": 0.0,
            "retries": 0,
            "terminal": False,
            "trajectory": [],
        }
        report = format_status_report(status)
        assert "no transitions" in report


# ---------------------------------------------------------------------------
# No-local-training invariant
# ---------------------------------------------------------------------------


class TestNoLocalTraining:
    def test_train_commands_use_dispatch_fn(self, tmp_path: pathlib.Path) -> None:
        """Train commands must call the dispatch_fn (remote), not train locally."""
        called: list[dict[str, Any]] = []

        def tracking_dispatch(
            *, job_id: str, request_payload: dict[str, Any], budget_cents: int
        ) -> dict[str, Any]:
            called.append({"job_id": job_id, "budget_cents": budget_cents})
            return {
                "runpod_job_id": f"rp-{job_id}",
                "status": "dispatched",
                "cost_cents": 10,
                "duration_seconds": 0.5,
            }

        cli = _make_cli(tmp_path, dispatch_fn=tracking_dispatch)
        _register_dataset(cli.registry, "ds-notrain-001")
        cli.dispatch("train canary", {"dataset_id": "ds-notrain-001"})
        assert len(called) == 1
        assert called[0]["budget_cents"] == 100

    def test_local_commands_do_not_call_dispatch_fn(self, tmp_path: pathlib.Path) -> None:
        called: list[dict[str, Any]] = []

        def tracking_dispatch(**kwargs: Any) -> dict[str, Any]:
            called.append(kwargs)
            return {"runpod_job_id": "rp", "status": "dispatched", "cost_cents": 0, "duration_seconds": 0}

        cli = _make_cli(tmp_path, dispatch_fn=tracking_dispatch)
        _register_dataset(cli.registry, "ds-notrain-002")
        cli.dispatch(
            "dataset register",
            {
                "dataset_id": "ds-local-001",
                "manifest_uri": "https://x/m.json",
                "data_uri": "https://x/d.parquet",
            },
        )
        cli.dispatch(
            "dataset upload",
            {
                "dataset_id": "ds-notrain-002",
                "manifest_uri": "https://example.com/manifests/ds-test-001.manifest.json",
                "data_uri": "https://example.com/data/ds-test-001.parquet",
            },
        )
        assert len(called) == 0

    def test_default_dispatch_fn_raises(self, tmp_path: pathlib.Path) -> None:
        """A CLI without a dispatch_fn cannot silently 'succeed' on train."""
        cli = _make_cli(tmp_path, dispatch_fn=None)
        # Override to use the default (raising) dispatch.
        cli._dispatch_fn = None  # type: ignore[assignment]
        # Re-assign the default by constructing fresh.
        from quant_foundry.cli.runpod_cli import _default_dispatch_fn

        cli._dispatch_fn = _default_dispatch_fn
        _register_dataset(cli.registry, "ds-notrain-003")
        result = cli.dispatch("train canary", {"dataset_id": "ds-notrain-003"})
        assert result.success is False
        assert "dispatch_fn" in result.error or "RuntimeError" in result.error


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_args_dict(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch("train status", {})
        assert result.success is False

    def test_none_args_value(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch("train canary", {"dataset_id": None})
        assert result.success is False

    def test_whitespace_dataset_id(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        result = cli.dispatch("train production", {"dataset_id": "   "})
        assert result.success is False

    def test_dispatch_catches_handler_exception(self, tmp_path: pathlib.Path) -> None:
        cli = _make_cli(tmp_path)
        # Force an exception in the ledger by passing a bad job_id path.
        result = cli.dispatch("train status", {"job_id": "nonexistent"})
        assert result.success is False
        # The exception was caught, not propagated.
        assert result.error is not None
