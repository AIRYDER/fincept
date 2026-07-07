"""Checkpoint/resume for walk-forward training jobs (Tier 2.7).

This module provides checkpoint management for the main LightGBM/XGBoost
walk-forward trainer. The existing ``sequence_runtime.CheckpointManager``
handles per-epoch PyTorch checkpoints; this module handles **per-fold**
checkpoints for the walk-forward cross-validation loop.

The checkpoint strategy:

  1. After each walk-forward fold completes, the trainer saves a
     checkpoint containing the fold's model, fold metrics, and fold
     index.
  2. If the worker is preempted (spot fleet), the dispatcher re-queues
     the job with the same ``job_id``.
  3. On resume, the handler checks for existing checkpoints in the
     checkpoint directory (on the RunPod network volume). If found,
     training resumes from the last completed fold — previously
     completed folds are not retrained.
  4. Old checkpoints are cleaned up (keep last N).

The checkpoint is a pickle file (not torch.save) because LightGBM
models are pickled, not torch tensors. Each checkpoint contains:

  - ``job_id``: the training job ID (for idempotency verification).
  - ``fold_index``: the 0-based fold index that was completed.
  - ``fold_model``: the pickled LightGBM model for this fold.
  - ``fold_metrics``: the metrics dict for this fold.
  - ``timestamp``: ISO timestamp of the checkpoint.

Design:
  * Pure-Python, no torch dependency (uses pickle + pathlib).
  * The checkpoint directory is expected to be on a RunPod network
    volume (``/runpod-volume/checkpoints/{job_id}/``).
  * The ``job_id`` is verified on resume to prevent cross-job
    checkpoint confusion.
  * Pydantic v2 config model, frozen + ``extra='forbid'``.
"""

from __future__ import annotations

import os
import pickle
import re
import time
from datetime import datetime, UTC
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "TrainingCheckpointConfig",
    "TrainingCheckpointManager",
    "CheckpointData",
    "CheckpointError",
]


# --------------------------------------------------------------------------- #
# Configuration                                                                #
# --------------------------------------------------------------------------- #


class TrainingCheckpointConfig(BaseModel):
    """Configuration for walk-forward training checkpoint/resume.

    Frozen + ``extra='forbid'`` for audit integrity. The
    ``checkpoint_dir`` is the directory where checkpoints are written
    (expected to be on a RunPod network volume for durability across
    preemption). The ``job_id`` is used to namespace checkpoints and
    verify idempotency on resume.

    Fields:
        checkpoint_dir: base directory for checkpoints.
        job_id: the training job ID (for idempotency verification).
        max_checkpoints: maximum number of checkpoint files to keep.
            Older checkpoints are removed by :meth:`cleanup`.
        resume_from_checkpoint: explicit path to resume from. If None,
            the latest checkpoint in ``checkpoint_dir`` is used.
        save_every_n_folds: save a checkpoint after every N folds.
            Default 1 (save after every fold).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_dir: str
    job_id: str
    max_checkpoints: int = 3
    resume_from_checkpoint: str | None = None
    save_every_n_folds: int = 1

    @field_validator("job_id")
    @classmethod
    def _job_id_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("job_id must be non-empty")
        return v

    @field_validator("max_checkpoints")
    @classmethod
    def _max_checkpoints_ge1(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_checkpoints must be >= 1")
        return v

    @field_validator("save_every_n_folds")
    @classmethod
    def _save_every_ge1(cls, v: int) -> int:
        if v < 1:
            raise ValueError("save_every_n_folds must be >= 1")
        return v

    @property
    def job_checkpoint_dir(self) -> Path:
        """The per-job checkpoint subdirectory."""
        safe_job = re.sub(r"[^a-zA-Z0-9_\-]", "-", self.job_id)
        return Path(self.checkpoint_dir) / safe_job


# --------------------------------------------------------------------------- #
# Checkpoint data                                                              #
# --------------------------------------------------------------------------- #


class CheckpointData(BaseModel):
    """The data stored in a single checkpoint.

    Frozen + ``extra='forbid'`` for audit integrity. The ``fold_model``
    is stored as bytes (pickled LightGBM model) — the checkpoint file
    on disk is a pickle of this dict.

    Fields:
        job_id: the training job ID (for idempotency verification).
        fold_index: the 0-based fold index that was completed.
        fold_model: the pickled model bytes for this fold.
        fold_metrics: the metrics dict for this fold.
        timestamp: ISO timestamp of the checkpoint.
        total_folds: the total number of folds expected (0 if unknown).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    fold_index: int
    fold_model: bytes
    fold_metrics: dict[str, float] = Field(default_factory=dict)
    timestamp: str
    total_folds: int = 0


class CheckpointError(Exception):
    """Raised when a checkpoint operation fails."""


# --------------------------------------------------------------------------- #
# Checkpoint manager                                                           #
# --------------------------------------------------------------------------- #


