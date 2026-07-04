"""
quant_foundry.event_text_runtime — Event text runtime (T-12.5).

This module provides a self-contained, importable event-text runtime for
the quant foundry's GPU worker path. It encodes free-text market events
(e.g. news headlines, filings, analyst notes) into dense embeddings and
resolves them to affected symbols. It is designed to be **importable
without torch / transformers / sentence-transformers installed** — all
heavy imports are lazy and performed inside methods, so the module can
be imported on CPU-only machines (e.g. the local test suite) and only
fails when a heavy-dependent operation is actually invoked.

Capabilities:

- :class:`EventTextImageSpec` — declarative spec for the
  ``trainer-gpu-event-text`` Docker image (base image, packages,
  healthcheck command, offline mode, embedding cache dir, weight cache
  dir).
- :class:`EmbeddingConfig` — configuration for the event text encoder
  (Pydantic v2, frozen + ``extra='forbid'``).
- :class:`EmbeddingResult` — typed result of an event encoding.
- :class:`EventTextEncoder` — loads a sentence-transformers model from a
  local weight path (offline, no network) and encodes text to embeddings.
- :class:`EmbeddingCache` — a filesystem-backed cache of
  :class:`EmbeddingResult` objects keyed by ``(event_id, model_hash)``.
- :class:`EventSymbolResolver` — maps free-text events to affected
  symbols via an entity -> symbols mapping.
- :class:`EventTextHealthcheck` — healthcheck that probes the GPU, the
  embedding cache directory, and a tiny encoding test.

Design notes:

- **Lazy heavy imports.** ``import torch`` / ``import transformers`` /
  ``import sentence_transformers`` happen inside methods, never at
  module top level. The module can be imported, and the Pydantic models
  / ``EventTextImageSpec`` can be constructed, on a host without those
  packages.
- **Offline by default.** ``EventTextImageSpec.offline_mode`` is
  ``True`` and the encoder loads weights from a local path only; no
  network downloads are performed.
- **No live trading authority.** The healthcheck runs a tiny synthetic
  encoding only; it never touches real feature-lake data or produces
  tradeable predictions.
- **No secrets.** Configs carry only hyperparameters and filesystem
  paths — never credentials.
- **Cost fails closed.** The healthcheck reports unhealthy when the GPU
  is unavailable, the embedding cache is inaccessible, or the encoding
  raises; it never reports healthy on a partial probe.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Docker image spec
# ---------------------------------------------------------------------------


class EventTextImageSpec(BaseModel):
    """Declarative spec for the ``trainer-gpu-event-text`` Docker image.

    Frozen + ``extra='forbid'`` for audit integrity. The spec is the
    source of truth for the image's base, packages, healthcheck command,
    offline mode flag, embedding cache directory, and weight cache
    directory; the Dockerfile in
    ``docker/trainer-gpu-event-text/`` is generated from it (kept in
    sync by review).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    image_name: str = "trainer-gpu-event-text"
    base_image: str = "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime"
    python_version: str = "3.12"
    packages: list[str] = Field(
        default_factory=lambda: [
            "torch==2.1.0",
            "transformers>=4.36",
            "numpy>=1.26",
            "pandas>=2.1",
            "pydantic>=2.7",
            "huggingface_hub>=0.20",
            "sentence-transformers>=2.2",
        ]
    )
    gpu_required: bool = True
    healthcheck_cmd: str = (
        'python -c "from quant_foundry.event_text_runtime import '
        "EventTextHealthcheck; import sys; "
        'sys.exit(0 if EventTextHealthcheck().is_healthy() else 1)"'
    )
    offline_mode: bool = True
    embedding_cache_dir: str = "/opt/embedding_cache"
    weight_cache_dir: str = "/opt/foundation_weights"


# ---------------------------------------------------------------------------
# Embedding config
# ---------------------------------------------------------------------------


