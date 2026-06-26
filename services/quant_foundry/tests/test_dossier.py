"""
TDD tests for quant_foundry.dossier / artifacts / registry (TASK-0403: Dossier Registry).

Acceptance criteria from NEXT_STEPS_PLAN + AAAAAAAAA_BIG_PLAN:
- Existing local model can get a dossier.
- Mock artifact imports with hash verification.
- Bad hash is rejected.
- Dossier status is visible through a read API.

Plus the spec details + cross-cutting rigor §3 (reproducibility):
- Dossier carries the full reproducibility set: dataset/feature/label hashes, code SHA,
  lockfile hash, image digest, seeds, hardware class.
- Dossier carries `trial_count` for the model family (so the tournament can deflate Sharpe).
- Dossier carries a `blocking_issues` list that the sentinel (TASK-0406) and tournament
  (TASK-0404) write into; a blocking issue is a hard gate on promotion.
- Artifact hash verification is mandatory (pull-based, hash-verified import).
- Unsupported URI scheme is rejected (allowlisted schemes only).
- Dossiers are immutable by version/hash (same content -> same dossier; content change ->
  new version).
- Dossiers are stored durably (JSONL, restart-safe).
- No secrets in dossier/artifact records.

File-disjoint from:
- TASK-0401/0402 (Builder 1: settlement.py, outcomes.py, metrics.py, shadow_ledger.py)
- TASK-0304/0305 (Builder 2: outbox.py, inbox.py, mock_dispatcher.py, callbacks.py)
- TASK-0405 (Builder 4: feature_lake.py, dataset_manifest.py, feature_availability.py)
- TASK-0203 (Builder 5: services/api routes/modules.py, dashboard system page, scripts)

These tests do NOT modify schemas.py (ModelDossier + ArtifactManifest consumed read-only from
TASK-0302) and do NOT create services/api/routes/quant_foundry.py (TASK-0306 owns the API route;
the registry exposes a Python read API only for MVP).
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest
from pydantic import ValidationError
from quant_foundry.artifacts import (
    ArtifactRecord,
    UnsupportedUriError,
    import_artifact,
    verify_artifact_hash,
)
from quant_foundry.dossier import (
    DossierBuilder,
    DossierRecord,
    DossierStatus,
)
from quant_foundry.registry import DossierRegistry

# --- helpers -----------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture_artifact_bytes() -> bytes:
    """Deterministic mock artifact bytes (simulates a worker-produced model blob)."""
    return b"MOCK-MODEL-BLOB\x00gbm-v1\x00seed=42\x00features=ret,vol,imbalance"


def _base_artifact_kwargs(tmp_path: pathlib.Path, data: bytes | None = None) -> dict:
    data = data if data is not None else _fixture_artifact_bytes()
    artifact_path = tmp_path / "artifact.bin"
    artifact_path.write_bytes(data)
    return {
        "artifact_id": "art-001",
        "sha256": _sha256(data),
        "size_bytes": len(data),
        "uri": f"file:///{artifact_path.as_posix()}",
        "model_family": "gbm",
        "created_at_ns": 1_700_000_000_000_000_000,
        "feature_schema_hash": _sha256(b"features:ret,vol,imbalance"),
        "label_schema_hash": _sha256(b"label:dir_1d"),
        "code_git_sha": "abc123gitsha",
        "lockfile_hash": _sha256(b"uv.lock-v1"),
        "container_image_digest": "sha256:imgdigest",
    }


def _base_dossier_kwargs(artifact: ArtifactRecord) -> dict:
    return {
        "model_id": "gbm-v1",
        "artifact_manifest_id": artifact.artifact_id,
        "artifact_sha256": artifact.sha256,
        "dataset_manifest_id": "ds-manifest-001",
        "dataset_manifest_ref": "file:///data/ds-001.json",
        "feature_schema_hash": artifact.feature_schema_hash,
        "label_schema_hash": artifact.label_schema_hash,
        "code_git_sha": artifact.code_git_sha,
        "lockfile_hash": artifact.lockfile_hash,
        "container_image_digest": artifact.container_image_digest,
        "random_seed": 42,
        "hardware_class": "cpu-local",
        "trial_count": 1,
        "training_metrics": {"accuracy": 0.54, "brier": 0.21},
        "status": DossierStatus.CANDIDATE,
    }


# --- module / public API -----------------------------------------------------


def test_modules_import_and_types() -> None:
    """Modules and public API must be importable."""
    assert callable(DossierRegistry)
    assert callable(DossierBuilder)
    assert DossierStatus.CANDIDATE
    assert issubclass(DossierRecord, object)
    assert issubclass(ArtifactRecord, object)
    assert callable(verify_artifact_hash)
    assert callable(import_artifact)


# --- artifact hash verification ---------------------------------------------


def test_verify_artifact_hash_matches() -> None:
    data = _fixture_artifact_bytes()
    assert verify_artifact_hash(data, _sha256(data)) is True


def test_verify_artifact_hash_rejects_bad_hash() -> None:
    """Bad hash must be rejected (fail closed, security event)."""
    data = _fixture_artifact_bytes()
    with pytest.raises(ValueError, match=r"hash mismatch|tamper|security"):
        verify_artifact_hash(data, "0" * 64)  # wrong hash


def test_verify_artifact_hash_rejects_malformed_hash() -> None:
    data = _fixture_artifact_bytes()
    with pytest.raises((ValueError, TypeError)):
        verify_artifact_hash(data, "not-a-hex-hash")


# --- artifact import (pull-based, URI allowlist) -----------------------------


def test_import_artifact_file_scheme_succeeds(tmp_path: pathlib.Path) -> None:
    data = _fixture_artifact_bytes()
    artifact_path = tmp_path / "artifact.bin"
    artifact_path.write_bytes(data)
    uri = f"file:///{artifact_path.as_posix()}"

    rec = import_artifact(
        uri=uri,
        expected_sha256=_sha256(data),
        artifact_id="art-001",
        model_family="gbm",
        feature_schema_hash=_sha256(b"features"),
        label_schema_hash=_sha256(b"labels"),
        code_git_sha="abc123",
        lockfile_hash=_sha256(b"lock"),
        container_image_digest="sha256:img",
    )
    assert isinstance(rec, ArtifactRecord)
    assert rec.sha256 == _sha256(data)
    assert rec.size_bytes == len(data)
    assert rec.uri == uri


def test_import_artifact_rejects_bad_hash(tmp_path: pathlib.Path) -> None:
    """Importing an artifact whose content does not match the expected hash is rejected."""
    data = _fixture_artifact_bytes()
    artifact_path = tmp_path / "artifact.bin"
    artifact_path.write_bytes(data)
    uri = f"file:///{artifact_path.as_posix()}"

    with pytest.raises(ValueError, match=r"hash mismatch|tamper|security"):
        import_artifact(
            uri=uri,
            expected_sha256="0" * 64,  # wrong
            artifact_id="art-001",
            model_family="gbm",
            feature_schema_hash=_sha256(b"features"),
            label_schema_hash=_sha256(b"labels"),
            code_git_sha="abc123",
            lockfile_hash=_sha256(b"lock"),
            container_image_digest="sha256:img",
        )


def test_import_artifact_rejects_unsupported_uri_scheme(tmp_path: pathlib.Path) -> None:
    """Only allowlisted URI schemes are permitted (no arbitrary fetch)."""
    with pytest.raises(UnsupportedUriError, match=r"scheme|unsupported|allowlist"):
        import_artifact(
            uri="http://evil.example.com/model.bin",  # http not allowlisted for MVP
            expected_sha256="0" * 64,
            artifact_id="art-001",
            model_family="gbm",
            feature_schema_hash=_sha256(b"features"),
            label_schema_hash=_sha256(b"labels"),
            code_git_sha="abc123",
            lockfile_hash=_sha256(b"lock"),
            container_image_digest="sha256:img",
        )


def test_import_artifact_rejects_path_traversal(tmp_path: pathlib.Path) -> None:
    """file:// URIs with traversal must not escape (defense-in-depth)."""
    with pytest.raises((UnsupportedUriError, ValueError), match=r"traversal|escape|invalid"):
        import_artifact(
            uri="file:///../../etc/passwd",
            expected_sha256="0" * 64,
            artifact_id="art-001",
            model_family="gbm",
            feature_schema_hash=_sha256(b"features"),
            label_schema_hash=_sha256(b"labels"),
            code_git_sha="abc123",
            lockfile_hash=_sha256(b"lock"),
            container_image_digest="sha256:img",
        )


