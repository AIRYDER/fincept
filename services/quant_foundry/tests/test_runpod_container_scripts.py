"""Tests for the RunPod container rebuild + verification scripts.

These tests verify that:
- The rebuild script (``scripts/rebuild_runpod_containers.py``) can be
  imported and its ``--dry-run`` mode prints commands without executing
  any Docker commands.
- The verify script (``scripts/verify_runpod_containers.py``) can be
  imported.
- Missing Docker raises a clear, actionable error.
- Missing Dockerfile raises a clear, actionable error.

The scripts live outside the ``quant_foundry`` package (in the repo-root
``scripts/`` directory), so we load them via ``importlib`` from their
file paths rather than a package import.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the scripts as modules
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"
REBUILD_SCRIPT = SCRIPTS_DIR / "rebuild_runpod_containers.py"
VERIFY_SCRIPT = SCRIPTS_DIR / "verify_runpod_containers.py"


def _load_module(path: Path, module_name: str):
    """Load a Python file as a module via importlib."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None, f"Could not create spec for {path}"
    assert spec.loader is not None, f"Spec has no loader for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rebuild_module():
    """Load the rebuild script as a module."""
    return _load_module(REBUILD_SCRIPT, "rebuild_runpod_containers")


@pytest.fixture(scope="module")
def verify_module():
    """Load the verify script as a module."""
    return _load_module(VERIFY_SCRIPT, "verify_runpod_containers")


# ---------------------------------------------------------------------------
# H3.1 + H3.2: Import tests
# ---------------------------------------------------------------------------


class TestScriptImports:
    """Test that both scripts can be imported."""

    def test_rebuild_script_importable(self, rebuild_module):
        """The rebuild script can be imported and exposes main()."""
        assert hasattr(rebuild_module, "main")
        assert hasattr(rebuild_module, "parse_args")
        assert hasattr(rebuild_module, "build_container")
        assert hasattr(rebuild_module, "push_container")
        assert hasattr(rebuild_module, "refresh_endpoint")
        assert hasattr(rebuild_module, "check_docker_installed")
        assert hasattr(rebuild_module, "check_docker_running")
        assert hasattr(rebuild_module, "check_dockerfile_exists")

    def test_verify_script_importable(self, verify_module):
        """The verify script can be imported and exposes main()."""
        assert hasattr(verify_module, "main")
        assert hasattr(verify_module, "parse_args")
        assert hasattr(verify_module, "verify_training_endpoint")
        assert hasattr(verify_module, "verify_inference_endpoint")
        assert hasattr(verify_module, "runpod_dispatch")
        assert hasattr(verify_module, "runpod_status")
        assert hasattr(verify_module, "runpod_health")

    def test_rebuild_script_containers_defined(self, rebuild_module):
        """The rebuild script knows about both containers."""
        assert "training" in rebuild_module.CONTAINERS
        assert "inference" in rebuild_module.CONTAINERS
        # Each entry is (dockerfile_rel, context_rel).
        train_dockerfile, _ = rebuild_module.CONTAINERS["training"]
        infer_dockerfile, _ = rebuild_module.CONTAINERS["inference"]
        assert "quant-foundry-training" in train_dockerfile
        assert "quant-foundry-inference" in infer_dockerfile

    def test_verify_script_endpoint_defaults(self, verify_module):
        """The verify script has the correct default endpoint IDs."""
        assert (
            verify_module.DEFAULT_TRAINING_ENDPOINT_ID == "8vol1uc9l75jgs"
        )
        assert (
            verify_module.DEFAULT_INFERENCE_ENDPOINT_ID == "36mz2q30jdyvru"
        )


# ---------------------------------------------------------------------------
# H3.3: --dry-run mode prints commands without executing
# ---------------------------------------------------------------------------