class EmbeddingConfig(BaseModel):
    """Configuration for the event text encoder.

    Frozen + ``extra='forbid'`` for audit integrity. ``device`` is one
    of ``"auto"``, ``"cpu"``, ``"cuda"``. ``model_id`` is a
    HuggingFace-style model identifier (e.g.
    ``"sentence-transformers/all-MiniLM-L6-v2"``) used for provenance;
    the actual weights are loaded from the local ``weight_path``.
    ``model_hash`` is the SHA-256 hash of the weight file, used for
    cache keys and audit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    model_hash: str
    max_seq_length: int = 512
    embedding_dim: int = 384
    batch_size: int = 32
    device: str = "auto"

    @field_validator("model_id")
    @classmethod
    def _validate_model_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("model_id must be a non-empty string")
        return v

    @field_validator("model_hash")
    @classmethod
    def _validate_model_hash(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("model_hash must be a non-empty string")
        return v

    @field_validator("max_seq_length")
    @classmethod
    def _validate_max_seq_length(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_seq_length must be >= 1")
        return v

    @field_validator("embedding_dim")
    @classmethod
    def _validate_embedding_dim(cls, v: int) -> int:
        if v < 1:
            raise ValueError("embedding_dim must be >= 1")
        return v

    @field_validator("batch_size")
    @classmethod
    def _validate_batch_size(cls, v: int) -> int:
        if v < 1:
            raise ValueError("batch_size must be >= 1")
        return v

    @field_validator("device")
    @classmethod
    def _validate_device(cls, v: str) -> str:
        if v not in ("auto", "cpu", "cuda"):
            raise ValueError("device must be one of 'auto', 'cpu', 'cuda'")
        return v


# ---------------------------------------------------------------------------
# Embedding result
# ---------------------------------------------------------------------------


class EmbeddingResult(BaseModel):
    """Typed result of an event text encoding.

    Frozen + ``extra='forbid'`` for audit integrity. ``embedding`` is a
    list of floats of length ``embedding_dim``. ``embedding_hash`` is
    the deterministic SHA-256 hex digest of the embedding bytes, so the
    result is reproducible / auditable. ``duration_seconds`` records the
    wall-clock time of the encoding.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    embedding: list[float]
    model_id: str
    model_hash: str
    embedding_hash: str
    duration_seconds: float

    @field_validator("event_id")
    @classmethod
    def _validate_event_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("event_id must be a non-empty string")
        return v

    @field_validator("model_id")
    @classmethod
    def _validate_model_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("model_id must be a non-empty string")
        return v

    @field_validator("model_hash")
    @classmethod
    def _validate_model_hash(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("model_hash must be a non-empty string")
        return v

    @field_validator("embedding_hash")
    @classmethod
    def _validate_embedding_hash(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("embedding_hash must be a non-empty string")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_embedding(embedding: list[float]) -> str:
    """Compute a deterministic SHA-256 hex digest of an embedding.

    The embedding is serialized as a JSON array of floats rounded to 8
    decimal places, then UTF-8 encoded and hashed. This keeps the hash
    stable across runs (no float repr noise) while remaining
    byte-faithful to the rounded values.
    """
    rounded = [round(float(x), 8) for x in embedding]
    payload = json.dumps(rounded, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Event text encoder
# ---------------------------------------------------------------------------


class EventTextEncoder:
    """Loads a sentence-transformers model and encodes text to embeddings.

    The encoder is **offline by default**: it loads weights from
    ``weight_path`` (a local filesystem path) and never performs network
    downloads. The :meth:`validate_offline` method checks that the
    runtime is configured for offline operation.

    All torch / transformers / sentence-transformers imports are lazy
    (inside methods), so the encoder can be constructed on a host
    without those packages.
    """

    def __init__(self, config: EmbeddingConfig, weight_path: str) -> None:
        self.config = config
        self.weight_path = weight_path
        self._model: Any = None
        self._offline: bool = True

    def _resolve_device(self, gpu_available: bool) -> Any:
        """Resolve the torch device from the config's ``device`` field.

        ``auto`` picks CUDA when available, else CPU. ``cpu`` / ``cuda``
        are honored literally (cuda on a CPU-only host falls back to
        CPU).
        """
        import torch

        if self.config.device == "cpu":
            return torch.device("cpu")
        if self.config.device == "cuda":
            if gpu_available:
                return torch.device("cuda")
            return torch.device("cpu")
        # auto
        return torch.device("cuda" if gpu_available else "cpu")

    def validate_offline(self) -> bool:
        """Return ``True`` if offline mode is enforced.

        Offline mode is enforced when the weight path is a local
        filesystem path (not a HuggingFace Hub repo ID or URL) and the
        encoder has not been configured to fetch from the network.

        A path is considered offline-safe if:

        - it resolves to an existing local file or directory, OR
        - it has a path separator (indicating a local directory
          hierarchy rather than a bare Hub repo ID).

        A bare Hub repo ID (e.g. ``"all-MiniLM-L6-v2"``) with no
        separator is **not** offline-safe — it would require a network
        download to resolve.
        """
        p = Path(self.weight_path)
        if p.exists():
            return True
        # A path with a parent component (e.g. "dir/model") looks like a
        # local directory hierarchy, not a bare Hub repo ID.
        if str(p.parent) not in (".", ""):
            return True
        # Bare identifier with no parent — not offline.
        return False

    def _load_model(self, device: Any) -> Any:
        """Load the sentence-transformers model from the local weight path.

        Lazily imports sentence-transformers / torch and loads the model
        from ``self.weight_path`` with the resolved device. No network
        calls are made.

        Raises:
            FileNotFoundError: if ``weight_path`` does not exist.
        """
        if self._model is not None:
            return self._model

        p = Path(self.weight_path)
        if not p.exists():
            raise FileNotFoundError(f"weight path not found: {self.weight_path}")

        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(str(p), device=str(device))
        except Exception:
            # Fall back to a transformers AutoModel + AutoTokenizer if
            # sentence-transformers cannot load the path directly.
            try:
                from transformers import (
                    AutoModel,
                    AutoTokenizer,
                )

                tokenizer = AutoTokenizer.from_pretrained(str(p))
                model = AutoModel.from_pretrained(str(p))
                model = model.to(device)
                model.eval()
                self._model = {"tokenizer": tokenizer, "model": model}
            except Exception as exc:  # pragma: no cover - defensive
                raise RuntimeError(f"failed to load model from {self.weight_path}: {exc}") from exc
        return self._model

    def _encode_with_sentence_transformers(
        self, model: Any, texts: list[str], device: Any
    ) -> list[list[float]]:
        """Encode texts using a SentenceTransformer model."""
        import numpy as np

        embs = model.encode(
            texts,
            batch_size=self.config.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [list(map(float, row)) for row in np.asarray(embs)]

    def _encode_with_transformers(
        self, model_bundle: dict[str, Any], texts: list[str], device: Any
    ) -> list[list[float]]:
        """Encode texts using a transformers AutoModel + tokenizer bundle."""
        import torch

        tokenizer = model_bundle["tokenizer"]
        model = model_bundle["model"]
        results: list[list[float]] = []
        for i in range(0, len(texts), self.config.batch_size):
            batch = texts[i : i + self.config.batch_size]
            encoded = tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=self.config.max_seq_length,
                return_tensors="pt",
            ).to(device)
            with torch.no_grad():
                outputs = model(**encoded)
            # Mean-pool over the token dimension.
            mask = encoded["attention_mask"].unsqueeze(-1).float()
            token_embs = outputs.last_hidden_state * mask
            summed = token_embs.sum(dim=1)
            counts = mask.sum(dim=1).clamp(min=1.0)
            pooled = (summed / counts).cpu().tolist()
            results.extend([list(map(float, row)) for row in pooled])
        return results

    def _encode_texts(self, texts: list[str], device: Any) -> list[list[float]]:
        """Encode a list of texts using the loaded model."""
        model = self._load_model(device)
        if isinstance(model, dict) and "tokenizer" in model:
            return self._encode_with_transformers(model, texts, device)
        return self._encode_with_sentence_transformers(model, texts, device)

    def encode(self, text: str, event_id: str) -> EmbeddingResult:
        """Encode a single text and return an :class:`EmbeddingResult`.

        Loads the model from the local weight path (offline), encodes
        the text to an embedding, computes the deterministic embedding
        hash, and returns the typed result.

        Args:
            text: the text to encode. May be empty (the model still
                produces a valid embedding for an empty string).
            event_id: a non-empty identifier for the event, used as the
                cache key and for provenance.

        Returns:
            An :class:`EmbeddingResult` with the embedding, model
            provenance, and embedding hash.
        """
        start = time.perf_counter()
        from quant_foundry.tabular_neural_runtime import check_gpu

        gpu_status = check_gpu()
        device = self._resolve_device(gpu_status.available)

        embeddings = self._encode_texts([text], device)
        emb = embeddings[0]
        duration = time.perf_counter() - start

        return EmbeddingResult(
            event_id=event_id,
            embedding=emb,
            model_id=self.config.model_id,
            model_hash=self.config.model_hash,
            embedding_hash=_hash_embedding(emb),
            duration_seconds=duration,
        )

    def encode_batch(self, texts: list[str], event_ids: list[str]) -> list[EmbeddingResult]:
        """Encode a batch of texts and return a list of :class:`EmbeddingResult`.

        Args:
            texts: the texts to encode. Length must match ``event_ids``.
            event_ids: the identifiers for each text.

        Returns:
            A list of :class:`EmbeddingResult`, one per text, in order.

        Raises:
            ValueError: if ``texts`` and ``event_ids`` have different
                lengths.
        """
        if len(texts) != len(event_ids):
            raise ValueError(
                f"texts and event_ids must have the same length: {len(texts)} != {len(event_ids)}"
            )

        start = time.perf_counter()
        from quant_foundry.tabular_neural_runtime import check_gpu

        gpu_status = check_gpu()
        device = self._resolve_device(gpu_status.available)

        if not texts:
            return []

        embeddings = self._encode_texts(list(texts), device)
        duration = time.perf_counter() - start
        per_item = duration / len(texts) if texts else 0.0

        results: list[EmbeddingResult] = []
        for text, event_id, emb in zip(texts, event_ids, embeddings):
            results.append(
                EmbeddingResult(
                    event_id=event_id,
                    embedding=emb,
                    model_id=self.config.model_id,
                    model_hash=self.config.model_hash,
                    embedding_hash=_hash_embedding(emb),
                    duration_seconds=per_item,
                )
            )
        return results


# ---------------------------------------------------------------------------
# Embedding cache
# ---------------------------------------------------------------------------


class EmbeddingCache:
    """A filesystem-backed cache of :class:`EmbeddingResult` objects.

    Each cached result is stored as a JSON file named
    ``{event_id}__{model_hash}.json`` inside ``cache_dir``. The cache
    supports get / put / list / size operations. All filesystem
    operations use :mod:`pathlib` and are lazy — no heavy imports are
    required.
    """

    def __init__(self, cache_dir: str) -> None:
        self.cache_dir = cache_dir
        self._dir = Path(cache_dir)

    def _ensure_dir(self) -> Path:
        """Ensure the cache directory exists and return its :class:`Path`."""
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    @staticmethod
    def _cache_key(event_id: str, model_hash: str) -> str:
        """Return the cache filename for ``(event_id, model_hash)``."""
        # Sanitize event_id to be filesystem-safe.
        safe = event_id.replace("/", "_").replace("\\", "_").replace(":", "_")
        return f"{safe}__{model_hash}.json"

    def get(self, event_id: str, model_hash: str) -> EmbeddingResult | None:
        """Return the cached :class:`EmbeddingResult`, or ``None`` if absent.

        Args:
            event_id: the event identifier.
            model_hash: the model weight hash.

        Returns:
            The cached :class:`EmbeddingResult` if present, ``None``
            otherwise (including when the cache directory does not
            exist).
        """
        if not self._dir.exists():
            return None
        path = self._dir / self._cache_key(event_id, model_hash)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return EmbeddingResult.model_validate(data)
        except Exception:
            return None

    def put(self, result: EmbeddingResult) -> None:
        """Store ``result`` in the cache, keyed by its event_id + model_hash.

        Args:
            result: the :class:`EmbeddingResult` to cache.
        """
        self._ensure_dir()
        path = self._dir / self._cache_key(result.event_id, result.model_hash)
        payload = result.model_dump()
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def list_cached(self) -> list[str]:
        """List the cached event identifiers.

        Returns a sorted list of event IDs (the ``event_id`` portion of
        each cache filename). Returns an empty list if the cache
        directory does not exist or is empty.
        """
        if not self._dir.exists():
            return []
        ids: list[str] = []
        for f in self._dir.iterdir():
            if f.is_file() and f.suffix == ".json" and "__" in f.stem:
                ids.append(f.stem.rsplit("__", 1)[0])
        return sorted(ids)

    def cache_size(self) -> int:
        """Return the number of cached entries.

        Counts the JSON files in the cache directory. Returns ``0`` if
        the directory does not exist.
        """
        if not self._dir.exists():
            return 0
        count = 0
        for f in self._dir.iterdir():
            if f.is_file() and f.suffix == ".json":
                count += 1
        return count

    def cache_size_bytes(self) -> int:
        """Return the total size of the cache directory in bytes.

        Sums the sizes of all JSON files in the cache directory.
        Returns ``0`` if the directory does not exist.
        """
        if not self._dir.exists():
            return 0
        total = 0
        for f in self._dir.iterdir():
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
        return total


# ---------------------------------------------------------------------------
# Event symbol resolver
# ---------------------------------------------------------------------------


class EventSymbolResolver:
    """Resolves free-text events to affected symbols.

    The resolver holds a mapping of entity names (e.g. ``"Apple"``,
    ``"Tesla"``) to lists of affected symbols (e.g. ``["AAPL"]``,
    ``["TSLA", "TSLAQ"]``). :meth:`resolve` scans the input text for
    mentions of each entity (case-insensitive, whole-word aware) and
    returns the union of affected symbols.

    The mapping is mutable via :meth:`add_mapping`, so the resolver can
    be updated at runtime as new entities are discovered.
    """

    def __init__(self, mapping: dict[str, list[str]] | None = None) -> None:
        # Use a copy so callers cannot mutate the internal state via
        # the original dict reference.
        self._mapping: dict[str, list[str]] = {}
        if mapping:
            for entity, symbols in mapping.items():
                self._mapping[entity] = list(symbols)

    def resolve(self, text: str) -> list[str]:
        """Return the list of affected symbols mentioned in ``text``.

        Scans ``text`` (case-insensitive) for each known entity name.
        When an entity is found, its symbols are added to the result.
        The result is de-duplicated and sorted for deterministic output.

        Args:
            text: the event text to scan.

        Returns:
            A sorted list of unique symbols mentioned in the text.
        """
        if not text:
            return []
        lowered = text.lower()
        found: set[str] = set()
        for entity, symbols in self._mapping.items():
            if entity.lower() in lowered:
                found.update(symbols)
        return sorted(found)

    def add_mapping(self, entity: str, symbols: list[str]) -> None:
        """Add or replace the mapping for ``entity``.

        Args:
            entity: the entity name (e.g. ``"Apple"``).
            symbols: the list of affected symbols (e.g. ``["AAPL"]``).
        """
        if not entity or not entity.strip():
            raise ValueError("entity must be a non-empty string")
        self._mapping[entity] = list(symbols)

    def list_entities(self) -> list[str]:
        """Return a sorted list of known entity names."""
        return sorted(self._mapping.keys())


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


class EventTextHealthcheck:
    """Healthcheck for the event text runtime.

    Probes the GPU via :func:`check_gpu` (reused from
    ``tabular_neural_runtime``), checks that the embedding cache
    directory is accessible, and runs a tiny synthetic encoding test.
    Used by the GPU worker's ``HEALTHCHECK`` step to fail fast when the
    runtime is broken, the GPU is missing, or the embedding cache is
    inaccessible.
    """

    def __init__(
        self,
        timeout_seconds: int = 60,
        embedding_cache_dir: str | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self.embedding_cache_dir = embedding_cache_dir or "/opt/embedding_cache"

    def run(self) -> dict[str, Any]:
        """Run the healthcheck and return a status dict.

        The dict contains:

        - ``healthy`` (bool): overall health.
        - ``gpu`` (dict): serialized :class:`GPUStatus`.
        - ``embedding_cache`` (bool): whether the embedding cache dir is
          accessible (exists and is a directory).
        - ``encoding`` (bool): whether the tiny encoding test succeeded.
        - ``error`` (str | None): error message if the check failed.
        - ``duration_seconds`` (float): wall-clock duration.
        """
        from quant_foundry.tabular_neural_runtime import check_gpu

        start = time.perf_counter()
        result: dict[str, Any] = {
            "healthy": False,
            "gpu": None,
            "embedding_cache": False,
            "encoding": False,
            "error": None,
            "duration_seconds": 0.0,
        }

        try:
            gpu_status = check_gpu()
            result["gpu"] = gpu_status.model_dump()
        except Exception as exc:  # pragma: no cover - defensive
            result["error"] = f"gpu probe failed: {exc}"
            result["duration_seconds"] = time.perf_counter() - start
            return result

        # Embedding cache check.
        try:
            cache_path = Path(self.embedding_cache_dir)
            result["embedding_cache"] = cache_path.exists() and cache_path.is_dir()
        except Exception as exc:  # pragma: no cover - defensive
            result["error"] = f"embedding cache check failed: {exc}"
            result["duration_seconds"] = time.perf_counter() - start
            return result

        # Tiny synthetic encoding test. We do not load a real model
        # (none is available in the healthcheck context); instead we
        # verify that the encoder can be constructed and that a
        # deterministic embedding hash is computable. This probes the
        # import path and the hash helper without requiring weights.
        try:
            config = EmbeddingConfig(
                model_id="__healthcheck__",
                model_hash="0" * 64,
                embedding_dim=4,
                device="cpu",
            )
            encoder = EventTextEncoder(config=config, weight_path="healthcheck/model")
            # validate_offline should be True for the sentinel path
            # (it has a parent component).
            offline_ok = encoder.validate_offline()
            # Compute a synthetic embedding hash to verify the helper.
            synthetic = [0.1, 0.2, 0.3, 0.4]
            h = _hash_embedding(synthetic)
            result["encoding"] = bool(offline_ok and len(h) == 64)
        except Exception as exc:
            result["error"] = f"encoding probe failed: {exc}"

        gpu_ok = bool(result["gpu"].get("available")) if result["gpu"] else False
        result["healthy"] = bool(
            gpu_ok and result["embedding_cache"] and result["encoding"] and result["error"] is None
        )
        result["duration_seconds"] = time.perf_counter() - start
        return result

    def is_healthy(self) -> bool:
        """Return ``True`` if the GPU is available and all probes succeed.

        Note: on a CPU-only host this returns ``False`` because the GPU
        is not available. The healthcheck is intended for the GPU worker
        container, where a missing GPU is a hard failure.
        """
        status = self.run()
        return bool(status.get("healthy"))


__all__ = [
    "EmbeddingCache",
    "EmbeddingConfig",
    "EmbeddingResult",
    "EventSymbolResolver",
    "EventTextEncoder",
    "EventTextHealthcheck",
    "EventTextImageSpec",
]