# --- ArtifactRecord contract -------------------------------------------------


def test_artifact_record_is_frozen_and_forbids_extra(tmp_path: pathlib.Path) -> None:
    rec = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    assert rec.model_config.get("frozen") is True
    assert rec.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError):
        ArtifactRecord(**{**_base_artifact_kwargs(tmp_path), "evil": "no"})  # type: ignore[arg-type]


# --- DossierRecord reproducibility set ---------------------------------------


def test_dossier_record_carries_full_reproducibility_set(tmp_path: pathlib.Path) -> None:
    artifact = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    d = DossierRecord(**_base_dossier_kwargs(artifact))
    # Reproducibility set (cross-cutting rigor §3)
    assert d.feature_schema_hash == artifact.feature_schema_hash
    assert d.label_schema_hash == artifact.label_schema_hash
    assert d.code_git_sha == artifact.code_git_sha
    assert d.lockfile_hash == artifact.lockfile_hash
    assert d.container_image_digest == artifact.container_image_digest
    assert d.random_seed == 42
    assert d.hardware_class == "cpu-local"
    assert d.dataset_manifest_id == "ds-manifest-001"
    # Trial count for Deflated Sharpe
    assert d.trial_count == 1
    # blocking_issues list present and empty by default
    assert d.blocking_issues == []