class TestDryRunMode:
    """Test that --dry-run mode prints commands without executing Docker."""

    def test_dry_run_build_does_not_call_subprocess(self, rebuild_module, capsys):
        """--dry-run build_container prints the command but never calls subprocess.run."""
        result = rebuild_module.build_container(
            container="training",
            tag="latest",
            dry_run=True,
        )
        captured = capsys.readouterr()

        # The result should report success (it's a dry run).
        assert result.success is True
        assert result.command is not None
        assert "docker build" in result.command

        # The printed output should mention dry-run.
        assert "dry-run" in captured.out.lower()
        assert "docker build" in captured.out

    def test_dry_run_push_does_not_call_subprocess(self, rebuild_module, capsys):
        """--dry-run push_container prints tag+push commands but never calls subprocess."""
        result = rebuild_module.push_container(
            container="training",
            image_tag="fincept/quant-foundry-training:latest",
            registry="ghcr.io/fincept",
            tag="latest",
            dry_run=True,
        )
        captured = capsys.readouterr()

        assert result.success is True
        assert result.command is not None
        assert "docker push" in result.command
        assert "dry-run" in captured.out.lower()

    def test_dry_run_refresh_does_not_call_http(self, rebuild_module, capsys):
        """--dry-run refresh_endpoint prints the request but never sends it."""
        result = rebuild_module.refresh_endpoint(
            endpoint_id="test-endpoint",
            api_key="fake-key",
            dry_run=True,
        )
        captured = capsys.readouterr()

        assert result.success is True
        assert "dry-run" in captured.out.lower()
        assert "test-endpoint" in captured.out
        # The API key must NOT appear in the dry-run output.
        assert "fake-key" not in captured.out

    def test_dry_run_main_does_not_execute_docker(self, rebuild_module, capsys):
        """Running main() with --dry-run builds both containers without Docker."""
        exit_code = rebuild_module.main(
            ["--container", "both", "--dry-run"]
        )
        captured = capsys.readouterr()

        assert exit_code == 0
        assert "dry-run" in captured.out.lower()
        # Both containers should be mentioned.
        assert "training" in captured.out
        assert "inference" in captured.out

    def test_dry_run_with_push_and_refresh(self, rebuild_module, capsys):
        """--dry-run with --push and --refresh-endpoint prints all commands."""
        exit_code = rebuild_module.main(
            [
                "--container", "both",
                "--push",
                "--registry", "ghcr.io/test",
                "--refresh-endpoint",
                "--dry-run",
            ]
        )
        captured = capsys.readouterr()

        assert exit_code == 0
        out = captured.out.lower()
        assert "dry-run" in out
        assert "docker build" in out
        assert "docker push" in out
        assert "refresh" in out

    def test_dry_run_summary_reports_dry_run(self, rebuild_module, capsys):
        """The summary in dry-run mode reports that no commands were executed."""
        rebuild_module.main(["--dry-run"])
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out


# ---------------------------------------------------------------------------
# H3.4: Missing Docker raises a clear error
# ---------------------------------------------------------------------------


