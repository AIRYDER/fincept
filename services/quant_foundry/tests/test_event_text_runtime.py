"""Tests for quant_foundry.event_text_runtime (T-12.5).

Covers the event text runtime: the Docker image spec, embedding config,
embedding result, the event text encoder, the embedding cache, the
event symbol resolver, and the event text healthcheck.

The test host is CPU-only (torch is installed with the CPU index URL), so
GPU-dependent assertions check the "no GPU" degradation path. The
embedding cache tests use synthetic temp directories. The encoder tests
mock the heavy model so no actual model is downloaded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("torch")

from quant_foundry.event_text_runtime import (
    EmbeddingCache,
    EmbeddingConfig,
    EmbeddingResult,
    EventSymbolResolver,
    EventTextEncoder,
    EventTextHealthcheck,
    EventTextImageSpec,
    _hash_embedding,
)

# ---------------------------------------------------------------------------
# EventTextImageSpec
# ---------------------------------------------------------------------------


class TestEventTextImageSpec:
    def test_defaults(self) -> None:
        spec = EventTextImageSpec()
        assert spec.image_name == "trainer-gpu-event-text"
        assert spec.base_image == "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime"
        assert spec.python_version == "3.12"
        assert spec.gpu_required is True
        assert spec.offline_mode is True
        assert spec.embedding_cache_dir == "/opt/embedding_cache"
        assert spec.weight_cache_dir == "/opt/foundation_weights"

    def test_default_packages(self) -> None:
        spec = EventTextImageSpec()
        assert "torch==2.1.0" in spec.packages
        assert "transformers>=4.36" in spec.packages
        assert "numpy>=1.26" in spec.packages
        assert "pandas>=2.1" in spec.packages
        assert "pydantic>=2.7" in spec.packages
        assert "huggingface_hub>=0.20" in spec.packages
        assert "sentence-transformers>=2.2" in spec.packages

    def test_healthcheck_cmd_references_event_text_runtime(self) -> None:
        spec = EventTextImageSpec()
        assert "EventTextHealthcheck" in spec.healthcheck_cmd
        assert "event_text_runtime" in spec.healthcheck_cmd

    def test_frozen(self) -> None:
        spec = EventTextImageSpec()
        with pytest.raises(Exception):
            spec.image_name = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            EventTextImageSpec(unexpected="x")  # type: ignore[call-arg]

    def test_custom_construction(self) -> None:
        spec = EventTextImageSpec(
            image_name="custom-event-text",
            base_image="pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime",
            python_version="3.11",
            packages=["torch==2.2.0", "numpy"],
            gpu_required=False,
            offline_mode=False,
            embedding_cache_dir="/data/emb_cache",
            weight_cache_dir="/data/weights",
        )
        assert spec.image_name == "custom-event-text"
        assert spec.gpu_required is False
        assert spec.offline_mode is False
        assert spec.embedding_cache_dir == "/data/emb_cache"
        assert spec.weight_cache_dir == "/data/weights"


# ---------------------------------------------------------------------------
# EmbeddingConfig
# ---------------------------------------------------------------------------


class TestEmbeddingConfig:
    def test_defaults(self) -> None:
        cfg = EmbeddingConfig(
            model_id="sentence-transformers/all-MiniLM-L6-v2",
            model_hash="abc123",
        )
        assert cfg.model_id == "sentence-transformers/all-MiniLM-L6-v2"
        assert cfg.model_hash == "abc123"
        assert cfg.max_seq_length == 512
        assert cfg.embedding_dim == 384
        assert cfg.batch_size == 32
        assert cfg.device == "auto"

    def test_frozen(self) -> None:
        cfg = EmbeddingConfig(model_id="m", model_hash="h")
        with pytest.raises(Exception):
            cfg.model_id = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            EmbeddingConfig(model_id="m", model_hash="h", unexpected=1)  # type: ignore[call-arg]

    def test_model_id_empty(self) -> None:
        with pytest.raises(Exception):
            EmbeddingConfig(model_id="", model_hash="h")

    def test_model_id_whitespace_only(self) -> None:
        with pytest.raises(Exception):
            EmbeddingConfig(model_id="   ", model_hash="h")

    def test_model_hash_empty(self) -> None:
        with pytest.raises(Exception):
            EmbeddingConfig(model_id="m", model_hash="")

    def test_model_hash_whitespace_only(self) -> None:
        with pytest.raises(Exception):
            EmbeddingConfig(model_id="m", model_hash="   ")

    def test_max_seq_length_minimum(self) -> None:
        with pytest.raises(Exception):
            EmbeddingConfig(model_id="m", model_hash="h", max_seq_length=0)

    def test_max_seq_length_valid(self) -> None:
        cfg = EmbeddingConfig(model_id="m", model_hash="h", max_seq_length=1)
        assert cfg.max_seq_length == 1

    def test_embedding_dim_minimum(self) -> None:
        with pytest.raises(Exception):
            EmbeddingConfig(model_id="m", model_hash="h", embedding_dim=0)

    def test_embedding_dim_valid(self) -> None:
        cfg = EmbeddingConfig(model_id="m", model_hash="h", embedding_dim=1)
        assert cfg.embedding_dim == 1

    def test_batch_size_minimum(self) -> None:
        with pytest.raises(Exception):
            EmbeddingConfig(model_id="m", model_hash="h", batch_size=0)

    def test_batch_size_valid(self) -> None:
        cfg = EmbeddingConfig(model_id="m", model_hash="h", batch_size=1)
        assert cfg.batch_size == 1

    def test_device_invalid(self) -> None:
        with pytest.raises(Exception):
            EmbeddingConfig(model_id="m", model_hash="h", device="tpu")

    def test_device_valid_cpu(self) -> None:
        cfg = EmbeddingConfig(model_id="m", model_hash="h", device="cpu")
        assert cfg.device == "cpu"

    def test_device_valid_cuda(self) -> None:
        cfg = EmbeddingConfig(model_id="m", model_hash="h", device="cuda")
        assert cfg.device == "cuda"


# ---------------------------------------------------------------------------
# EmbeddingResult
# ---------------------------------------------------------------------------


class TestEmbeddingResult:
    def _make_result(self) -> EmbeddingResult:
        return EmbeddingResult(
            event_id="evt-001",
            embedding=[0.1, 0.2, 0.3, 0.4],
            model_id="sentence-transformers/all-MiniLM-L6-v2",
            model_hash="abc123",
            embedding_hash=_hash_embedding([0.1, 0.2, 0.3, 0.4]),
            duration_seconds=0.05,
        )

    def test_construction(self) -> None:
        r = self._make_result()
        assert r.event_id == "evt-001"
        assert r.embedding == [0.1, 0.2, 0.3, 0.4]
        assert r.model_id == "sentence-transformers/all-MiniLM-L6-v2"
        assert r.model_hash == "abc123"
        assert len(r.embedding_hash) == 64
        assert r.duration_seconds == 0.05

    def test_frozen(self) -> None:
        r = self._make_result()
        with pytest.raises(Exception):
            r.event_id = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            EmbeddingResult(
                event_id="e",
                embedding=[0.1],
                model_id="m",
                model_hash="h",
                embedding_hash="x",
                duration_seconds=0.0,
                unexpected=1,  # type: ignore[call-arg]
            )

    def test_event_id_empty_rejected(self) -> None:
        with pytest.raises(Exception):
            EmbeddingResult(
                event_id="",
                embedding=[0.1],
                model_id="m",
                model_hash="h",
                embedding_hash="x",
                duration_seconds=0.0,
            )

    def test_model_id_empty_rejected(self) -> None:
        with pytest.raises(Exception):
            EmbeddingResult(
                event_id="e",
                embedding=[0.1],
                model_id="",
                model_hash="h",
                embedding_hash="x",
                duration_seconds=0.0,
            )

    def test_embedding_hash_deterministic(self) -> None:
        emb = [0.1, 0.2, 0.3]
        h1 = _hash_embedding(emb)
        h2 = _hash_embedding(emb)
        assert h1 == h2
        assert len(h1) == 64

    def test_embedding_hash_differs_for_different_embeddings(self) -> None:
        h1 = _hash_embedding([0.1, 0.2])
        h2 = _hash_embedding([0.1, 0.3])
        assert h1 != h2

    def test_embedding_hash_rounds_floats(self) -> None:
        # Rounding to 8 decimals should make these equal.
        h1 = _hash_embedding([0.100000001, 0.2])
        h2 = _hash_embedding([0.1, 0.2])
        assert h1 == h2


# ---------------------------------------------------------------------------
# EventTextEncoder
# ---------------------------------------------------------------------------


def _make_weight_dir(tmp_path: Path) -> tuple[str, str]:
    """Create a synthetic weight directory and return (path, hash)."""
    weight_dir = tmp_path / "model_weights"
    weight_dir.mkdir(parents=True, exist_ok=True)
    # Write a dummy config so the directory looks like a model dir.
    (weight_dir / "config.json").write_text("{}")
    # Hash the directory contents deterministically.
    h = hashlib.sha256()
    for f in sorted(weight_dir.iterdir()):
        h.update(f.name.encode("utf-8"))
        h.update(f.read_bytes())
    return str(weight_dir), h.hexdigest()


class TestEventTextEncoder:
    def test_validate_offline_local_path(self, tmp_path: Path) -> None:
        path, _ = _make_weight_dir(tmp_path)
        cfg = EmbeddingConfig(model_id="m", model_hash="h")
        encoder = EventTextEncoder(config=cfg, weight_path=path)
        assert encoder.validate_offline() is True

    def test_validate_offline_nonexistent_with_parent(self, tmp_path: Path) -> None:
        cfg = EmbeddingConfig(model_id="m", model_hash="h")
        encoder = EventTextEncoder(
            config=cfg,
            weight_path=str(tmp_path / "missing" / "model"),
        )
        # A path with a parent component is treated as offline.
        assert encoder.validate_offline() is True

    def test_validate_offline_hub_id_no_parent(self) -> None:
        cfg = EmbeddingConfig(model_id="m", model_hash="h")
        encoder = EventTextEncoder(config=cfg, weight_path="all-MiniLM-L6-v2")
        # A bare Hub repo ID (no parent) is not offline.
        assert encoder.validate_offline() is False

    def test_resolve_device_cpu(self) -> None:
        cfg = EmbeddingConfig(model_id="m", model_hash="h", device="cpu")
        encoder = EventTextEncoder(config=cfg, weight_path="dir/model")
        import torch

        dev = encoder._resolve_device(gpu_available=False)
        assert dev == torch.device("cpu")

    def test_resolve_device_auto_cpu_host(self) -> None:
        cfg = EmbeddingConfig(model_id="m", model_hash="h", device="auto")
        encoder = EventTextEncoder(config=cfg, weight_path="dir/model")
        import torch

        dev = encoder._resolve_device(gpu_available=False)
        assert dev == torch.device("cpu")

    def test_resolve_device_cuda_falls_back_on_cpu_host(self) -> None:
        cfg = EmbeddingConfig(model_id="m", model_hash="h", device="cuda")
        encoder = EventTextEncoder(config=cfg, weight_path="dir/model")
        import torch

        dev = encoder._resolve_device(gpu_available=False)
        assert dev == torch.device("cpu")

    def test_encode_with_mocked_model(self, tmp_path: Path) -> None:
        path, whash = _make_weight_dir(tmp_path)
        cfg = EmbeddingConfig(
            model_id="sentence-transformers/all-MiniLM-L6-v2",
            model_hash=whash,
            embedding_dim=4,
            device="cpu",
        )
        encoder = EventTextEncoder(config=cfg, weight_path=path)

        # Mock the SentenceTransformer so no real model is loaded.
        mock_model = MagicMock()
        mock_model.encode.return_value = [[0.1, 0.2, 0.3, 0.4]]
        with patch(
            "quant_foundry.event_text_runtime.SentenceTransformer",
            create=True,
        ):
            # Patch the import inside _load_model by pre-seeding the model.
            encoder._model = mock_model
            result = encoder.encode("Apple announces new product", "evt-001")

        assert isinstance(result, EmbeddingResult)
        assert result.event_id == "evt-001"
        assert result.embedding == [0.1, 0.2, 0.3, 0.4]
        assert result.model_id == "sentence-transformers/all-MiniLM-L6-v2"
        assert result.model_hash == whash
        assert len(result.embedding_hash) == 64
        assert result.duration_seconds >= 0.0
        mock_model.encode.assert_called_once()

    def test_encode_batch_with_mocked_model(self, tmp_path: Path) -> None:
        path, whash = _make_weight_dir(tmp_path)
        cfg = EmbeddingConfig(
            model_id="m",
            model_hash=whash,
            embedding_dim=3,
            batch_size=2,
            device="cpu",
        )
        encoder = EventTextEncoder(config=cfg, weight_path=path)

        mock_model = MagicMock()
        mock_model.encode.return_value = [
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
        ]
        encoder._model = mock_model
        results = encoder.encode_batch(["text one", "text two"], ["evt-1", "evt-2"])

        assert len(results) == 2
        assert results[0].event_id == "evt-1"
        assert results[0].embedding == [0.1, 0.2, 0.3]
        assert results[1].event_id == "evt-2"
        assert results[1].embedding == [0.4, 0.5, 0.6]
        assert results[0].embedding_hash != results[1].embedding_hash

    def test_encode_batch_empty(self, tmp_path: Path) -> None:
        path, whash = _make_weight_dir(tmp_path)
        cfg = EmbeddingConfig(model_id="m", model_hash=whash, device="cpu")
        encoder = EventTextEncoder(config=cfg, weight_path=path)
        results = encoder.encode_batch([], [])
        assert results == []

    def test_encode_batch_length_mismatch(self, tmp_path: Path) -> None:
        path, whash = _make_weight_dir(tmp_path)
        cfg = EmbeddingConfig(model_id="m", model_hash=whash, device="cpu")
        encoder = EventTextEncoder(config=cfg, weight_path=path)
        with pytest.raises(ValueError):
            encoder.encode_batch(["a", "b"], ["evt-1"])

    def test_encode_missing_weight_raises(self, tmp_path: Path) -> None:
        cfg = EmbeddingConfig(model_id="m", model_hash="h", device="cpu", embedding_dim=4)
        encoder = EventTextEncoder(
            config=cfg,
            weight_path=str(tmp_path / "nonexistent" / "model"),
        )
        with pytest.raises(FileNotFoundError):
            encoder.encode("text", "evt-1")

    def test_encode_empty_text_with_mock(self, tmp_path: Path) -> None:
        path, whash = _make_weight_dir(tmp_path)
        cfg = EmbeddingConfig(model_id="m", model_hash=whash, embedding_dim=3, device="cpu")
        encoder = EventTextEncoder(config=cfg, weight_path=path)
        mock_model = MagicMock()
        mock_model.encode.return_value = [[0.0, 0.0, 0.0]]
        encoder._model = mock_model
        result = encoder.encode("", "evt-empty")
        assert result.event_id == "evt-empty"
        assert result.embedding == [0.0, 0.0, 0.0]
        assert len(result.embedding_hash) == 64

    def test_encode_batch_reproducible_with_mock(self, tmp_path: Path) -> None:
        path, whash = _make_weight_dir(tmp_path)
        cfg = EmbeddingConfig(model_id="m", model_hash=whash, embedding_dim=2, device="cpu")
        mock_model = MagicMock()
        mock_model.encode.return_value = [[0.1, 0.2], [0.3, 0.4]]

        encoder1 = EventTextEncoder(config=cfg, weight_path=path)
        encoder1._model = mock_model
        r1 = encoder1.encode_batch(["a", "b"], ["e1", "e2"])

        encoder2 = EventTextEncoder(config=cfg, weight_path=path)
        encoder2._model = mock_model
        r2 = encoder2.encode_batch(["a", "b"], ["e1", "e2"])

        assert r1[0].embedding_hash == r2[0].embedding_hash
        assert r1[1].embedding_hash == r2[1].embedding_hash


# ---------------------------------------------------------------------------
# EmbeddingCache
# ---------------------------------------------------------------------------


def _make_result(event_id: str = "evt-001", model_hash: str = "abc123") -> EmbeddingResult:
    """Build a synthetic EmbeddingResult for cache tests."""
    emb = [0.1, 0.2, 0.3, 0.4]
    return EmbeddingResult(
        event_id=event_id,
        embedding=emb,
        model_id="sentence-transformers/all-MiniLM-L6-v2",
        model_hash=model_hash,
        embedding_hash=_hash_embedding(emb),
        duration_seconds=0.01,
    )


class TestEmbeddingCache:
    def test_get_from_empty_cache(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "cache"))
        assert cache.get("evt-1", "hash") is None

    def test_get_from_nonexistent_dir(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "nope"))
        assert cache.get("evt-1", "hash") is None

    def test_put_and_get(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "cache"))
        result = _make_result()
        cache.put(result)
        retrieved = cache.get("evt-001", "abc123")
        assert retrieved is not None
        assert retrieved.event_id == "evt-001"
        assert retrieved.embedding == [0.1, 0.2, 0.3, 0.4]
        assert retrieved.model_hash == "abc123"
        assert retrieved.embedding_hash == result.embedding_hash

    def test_put_creates_directory(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "new_cache"
        assert not cache_dir.exists()
        cache = EmbeddingCache(str(cache_dir))
        cache.put(_make_result())
        assert cache_dir.exists()

    def test_get_missing_key(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "cache"))
        cache.put(_make_result(event_id="evt-001"))
        assert cache.get("evt-999", "abc123") is None

    def test_get_wrong_model_hash(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "cache"))
        cache.put(_make_result(model_hash="hash_a"))
        assert cache.get("evt-001", "hash_b") is None

    def test_list_cached_empty(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "cache"))
        assert cache.list_cached() == []

    def test_list_cached_nonexistent_dir(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "nope"))
        assert cache.list_cached() == []

    def test_list_cached_after_puts(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "cache"))
        cache.put(_make_result(event_id="evt-002", model_hash="h1"))
        cache.put(_make_result(event_id="evt-001", model_hash="h2"))
        ids = cache.list_cached()
        assert ids == ["evt-001", "evt-002"]

    def test_cache_size_empty(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "cache"))
        assert cache.cache_size() == 0

    def test_cache_size_nonexistent_dir(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "nope"))
        assert cache.cache_size() == 0

    def test_cache_size_after_puts(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "cache"))
        cache.put(_make_result(event_id="evt-001"))
        cache.put(_make_result(event_id="evt-002"))
        assert cache.cache_size() == 2

    def test_cache_size_bytes_positive(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "cache"))
        cache.put(_make_result())
        assert cache.cache_size_bytes() > 0

    def test_cache_size_bytes_empty(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "cache"))
        assert cache.cache_size_bytes() == 0

    def test_put_overwrites_same_key(self, tmp_path: Path) -> None:
        cache = EmbeddingCache(str(tmp_path / "cache"))
        cache.put(_make_result(event_id="evt-001", model_hash="h"))
        cache.put(
            EmbeddingResult(
                event_id="evt-001",
                embedding=[0.9, 0.8, 0.7, 0.6],
                model_id="m",
                model_hash="h",
                embedding_hash=_hash_embedding([0.9, 0.8, 0.7, 0.6]),
                duration_seconds=0.02,
            )
        )
        assert cache.cache_size() == 1
        retrieved = cache.get("evt-001", "h")
        assert retrieved is not None
        assert retrieved.embedding == [0.9, 0.8, 0.7, 0.6]

    def test_get_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        # Write a corrupt JSON file matching the key pattern.
        (cache_dir / "evt-001__abc123.json").write_text("not json")
        cache = EmbeddingCache(str(cache_dir))
        assert cache.get("evt-001", "abc123") is None


# ---------------------------------------------------------------------------
# EventSymbolResolver
# ---------------------------------------------------------------------------


class TestEventSymbolResolver:
    def test_resolve_empty_mapping(self) -> None:
        resolver = EventSymbolResolver()
        assert resolver.resolve("Apple announces new product") == []

    def test_resolve_no_match(self) -> None:
        resolver = EventSymbolResolver({"Apple": ["AAPL"]})
        assert resolver.resolve("Tesla announces new product") == []

    def test_resolve_single_match(self) -> None:
        resolver = EventSymbolResolver({"Apple": ["AAPL"]})
        assert resolver.resolve("Apple announces new product") == ["AAPL"]

    def test_resolve_case_insensitive(self) -> None:
        resolver = EventSymbolResolver({"Apple": ["AAPL"]})
        assert resolver.resolve("apple announces new product") == ["AAPL"]
        assert resolver.resolve("APPLE announces new product") == ["AAPL"]

    def test_resolve_multiple_entities(self) -> None:
        resolver = EventSymbolResolver({"Apple": ["AAPL"], "Tesla": ["TSLA"]})
        result = resolver.resolve("Apple and Tesla both announce earnings")
        assert result == ["AAPL", "TSLA"]

    def test_resolve_deduplicates_symbols(self) -> None:
        resolver = EventSymbolResolver({"Apple": ["AAPL", "AAPL"], "iPhone": ["AAPL"]})
        result = resolver.resolve("Apple iPhone launch")
        assert result == ["AAPL"]

    def test_resolve_empty_text(self) -> None:
        resolver = EventSymbolResolver({"Apple": ["AAPL"]})
        assert resolver.resolve("") == []

    def test_resolve_sorted_output(self) -> None:
        resolver = EventSymbolResolver({"Zebra": ["ZSYM"], "Apple": ["AAPL"]})
        result = resolver.resolve("Zebra and Apple")
        assert result == ["AAPL", "ZSYM"]

    def test_add_mapping(self) -> None:
        resolver = EventSymbolResolver()
        resolver.add_mapping("Microsoft", ["MSFT"])
        assert resolver.resolve("Microsoft launches Windows") == ["MSFT"]

    def test_add_mapping_overwrites(self) -> None:
        resolver = EventSymbolResolver({"Apple": ["AAPL"]})
        resolver.add_mapping("Apple", ["AAPL", "APLE"])
        result = resolver.resolve("Apple news")
        assert result == ["AAPL", "APLE"]

    def test_add_mapping_empty_entity_rejected(self) -> None:
        resolver = EventSymbolResolver()
        with pytest.raises(ValueError):
            resolver.add_mapping("", ["SYM"])
        with pytest.raises(ValueError):
            resolver.add_mapping("   ", ["SYM"])

    def test_list_entities_empty(self) -> None:
        resolver = EventSymbolResolver()
        assert resolver.list_entities() == []

    def test_list_entities_sorted(self) -> None:
        resolver = EventSymbolResolver({"Zebra": ["Z"], "Apple": ["A"], "Mango": ["M"]})
        assert resolver.list_entities() == ["Apple", "Mango", "Zebra"]

    def test_init_with_mapping_copy(self) -> None:
        original = {"Apple": ["AAPL"]}
        resolver = EventSymbolResolver(original)
        original["Apple"].append("HACK")
        # The resolver's internal mapping should not be affected.
        assert resolver.resolve("Apple news") == ["AAPL"]


# ---------------------------------------------------------------------------
# EventTextHealthcheck
# ---------------------------------------------------------------------------


class TestEventTextHealthcheck:
    def test_init_default(self) -> None:
        hc = EventTextHealthcheck()
        assert hc.timeout_seconds == 60
        assert hc.embedding_cache_dir == "/opt/embedding_cache"

    def test_init_custom(self, tmp_path: Path) -> None:
        hc = EventTextHealthcheck(timeout_seconds=30, embedding_cache_dir=str(tmp_path))
        assert hc.timeout_seconds == 30
        assert hc.embedding_cache_dir == str(tmp_path)

    def test_init_invalid_timeout(self) -> None:
        with pytest.raises(ValueError):
            EventTextHealthcheck(timeout_seconds=0)
        with pytest.raises(ValueError):
            EventTextHealthcheck(timeout_seconds=-1)

    def test_run_returns_dict(self, tmp_path: Path) -> None:
        hc = EventTextHealthcheck(embedding_cache_dir=str(tmp_path))
        status = hc.run()
        assert isinstance(status, dict)
        assert "healthy" in status
        assert "gpu" in status
        assert "embedding_cache" in status
        assert "encoding" in status
        assert "error" in status
        assert "duration_seconds" in status

    def test_run_embedding_cache_accessible(self, tmp_path: Path) -> None:
        hc = EventTextHealthcheck(embedding_cache_dir=str(tmp_path))
        status = hc.run()
        assert status["embedding_cache"] is True

    def test_run_embedding_cache_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist"
        hc = EventTextHealthcheck(embedding_cache_dir=str(missing))
        status = hc.run()
        assert status["embedding_cache"] is False
        assert status["healthy"] is False

    def test_run_encoding_succeeds_on_cpu(self, tmp_path: Path) -> None:
        # On a CPU-only host the encoding probe still runs (it uses a
        # self-contained hash helper), but healthy is False because the
        # GPU is unavailable.
        hc = EventTextHealthcheck(embedding_cache_dir=str(tmp_path))
        status = hc.run()
        assert status["encoding"] is True

    def test_is_healthy_false_on_cpu_host(self, tmp_path: Path) -> None:
        hc = EventTextHealthcheck(embedding_cache_dir=str(tmp_path))
        # CPU-only host -> GPU not available -> not healthy.
        assert hc.is_healthy() is False

    def test_run_gpu_status_present(self, tmp_path: Path) -> None:
        hc = EventTextHealthcheck(embedding_cache_dir=str(tmp_path))
        status = hc.run()
        assert status["gpu"] is not None
        assert "available" in status["gpu"]

    def test_run_duration_positive(self, tmp_path: Path) -> None:
        hc = EventTextHealthcheck(embedding_cache_dir=str(tmp_path))
        status = hc.run()
        assert status["duration_seconds"] >= 0.0

    def test_run_healthy_false_when_cache_missing(self, tmp_path: Path) -> None:
        # Embedding cache missing -> healthy False even if encoding works.
        hc = EventTextHealthcheck(embedding_cache_dir=str(tmp_path / "nope"))
        status = hc.run()
        assert status["healthy"] is False

    def test_run_error_none_when_probes_succeed(self, tmp_path: Path) -> None:
        hc = EventTextHealthcheck(embedding_cache_dir=str(tmp_path))
        status = hc.run()
        # On CPU host, probes run but healthy is False due to GPU.
        # error should be None because no probe raised.
        assert status["error"] is None
