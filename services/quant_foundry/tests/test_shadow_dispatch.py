"""
Tests for the scheduled shadow inference dispatch loop (Agent C).

Covers:
- ``dispatch_shadow_inference_batch()`` dispatches inference jobs for
  SHADOW_APPROVED models in ``runpod_shadow`` / ``runpod_research`` mode.
- Disabled gateway returns ``{"enabled": False}``.
- Non-shadow mode returns ``{"skipped": True}``.
- Errors for one model do not stop other models from dispatching.
- ``shadow_dispatch_status`` returns cumulative count + timestamp.
- Integration: settlement sweep, tournament sweep, and RunPod polling
  still work after a dispatch batch.
"""

from __future__ import annotations

import pathlib
import time
from typing import Any

from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.runpod_client import MockRunPodClient


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _mock_client() -> MockRunPodClient:
    return MockRunPodClient(
        api_key="test-key",
        cost_per_dispatch_cents=0,
        duration_per_dispatch_seconds=0.0,
    )


def _gateway(
    base_dir: pathlib.Path,
    *,
    enabled: bool = True,
    mode: str = "runpod_shadow",
    client: Any = None,
) -> QuantFoundryGateway:
    if client is None:
        client = _mock_client()
    return QuantFoundryGateway(
        enabled=enabled,
        mode=mode,
        shadow_only=True,
        callback_secret="test-secret",
        base_dir=base_dir,
        runpod_clients={"inference": client, "training": client},
    )


def _dossier(
    model_id: str,
    *,
    status: DossierStatus = DossierStatus.SHADOW_APPROVED,
) -> DossierRecord:
    return DossierRecord(
        model_id=model_id,
        artifact_manifest_id=f"artifact-{model_id}",
        artifact_sha256=f"sha256-{model_id}",
        dataset_manifest_id="dataset-test",
        feature_schema_hash="fs-hash",
        label_schema_hash="ls-hash",
        trial_count=3,
        status=status,
    )


# --------------------------------------------------------------------------- #
# dispatch_shadow_inference_batch                                              #
# --------------------------------------------------------------------------- #


class TestDispatchShadowInferenceBatch:
    def test_disabled_gateway_returns_disabled(self, tmp_path: pathlib.Path) -> None:
        gw = _gateway(tmp_path, enabled=False)
        receipt = gw.dispatch_shadow_inference_batch()
        assert receipt["enabled"] is False

    def test_not_shadow_mode_returns_skipped(self, tmp_path: pathlib.Path) -> None:
        gw = _gateway(tmp_path, mode="local_mock")
        receipt = gw.dispatch_shadow_inference_batch()
        assert receipt["enabled"] is True
        assert receipt["skipped"] is True
        assert receipt["reason"] == "not in shadow mode"

    def test_runpod_research_mode_dispatches(self, tmp_path: pathlib.Path) -> None:
        gw = _gateway(tmp_path, mode="runpod_research")
        gw.dossier_registry().register(_dossier("model-research"))
        receipt = gw.dispatch_shadow_inference_batch()
        assert receipt["enabled"] is True
        assert receipt["dispatched"] == 1
        assert receipt["skipped"] == 0
        assert len(receipt["job_ids"]) == 1

    def test_dispatches_for_shadow_approved_models(
        self, tmp_path: pathlib.Path
    ) -> None:
        gw = _gateway(tmp_path)
        gw.dossier_registry().register(_dossier("model-a"))
        gw.dossier_registry().register(_dossier("model-b"))

        receipt = gw.dispatch_shadow_inference_batch()

        assert receipt["enabled"] is True
        assert receipt["dispatched"] == 2
        assert receipt["skipped"] == 0
        assert len(receipt["job_ids"]) == 2
        assert receipt["errors"] == []

    def test_skips_non_shadow_approved_models(self, tmp_path: pathlib.Path) -> None:
        gw = _gateway(tmp_path)
        gw.dossier_registry().register(
            _dossier("model-candidate", status=DossierStatus.CANDIDATE)
        )
        gw.dossier_registry().register(_dossier("model-shadow"))

        receipt = gw.dispatch_shadow_inference_batch()

        assert receipt["enabled"] is True
        assert receipt["dispatched"] == 1
        assert receipt["skipped"] == 0
        assert len(receipt["job_ids"]) == 1

    def test_no_dossiers_returns_empty_receipt(self, tmp_path: pathlib.Path) -> None:
        gw = _gateway(tmp_path)
        receipt = gw.dispatch_shadow_inference_batch()
        assert receipt["enabled"] is True
        assert receipt["dispatched"] == 0
        assert receipt["skipped"] == 0
        assert receipt["job_ids"] == []
        assert receipt["errors"] == []

    def test_handles_errors_gracefully(self, tmp_path: pathlib.Path) -> None:
        gw = _gateway(tmp_path)
        gw.dossier_registry().register(_dossier("model-good"))
        gw.dossier_registry().register(_dossier("model-bad"))

        original_create_job = gw.create_job
        call_count = {"n": 0}

        def flaky_create_job(*args: Any, **kwargs: Any) -> dict[str, Any]:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated dispatch failure")
            return original_create_job(*args, **kwargs)

        gw.create_job = flaky_create_job  # type: ignore[method-assign]
        receipt = gw.dispatch_shadow_inference_batch()

        assert receipt["enabled"] is True
        assert receipt["dispatched"] == 1
        assert receipt["skipped"] == 1
        assert len(receipt["job_ids"]) == 1
        assert len(receipt["errors"]) == 1
        assert receipt["errors"][0]["error_code"] == "RuntimeError"

    def test_returns_correct_dispatch_receipt(self, tmp_path: pathlib.Path) -> None:
        gw = _gateway(tmp_path)
        gw.dossier_registry().register(_dossier("model-x"))

        receipt = gw.dispatch_shadow_inference_batch()

        assert set(receipt.keys()) >= {
            "enabled", "dispatched", "skipped", "job_ids", "errors",
        }
        assert receipt["enabled"] is True
        assert receipt["dispatched"] == 1
        assert receipt["skipped"] == 0
        assert isinstance(receipt["job_ids"], list)
        assert all(isinstance(jid, str) for jid in receipt["job_ids"])
        assert isinstance(receipt["errors"], list)

    def test_updates_dispatch_count_and_timestamp(
        self, tmp_path: pathlib.Path
    ) -> None:
        gw = _gateway(tmp_path)
        gw.dossier_registry().register(_dossier("model-count"))

        before_ns = time.time_ns()
        receipt = gw.dispatch_shadow_inference_batch()
        after_ns = time.time_ns()

        assert receipt["dispatched"] == 1
        status = gw.shadow_dispatch_status
        assert status["dispatch_count"] == 1
        assert status["last_dispatch_ns"] >= before_ns
        assert status["last_dispatch_ns"] <= after_ns

    def test_no_secrets_in_receipt(self, tmp_path: pathlib.Path) -> None:
        gw = _gateway(tmp_path)
        gw.dossier_registry().register(_dossier("model-secret"))

        receipt = gw.dispatch_shadow_inference_batch()
        receipt_json = str(receipt)
        assert "test-key" not in receipt_json
        assert "api_key" not in receipt_json


