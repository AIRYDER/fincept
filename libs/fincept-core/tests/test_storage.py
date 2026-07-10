"""
Tests for fincept_core.storage — provider-agnostic storage abstraction.

Covers:
- LocalStorageBackend (file:// URIs, bare paths, text, exists, traversal, mkdir).
- S3StorageBackend (URI parsing, traversal, mocked read/write, custom endpoint).
- StorageConfig (env var reading, defaults).
- get_storage_backend() factory (local, s3, singleton cache).
- Integration: RealLightGBMTrainer + ModelLoader with LocalStorageBackend
  (backward compat — runs only when lightgbm is installed).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fincept_core.storage import (
    LocalStorageBackend,
    PathTraversalError,
    S3StorageBackend,
    StorageBackend,
    StorageConfig,
    StorageConfigError,
    StorageError,
    UnsupportedUriError,
    clear_storage_backend_cache,
    get_storage_backend,
    parse_file_uri,
    parse_s3_uri,
    resolve_uri,
)

# ---------------------------------------------------------------------------
# LocalStorageBackend
# ===========================================================================


class TestLocalStorageBackend:
    def test_is_storage_backend(self) -> None:
        backend = LocalStorageBackend()
        assert isinstance(backend, StorageBackend)

    def test_read_write_bytes_file_uri(self, tmp_path: Path) -> None:
        backend = LocalStorageBackend(base_dir=tmp_path)
        uri = (tmp_path / "a.bin").as_uri()
        canonical = backend.write_bytes(uri, b"hello-bytes")
        assert canonical.startswith("file://")
        assert backend.read_bytes(uri) == b"hello-bytes"

    def test_read_write_bytes_bare_path(self, tmp_path: Path) -> None:
        backend = LocalStorageBackend(base_dir=tmp_path)
        canonical = backend.write_bytes("rel/file.bin", b"abc")
        assert canonical.startswith("file://")
        assert backend.read_bytes("rel/file.bin") == b"abc"
        assert (tmp_path / "rel" / "file.bin").is_file()

    def test_read_write_text(self, tmp_path: Path) -> None:
        backend = LocalStorageBackend(base_dir=tmp_path)
        canonical = backend.write_text("notes.txt", "plain text")
        assert canonical.startswith("file://")
        assert backend.read_text("notes.txt") == "plain text"

    def test_exists_true_false(self, tmp_path: Path) -> None:
        backend = LocalStorageBackend(base_dir=tmp_path)
        assert backend.exists("missing.txt") is False
        backend.write_bytes("present.bin", b"x")
        assert backend.exists("present.bin") is True

    def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        backend = LocalStorageBackend(base_dir=tmp_path)
        with pytest.raises(PathTraversalError):
            backend.read_bytes("file:///../escape.bin")
        with pytest.raises(PathTraversalError):
            backend.write_bytes("../escape.bin", b"x")

    def test_parent_dirs_created_on_write(self, tmp_path: Path) -> None:
        backend = LocalStorageBackend(base_dir=tmp_path)
        backend.write_bytes("deep/nested/dir/file.bin", b"data")
        assert (tmp_path / "deep" / "nested" / "dir" / "file.bin").is_file()

    def test_read_missing_raises(self, tmp_path: Path) -> None:
        backend = LocalStorageBackend(base_dir=tmp_path)
        with pytest.raises(StorageError):
            backend.read_bytes("nope.bin")

    def test_absolute_bare_path(self, tmp_path: Path) -> None:
        abs_path = tmp_path / "abs.bin"
        backend = LocalStorageBackend()
        canonical = backend.write_bytes(str(abs_path), b"abs")
        assert canonical.startswith("file://")
        assert backend.read_bytes(str(abs_path)) == b"abs"


# ---------------------------------------------------------------------------
# URI helpers
# ===========================================================================


class TestUriHelpers:
    def test_parse_s3_uri(self) -> None:
        bucket, key = parse_s3_uri("s3://my-bucket/path/to/object.bin")
        assert bucket == "my-bucket"
        assert key == "path/to/object.bin"

    def test_parse_s3_uri_empty_bucket(self) -> None:
        with pytest.raises(UnsupportedUriError):
            parse_s3_uri("s3:///key.bin")

    def test_parse_s3_uri_empty_key(self) -> None:
        with pytest.raises(UnsupportedUriError):
            parse_s3_uri("s3://bucket/")

    def test_parse_s3_uri_traversal(self) -> None:
        with pytest.raises(PathTraversalError):
            parse_s3_uri("s3://bucket/../escape.bin")

    def test_parse_s3_uri_wrong_scheme(self) -> None:
        with pytest.raises(UnsupportedUriError):
            parse_s3_uri("file:///x")

    def test_parse_file_uri_bare(self, tmp_path: Path) -> None:
        path = parse_file_uri(str(tmp_path / "x.bin"))
        assert path == Path(str(tmp_path / "x.bin"))

    def test_parse_file_uri_traversal(self) -> None:
        with pytest.raises(PathTraversalError):
            parse_file_uri("file:///../escape.bin")


# ---------------------------------------------------------------------------
# S3StorageBackend (mocked client; boto3 import is optional)
# ===========================================================================


class _MockBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def _make_mock_s3_client() -> MagicMock:
    client = MagicMock()
    objects: dict[tuple[str, str], bytes] = {}

    def get_object(Bucket: str, Key: str) -> dict:
        if (Bucket, Key) not in objects:
            client.head_object.side_effect = Exception("NotFound")
            raise Exception("NotFound")
        return {"Body": _MockBody(objects[(Bucket, Key)])}

    def put_object(Bucket: str, Key: str, Body: bytes) -> dict:
        objects[(Bucket, Key)] = bytes(Body)
        return {}

    def head_object(Bucket: str, Key: str) -> dict:
        if (Bucket, Key) not in objects:
            raise Exception("NotFound")
        return {}

    client.get_object.side_effect = get_object
    client.put_object.side_effect = put_object
    client.head_object.side_effect = head_object
    client._objects = objects  # type: ignore[attr-defined]
    return client


class TestS3StorageBackend:
    def test_is_storage_backend(self) -> None:
        backend = S3StorageBackend(client=MagicMock())
        assert isinstance(backend, StorageBackend)

    def test_uri_parsing(self) -> None:
        backend = S3StorageBackend(client=MagicMock())
        bucket, key = backend._resolve_bucket("s3://bkt/key.bin")
        assert bucket == "bkt"
        assert key == "key.bin"

    def test_bare_key_uses_default_bucket(self) -> None:
        backend = S3StorageBackend(bucket="default-bucket", client=MagicMock())
        bucket, key = backend._resolve_bucket("path/key.bin")
        assert bucket == "default-bucket"
        assert key == "path/key.bin"

    def test_bare_key_no_default_bucket_raises(self) -> None:
        backend = S3StorageBackend(client=MagicMock())
        with pytest.raises(StorageConfigError):
            backend._resolve_bucket("key.bin")

    def test_path_traversal_rejected(self) -> None:
        backend = S3StorageBackend(client=MagicMock())
        with pytest.raises(PathTraversalError):
            backend._resolve_bucket("s3://bkt/../escape.bin")

    def test_read_write_with_mocked_client(self) -> None:
        client = _make_mock_s3_client()
        backend = S3StorageBackend(client=client)
        canonical = backend.write_bytes("s3://bkt/model.bin", b"model-bytes")
        assert canonical == "s3://bkt/model.bin"
        assert backend.read_bytes("s3://bkt/model.bin") == b"model-bytes"
        assert backend.exists("s3://bkt/model.bin") is True
        assert backend.exists("s3://bkt/missing.bin") is False

    def test_custom_endpoint_url_stored(self) -> None:
        backend = S3StorageBackend(
            endpoint_url="https://r2.cloudflarestorage.com",
            region="auto",
            client=MagicMock(),
        )
        assert backend.endpoint_url == "https://r2.cloudflarestorage.com"
        assert backend.region == "auto"

    def test_write_failure_wrapped(self) -> None:
        client = MagicMock()
        client.put_object.side_effect = Exception("boom")
        backend = S3StorageBackend(client=client)
        with pytest.raises(StorageError):
            backend.write_bytes("s3://bkt/k.bin", b"x")

    def test_read_failure_wrapped(self) -> None:
        client = MagicMock()
        client.get_object.side_effect = Exception("boom")
        backend = S3StorageBackend(client=client)
        with pytest.raises(StorageError):
            backend.read_bytes("s3://bkt/k.bin")

    def test_download_to_temp(self) -> None:
        client = _make_mock_s3_client()
        backend = S3StorageBackend(client=client)
        backend.write_bytes("s3://bkt/data.csv", b"col1,col2\n1,2\n")
        tmp = backend.download_to_temp("s3://bkt/data.csv")
        try:
            assert os.path.isfile(tmp)
            with open(tmp, "rb") as fh:
                assert fh.read() == b"col1,col2\n1,2\n"
            assert tmp.endswith(".csv")
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# StorageConfig
# ===========================================================================


class TestStorageConfig:
    def test_defaults_to_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "FINCEPT_STORAGE_BACKEND",
            "FINCEPT_STORAGE_LOCAL_BASE_DIR",
            "FINCEPT_STORAGE_S3_ENDPOINT",
            "FINCEPT_STORAGE_S3_REGION",
            "FINCEPT_STORAGE_S3_ACCESS_KEY",
            "FINCEPT_STORAGE_S3_SECRET_KEY",
            "FINCEPT_STORAGE_S3_BUCKET",
        ):
            monkeypatch.delenv(var, raising=False)
        cfg = StorageConfig()
        assert cfg.BACKEND == "local"
        assert cfg.LOCAL_BASE_DIR == "data"
        assert cfg.S3_REGION == "us-east-1"
        assert cfg.S3_ENDPOINT is None
        assert cfg.S3_ACCESS_KEY is None
        assert cfg.S3_SECRET_KEY is None
        assert cfg.S3_BUCKET is None

    def test_reads_s3_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FINCEPT_STORAGE_BACKEND", "s3")
        monkeypatch.setenv("FINCEPT_STORAGE_S3_ENDPOINT", "https://r2.example")
        monkeypatch.setenv("FINCEPT_STORAGE_S3_REGION", "eu-west-1")
        monkeypatch.setenv("FINCEPT_STORAGE_S3_ACCESS_KEY", "AKIAEXAMPLE")
        monkeypatch.setenv("FINCEPT_STORAGE_S3_SECRET_KEY", "secretexample")
        monkeypatch.setenv("FINCEPT_STORAGE_S3_BUCKET", "my-bucket")
        cfg = StorageConfig()
        assert cfg.BACKEND == "s3"
        assert cfg.S3_ENDPOINT == "https://r2.example"
        assert cfg.S3_REGION == "eu-west-1"
        assert cfg.S3_ACCESS_KEY == "AKIAEXAMPLE"
        assert cfg.S3_SECRET_KEY == "secretexample"
        assert cfg.S3_BUCKET == "my-bucket"


# ---------------------------------------------------------------------------
# get_storage_backend factory
# ===========================================================================


class TestGetStorageBackend:
    def test_returns_local_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clear_storage_backend_cache()
        monkeypatch.delenv("FINCEPT_STORAGE_BACKEND", raising=False)
        backend = get_storage_backend()
        assert isinstance(backend, LocalStorageBackend)

    def test_returns_local_when_env_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clear_storage_backend_cache()
        monkeypatch.setenv("FINCEPT_STORAGE_BACKEND", "local")
        backend = get_storage_backend()
        assert isinstance(backend, LocalStorageBackend)

    def test_returns_s3_when_env_s3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clear_storage_backend_cache()
        monkeypatch.setenv("FINCEPT_STORAGE_BACKEND", "s3")
        monkeypatch.setenv("FINCEPT_STORAGE_S3_BUCKET", "bkt")
        backend = get_storage_backend()
        assert isinstance(backend, S3StorageBackend)
        assert backend.bucket == "bkt"

    def test_caches_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clear_storage_backend_cache()
        monkeypatch.setenv("FINCEPT_STORAGE_BACKEND", "local")
        first = get_storage_backend()
        second = get_storage_backend()
        assert first is second

    def test_explicit_config_not_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clear_storage_backend_cache()
        monkeypatch.setenv("FINCEPT_STORAGE_BACKEND", "local")
        cfg = StorageConfig(BACKEND="local", LOCAL_BASE_DIR="other")
        backend = get_storage_backend(cfg)
        assert isinstance(backend, LocalStorageBackend)
        assert backend.base_dir == Path("other")

    def test_unknown_backend_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clear_storage_backend_cache()
        monkeypatch.setenv("FINCEPT_STORAGE_BACKEND", "gcs")
        with pytest.raises(StorageConfigError):
            get_storage_backend()


# ---------------------------------------------------------------------------
# resolve_uri
# ===========================================================================


class TestResolveUri:
    def test_local_bare_path_to_file_uri(self, tmp_path: Path) -> None:
        backend = LocalStorageBackend(base_dir=tmp_path)
        assert resolve_uri("rel/x.bin", backend).startswith("file://")

    def test_local_file_uri_passthrough(self, tmp_path: Path) -> None:
        backend = LocalStorageBackend(base_dir=tmp_path)
        uri = f"file://{(tmp_path / 'x.bin').as_posix()}"
        assert resolve_uri(uri, backend) == uri

    def test_s3_uri_validated(self) -> None:
        backend = S3StorageBackend(client=MagicMock())
        assert resolve_uri("s3://bkt/k.bin", backend) == "s3://bkt/k.bin"

    def test_s3_backend_rejects_file_uri(self) -> None:
        backend = S3StorageBackend(client=MagicMock())
        with pytest.raises(UnsupportedUriError):
            resolve_uri("file:///x.bin", backend)


# ---------------------------------------------------------------------------
# Integration: RealLightGBMTrainer + ModelLoader with LocalStorageBackend
# (backward compat — only runs when lightgbm is installed)
# ===========================================================================


_LIGHTGBM = pytest.importorskip("lightgbm")
_NUMPY = pytest.importorskip("numpy")


def _make_csv_dataset(tmp_path: Path, n: int = 300, seed: int = 7) -> Path:
    import numpy as np

    rng = np.random.RandomState(seed)
    ts = np.arange(n, dtype=np.int64)
    f1 = rng.randn(n)
    f2 = rng.randn(n)
    logit = 0.8 * f1 + 0.5 * f2 + 0.05 * rng.randn(n)
    label = (logit > 0).astype(float)
    data = np.column_stack([ts, f1, f2, label])
    path = tmp_path / "ds.csv"
    np.savetxt(str(path), data, delimiter=",", header="ts,f1,f2,label", comments="")
    return path


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
class TestTrainerIntegrationLocalStorage:
    def test_trainer_loads_dataset_via_local_backend(self, tmp_path: Path) -> None:
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.runpod_training import RunPodTrainingRequest

        ds_path = _make_csv_dataset(tmp_path)
        backend = LocalStorageBackend(base_dir=tmp_path)
        trainer = RealLightGBMTrainer(storage_backend=backend)
        ds_uri = ds_path.as_uri()
        resolved = trainer._resolve_path(ds_uri)
        assert resolved.resolve() == ds_path.resolve()

        req = RunPodTrainingRequest(
            job_id="job-1",
            dataset_manifest_ref=ds_uri,
            model_family="lightgbm",
            hardware_class="cpu",
            random_seed=0,
        )
        deadline = 30_000_000_000 + __import__("time").time_ns()
        artifact, dossier = trainer.train(req, deadline_ns=deadline)
        assert artifact.sha256 and len(artifact.sha256) == 64
        assert dossier.authority.value == "shadow-only"

    def test_trainer_s3_uri_without_backend_raises(self) -> None:
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.runpod_training import TrainingFailure

        trainer = RealLightGBMTrainer(storage_backend=None)
        with pytest.raises(TrainingFailure) as exc:
            trainer._resolve_path("s3://bkt/ds.csv")
        assert exc.value.error_code == "unsupported_uri"


class TestModelLoaderIntegrationLocalStorage:
    def test_model_loader_file_uri_backward_compat(self, tmp_path: Path) -> None:
        import pickle

        from quant_foundry.real_inference import ModelLoader

        booster = _LIGHTGBM.Booster(
            params={"objective": "binary", "verbose": -1},
            train_set=_LIGHTGBM.Dataset(_NUMPY.zeros((10, 2)), label=_NUMPY.zeros(10)),
        )
        model_path = tmp_path / "model.pkl"
        with open(model_path, "wb") as fh:
            pickle.dump(booster, fh)

        loader = ModelLoader(storage_backend=LocalStorageBackend(base_dir=tmp_path))
        scorer = loader.load(f"file://{model_path}")
        out = scorer.predict([[0.1, 0.2]])
        assert isinstance(out, list)

    def test_model_loader_s3_with_storage_backend(self, tmp_path: Path) -> None:
        import pickle

        from quant_foundry.real_inference import ModelLoader

        booster = _LIGHTGBM.Booster(
            params={"objective": "binary", "verbose": -1},
            train_set=_LIGHTGBM.Dataset(_NUMPY.zeros((10, 2)), label=_NUMPY.zeros(10)),
        )
        model_bytes = pickle.dumps(booster)

        client = _make_mock_s3_client()
        s3_backend = S3StorageBackend(client=client)
        s3_backend.write_bytes("s3://bkt/model.pkl", model_bytes)

        loader = ModelLoader(storage_backend=s3_backend)
        scorer = loader.load("s3://bkt/model.pkl")
        out = scorer.predict([[0.1, 0.2]])
        assert isinstance(out, list)