def test_dossier_record_is_frozen_and_forbids_extra(tmp_path: pathlib.Path) -> None:
    artifact = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    d = DossierRecord(**_base_dossier_kwargs(artifact))
    assert d.model_config.get("frozen") is True
    assert d.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError):
        DossierRecord(**{**_base_dossier_kwargs(artifact), "evil": "no"})  # type: ignore[arg-type]


def test_dossier_status_enum_has_promotion_lifecycle() -> None:
    """DossierStatus must carry the promotion lifecycle states."""
    statuses = {s.value for s in DossierStatus}
    assert "candidate" in statuses
    assert "research_approved" in statuses
    assert "shadow_approved" in statuses
    assert "paper_approved" in statuses
    assert "rejected" in statuses


# --- DossierBuilder ----------------------------------------------------------


def test_dossier_builder_assembles_dossier(tmp_path: pathlib.Path) -> None:
    artifact = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    builder = DossierBuilder()
    d = builder.build(
        artifact=artifact,
        model_id="gbm-v1",
        dataset_manifest_id="ds-001",
        dataset_manifest_ref="file:///data/ds-001.json",
        random_seed=42,
        hardware_class="cpu-local",
        trial_count=1,
        training_metrics={"accuracy": 0.54},
    )
    assert d.model_id == "gbm-v1"
    assert d.artifact_manifest_id == artifact.artifact_id
    assert d.artifact_sha256 == artifact.sha256
    assert d.feature_schema_hash == artifact.feature_schema_hash
    assert d.label_schema_hash == artifact.label_schema_hash
    assert d.code_git_sha == artifact.code_git_sha
    assert d.lockfile_hash == artifact.lockfile_hash
    assert d.container_image_digest == artifact.container_image_digest
    assert d.status == DossierStatus.CANDIDATE
    assert d.trial_count == 1
    assert d.blocking_issues == []


def test_dossier_builder_rejects_empty_model_id(tmp_path: pathlib.Path) -> None:
    artifact = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    builder = DossierBuilder()
    with pytest.raises((ValueError, ValidationError), match=r"model_id|empty"):
        builder.build(
            artifact=artifact,
            model_id="",
            dataset_manifest_id="ds-001",
            dataset_manifest_ref="file:///data/ds-001.json",
            random_seed=42,
            hardware_class="cpu-local",
            trial_count=1,
            training_metrics={},
        )


# --- DossierRegistry: register + idempotency + immutability ------------------