# --------------------------------------------------------------------------- #
# shadow_dispatch_status                                                       #
# --------------------------------------------------------------------------- #


class TestShadowDispatchStatus:
    def test_initial_status(self, tmp_path: pathlib.Path) -> None:
        gw = _gateway(tmp_path)
        status = gw.shadow_dispatch_status
        assert status["dispatch_count"] == 0
        assert status["last_dispatch_ns"] == 0
        assert status["enabled"] is True

    def test_status_after_dispatch(self, tmp_path: pathlib.Path) -> None:
        gw = _gateway(tmp_path)
        gw.dossier_registry().register(_dossier("model-s1"))
        gw.dossier_registry().register(_dossier("model-s2"))

        gw.dispatch_shadow_inference_batch()
        status = gw.shadow_dispatch_status
        assert status["dispatch_count"] == 2
        assert status["last_dispatch_ns"] > 0

    def test_cumulative_count_across_batches(self, tmp_path: pathlib.Path) -> None:
        gw = _gateway(tmp_path)
        gw.dossier_registry().register(_dossier("model-cum1"))

        gw.dispatch_shadow_inference_batch()
        count_after_first = gw.shadow_dispatch_status["dispatch_count"]
        assert count_after_first == 1

        gw.dossier_registry().register(_dossier("model-cum2"))
        gw.dispatch_shadow_inference_batch()

        status = gw.shadow_dispatch_status
        assert status["dispatch_count"] > count_after_first

    def test_disabled_gateway_status(self, tmp_path: pathlib.Path) -> None:
        gw = _gateway(tmp_path, enabled=False)
        status = gw.shadow_dispatch_status
        assert status["enabled"] is False
        assert status["dispatch_count"] == 0
        assert status["last_dispatch_ns"] == 0


# --------------------------------------------------------------------------- #
# Integration with existing gateway sweeps                                     #
# --------------------------------------------------------------------------- #


class TestDispatchIntegration:
    def test_settlement_sweep_still_works_after_dispatch(
        self, tmp_path: pathlib.Path
    ) -> None:
        gw = _gateway(tmp_path)
        gw.dossier_registry().register(_dossier("model-int-settle"))
        gw.dispatch_shadow_inference_batch()

        receipt = gw.run_settlement_sweep()
        assert "settled_count" in receipt

    def test_tournament_sweep_still_works_after_dispatch(
        self, tmp_path: pathlib.Path
    ) -> None:
        gw = _gateway(tmp_path)
        gw.dossier_registry().register(_dossier("model-int-tourn"))
        gw.dispatch_shadow_inference_batch()

        receipt = gw.run_tournament_sweep()
        assert receipt["enabled"] is True

    def test_poll_runpod_results_still_works_after_dispatch(
        self, tmp_path: pathlib.Path
    ) -> None:
        gw = _gateway(tmp_path)
        gw.dossier_registry().register(_dossier("model-int-poll"))
        gw.dispatch_shadow_inference_batch()

        results = gw.poll_runpod_results()
        assert isinstance(results, list)

    def test_shadow_health_still_works_after_dispatch(
        self, tmp_path: pathlib.Path
    ) -> None:
        gw = _gateway(tmp_path)
        gw.dossier_registry().register(_dossier("model-int-health"))
        gw.dispatch_shadow_inference_batch()

        health = gw.shadow_health()
        assert health["enabled"] is True