class TestMissingDocker:
    """Test that missing Docker raises a clear, actionable error."""

    def test_check_docker_installed_raises_when_missing(self, rebuild_module):
        """check_docker_installed raises PreconditionError when docker is not on PATH."""
        with patch.object(rebuild_module.shutil, "which", return_value=None):
            with pytest.raises(rebuild_module.PreconditionError) as exc_info:
                rebuild_module.check_docker_installed()

        msg = str(exc_info.value).lower()
        assert "docker" in msg
        assert "not installed" in msg or "not on path" in msg

    def test_main_returns_error_when_docker_missing(self, rebuild_module, capsys):
        """main() returns exit code 1 and prints a clear error when Docker is missing."""
        with patch.object(rebuild_module.shutil, "which", return_value=None):
            exit_code = rebuild_module.main(["--container", "training"])

        assert exit_code == 1
        captured = capsys.readouterr()
        err = captured.err.lower()
        assert "docker" in err
        assert "not installed" in err or "not on path" in err

    def test_check_docker_running_raises_when_daemon_down(self, rebuild_module):
        """check_docker_running raises PreconditionError when the daemon is not running."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Cannot connect to the Docker daemon"

        with patch.object(rebuild_module.subprocess, "run", return_value=mock_result):
            with pytest.raises(rebuild_module.PreconditionError) as exc_info:
                rebuild_module.check_docker_running(dry_run=False)

        msg = str(exc_info.value).lower()
        assert "docker" in msg
        assert "not running" in msg or "daemon" in msg

    def test_check_docker_running_skipped_in_dry_run(self, rebuild_module):
        """check_docker_running is a no-op in dry-run mode (no subprocess call)."""
        with patch.object(rebuild_module.subprocess, "run") as mock_run:
            rebuild_module.check_docker_running(dry_run=True)
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# H3.5: Missing Dockerfile raises a clear error
# ---------------------------------------------------------------------------


class TestMissingDockerfile:
    """Test that a missing Dockerfile raises a clear, actionable error."""

    def test_check_dockerfile_exists_raises_for_unknown_container(self, rebuild_module):
        """check_dockerfile_exists raises for an unknown container name."""
        with pytest.raises(rebuild_module.PreconditionError) as exc_info:
            rebuild_module.check_dockerfile_exists("nonexistent")

        msg = str(exc_info.value).lower()
        assert "unknown container" in msg

    def test_check_dockerfile_exists_raises_when_file_missing(
        self, rebuild_module, tmp_path, monkeypatch
    ):
        """check_dockerfile_exists raises when the Dockerfile is not on disk."""
        # Point REPO_ROOT at a temp dir so the Dockerfile won't be found.
        monkeypatch.setattr(rebuild_module, "REPO_ROOT", tmp_path)
        with pytest.raises(rebuild_module.PreconditionError) as exc_info:
            rebuild_module.check_dockerfile_exists("training")

        msg = str(exc_info.value).lower()
        assert "dockerfile" in msg
        assert "not found" in msg

    def test_main_returns_error_when_dockerfile_missing(
        self, rebuild_module, tmp_path, monkeypatch, capsys
    ):
        """main() returns exit code 1 when the Dockerfile is missing."""
        monkeypatch.setattr(rebuild_module, "REPO_ROOT", tmp_path)
        # Docker is installed (so we get past that check) but the
        # Dockerfile check runs first and fails.
        exit_code = rebuild_module.main(["--container", "training"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "dockerfile" in captured.err.lower()

    def test_check_dockerfile_exists_succeeds_for_real_dockerfiles(
        self, rebuild_module
    ):
        """check_dockerfile_exists succeeds for the real training + inference Dockerfiles."""
        train_path = rebuild_module.check_dockerfile_exists("training")
        infer_path = rebuild_module.check_dockerfile_exists("inference")
        assert train_path.is_file()
        assert infer_path.is_file()
        assert "quant-foundry-training" in str(train_path)
        assert "quant-foundry-inference" in str(infer_path)


# ---------------------------------------------------------------------------
# Bonus: argument parsing tests
# ---------------------------------------------------------------------------


class TestArgumentParsing:
    """Test that CLI arguments are parsed correctly."""

    def test_rebuild_default_args(self, rebuild_module):
        """Default args: container=both, tag=latest, push=False, dry_run=False."""
        args = rebuild_module.parse_args([])
        assert args.container == "both"
        assert args.tag == "latest"
        assert args.push is False
        assert args.refresh_endpoint is False
        assert args.dry_run is False

    def test_rebuild_custom_args(self, rebuild_module):
        """Custom args are parsed correctly."""
        args = rebuild_module.parse_args(
            [
                "--container", "inference",
                "--tag", "v1.2.3",
                "--registry", "ghcr.io/myorg",
                "--push",
                "--refresh-endpoint",
                "--dry-run",
            ]
        )
        assert args.container == "inference"
        assert args.tag == "v1.2.3"
        assert args.registry == "ghcr.io/myorg"
        assert args.push is True
        assert args.refresh_endpoint is True
        assert args.dry_run is True

    def test_verify_default_args(self, verify_module):
        """Default verify args: endpoint=both."""
        args = verify_module.parse_args([])
        assert args.endpoint == "both"
        assert args.api_key is None

    def test_verify_custom_args(self, verify_module):
        """Custom verify args are parsed correctly."""
        args = verify_module.parse_args(
            [
                "--endpoint", "training",
                "--api-key", "test-key",
                "--training-endpoint-id", "custom-train-id",
            ]
        )
        assert args.endpoint == "training"
        assert args.api_key == "test-key"
        assert args.training_endpoint_id == "custom-train-id"


# ---------------------------------------------------------------------------
# Bonus: stub detection helper tests
# ---------------------------------------------------------------------------


class TestStubDetection:
    """Test the stub-detection helpers in the verify script."""

    def test_is_stub_artifact_hash_detects_stub(self, verify_module):
        """_is_stub_artifact_hash returns True for the stub pattern."""
        import hashlib
        import json as _json

        request_inputs = {
            "schema_version": 1,
            "job_id": "test-job",
            "dataset_manifest_ref": "ds-1",
            "model_family": "gbm",
            "search_space": {"n_estimators": [100]},
            "random_seed": 42,
            "hardware_class": "gpu-1",
            "extra_constraints": {},
        }
        canonical = _json.dumps(request_inputs, sort_keys=True).encode("utf-8")
        stub_hash = hashlib.sha256(canonical).hexdigest()
        stub_artifact_id = f"artifact:{stub_hash[:16]}"

        assert verify_module._is_stub_artifact_hash(stub_artifact_id, request_inputs) is True

    def test_is_stub_artifact_hash_rejects_real_hash(self, verify_module):
        """_is_stub_artifact_hash returns False for a real (non-stub) hash."""
        # A real artifact hash would be derived from model bytes, not
        # request inputs — so it won't match the stub formula.
        request_inputs = {
            "job_id": "test-job",
            "dataset_manifest_ref": "ds-1",
            "model_family": "gbm",
            "search_space": {"n_estimators": [100]},
            "random_seed": 42,
            "hardware_class": "gpu-1",
        }
        real_artifact_id = "artifact:abcdef0123456789"  # arbitrary, not stub-derived
        assert verify_module._is_stub_artifact_hash(real_artifact_id, request_inputs) is False

    def test_is_stub_prediction_detects_stub(self, verify_module):
        """_is_stub_prediction returns True for the linear-combination stub."""
        features = [0.1, 0.2, 0.3, 0.4]
        raw_score = sum(features) / len(features)
        direction = max(-1.0, min(1.0, raw_score * 2.0))
        confidence = min(1.0, abs(raw_score) + 0.3)
        p_up = 1.0 / (1.0 + (2.718281828 ** (-raw_score * 5.0)))

        stub_prediction = {
            "direction": direction,
            "confidence": confidence,
            "p_up": p_up,
        }
        assert verify_module._is_stub_prediction(stub_prediction, features) is True

    def test_is_stub_prediction_rejects_real(self, verify_module):
        """_is_stub_prediction returns False for a real (non-stub) prediction."""
        features = [0.1, 0.2, 0.3, 0.4]
        # A real model would produce different values.
        real_prediction = {
            "direction": 0.95,
            "confidence": 0.88,
            "p_up": 0.73,
        }
        assert verify_module._is_stub_prediction(real_prediction, features) is False

    def test_validate_callback_envelope_valid(self, verify_module):
        """_validate_callback_envelope returns no errors for a valid envelope."""
        envelope = {
            "schema_version": 1,
            "job_id": "test-job",
            "worker_id": "worker-1",
            "result_type": "training_complete",
            "payload": {"metrics": {}},
        }
        errors = verify_module._validate_callback_envelope(envelope)
        assert errors == []

    def test_validate_callback_envelope_missing_field(self, verify_module):
        """_validate_callback_envelope reports missing fields."""
        envelope = {
            "schema_version": 1,
            "job_id": "test-job",
            # missing worker_id, result_type, payload
        }
        errors = verify_module._validate_callback_envelope(envelope)
        assert len(errors) == 3
        assert any("worker_id" in e for e in errors)
        assert any("result_type" in e for e in errors)
        assert any("payload" in e for e in errors)

    def test_validate_callback_envelope_wrong_type(self, verify_module):
        """_validate_callback_envelope reports type mismatches."""
        envelope = {
            "schema_version": "not-an-int",  # wrong type
            "job_id": "test-job",
            "worker_id": "worker-1",
            "result_type": "training_complete",
            "payload": "not-a-dict",  # wrong type
        }
        errors = verify_module._validate_callback_envelope(envelope)
        assert len(errors) == 2
        assert any("schema_version" in e for e in errors)
        assert any("payload" in e for e in errors)


# ---------------------------------------------------------------------------
# Bonus: verify script config error tests
# ---------------------------------------------------------------------------


class TestVerifyConfigErrors:
    """Test that the verify script handles config errors gracefully."""

    def test_main_returns_error_when_api_key_missing(self, verify_module, monkeypatch, capsys):
        """main() returns exit code 1 when no API key is available."""
        monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
        exit_code = verify_module.main(["--endpoint", "both"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "api key" in captured.err.lower()