def test_registry_register_and_get(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf-registry"
    reg = DossierRegistry(base_dir=base)
    artifact = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    d = DossierRecord(**_base_dossier_kwargs(artifact))
    reg.register(d)
    got = reg.get(d.model_id)
    assert got is not None
    assert got.model_id == d.model_id
    assert got.artifact_sha256 == d.artifact_sha256
    assert (base / "dossier_registry.jsonl").is_file()


def test_registry_register_idempotent_same_content(tmp_path: pathlib.Path) -> None:
    """Registering the same dossier (same model_id + same content hash) is idempotent."""
    base = tmp_path / "qf-registry"
    reg = DossierRegistry(base_dir=base)
    artifact = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    d = DossierRecord(**_base_dossier_kwargs(artifact))
    reg.register(d)
    reg.register(d)  # idempotent
    assert len(reg.list()) == 1


def test_registry_rejects_same_model_id_different_content(tmp_path: pathlib.Path) -> None:
    """Same model_id with different content (different artifact hash) is a security event.

    A dossier is immutable by version/hash. Re-registering the same model_id with a different
    artifact must fail closed (tamper/replay), mirroring the outbox/inbox diff-hash invariant.
    """
    base = tmp_path / "qf-registry"
    reg = DossierRegistry(base_dir=base)
    artifact = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    d1 = DossierRecord(**_base_dossier_kwargs(artifact))
    reg.register(d1)

    # Tampered: same model_id, different artifact hash
    d2 = d1.model_copy(update={"artifact_sha256": "f" * 64, "artifact_manifest_id": "art-002"})
    with pytest.raises(ValueError, match=r"content hash mismatch|security|tamper|immutable"):
        reg.register(d2)


def test_registry_survives_restart(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf-registry"
    reg1 = DossierRegistry(base_dir=base)
    artifact = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    d = DossierRecord(**_base_dossier_kwargs(artifact))
    reg1.register(d)

    reg2 = DossierRegistry(base_dir=base)
    got = reg2.get(d.model_id)
    assert got is not None
    assert got.model_id == d.model_id
    assert got.artifact_sha256 == d.artifact_sha256


def test_registry_list_filters_by_status(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf-registry"
    reg = DossierRegistry(base_dir=base)
    artifact = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    d1 = DossierRecord(**_base_dossier_kwargs(artifact))
    d2 = DossierRecord(
        **{**_base_dossier_kwargs(artifact), "model_id": "gbm-v2", "status": DossierStatus.REJECTED}
    )
    reg.register(d1)
    reg.register(d2)

    all_d = reg.list()
    assert len(all_d) == 2
    candidates = reg.list(status=DossierStatus.CANDIDATE)
    assert len(candidates) == 1 and candidates[0].model_id == "gbm-v1"
    rejected = reg.list(status=DossierStatus.REJECTED)
    assert len(rejected) == 1 and rejected[0].model_id == "gbm-v2"


def test_registry_update_status_appends_new_version(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf-registry"
    reg = DossierRegistry(base_dir=base)
    artifact = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    dossier = DossierRecord(**_base_dossier_kwargs(artifact))
    registered = reg.register(dossier)

    updated = reg.update_status(dossier.model_id, DossierStatus.SHADOW_APPROVED)

    assert updated.model_id == dossier.model_id
    assert updated.status == DossierStatus.SHADOW_APPROVED
    assert updated.content_hash != registered.content_hash
    assert reg.get(dossier.model_id).status == DossierStatus.SHADOW_APPROVED
    shadow = reg.list(status=DossierStatus.SHADOW_APPROVED)
    assert [d.model_id for d in shadow] == [dossier.model_id]

    reloaded = DossierRegistry(base_dir=base)
    assert reloaded.get(dossier.model_id).status == DossierStatus.SHADOW_APPROVED


def test_registry_update_status_unknown_model(tmp_path: pathlib.Path) -> None:
    reg = DossierRegistry(base_dir=tmp_path / "qf-registry")
    with pytest.raises(KeyError, match=r"unknown|model_id"):
        reg.update_status("nope", DossierStatus.SHADOW_APPROVED)


def test_registry_get_by_hash(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf-registry"
    reg = DossierRegistry(base_dir=base)
    artifact = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    d = DossierRecord(**_base_dossier_kwargs(artifact))
    reg.register(d)
    got = reg.get_by_hash(d.content_hash)
    assert got is not None
    assert got.model_id == d.model_id


# --- blocking_issues (sentinel + tournament write into) ----------------------


def test_registry_add_blocking_issue(tmp_path: pathlib.Path) -> None:
    """A blocking issue can be appended to a dossier; it is visible and append-only."""
    base = tmp_path / "qf-registry"
    reg = DossierRegistry(base_dir=base)
    artifact = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    d = DossierRecord(**_base_dossier_kwargs(artifact))
    reg.register(d)

    updated = reg.add_blocking_issue(
        d.model_id, source="sentinel", code="leakage_detected", note="future-leak feature"
    )
    assert len(updated.blocking_issues) == 1
    issue = updated.blocking_issues[0]
    assert issue["source"] == "sentinel"
    assert issue["code"] == "leakage_detected"
    assert issue["note"] == "future-leak feature"
    assert issue["ts_ns"] > 0

    # Append a second issue (append-only)
    updated2 = reg.add_blocking_issue(
        d.model_id, source="tournament", code="stale_evidence", note=">30d"
    )
    assert len(updated2.blocking_issues) == 2


def test_registry_add_blocking_issue_unknown_model(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf-registry"
    reg = DossierRegistry(base_dir=base)
    with pytest.raises(KeyError, match=r"unknown|model_id"):
        reg.add_blocking_issue("nope", source="sentinel", code="x", note="y")


# --- no secrets --------------------------------------------------------------


def test_dossier_records_contain_no_secret_fields(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf-registry"
    reg = DossierRegistry(base_dir=base)
    artifact = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    d = DossierRecord(**_base_dossier_kwargs(artifact))
    reg.register(d)
    got = reg.get(d.model_id)
    assert got is not None
    dumped = json.dumps(got.model_dump(), sort_keys=True)
    for forbidden in (
        "token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "broker_account",
        "credential",
    ):
        assert forbidden not in dumped.lower(), f"dossier leaks secret-like field: {forbidden}"


def test_artifact_records_contain_no_secret_fields(tmp_path: pathlib.Path) -> None:
    rec = ArtifactRecord(**_base_artifact_kwargs(tmp_path))
    dumped = json.dumps(rec.model_dump(), sort_keys=True)
    for forbidden in (
        "token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "broker_account",
        "credential",
    ):
        assert forbidden not in dumped.lower(), f"artifact leaks secret-like field: {forbidden}"


# --- end-to-end: existing local model gets a dossier -------------------------


def test_end_to_end_local_model_gets_dossier(tmp_path: pathlib.Path) -> None:
    """Acceptance: existing local model can get a dossier (fixture-backed)."""
    # 1. Write a fixture "local model" blob.
    model_blob = b"GBM-MODEL\x00n_estimators=100\x00max_depth=3"
    model_path = tmp_path / "gbm-v1.bin"
    model_path.write_bytes(model_blob)

    # 2. Import the artifact (pull-based, hash-verified).
    artifact = import_artifact(
        uri=f"file:///{model_path.as_posix()}",
        expected_sha256=_sha256(model_blob),
        artifact_id="art-gbm-v1",
        model_family="gbm",
        feature_schema_hash=_sha256(b"features:ret,vol,imbalance"),
        label_schema_hash=_sha256(b"label:dir_1d"),
        code_git_sha="abc123gitsha",
        lockfile_hash=_sha256(b"uv.lock-v1"),
        container_image_digest="sha256:imgdigest",
    )

    # 3. Build a dossier.
    builder = DossierBuilder()
    dossier = builder.build(
        artifact=artifact,
        model_id="gbm-v1",
        dataset_manifest_id="ds-001",
        dataset_manifest_ref="file:///data/ds-001.json",
        random_seed=42,
        hardware_class="cpu-local",
        trial_count=1,
        training_metrics={"accuracy": 0.54, "brier": 0.21},
    )

    # 4. Register it.
    reg = DossierRegistry(base_dir=tmp_path / "qf-registry")
    reg.register(dossier)

    # 5. Read it back.
    got = reg.get("gbm-v1")
    assert got is not None
    assert got.status == DossierStatus.CANDIDATE
    assert got.artifact_sha256 == _sha256(model_blob)
    assert got.trial_count == 1
    assert got.training_metrics["accuracy"] == 0.54
