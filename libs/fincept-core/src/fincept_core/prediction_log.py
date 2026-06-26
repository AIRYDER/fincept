"""
fincept_core.prediction_log - filesystem-backed prediction record.

Why this module exists
~~~~~~~~~~~~~~~~~~~~~~

After the model registry (Phase A) and active-model promotion (Phase C)
landed, an operator can promote a candidate model and watch the active
badge flip in the dashboard.  But "what is this model actually doing
right now?" remained unanswerable from the dashboard alone -- the only
log of the agent's output was the structlog stream, which is opaque
unless the operator SSHs to the box.

``PredictionLog`` writes every emitted ``Prediction`` to a JSONL file
under ``data/predictions/<agent_id>.jsonl`` so the api can show "this
model has emitted 1,247 predictions in the last hour, mean confidence
0.42, balanced ~52/48 long/short" without depending on Redis stream
retention or Timescale schema work.

Filesystem layout
~~~~~~~~~~~~~~~~~

  data/predictions/<agent_id>.jsonl

One file per agent, append-only.  Each line is a JSON object with the
``PredictionRow`` shape below.  Because the file is append-only, two
processes can both call ``append`` without coordination as long as
each ``write`` is atomic (we use a single ``f.write(line + "\\n")``
which on POSIX + NTFS is atomic for sub-page writes).

Why filesystem instead of Postgres / Redis Streams?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

  * Same reasoning as ``api.promotions`` and ``api.training``: zero
    infra, ``cat``-able state, easy backup, survives ``FLUSHDB``.
  * The volume is small (60 predictions/min/symbol * a few symbols
    fits comfortably in <10MB/day at ~150 bytes/row).
  * Settlement / calibration (Phase E) layers on top of this without
    schema work: a separate ``<agent_id>.settlements.jsonl`` joins by
    ``id`` and answers "did this prediction come true at horizon?"

The reader API supports filtering by ``model_name`` and ``since_ns``,
so the dashboard's "last hour" stats view is a single tail-and-filter
operation rather than a custom indexed query.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import time
import uuid
from typing import Any

# --------------------------------------------------------------------------- #
# Configuration                                                              #
# --------------------------------------------------------------------------- #


def _default_predictions_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PREDICTIONS_DIR", "data/predictions"))


# Reject agent ids that could escape the predictions dir or break a
# path join.  Same allow-list as api.promotions for consistency: a
# human-typed identifier with dots, dashes, and underscores.
_BAD_NAME_CHARS = set('/\\:*?"<>|\0')


def _validate_agent_id(agent_id: str) -> None:
    if not agent_id:
        raise ValueError("agent_id must be non-empty")
    if any(c in _BAD_NAME_CHARS for c in agent_id):
        raise ValueError(f"agent_id contains forbidden character: {agent_id!r}")
    if agent_id in {".", ".."} or agent_id.startswith("."):
        raise ValueError(f"agent_id may not start with '.': {agent_id!r}")


# --------------------------------------------------------------------------- #
# Row shape                                                                  #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class PredictionRow:
    """One persisted prediction.

    ``id`` is a uuid4 generated at write time; consumers (a future
    settlement worker, the dashboard's "open this prediction" view)
    use it to join settlements without needing a synthetic primary
    key derived from ``(agent_id, ts_event, symbol)``.

    ``ts_recorded`` is the wall-clock time of the ``append`` call,
    in nanoseconds.  ``ts_event`` is the timestamp on the original
    ``Prediction`` envelope -- usually the same, but they can drift
    if the recording task is queued behind a slow disk.

    ``horizon_ns`` echoes the prediction's horizon so a settlement
    job can compute the deadline (``ts_event + horizon_ns``) without
    re-reading the model meta.

    ``model_name`` is set by the agent at record time -- the active
    model directory name.  This is what makes per-model stats and
    calibration views possible without joining against
    ``models/active/<agent_id>.history.jsonl``.
    """

    id: str
    agent_id: str
    model_name: str
    ts_recorded: int
    ts_event: int
    horizon_ns: int
    symbol: str
    direction: float
    confidence: float

    def to_json(self) -> str:
        """Render to a JSONL line.  Keys are explicit so a future field
        addition is forward-compatible (readers tolerate unknown keys).
        """
        return json.dumps(dataclasses.asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> PredictionRow:
        data = json.loads(line)
        return cls(
            id=str(data["id"]),
            agent_id=str(data["agent_id"]),
            model_name=str(data["model_name"]),
            ts_recorded=int(data["ts_recorded"]),
            ts_event=int(data["ts_event"]),
            horizon_ns=int(data["horizon_ns"]),
            symbol=str(data["symbol"]),
            direction=float(data["direction"]),
            confidence=float(data["confidence"]),
        )


@dataclasses.dataclass(frozen=True)
class PredictionStats:
    """Summary statistics over a window of predictions.

    Computed on demand by ``PredictionLog.stats``; never persisted.
    A future ``hit_rate`` / ``brier_score`` field would be added by
    the settlement layer (Phase E), not here, because those require
    joining against settled outcomes.
    """

    count: int
    mean_confidence: float
    long_count: int
    short_count: int
    flat_count: int

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------- #
# The store                                                                  #
# --------------------------------------------------------------------------- #


class PredictionLog:
    """Append-only prediction record on the filesystem.

    Constructor takes the predictions root directory.  Tests pass a
    ``tmp_path``; production reads from ``$PREDICTIONS_DIR`` (default
    ``data/predictions``).
    """

    def __init__(self, *, predictions_dir: pathlib.Path | None = None) -> None:
        self._predictions_dir = predictions_dir or _default_predictions_dir()

    @property
    def predictions_dir(self) -> pathlib.Path:
        return self._predictions_dir

    def _path(self, agent_id: str) -> pathlib.Path:
        _validate_agent_id(agent_id)
        return self._predictions_dir / f"{agent_id}.jsonl"

    # ------------------------------------------------------------------ #
    # Write                                                              #
    # ------------------------------------------------------------------ #

    def append(
        self,
        *,
        agent_id: str,
        model_name: str,
        ts_event: int,
        horizon_ns: int,
        symbol: str,
        direction: float,
        confidence: float,
    ) -> PredictionRow:
        """Persist a single prediction and return the row that was written.

        Caller must already hold the prediction values; we don't extract
        them from a ``Prediction`` envelope here because the writer
        (the agent) and the reader (the api) live in separate processes
        that import different schema versions.  Keeping this signature
        primitive avoids a fincept_core -> agents -> fincept_core
        circular dependency.
        """
        _validate_agent_id(agent_id)
        if not isinstance(model_name, str) or not model_name:
            raise ValueError("model_name must be a non-empty string")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a non-empty string")

        self._predictions_dir.mkdir(parents=True, exist_ok=True)
        row = PredictionRow(
            id=uuid.uuid4().hex,
            agent_id=agent_id,
            model_name=model_name,
            ts_recorded=time.time_ns(),
            ts_event=ts_event,
            horizon_ns=horizon_ns,
            symbol=symbol,
            direction=float(direction),
            confidence=float(confidence),
        )

        path = self._path(agent_id)
        # One write call per line.  On POSIX a write smaller than
        # PIPE_BUF (4096) is atomic; the largest row we expect is
        # ~250 bytes, well under that ceiling.  On NTFS a write that
        # fits in one cluster is atomic for our purposes.  We don't
        # use temp-file-then-rename here because rename would replace
        # the whole log file rather than append a single line.
        with path.open("a", encoding="utf-8") as f:
            f.write(row.to_json() + "\n")
        return row

    # ------------------------------------------------------------------ #
    # Read                                                               #
    # ------------------------------------------------------------------ #

    def read(
        self,
        *,
        agent_id: str,
        model_name: str | None = None,
        limit: int = 200,
        since_ns: int | None = None,
    ) -> list[PredictionRow]:
        """Return the most-recent ``limit`` predictions for an agent.

        Filters:
          * ``model_name`` -- only rows produced by that model.
          * ``since_ns``   -- only rows with ``ts_recorded >= since_ns``.

        We tail the file from the end rather than streaming top-down
        because typical queries want "the last 100 rows", and a JSONL
        file might have millions of rows after a few weeks of uptime.
        For ``limit`` <= a few thousand the naive read-all-and-slice
        is fast enough on the volumes this project produces; we revisit
        if profiling shows it as a hot path.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        path = self._path(agent_id)
        if not path.is_file():
            return []

        rows: list[PredictionRow] = []
        # Read the whole file lazily, parse, then slice.  This is
        # acceptable while files are <100MB; once they cross that
        # threshold we'll add daily rotation + a per-day index.
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = PredictionRow.from_json(line)
                except (json.JSONDecodeError, KeyError, ValueError):
                    # A malformed line shouldn't take the whole read
                    # down -- skip and continue, matching the
                    # tolerance pattern in api.promotions.
                    continue
                if model_name is not None and row.model_name != model_name:
                    continue
                if since_ns is not None and row.ts_recorded < since_ns:
                    continue
                rows.append(row)

        # Newest-first, then truncate.  Sorting is O(n log n) but
        # ``rows`` is at most the file's row count after filters, and
        # we already paid that read cost.
        rows.sort(key=lambda r: r.ts_recorded, reverse=True)
        return rows[:limit]

    # ------------------------------------------------------------------ #
    # Aggregate                                                          #
    # ------------------------------------------------------------------ #

    def stats(
        self,
        *,
        agent_id: str,
        model_name: str | None = None,
        since_ns: int | None = None,
    ) -> PredictionStats:
        """Compute summary statistics over a window.

        The thresholds used to bin a prediction as long / short / flat
        are intentionally simple: any positive ``direction`` is long,
        any negative is short, exactly ``0.0`` is flat.  Real systems
        use a confidence deadband; we don't model that here because
        consensus-layer logic in the orchestrator already applies a
        deadband before turning a prediction into a decision.
        """
        # Pull the entire matching window.  ``read`` already does the
        # filter + sort; we ignore the limit so stats see everything.
        rows = self.read(
            agent_id=agent_id,
            model_name=model_name,
            since_ns=since_ns,
            limit=10**9,
        )
        if not rows:
            return PredictionStats(
                count=0,
                mean_confidence=0.0,
                long_count=0,
                short_count=0,
                flat_count=0,
            )
        long_count = sum(1 for r in rows if r.direction > 0)
        short_count = sum(1 for r in rows if r.direction < 0)
        flat_count = sum(1 for r in rows if r.direction == 0)
        mean_confidence = sum(r.confidence for r in rows) / len(rows)
        return PredictionStats(
            count=len(rows),
            mean_confidence=mean_confidence,
            long_count=long_count,
            short_count=short_count,
            flat_count=flat_count,
        )