class TrainingCheckpointManager:
    """Manages per-fold checkpoints for walk-forward training.

    Checkpoints are written to ``{checkpoint_dir}/{job_id}/`` as
    ``checkpoint_fold_{n}.pkl`` files. Each checkpoint is a pickled
    :class:`CheckpointData` dict.

    On resume, :meth:`latest_checkpoint` returns the path to the
    highest fold index checkpoint, and :meth:`load` returns its data.
    The handler can then skip already-completed folds and resume
    training from the next fold.
    """

    def __init__(self, config: TrainingCheckpointConfig) -> None:
        self.config = config
        self._dir = config.job_checkpoint_dir

    def _checkpoint_path(self, fold_index: int) -> Path:
        """Return the path for a given fold's checkpoint file."""
        return self._dir / f"checkpoint_fold_{fold_index}.pkl"

    def should_save(self, fold_index: int) -> bool:
        """Check if a checkpoint should be saved for this fold.

        A checkpoint is saved when ``fold_index % save_every_n_folds == 0``.
        """
        return (fold_index + 1) % self.config.save_every_n_folds == 0

    def save(
        self,
        fold_index: int,
        fold_model: bytes,
        fold_metrics: dict[str, float],
        total_folds: int = 0,
    ) -> str:
        """Save a checkpoint and return its path.

        Creates the checkpoint directory if it does not exist. The
        checkpoint is a pickled dict with job_id, fold_index,
        fold_model, fold_metrics, timestamp, and total_folds.

        Args:
            fold_index: the 0-based fold index that was completed.
            fold_model: the pickled model bytes for this fold.
            fold_metrics: the metrics dict for this fold.
            total_folds: the total number of folds expected.

        Returns:
            The string path to the written checkpoint file.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        data = CheckpointData(
            job_id=self.config.job_id,
            fold_index=fold_index,
            fold_model=fold_model,
            fold_metrics=fold_metrics,
            timestamp=datetime.now(UTC).isoformat(),
            total_folds=total_folds,
        )
        path = self._checkpoint_path(fold_index)
        with open(path, "wb") as f:
            pickle.dump(data.model_dump(), f, protocol=pickle.HIGHEST_PROTOCOL)
        self.cleanup()
        return str(path)

    def load(self, path: str) -> CheckpointData:
        """Load a checkpoint from ``path`` and return its data.

        Raises:
            CheckpointError: if the path does not exist, the payload
                is not a dict, or the job_id does not match.
        """
        p = Path(path)
        if not p.exists():
            raise CheckpointError(f"checkpoint not found: {path}")
        with open(p, "rb") as f:
            payload = pickle.load(f)
        if not isinstance(payload, dict):
            raise CheckpointError(
                f"checkpoint payload is not a dict: {type(payload).__name__}"
            )
        # Verify job_id to prevent cross-job checkpoint confusion.
        if payload.get("job_id") != self.config.job_id:
            raise CheckpointError(
                f"checkpoint job_id mismatch: "
                f"expected {self.config.job_id}, "
                f"got {payload.get('job_id')}"
            )
        return CheckpointData(**payload)

    def latest_checkpoint(self) -> str | None:
        """Return the path to the latest checkpoint, or None if none.

        "Latest" is determined by the fold index encoded in the
        filename (``checkpoint_fold_{n}.pkl``), not by mtime.
        """
        if not self._dir.exists():
            return None
        checkpoints: list[tuple[int, Path]] = []
        for f in self._dir.glob("checkpoint_fold_*.pkl"):
            stem = f.stem.replace("checkpoint_fold_", "")
            try:
                fold_index = int(stem)
            except ValueError:
                continue
            checkpoints.append((fold_index, f))
        if not checkpoints:
            return None
        checkpoints.sort(key=lambda item: item[0])
        return str(checkpoints[-1][1])

    def latest_fold_index(self) -> int | None:
        """Return the fold index of the latest checkpoint, or None.

        This is the fold to resume from — the handler should skip
        folds 0..latest_fold_index and resume training from
        latest_fold_index + 1.
        """
        path = self.latest_checkpoint()
        if path is None:
            return None
        data = self.load(path)
        return data.fold_index

    def completed_folds(self) -> list[int]:
        """Return a sorted list of all completed fold indices."""
        if not self._dir.exists():
            return []
        folds: list[int] = []
        for f in self._dir.glob("checkpoint_fold_*.pkl"):
            stem = f.stem.replace("checkpoint_fold_", "")
            try:
                fold_index = int(stem)
            except ValueError:
                continue
            folds.append(fold_index)
        return sorted(folds)

    def cleanup(self) -> None:
        """Remove old checkpoints beyond ``config.max_checkpoints``.

        Keeps the most recent ``max_checkpoints`` checkpoints (by fold
        index) and deletes the rest.
        """
        if not self._dir.exists():
            return
        checkpoints: list[tuple[int, Path]] = []
        for f in self._dir.glob("checkpoint_fold_*.pkl"):
            stem = f.stem.replace("checkpoint_fold_", "")
            try:
                fold_index = int(stem)
            except ValueError:
                continue
            checkpoints.append((fold_index, f))
        if len(checkpoints) <= self.config.max_checkpoints:
            return
        checkpoints.sort(key=lambda item: item[0])
        excess = checkpoints[: len(checkpoints) - self.config.max_checkpoints]
        for _fold, path in excess:
            try:
                path.unlink()
            except OSError:
                continue

    def clear(self) -> None:
        """Remove all checkpoints for this job.

        Called after successful training completion to clean up
        checkpoint files (the final artifact is written separately
        by the artifact writer).
        """
        if not self._dir.exists():
            return
        for f in self._dir.glob("checkpoint_fold_*.pkl"):
            try:
                f.unlink()
            except OSError:
                continue
        # Remove the job directory if empty.
        try:
            self._dir.rmdir()
        except OSError:
            pass  # not empty or doesn't exist
